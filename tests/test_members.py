import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from evo_tiktok import members, metrics, runner
from evo_tiktok.config import Settings


@pytest.fixture
def portal(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path / "logs")
    seen = []
    status = {"code": 200}
    yesterday = datetime.now(ZoneInfo(settings["timezone"])).date() - timedelta(days=1)
    days = [
        {"date": (yesterday - timedelta(days=n)).isoformat(), "totalPaid": 200 - n, "started": 3, "cancelled": 2,
         "byTier": {}, "startedByTier": {}, "cancelledByTier": {}}
        for n in reversed(range(8))
    ]

    def handler(request):
        seen.append(request)
        if status["code"] != 200:
            return httpx.Response(status["code"], json={"error": "nope"})
        return httpx.Response(200, json={"generatedAt": "x", "days": days})

    real_client = httpx.Client
    monkeypatch.setattr(metrics.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    s = Settings(data=settings.data, source=settings.source, env={"MEMBERS_REPORTING_API_KEY": "k"})
    return s, seen, status, tmp_path, yesterday


def test_check_reports_the_last_seven_days(portal):
    s, seen, _, tmp, yesterday = portal
    assert members.main(["--dry-run"], s) == 0
    assert seen[0].headers["Authorization"] == "Bearer k"
    record = json.loads((tmp / "logs" / "runs.jsonl").read_text().splitlines()[-1])
    summary = record["summary"]
    assert record["status"] == "ok" and summary["end"] == yesterday.isoformat()
    assert summary["start"] == (yesterday - timedelta(days=6)).isoformat()
    assert summary["new_paid_members"] == 21 and summary["cancelled"] == 14
    assert summary["paid_members_at_end"] == 200


def test_bad_key_fails_the_check(portal):
    s, _, status, tmp, _ = portal
    status["code"] = 401
    assert members.main(["--dry-run"], s) == 1
    record = json.loads((tmp / "logs" / "runs.jsonl").read_text().splitlines()[-1])
    assert "MEMBERS_REPORTING_API_KEY" in record["error"]


def test_missing_key_fails_the_check(portal, settings):
    s = Settings(data=settings.data, source=settings.source, env={})
    assert members.main(["--dry-run"], s) == 1
