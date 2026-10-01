import copy
import csv
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from evo_tiktok import metrics, report, runner
from evo_tiktok.config import Settings
from evo_tiktok.db import Database

END = date(2026, 10, 9)  # Friday; window is 3-9 October

# Seeded week: (post_id, script_no, format, hook, posted_at, views, avg_watch, completion %, shares, comments, follows)
WEEK = [
    ("P1", 1, "value_comparison", "Best polo under £40?", "2026-10-05", 5000, "12.0", "40%", 9, 30, 12),
    ("P2", 2, "value_comparison", "Best shoe under £60?", "2026-10-05", 3000, "9", "30%", 3, 10, 2),
    ("P3", 4, "size_roulette", "Every UK9 left", "2026-10-06", 4000, "0:15", "55%", 4, 25, 8),
    ("P4", 5, "size_roulette", "Every UK10 left", "2026-10-07", 2500, "6", "20%", 20, 12, 5),
    ("P5", 6, "giveaway", "Win these shoes", "2026-10-08", 8000, "8", "35%", 5, 140, 40),
    ("P6", 7, "trolley", "3 trolley mistakes", "2026-10-08", 1200, "7", "25%", 1, 4, 1),
]
# History for the medians: watch 5..10 (median 7.5), shares 2..7 (median 4.5)
HISTORY = [(f"H{i}", f"2026-09-{10 + i:02d}", 5 + i, 2 + i) for i in range(6)]


@pytest.fixture
def seeded(tmp_path):
    url = f"sqlite:///{tmp_path / 'evo.db'}"
    db = Database(url)
    db.migrate()
    for pid, n, fmt, hook, posted, *_ in WEEK:
        db.execute(
            "INSERT INTO content_log (week_start, script_no, format, hook, status, tiktok_post_id, posted_at, created_at) "
            "VALUES ('2026-10-05', ?, ?, ?, 'posted', ?, ?, '2026-10-05')",
            (n, fmt, hook, pid, posted),
        )
    for i, (pid, posted, watch, shares) in enumerate(HISTORY):
        db.execute(
            "INSERT INTO content_log (week_start, script_no, format, hook, status, tiktok_post_id, posted_at, "
            "avg_watch_seconds, shares, created_at) VALUES ('2026-09-07', ?, 'value_comparison', 'old', 'posted', ?, ?, ?, ?, '2026-09-07')",
            (i + 1, pid, posted, watch, shares),
        )
    db.commit()
    db.close()

    organic = tmp_path / "organic.csv"
    with organic.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Video link", "Video views", "Average watch time", "Full video views %", "Shares", "Comments",
                    "New followers", "Profile views"])
        for pid, *_rest in WEEK:
            _, _, _, _, views, watch, comp, shares, comments, follows = _rest
            w.writerow([f"https://www.tiktok.com/@evo/video/{pid}", views, watch, comp, shares, comments, follows, 3])
        w.writerow(["https://www.tiktok.com/@evo/video/P9", 999, "30", "90%", 99, 9, 9, 9])  # not linked
        w.writerow(["H0", 100, "4", "10%", 0, 0, 0, 0])  # older linked post: metrics saved, not ranked
    paid = tmp_path / "paid.csv"
    with paid.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Ad name", "tiktok_item_id", "Spend", "Impressions", "Conversions"])
        w.writerow(["Spark P1", "P1", "30.00", "20000", "3"])
        w.writerow(["Spark P5", "P5", "15.50", "9000", "2"])
    return url, organic, paid, tmp_path


def args(organic, paid, **kw):
    return SimpleNamespace(organic_csv=str(organic), paid_csv=str(paid), new_members=10, member_orders=4,
                           member_revenue=180.0, **kw)


def analysis(settings, seeded):
    url, organic, paid, _ = seeded
    a_ = args(organic, paid)
    start, end = report.week_window(END, 7)
    db = Database(url)
    try:
        return report.analyse(
            settings, db, start, end,
            metrics.load_organic(settings, a_, start, end),
            metrics.load_paid(settings, a_, start, end),
            metrics.load_members(settings, a_, start, end),
        )
    finally:
        db.close()


# ---------------------------------------------------------------- parsing


def test_metric_parsing():
    assert metrics._seconds("0:15") == 15 and metrics._seconds("12.5s") == 12.5
    assert metrics._rate("45%") == 0.45 and metrics._rate("0.45") == 0.45 and metrics._rate("45") == 0.45


def test_daily_rows_merge_view_weighted():
    rows = [metrics.PostMetrics("A", views=100, avg_watch_seconds=10, shares=1),
            metrics.PostMetrics("A", views=300, avg_watch_seconds=6, shares=2)]
    [m] = metrics.merge_organic(rows)
    assert m.views == 400 and m.shares == 3 and m.avg_watch_seconds == pytest.approx(7.0)


# ---------------------------------------------------------------- acceptance


def test_totals_reconcile_with_source_data(settings, seeded):
    a = analysis(settings, seeded)
    assert [p.post_id for p in a.posts] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert a.totals == {
        "views": sum(r[5] for r in WEEK), "shares": sum(r[8] for r in WEEK), "comments": sum(r[9] for r in WEEK),
        "follows": sum(r[10] for r in WEEK), "profile_visits": 3 * len(WEEK), "link_clicks": 0, "posts": 6,
    }
    assert a.paid["spend_gbp"] == "45.50" and a.paid["results"] == 5 and a.paid["impressions"] == 29000
    assert a.paid["cost_per_result_gbp"] == "9.10" and a.paid["cost_per_member_gbp"] == "4.55"
    assert a.medians == {"avg_watch_seconds": 7.5, "shares": 4.5}
    assert "last 4 weeks" in a.median_basis
    assert any("P9" in n for n in a.notes)


def test_boost_only_for_a_post_beating_both_medians(settings, seeded):
    a = analysis(settings, seeded)
    # P3 wins on watch but not shares, P4 on shares but not watch; P1 beats both by the most.
    assert a.boost.post_id == "P1"
    assert report.boost_line(a) == "Boost recommendation: Value comparison: \"Best polo under £40?\", £10/day, 3 days — approve?"


def test_no_boost_below_the_median(settings, seeded):
    a = analysis(settings, seeded)
    for p in a.posts:
        p.m.shares = 4  # nobody beats the 4.5 share median now
    boost, reason = report.pick_boost(settings, a.posts, a.medians)
    assert boost is None and "no post beat" in reason
    a.boost, a.boost_reason = boost, reason
    assert report.boost_line(a).startswith("Boost recommendation: no boost this week")


def test_gate_progress(settings, seeded):
    g = analysis(settings, seeded).gate
    assert g["best_format_vs_median"] == pytest.approx(1.4) and g["format_target_met"] is False
    assert g["weeks_in_a_row"] == 0 and g["cost_target_met"] is False  # £4.55 > £3


def test_narrative_cannot_set_spend(settings, seeded):
    a = analysis(settings, seeded)
    good = {"headline": "P1 led", "best_and_worst": "x", "format_ranking": "x", "pilot_gate": "x",
            "film_next_week": ["a", "b", "c"], "boost_reasoning": "P1 beat both medians."}
    assert report.validate_narrative(good, a) == []
    assert report.validate_narrative(good | {"boost_reasoning": "Put £25/day behind P1"}, a)
    assert report.validate_narrative(good | {"headline": "Approve the boost?"}, a)


# ---------------------------------------------------------------- the job


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete_json(self, system, messages, schema, max_tokens):
        data = self.responses.pop(0)
        return data, [{"type": "text", "text": json.dumps(data)}]


GOOD = {"headline": "Value comparisons led: 12s average watch on the best post.", "best_and_worst": "b",
        "format_ranking": "f", "pilot_gate": "g", "film_next_week": ["one", "two", "three"],
        "boost_reasoning": "It beat the median on both watch time and shares."}


@pytest.fixture
def job(settings, seeded, monkeypatch):
    url, organic, paid, tmp = seeded
    monkeypatch.setattr(runner, "LOG_DIR", tmp / "logs")
    data = copy.deepcopy(settings.data)
    data["outputs"]["report_dir"] = str(tmp / "reports")
    s = Settings(data=data, source=settings.source, env={"DATABASE_URL": url})
    posted = []
    monkeypatch.setattr("evo_tiktok.report.publish", lambda *a, **k: posted.append(a) or {"published": True})
    methods = []
    real_send = httpx.Client.send
    monkeypatch.setattr(httpx.Client, "send", lambda self, req, **kw: methods.append(req.method) or real_send(self, req, **kw))
    argv = ["--week-end", END.isoformat(), "--organic-csv", str(organic), "--paid-csv", str(paid),
            "--new-members", "10", "--member-orders", "4", "--member-revenue", "180"]
    return s, url, tmp, posted, methods, argv


def test_dry_run_writes_report_only(job):
    s, url, tmp, posted, methods, argv = job
    assert report.main(argv + ["--dry-run"], llm=FakeLLM(GOOD), settings=s) == 0
    md = (tmp / "reports" / "2026-10-09" / "weekly_report.md").read_text()
    assert md.rstrip().endswith("— approve?")
    assert posted == [] and methods == []
    db = Database(url)
    assert db.query("SELECT COUNT(*) FROM weekly_report")[0][0] == 0
    assert db.query("SELECT views FROM content_log WHERE tiktok_post_id = 'P1'")[0][0] is None
    db.close()


def test_live_run_saves_metrics_and_awaits_approval(job):
    s, url, tmp, posted, methods, argv = job
    assert report.main(argv, llm=FakeLLM(GOOD), settings=s) == 0
    db = Database(url)
    assert db.query("SELECT views, shares FROM content_log WHERE tiktok_post_id = 'P1'") == [(5000, 9)]
    assert db.query("SELECT views FROM content_log WHERE tiktok_post_id = 'H0'") == [(100,)]
    assert db.query("SELECT boost_post_id, boost_status FROM weekly_report") == [("P1", "awaiting approval")]
    db.close()
    assert len(posted) == 1 and posted[0][1].rstrip().endswith("— approve?")  # the full report goes to the Doc
    assert all(m == "GET" for m in methods)  # no spend changes: nothing but reads


def test_narrative_that_sets_spend_is_dropped_but_report_still_goes(job):
    s, url, tmp, posted, methods, argv = job
    bad = GOOD | {"boost_reasoning": "Spend £50/day on P4."}
    assert report.main(argv, llm=FakeLLM(bad, bad), settings=s) == 0
    md = (tmp / "reports" / "2026-10-09" / "weekly_report.md").read_text()
    assert "£50/day" not in md and "Narrative dropped" in md and "£10/day" in md
    assert "£50/day" not in posted[0][1] and "£50/day" not in posted[0][2]


def test_windsor_client_only_reads(settings):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        return httpx.Response(200, json={"data": [
            {"video_id": "P1", "video_views": 10, "average_time_watched": 5, "shares": 1, "date": "2026-10-05"},
            {"video_id": "P1", "video_views": 30, "average_time_watched": 9, "shares": 2, "date": "2026-10-06"},
        ]})

    s = Settings(settings.data, settings.source, env={"WINDSOR_API_KEY": "k"})
    w = metrics.Windsor(s, http=httpx.Client(transport=httpx.MockTransport(handler)))
    [m] = w.organic(date(2026, 10, 3), END)
    assert m.views == 40 and m.shares == 3 and m.avg_watch_seconds == pytest.approx(8.0)
    method, path, params = seen[0]
    assert method == "GET" and path == "/tiktok_organic" and params["date_to"] == "2026-10-09"
