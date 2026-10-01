"""Shared job runner: CLI flags, dry-run, the UK-time guard and the run log.

Every job calls ``run_job``. A run log line is always appended to
``logs/runs.jsonl``; live runs also write a ``run_log`` row to the database.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import ROOT, Settings, load_settings
from .db import Database

log = logging.getLogger(__name__)

LOG_DIR = Path(os.environ.get("EVO_LOG_DIR", ROOT / "logs"))


@dataclass
class JobContext:
    job: str
    run_id: str
    settings: Settings
    dry_run: bool
    args: argparse.Namespace
    summary: dict[str, Any] = field(default_factory=dict)
    llm: Any = None  # injected model client for tests; jobs build their own otherwise
    _db: Database | None = None

    def db(self) -> Database | None:
        """The database for writes, or None in dry-run. Dry-run never writes."""
        if self.dry_run:
            return None
        if self._db is None:
            self._db = Database(self.settings.secret("DATABASE_URL"))
        return self._db

    def read_db(self) -> Database | None:
        """The database for reads (history, snapshots), in live and dry runs alike.
        None when no DATABASE_URL is set. Never write through this."""
        if self._db is None:
            url = self.settings.secret("DATABASE_URL", required=False)
            if not url:
                return None
            self._db = Database(url)
        return self._db


def local_hour_matches(cron: str, tz: str, now: datetime | None = None) -> bool:
    """True if ``now`` in ``tz`` is at the hour named in a "M H * * *" cron string.

    Railway cron runs in UTC, so a 07:00 UK job is scheduled at both 06:00 and
    07:00 UTC and this guard drops whichever one isn't 07:00 in London.
    """
    hour = int(cron.split()[1])
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(tz)).hour == hour


def _write_jsonl(record: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _write_db(ctx: JobContext, record: dict) -> None:
    db = ctx.db()
    if db is None:
        return
    db.execute(
        "INSERT INTO run_log (run_id, job, started_at, finished_at, dry_run, status, summary, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["run_id"], record["job"], record["started_at"], record["finished_at"],
            record["dry_run"], record["status"], json.dumps(record["summary"], default=str), record["error"],
        ),
    )
    db.commit()


def run_job(
    job: str,
    body: Callable[[JobContext], None],
    argv: list[str] | None = None,
    add_args: Callable[[argparse.ArgumentParser], None] | None = None,
    settings: Settings | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m evo_tiktok.{job}")
    parser.add_argument("--dry-run", action="store_true", help="read only; no external writes")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        default=os.environ.get("EVO_SCHEDULED") == "1",
        help="cron run: skip unless it's the configured UK hour (set EVO_SCHEDULED=1 on Railway)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    if add_args:
        add_args(parser)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = settings or load_settings()
    ctx = JobContext(job=job, run_id=uuid.uuid4().hex[:12], settings=settings, dry_run=args.dry_run, args=args)
    record: dict[str, Any] = {
        "run_id": ctx.run_id,
        "job": job,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": ctx.dry_run,
        "status": "running",
        "summary": ctx.summary,
        "error": None,
    }
    code = 0
    cron = settings["schedule"].get(job)
    if args.scheduled and cron and not local_hour_matches(cron, settings["timezone"]):
        record["status"] = "skipped"
        ctx.summary["reason"] = f"not the configured UK hour for '{cron}'"
        log.info("Skipping %s: %s", job, ctx.summary["reason"])
    else:
        try:
            body(ctx)
            record["status"] = "ok"
        except Exception as exc:  # the run log must record every failure
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            log.error("%s failed: %s\n%s", job, exc, traceback.format_exc())
            code = 1
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_jsonl(record)
    if record["status"] != "skipped":
        try:
            _write_db(ctx, record)
        except Exception as exc:
            log.error("Could not write run_log row: %s", exc)
            code = code or 1
    if ctx._db is not None:
        ctx._db.close()
    log.info("%s %s (run %s%s)", job, record["status"], ctx.run_id, ", dry run" if ctx.dry_run else "")
    return code
