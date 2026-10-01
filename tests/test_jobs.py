from datetime import datetime, timezone
from pathlib import Path

import pytest

from evo_tiktok import runner, stock
from evo_tiktok.config import Settings
from evo_tiktok.db import Database

FIXTURE = str(Path(__file__).parent / "fixtures" / "shopify_bulk.jsonl")


@pytest.fixture
def db_settings(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path / "logs")
    url = f"sqlite:///{tmp_path / 'evo.db'}"
    db = Database(url)
    db.migrate()
    db.close()
    return Settings(data=settings.data, source=settings.source, env={"DATABASE_URL": url}), url, tmp_path


def count(url, table):
    db = Database(url)
    try:
        return db.query(f"SELECT COUNT(*) FROM {table}")[0][0]
    finally:
        db.close()


def run_stock(settings, *flags):
    return runner.run_job("stock", stock.body, ["--from-jsonl", FIXTURE, *flags], stock.add_args, settings)


def test_migrations_are_idempotent(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'm.db'}")
    assert db.migrate() == ["001_init", "002_weekly_report"]
    assert db.migrate() == []
    db.close()


def test_dry_run_makes_no_db_writes(db_settings):
    settings, url, tmp = db_settings
    assert run_stock(settings, "--dry-run") == 0
    assert count(url, "stock_snapshot") == 0 and count(url, "run_log") == 0
    assert (tmp / "logs" / "runs.jsonl").read_text().count('"status": "ok"') == 1


def test_live_run_saves_snapshot_and_run_log(db_settings):
    settings, url, _ = db_settings
    assert run_stock(settings) == 0
    db = Database(url)
    featured = {r[0] for r in db.query("SELECT sku FROM stock_snapshot WHERE featured = 1")}
    db.close()
    # Core-size non-live footwear and last-season polos with an RRP, active and in stock.
    assert featured == {"SHOE-9", "SHOE-10", "POLO-M", "JKT-L"}
    assert count(url, "stock_snapshot") == 11  # 14 variants, 2 excluded, OOS-9 has no stock
    assert count(url, "run_log") == 1


def test_failures_are_logged_and_return_nonzero(db_settings, tmp_path):
    settings, url, _ = db_settings
    code = runner.run_job("stock", stock.body, ["--from-jsonl", str(tmp_path / "missing.jsonl")],
                          stock.add_args, settings)
    assert code == 1
    db = Database(url)
    assert db.query("SELECT status FROM run_log")[0][0] == "failed"
    db.close()


@pytest.mark.parametrize(
    "utc,expected",
    [
        (datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc), True),   # BST: 07:00 UK
        (datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 12, 7, 7, 0, tzinfo=timezone.utc), True),  # GMT: 07:00 UK
        (datetime(2026, 12, 7, 6, 0, tzinfo=timezone.utc), False),
    ],
)
def test_uk_hour_guard(utc, expected):
    assert runner.local_hour_matches("0 7 * * MON", "Europe/London", utc) is expected


def test_scheduled_run_outside_uk_hour_is_skipped(db_settings, monkeypatch):
    settings, url, _ = db_settings
    monkeypatch.setattr(runner, "local_hour_matches", lambda *a, **k: False)
    assert run_stock(settings, "--scheduled") == 0
    assert count(url, "stock_snapshot") == 0


def test_saved_snapshot_round_trips(db_settings):
    settings, url, _ = db_settings
    assert run_stock(settings) == 0
    db = Database(url)
    lines = {l.sku: l for l in stock.load_latest_snapshot(db)}
    db.close()
    shoe = lines["SHOE-9"]
    assert shoe.units_by_location == {"Warehouse": 3, "Burley Golf Club": 1}
    assert shoe.members_units == 4 and shoe.category == "Footwear" and shoe.has_rrp
    assert "OOS-9" not in lines  # only lines with stock are saved


def test_stale_snapshot_is_ignored(db_settings):
    settings, url, _ = db_settings
    assert run_stock(settings) == 0
    db = Database(url)
    db.execute("UPDATE stock_snapshot SET taken_at = '2026-01-01T06:00:00+00:00'")
    db.commit()
    assert stock.load_latest_snapshot(db) is None
    db.close()
