"""Check the members portal key and see recent members numbers.

    python -m evo_tiktok.members            # the last 7 complete days
    python -m evo_tiktok.members --days 28

Read-only: it calls the portal's reporting API and logs what the weekly
report would say about members. Nothing is published.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import metrics
from .report import members_facts, members_line
from .runner import JobContext, run_job

log = logging.getLogger(__name__)


def body(ctx: JobContext) -> None:
    days = ctx.args.days
    if not 1 <= days <= 365:
        raise ValueError("--days must be between 1 and 365")
    # The portal counts complete London days only, so the window ends yesterday.
    end = datetime.now(ZoneInfo(ctx.settings["timezone"])).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    m = metrics.load_members_api(ctx.settings, start, end)
    ctx.summary.update(start=start.isoformat(), end=end.isoformat(), **members_facts(m))
    log.info("Members portal OK, %s to %s: %s", start, end, members_line(m))


def add_args(parser) -> None:
    parser.add_argument("--days", type=int, default=7, help="complete days to cover, ending yesterday (default 7)")


def main(argv=None, settings=None) -> int:
    return run_job("members", body, argv, add_args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
