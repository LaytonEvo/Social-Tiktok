import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import ImageFont

from evo_tiktok import production, runner, stock, video, video_render
from evo_tiktok.config import Settings
from evo_tiktok.storage import LocalStore
from evo_tiktok.db import Database
from evo_tiktok.validators import GuardrailError

WEEK = "2026-10-12"


@pytest.fixture
def lines(make_line):
    # Two shoes in Clubhouse deals: members save 15% and 25% on today's website price.
    return [
        make_line("SPKL-9", price="80", rrp="90", member="68", product_title="Drive Fade 2 Spikeless Golf Shoes",
                  vendor="Under Armour", url="https://evolutiongolf.co.uk/products/fade"),
        make_line("SPKD-9", price="160", rrp=None, member="120", product_title="Drive Pro Clone Spiked Golf Shoes",
                  vendor="Under Armour", url="https://evolutiongolf.co.uk/products/clone"),
        make_line("GONE-9", units=0, price="90", member="70", product_title="Halo Hybrid Golf Shoes"),
    ]


@pytest.fixture
def stock_by_sku(lines):
    return {line.sku: line for line in lines}


def shot(say, headline="", kicker="", prompt="The shoes on the turf mat, slow orbit.", skus=("SPKL-9",), seconds=4):
    return video.Shot(say, headline, kicker, prompt, list(skus), "studio", seconds)


def good_plan() -> video.VideoPlan:
    return video.VideoPlan(WEEK, 1, "value_comparison", "Two winter shoes. Link in bio.", [
        shot("Wet feet by the third hole?", "Winter golf shoes", "MEMBER DEAL", skus=("SPKL-9", "SPKD-9")),
        shot("Both of these are fully waterproof.", "UK7 to UK10.5", "SPIKELESS"),
        shot("Clubhouse members save up to twenty-five percent.", "Up to 25%", "MEMBERS SAVE", skus=("SPKD-9",)),
        shot("Join the Clubhouse, link in bio.", "", "", skus=("SPKL-9",)),
    ])


# ------------------------------------------------------------------ the plan


def test_good_plan_passes(stock_by_sku, settings):
    assert video.plan_errors(good_plan(), stock_by_sku, settings) == []


@pytest.mark.parametrize("change,expect", [
    (lambda p: setattr(p.shots[1], "headline", "Now £79.95"), "No £ prices"),
    (lambda p: setattr(p.shots[2], "headline", "Up to 30%"), "up to 30%"),           # best saving here is 25%
    (lambda p: [setattr(x, "skus", ["SPKL-9"]) for x in p.shots], "up to 25%"),     # the spikeless pair only saves 15%
    (lambda p: setattr(p.shots[1], "prompt", "A golfer's hand lifts the shoe."), "products only"),
    (lambda p: setattr(p.shots[1], "skus", ["GONE-9"]), "isn't in stock"),
    (lambda p: setattr(p.shots[1], "skus", []), "shows no product"),
    (lambda p: setattr(p.shots[1], "headline", "Fully waterproof"), "repeats the voice"),
    (lambda p: setattr(p.shots[2], "headline", "Under Armour up to 25% off"), "Brand"),
    (lambda p: setattr(p.shots[3], "say", "Link in bio."), "call to action"),
    (lambda p: setattr(p.shots[0], "kicker", "SAVE £20 TODAY"), "no £ or %"),
    (lambda p: p.shots.__setitem__(slice(1, 3), []), "3 to 6 shots"),
])
def test_plan_breaking_a_rule_is_refused(stock_by_sku, settings, change, expect):
    plan = good_plan()
    change(plan)
    errors = video.plan_errors(plan, stock_by_sku, settings)
    assert any(expect in e for e in errors), errors


def test_edits_may_only_use_allowed_products(stock_by_sku, settings):
    plan = good_plan()
    assert any("may show" in e for e in video.plan_errors(plan, stock_by_sku, settings, allowed={"SPKL-9"}))


class FakeLLM:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def complete_json(self, system, messages, schema, max_tokens):
        self.calls.append((system, copy.deepcopy(messages), schema))
        return self.responses.pop(0), []


SCRIPT_SHOTS = [
    {"seconds": 3, "film": "Close-up: hand lifts both shoes", "say": "Wet feet by the third hole?", "on_screen": "Winter golf shoes"},
    {"seconds": 5, "film": "Side view of the spikeless pair", "say": "Both of these are fully waterproof.", "on_screen": ""},
    {"seconds": 6, "film": "Phone shows the Clubhouse page", "say": "Join the Clubhouse, link in bio.", "on_screen": ""},
]


def script_row(**kw) -> dict:
    row = {"week_start": WEEK, "script_no": 1, "format": "value_comparison", "hook": "Wet feet by the third hole?",
           "caption": "Two winter shoes. Link in bio.", "featured_skus": json.dumps(["SPKL-9", "SPKD-9"]),
           "shots": json.dumps(SCRIPT_SHOTS), "status": "planned", "edit_request": None, "approved_version": None}
    row.update(kw)
    return row


def test_voice_and_headlines_come_from_the_script(stock_by_sku):
    planned = [{"kicker": "member deal", "prompt": "Both shoes on the mat.", "skus": ["SPKL-9", "SPKD-9"], "setting": "studio"},
               {"kicker": "Spikeless", "prompt": "The spikeless pair, slow orbit.", "skus": ["SPKL-9"], "setting": "studio"},
               {"kicker": "", "prompt": "Both shoes, pull back.", "skus": ["SPKL-9"], "setting": "studio"}]
    llm = FakeLLM({"shots": planned})
    plan = video.plan_from_script(llm, script_row(), stock_by_sku)
    assert [s.say for s in plan.shots] == [s["say"] for s in SCRIPT_SHOTS]
    assert plan.shots[0].headline == "Winter golf shoes" and plan.shots[0].kicker == "MEMBER DEAL"
    assert "Drive Fade 2 Spikeless" in llm.calls[0][1][0]["content"]
    with pytest.raises(GuardrailError):
        video.plan_from_script(FakeLLM({"shots": planned[:2]}), script_row(), stock_by_sku)


def test_edit_notes_become_a_revised_plan(lines):
    plan = good_plan()
    revised = [dict(say=s.say, headline=s.headline, kicker=s.kicker, prompt=s.prompt, skus=s.skus, setting=s.setting)
               for s in plan.shots]
    revised[0]["say"] = "Cold, wet rounds coming?"
    llm = FakeLLM({"shots": revised, "summary": "New hook", "refused": ["showing the price: no £ in videos"]})
    new, summary, refused = video.edit_plan(llm, plan, "Better hook, and show the price", lines)
    assert new.shots[0].say == "Cold, wet rounds coming?" and new.shots[1].say == plan.shots[1].say
    assert summary == "New hook" and refused == ["showing the price: no £ in videos"]
    assert "Better hook" in llm.calls[0][1][0]["content"] and '"members_save_pct": 25' in llm.calls[0][1][0]["content"]


# ---------------------------------------------------------------- timings


def words_for(plan, gap=0.3):
    out, t = [], 0.0
    for s in plan.shots:
        for w in s.say.split():
            out.append((w, t, t + 0.25))
            t += 0.25
        t += gap
    return out


def test_each_shot_starts_with_its_own_line():
    plan = good_plan()
    words = words_for(plan)
    win = video.shot_windows(plan, words, words[-1][2] + 0.4)
    starts = [a for a, _ in win]
    assert starts[0] == 0 and starts[1] == words[len(plan.shots[0].say.split())][1]
    assert all(b > a for a, b in win) and win[-1][1] == pytest.approx(words[-1][2] + 0.4)


def test_silent_shots_share_time_by_the_script():
    plan = good_plan()
    plan.shots.insert(2, shot("", "", "", seconds=4))
    plan.shots[1].seconds = 4
    words = words_for(plan)
    win = video.shot_windows(plan, words, words[-1][2] + 0.4)
    assert win[1][1] - win[1][0] == pytest.approx(win[2][1] - win[2][0])
    # and timings that don't line up word for word fall back to how much each shot says
    win2 = video.shot_windows(plan, words[:-2], 20.0)
    assert win2[0][0] == 0 and win2[-1][1] == 20.0 and all(b > a for a, b in win2)


def test_timestamps_become_words():
    seg = [{"characters": list("Hi you"), "character_start_times_seconds": [0, .1, .2, .3, .4, .5],
            "character_end_times_seconds": [.1, .2, .3, .4, .5, .6]}]
    assert video.words_from_timestamps(seg) == [("Hi", 0, 0.2), ("you", 0.3, 0.6)]


# ----------------------------------------------------------- what to make


def test_what_to_make():
    row = script_row()
    assert video.work_to_do(row, None, 3) == ("make", "")
    latest = {"version": 1, "source": video.source_hash(row), "status": "review", "edit_request": None}
    assert video.work_to_do(row, latest, 3) is None                                     # v1 exists
    assert video.work_to_do(script_row(hook="New hook"), latest, 3) == ("make", "")     # pack regenerated
    changes = script_row(status="changes", edit_request="Shorter hook")
    assert video.work_to_do(changes, latest, 3) == ("edit", "Shorter hook")
    done = dict(latest, version=2, edit_request="Shorter hook")
    assert video.work_to_do(changes, done, 3) is None                                   # already answered
    assert video.work_to_do(script_row(status="changes", edit_request="More"), dict(done, version=3), 3)[0] == "limit"
    assert video.work_to_do(script_row(status="approved"), latest, 3) is None


# --------------------------------------------------------- safe area / fit


class SmallFonts:
    def __init__(self):
        self.kicker = ImageFont.load_default(34)
        self.heads = {s: ImageFont.load_default(s) for s in video_render.HEAD_SIZES}


def test_text_must_fit_tiktoks_safe_area():
    fonts = SmallFonts()
    ok = video_render.Segment(Path("x"), 0, 3, kicker="MEMBERS SAVE", headline="Up to 25%")
    long = video_render.Segment(Path("x"), 0, 3, headline="A headline far too long to fit beside TikTok's buttons")
    assert video_render.check_text_fits(fonts, [ok]) == []
    assert "safe area" in video_render.check_text_fits(fonts, [long])[0]


# ---------------------------------------------------------------- the job


@pytest.fixture
def job_db(settings, lines, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path / "logs")
    url = f"sqlite:///{tmp_path / 'v.db'}"
    db = Database(url)
    db.migrate()
    stock.save_snapshot(db, "snap1", datetime.now(timezone.utc).isoformat(), lines)
    db.execute(
        "INSERT INTO content_log (week_start, script_no, format, hook, caption, featured_skus, shots, status, created_at) "
        "VALUES (?, 1, 'value_comparison', ?, ?, ?, ?, 'planned', ?)",
        (WEEK, "Wet feet by the third hole?", "Two winter shoes. Link in bio.", json.dumps(["SPKL-9", "SPKD-9"]),
         json.dumps(SCRIPT_SHOTS), datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    db.close()
    data = copy.deepcopy(settings.data)
    data["video"]["enabled"] = True
    return Settings(data=data, source=settings.source, env={"DATABASE_URL": url}), url


PLANNED = {"shots": [
    {"kicker": "MEMBER DEAL", "prompt": "Both shoes on the mat.", "skus": ["SPKL-9", "SPKD-9"], "setting": "studio"},
    {"kicker": "SPIKELESS", "prompt": "The spikeless pair, slow orbit.", "skus": ["SPKL-9"], "setting": "studio"},
    {"kicker": "", "prompt": "Both shoes, pull back.", "skus": ["SPKL-9"], "setting": "studio"},
]}


def test_job_does_nothing_while_switched_off(job_db, monkeypatch):
    s, url = job_db
    data = copy.deepcopy(s.data)
    data["video"]["enabled"] = False
    monkeypatch.setattr(video, "Maker", lambda *a, **k: pytest.fail("switched off"))
    assert video.main(["--week-start", WEEK], llm=FakeLLM(), settings=Settings(data, s.source, s.env)) == 0


def test_dry_run_plans_but_makes_nothing(job_db, monkeypatch):
    s, url = job_db
    monkeypatch.setattr(video, "Maker", lambda *a, **k: pytest.fail("dry run must not make a video"))
    assert video.main(["--dry-run", "--week-start", WEEK], llm=FakeLLM(copy.deepcopy(PLANNED)), settings=s) == 0
    db = Database(url)
    assert db.query("SELECT COUNT(*) FROM video_version")[0][0] == 0
    db.close()


class FakeMaker:
    made = []

    def __init__(self, settings, llm, fal, store, http, work):
        pass

    def make(self, plan, stock_by_sku, out):
        out.write_bytes(b"mp4")
        FakeMaker.made.append(plan)
        return video.Result(ok=True, file=out, checks={"voice_matches_script": 0.98},
                            flags=["shot 2: sole studs differ from the photo"], cost=1.2)


def test_live_run_makes_a_draft_for_review_then_answers_edits(job_db, monkeypatch, tmp_path):
    s, url = job_db
    cells = []
    monkeypatch.setattr(video, "Maker", FakeMaker)
    monkeypatch.setattr(video, "make_store", lambda settings: LocalStore(tmp_path / "store"))
    monkeypatch.setattr(production, "set_cells", lambda st, week, no, values, sheets=None: cells.append((week, no, values)))
    monkeypatch.setattr(production, "sync", lambda *a, **k: 0)
    FakeMaker.made = []
    assert video.main(["--week-start", WEEK], llm=FakeLLM(copy.deepcopy(PLANNED)), settings=s) == 0
    db = Database(url)
    v = db.query("SELECT version, status, file_key, flags, cost_usd FROM video_version")
    assert v[0][:3] == (1, "review", f"videos/{WEEK}/1/v1.mp4") and "sole studs" in v[0][3]
    assert db.query("SELECT status FROM content_log")[0][0] == "review"
    assert cells[-1][2]["Status"] == "Ready to review" and cells[-1][2]["Version"] == "v1"
    assert "sole studs" in cells[-1][2]["Notes"]
    # Nothing new to do on the next run.
    assert video.main(["--week-start", WEEK], llm=FakeLLM(), settings=s) == 0
    assert len(FakeMaker.made) == 1
    # Layton asks for changes: the next run makes v2 from his notes.
    db.execute("UPDATE content_log SET status = 'changes', edit_request = 'Mention the waterproof warranty'")
    db.commit()
    plan = FakeMaker.made[0]
    revised = [dict(say=x.say, headline=x.headline, kicker=x.kicker, prompt=x.prompt, skus=x.skus, setting=x.setting)
               for x in plan.shots]
    revised[1]["headline"] = "1-year waterproof warranty"
    edit = FakeLLM({"shots": revised, "summary": "Added the warranty", "refused": []})
    assert video.main(["--week-start", WEEK], llm=edit, settings=s) == 0
    assert db.query("SELECT MAX(version) FROM video_version")[0][0] == 2
    assert FakeMaker.made[-1].shots[1].headline == "1-year waterproof warranty"
    assert cells[-1][2]["Version"] == "v2" and "Added the warranty" in cells[-1][2]["Notes"]
    db.close()


def test_a_plan_that_breaks_a_rule_is_never_made(job_db, monkeypatch, tmp_path):
    s, url = job_db
    cells = []
    monkeypatch.setattr(video, "Maker", lambda *a, **k: pytest.fail("must not be made"))
    monkeypatch.setattr(production, "set_cells", lambda st, week, no, values, sheets=None: cells.append(values))
    monkeypatch.setattr(production, "sync", lambda *a, **k: 0)
    bad = copy.deepcopy(PLANNED)
    bad["shots"][1]["prompt"] = "A golfer's hand picks up the shoe."
    assert video.main(["--week-start", WEEK], llm=FakeLLM(bad), settings=s) == 0
    db = Database(url)
    assert db.query("SELECT status FROM video_version")[0][0] == "failed"
    db.close()
    assert "Can't make this video" in cells[-1]["Notes"] and "products only" in cells[-1]["Notes"]


def test_weekly_budget_stops_the_job(job_db, monkeypatch):
    s, url = job_db
    data = copy.deepcopy(s.data)
    data["video"]["weekly_budget_usd"] = 1
    s2 = Settings(data=data, source=s.source, env=s.env)
    cells = []
    monkeypatch.setattr(video, "Maker", lambda *a, **k: pytest.fail("over budget"))
    monkeypatch.setattr(production, "set_cells", lambda st, week, no, values, sheets=None: cells.append(values))
    monkeypatch.setattr(production, "sync", lambda *a, **k: 0)
    assert video.main(["--week-start", WEEK], llm=FakeLLM(), settings=s2) == 0
    assert "budget" in cells[-1]["Notes"]


# ------------------------------------------------- approvals in the tab


def test_approval_belongs_to_the_version_reviewed(job_db):
    from test_production import FakeSheets

    s, url = job_db
    s = Settings(data=s.data, source=s.source, env=dict(s.env, DASHBOARD_SHEET_ID="sheet1"))
    sheets = FakeSheets()
    sheets.ensure_tab("sheet1", "Production")
    row = [""] * len(production.HEADER)
    for name, value in {"Week": WEEK, "Video": "1", "Status": "Approved", "Version": "v2",
                        "Edit request": "Shorter hook"}.items():
        row[production.COL[name]] = value
    sheets.values["Production"] = [production.HEADER, row]
    db = Database(url)
    assert production.sync(s, db, sheets) == 1
    assert db.query("SELECT status, approved_version, edit_request FROM content_log")[0] == ("approved", 2, "Shorter hook")
    # Moving it back to review clears the approval: it belonged to v2.
    row[production.COL["Status"]] = "Ready to review"
    row[production.COL["Version"]] = "v3"
    production.sync(s, db, sheets)
    assert db.query("SELECT status, approved_version FROM content_log")[0] == ("review", None)
    db.close()
