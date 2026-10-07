import json
from pathlib import Path

import pytest

from evo_tiktok import dashboard, runner, stock
from evo_tiktok.config import Settings
from evo_tiktok.db import Database

FIXTURE = str(Path(__file__).parent / "fixtures" / "shopify_bulk.jsonl")


class FakeSheets:
    def __init__(self):
        self.tabs, self.written, self.requests = {}, {}, []

    def ensure_tab(self, sheet_id, title):
        return self.tabs.setdefault(title, 42)

    def replace(self, sheet_id, tab, rows):
        self.written[tab] = rows

    def batch_update(self, sheet_id, requests):
        self.requests += requests
        return {}


@pytest.fixture
def seeded(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path / "logs")
    url = f"sqlite:///{tmp_path / 'evo.db'}"
    db = Database(url)
    db.migrate()
    db.close()
    s = Settings(data=settings.data, source=settings.source, env={"DATABASE_URL": url})
    assert runner.run_job("stock", stock.body, ["--from-jsonl", FIXTURE], stock.add_args, s) == 0
    return s, url


def test_build_reads_job_health_and_stock(seeded):
    s, url = seeded
    db = Database(url)
    data = dashboard.build(db, s)
    db.close()
    jobs = {j["job"]: j for j in data["jobs"]}
    assert jobs["stock"]["status"] == "ok" and jobs["stock"]["schedule"] == "Daily 06:00 UK"
    assert jobs["scripts"]["status"] == "not run yet" and jobs["scripts"]["schedule"] == "Mondays 07:00 UK"
    assert data["stock"]["lines_in_stock"] == 11 and data["stock"]["featured_lines"] == 4
    assert data["members"] is None and data["content"] is None and data["report"] is None
    json.dumps(data)  # a portal reads this as JSON


def test_sheet_layout_and_bold_headings(seeded):
    s, url = seeded
    db = Database(url)
    data = dashboard.build(db, s)
    db.close()
    sheets = FakeSheets()
    s.env["DASHBOARD_SHEET_ID"] = "sheet1"
    n = dashboard.publish_sheet(s, data, sheets)
    rows = sheets.written["Dashboard"]
    assert n == len(rows) and rows[0] == ["Evo TikTok ops dashboard"]
    text = "\n".join(" | ".join(map(str, r)) for r in rows)
    assert "stock | Daily 06:00 UK" in text and "Lines in stock | 11" in text
    assert "Members | no numbers yet" in text and "no filming pack yet" in text
    bold_rows = [r["repeatCell"]["range"].get("startRowIndex") for r in sheets.requests[1:]]
    assert all(rows[i][0] for i in bold_rows) and 0 in bold_rows


def test_every_live_run_refreshes_the_dashboard(seeded, monkeypatch):
    s, url = seeded
    s.env["DASHBOARD_SHEET_ID"] = "sheet1"
    sheets = FakeSheets()
    monkeypatch.setattr("evo_tiktok.gsheets.Sheets", lambda settings: sheets)
    assert runner.run_job("stock", stock.body, ["--from-jsonl", FIXTURE], stock.add_args, s) == 0
    assert "Dashboard" in sheets.written
    db = Database(url)
    saved = db.query("SELECT data FROM dashboard_snapshot")
    db.close()
    assert len(saved) == 1 and json.loads(saved[0][0])["stock"]["lines_in_stock"] == 11


def test_dashboard_failure_never_fails_the_job(seeded, monkeypatch):
    s, _ = seeded
    s.env["DASHBOARD_SHEET_ID"] = "sheet1"

    def broken(settings):
        raise RuntimeError("Sheets is down")

    monkeypatch.setattr("evo_tiktok.gsheets.Sheets", broken)
    assert runner.run_job("stock", stock.body, ["--from-jsonl", FIXTURE], stock.add_args, s) == 0


def test_members_fall_back_to_the_last_report(seeded):
    s, url = seeded
    db = Database(url)
    feed = {"members": {"new_members": 26, "cancelled": 12, "paid_at_end": 244, "cancel_rate": 0.052}}
    db.execute(
        "INSERT INTO weekly_report (week_start, week_end, report_md, feed, gate, boost_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-10-03", "2026-10-09", "x", json.dumps(feed), "{}", "none", "2026-10-09T15:00:00"),
    )
    db.commit()
    m = dashboard.members_numbers(db, s)
    rep = dashboard.report_numbers(db)
    db.close()
    assert m["source"] == "last weekly report" and m["new"] == 26 and m["paid_total"] == 244
    assert rep["boost_status"] == "none"


def test_dry_run_writes_nothing(seeded, monkeypatch):
    s, url = seeded
    s.env["DASHBOARD_SHEET_ID"] = "sheet1"
    monkeypatch.setattr("evo_tiktok.gsheets.Sheets", lambda settings: pytest.fail("no Sheets in a dry run"))
    assert dashboard.main(["--dry-run"], s) == 0
    db = Database(url)
    assert db.query("SELECT COUNT(*) FROM dashboard_snapshot")[0][0] == 0
    db.close()
