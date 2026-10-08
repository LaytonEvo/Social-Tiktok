import copy
import json
import re
from pathlib import Path

import httpx
import pytest

from evo_tiktok import replies, runner, stock
from evo_tiktok.config import Settings
from evo_tiktok.db import Database
from evo_tiktok.validators import GuardrailError

FIXTURES = Path(__file__).parent / "fixtures"
CSV = FIXTURES / "comments.csv"
JSONL = FIXTURES / "shopify_bulk.jsonl"
POST = "7400000000000000001"


@pytest.fixture
def lines(settings):
    records = [json.loads(l) for l in JSONL.read_text().splitlines() if l.strip()]
    return stock.build_snapshot(records, settings)[0]


@pytest.fixture
def comments():
    return replies.parse_rows(list(__import__("csv").reader(CSV.open(encoding="utf-8"))))


class FakeLLM:
    """Classifies by keyword and drafts a reply from the facts, like a well-behaved model."""

    def __init__(self, draft=None, labels=None):
        self.calls = []
        self.draft = draft or self.honest_draft
        self.labels = labels or {}

    @staticmethod
    def honest_draft(facts, text):
        asked = facts.get("asked_sizes", [])
        if not facts.get("post_known"):
            return "Good question! We'll check and DM you.", True
        if asked and asked[0]["in_stock"]:
            return f"Yes, we've got {asked[0]['size']} in the Members Club, link in bio.", False
        if asked:
            return f"Sorry, {asked[0]['size']} has sold out.", False
        return "Thanks so much! 🙌", False

    def complete_json(self, system, messages, schema, max_tokens):
        self.calls.append((schema, copy.deepcopy(messages)))
        user = messages[0]["content"]
        if schema is replies.CLASSIFY_SCHEMA:
            items = json.loads(user.split("Comments (JSON list):", 1)[1].split("\n\nReturn JSON", 1)[0])
            out = []
            for c in items:
                t = c["text"].lower()
                cat = self.labels.get(c["comment_id"]) or (
                    "question-price" if "how much" in t else
                    "question-sizing" if re.search(r"\d|size", t) else "positive"
                )
                out.append({"comment_id": c["comment_id"], "category": cat, "confident": True})
            return {"results": out}, []
        facts = json.loads(user.split("Stock facts you may use (and nothing else): ", 1)[1].split("\n", 1)[0])
        reply, check = self.draft(facts, user)
        return {"reply": reply, "needs_human_check": check, "reason": "test"}, [{"type": "text", "text": reply}]


@pytest.fixture
def db_with_post(tmp_path):
    url = f"sqlite:///{tmp_path / 'evo.db'}"
    db = Database(url)
    db.migrate()
    db.execute(
        "INSERT INTO content_log (week_start, script_no, format, hook, caption, featured_skus, status, "
        "tiktok_post_id, created_at) VALUES ('2026-10-05', 4, 'size_roulette', 'Every pair left', "
        "'Size Roulette', ?, 'posted', ?, '2026-10-05')",
        (json.dumps(["SHOE-9", "SHOE-10"]), POST),
    )
    db.commit()
    yield db, url
    db.close()


def by_id(drafts):
    return {d.comment.comment_id: d for d in drafts}


# ---------------------------------------------------------------- input


def test_parse_rows_handles_aliases_urls_and_missing_ids():
    rows = [["Video link", "Username", "Comment"], ["https://tiktok.com/@e/video/123", "@a", "hi"], ["", "", ""]]
    [c] = replies.parse_rows(rows)
    assert c.post_id == "123" and c.author == "a" and c.comment_id.startswith("h")
    assert replies.parse_rows(rows)[0].comment_id == c.comment_id  # stable hash, so re-runs dedupe


def test_extract_sizes():
    assert replies.extract_sizes("Got any 10s? or uk 9.5, size 8, in a 11") == ["UK10", "UK9.5", "UK8", "UK11"]
    assert replies.extract_sizes("XL or large please") == ["XL", "L"]
    assert replies.extract_sizes("we've got 3 pairs left") == []


# ---------------------------------------------------------------- the acceptance tests


def test_stock_answers_match_the_snapshot(settings, comments, lines, db_with_post):
    db, _ = db_with_post
    drafts = by_id(replies.build_drafts(FakeLLM(), settings, comments, lines, db))
    assert drafts["c1"].facts["asked_sizes"] == [{"size": "UK10", "in_stock": True, "units": 3}]
    assert "UK10" in drafts["c1"].reply and not drafts["c1"].needs_human_check
    assert drafts["c2"].facts["asked_sizes"] == [{"size": "UK12", "in_stock": False, "units": 0}]
    assert "sold out" in drafts["c2"].reply
    assert drafts["c7"].needs_human_check  # post not in content_log
    assert "post not in content log" in drafts["c7"].reason


def test_a_draft_that_contradicts_the_snapshot_is_never_queued(settings, comments, lines, db_with_post):
    db, _ = db_with_post
    liar = FakeLLM(draft=lambda facts, text: ("Yes! UK12 is in stock, grab them quick.", False))
    drafts = by_id(replies.build_drafts(liar, settings, comments, lines, db))
    d = drafts["c2"]
    assert d.reply == "" and d.needs_human_check and "snapshot has none" in d.reason


def test_stock_consistency_rules():
    facts = {"post_known": True, "products": [{"product": "Shoe", "sizes_in_stock": {"UK9": 4}}],
             "asked_sizes": [{"size": "UK12", "in_stock": False, "units": 0}]}
    assert replies.stock_consistency("We've got UK9 left.", facts) == []
    assert replies.stock_consistency("Sorry, UK12 is sold out.", facts) == []
    assert replies.stock_consistency("UK12 is available now!", facts)
    assert replies.stock_consistency("We have UK8 too", facts)  # not in the facts at all
    assert replies.stock_consistency("UK9 is sold out", facts)
    assert replies.stock_consistency("We've got plenty!", {"post_known": False, "products": [], "asked_sizes": []})


def test_no_draft_contains_a_brand_price(settings, comments, lines, db_with_post):
    db, _ = db_with_post
    pricey = FakeLLM(draft=lambda facts, text: ("The adidas ones are £45 for members!", False))
    drafts = replies.build_drafts(pricey, settings, comments, lines, db)
    for d in drafts:
        assert not re.search(r"£\d", d.reply), d.reply
    assert by_id(drafts)["c3"].reply == "Member prices are in the Evo Members Club — link in bio."


def test_complaints_are_flagged_never_resolved(settings, comments, lines, db_with_post):
    db, _ = db_with_post
    # Even if the model calls it positive, the complaint pattern wins.
    drafts = by_id(replies.build_drafts(FakeLLM(labels={"c4": "positive"}), settings, comments, lines, db))
    d = drafts["c4"]
    assert d.category == "complaint" and d.needs_human_check
    assert d.reply == settings["replies"]["complaint_holding_reply"]
    assert "Karin" in d.reason


def test_spam_gets_no_draft_and_is_marked_for_hiding(settings, comments, lines, db_with_post):
    db, _ = db_with_post
    d = by_id(replies.build_drafts(FakeLLM(), settings, comments, lines, db))["c5"]
    assert d.category == "spam" and d.reply == "" and d.status == "hide"


@pytest.mark.parametrize(
    "reply,expect",
    [
        ("x" * 151, "max 150"),
        ("Nike polos 40% off for members", "Brand"),
        ("Members save 40%", "Price or discount"),
        ("Back in stock next week!", "restock"),
    ],
)
def test_validate_reply(settings, reply, expect):
    assert any(expect in e for e in replies.validate_reply(reply, {}, settings))


def test_bad_config_reply_fails_the_run(settings, comments, lines):
    data = copy.deepcopy(settings.data)
    data["replies"]["complaint_holding_reply"] = "Sorry! Here's £10 off your next adidas order"
    with pytest.raises(GuardrailError):
        replies.build_drafts(FakeLLM(), Settings(data, settings.source), comments, lines, None)


# ---------------------------------------------------------------- the job


@pytest.fixture
def job(settings, tmp_path, monkeypatch, db_with_post):
    _, url = db_with_post
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path / "logs")
    data = copy.deepcopy(settings.data)
    data["outputs"]["reply_dir"] = str(tmp_path / "replies")
    s = Settings(data=data, source=settings.source, env={"DATABASE_URL": url})
    posted = []
    monkeypatch.setattr("evo_tiktok.replies.publish", lambda *a, **k: posted.append(a) or {"published": True})
    # Record every HTTP request: nothing may go to TikTok (or anywhere) in these runs.
    hosts = []
    real_send = httpx.Client.send
    monkeypatch.setattr(httpx.Client, "send", lambda self, req, **kw: hosts.append(req.url.host) or real_send(self, req, **kw))
    return s, url, tmp_path, posted, hosts


def _queue(url):
    db = Database(url)
    try:
        return db.query("SELECT comment_id, category, status, needs_human_check FROM reply_queue ORDER BY comment_id")
    finally:
        db.close()


def _run(s, *flags):
    return replies.main(["--from-csv", str(CSV), "--from-jsonl", str(JSONL), *flags], llm=FakeLLM(), settings=s)


def test_dry_run_writes_local_queue_only(job):
    s, url, tmp, posted, hosts = job
    assert _run(s, "--dry-run") == 0
    [csv_file] = (tmp / "replies").glob("*.csv")
    assert len(csv_file.read_text().splitlines()) == 8  # header + 7 comments
    assert _queue(url) == [] and posted == [] and hosts == []


def test_live_run_queues_once_and_posts_nothing_to_tiktok(job):
    s, url, tmp, posted, hosts = job
    assert _run(s) == 0
    q = _queue(url)
    assert len(q) == 7
    assert ("c5", "spam", "hide", 0) in q
    assert len(posted) == 1 and "## Complaints for Karin (1)" in posted[0][1] and "@angry1" in posted[0][1]
    assert not any("tiktok" in h for h in hosts)
    assert _run(s) == 0  # the same comments again: nothing new to draft
    assert len(_queue(url)) == 7 and len(posted) == 1


def test_link_post(job, db_with_post):
    from evo_tiktok import posts

    s, url, *_ = job
    assert posts.main(["--week", "2026-10-05", "--script", "4", "--post",
                       "https://www.tiktok.com/@evo/video/7411111111111111111"], settings=s) == 0
    db = Database(url)
    assert db.query("SELECT tiktok_post_id, status FROM content_log WHERE script_no = 4") == [("7411111111111111111", "posted")]
    db.close()


def test_no_reply_sheet_yet_is_a_quiet_skip(job):
    s, url, tmp, posted, hosts = job
    assert replies.main([], llm=FakeLLM(), settings=s) == 0
    record = json.loads((tmp / "logs" / "runs.jsonl").read_text().splitlines()[-1])
    assert record["status"] == "ok" and "no reply Sheet" in record["summary"]["skipped"]
    assert posted == [] and hosts == [] and _queue(url) == []
