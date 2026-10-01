import copy
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_tiktok import runner, scripts, stock
from evo_tiktok.config import Settings
from evo_tiktok.db import Database
from evo_tiktok.llm import ClaudeJSON, LLMError, load_prompt
from evo_tiktok.validators import GuardrailError, free_entry_wording

FIXTURE = Path(__file__).parent / "fixtures" / "shopify_bulk.jsonl"
MONDAY = date(2026, 10, 5)


@pytest.fixture
def lines(settings):
    records = [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]
    return stock.build_snapshot(records, settings)[0]


@pytest.fixture
def plan(settings, lines):
    return scripts.build_plan(settings, lines, [], MONDAY)


def good_scripts(plan) -> list[dict]:
    """A pack that passes every check, built from the plan."""
    sizes = iter(plan.roulette_sizes)
    out = []
    for fmt in plan.order:
        s = {
            "format": fmt,
            "hook": "Which one would you pick?",
            "shot_list": ["Wide shot of the rail", "Close-up on fabric", "Pick one up and point"],
            "on_screen_text": ["Best golf polo under £40?", "40–50% off for members"],
            "caption": "Best polo under £40? Comment your pick. Members save 40–50%, link in bio.",
            "hashtags": ["#golf", "#golfuk", "#golfclothes"],
            "featured_skus": ["POLO-M"],
            "cta": "Join the Evo Members Club, link in bio",
            "est_length_seconds": 25,
        }
        if fmt == "size_roulette":
            size, _ = next(sizes)
            skus = [l.sku for l in plan.pool if scripts.size_label(l.size) == size]
            s.update(
                on_screen_text=[f"Size Roulette: {size}", "Members only", "When it's gone, it's gone"],
                caption=f"Every pair we've got left in {size}. Members only, link in bio.",
                featured_skus=skus,
            )
        elif fmt == "giveaway":
            wording = free_entry_wording().replace("[date]", f"{plan.giveaway_close:%-d %B %Y}")
            s.update(
                on_screen_text=["WIN these", "Follow, like, tag a mate"],
                caption="Win a pair! Follow, like and tag a mate. " + wording,
                featured_skus=[plan.giveaway.sku],
            )
        elif fmt == "trolley":
            s.update(
                on_screen_text=["3 trolley set-up mistakes"],
                caption="Three set-up mistakes we see every week. Which one are you guilty of?",
                featured_skus=["TROLLEY-M5"],
            )
        out.append(s)
    return out


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, system, messages, schema, max_tokens):
        self.calls.append(copy.deepcopy(messages))
        data = self.responses.pop(0)
        return data, [{"type": "text", "text": json.dumps(data)}]


# ------------------------------------------------------------------ planning


def test_week_start():
    assert scripts.week_start_for(date(2026, 10, 5)) == MONDAY  # Monday
    assert scripts.week_start_for(date(2026, 10, 1)) == MONDAY  # Thursday before


def test_apportion():
    assert scripts.apportion({"a": 3, "b": 1}, 4, 1) == {"a": 3, "b": 1}
    assert sum(scripts.apportion({"a": 1, "b": 1, "c": 1}, 5, 1).values()) == 5


def test_plan_uses_configured_mix(plan):
    assert plan.mix == {"value_comparison": 3, "size_roulette": 2, "giveaway": 1, "trolley": 1}
    assert len(plan.order) == 7


def test_mix_weighted_by_watch_time_after_two_weeks(settings):
    history = [
        {"week_start": MONDAY - timedelta(weeks=w), "format": f, "avg_watch_seconds": secs}
        for w in (1, 2)
        for f, secs in [("value_comparison", 4), ("size_roulette", 12), ("giveaway", 8), ("trolley", 8)]
    ]
    mix, how = scripts.plan_mix(settings, history)
    assert sum(mix.values()) == 7 and mix["giveaway"] == 1
    assert mix["size_roulette"] > 2 and mix["value_comparison"] < 3
    assert "weighted" in how
    one_week = [h for h in history if h["week_start"] == MONDAY - timedelta(weeks=1)]
    assert scripts.plan_mix(settings, one_week)[0] == settings["weekly_mix"]


def test_roulette_sizes_need_three_pairs_and_no_repeats(plan, settings, lines):
    assert plan.roulette_sizes == [("UK9", 4), ("UK10", 3)]
    history = [{"week_start": MONDAY - timedelta(weeks=2), "format": "size_roulette", "roulette_size": "UK9"}]
    replanned = scripts.build_plan(settings, lines, history, MONDAY)
    assert replanned.roulette_sizes == [("UK10", 3)]
    assert replanned.mix["size_roulette"] == 1 and replanned.mix["value_comparison"] == 4
    old = [{"week_start": MONDAY - timedelta(weeks=4), "format": "size_roulette", "roulette_size": "UK9"}]
    assert scripts.build_plan(settings, lines, old, MONDAY).roulette_sizes[0] == ("UK9", 4)


def test_giveaway_and_trolleys(plan):
    assert plan.giveaway.sku in {l.sku for l in plan.pool}
    assert plan.giveaway_close == date(2026, 10, 11)
    assert [l.sku for l in plan.trolleys] == ["TROLLEY-M5"]  # the trolley bag is an accessory


def test_prompt_has_skus_but_no_rrps(settings, plan):
    values = scripts.prompt_values(settings, plan, [])
    prompt = load_prompt("script_generator", values)
    assert "SHOE-9" in prompt.user and "TROLLEY-M5" in prompt.user
    assert "149.95" not in prompt.user and "compare" not in prompt.user.lower()
    assert "under £90" in prompt.user
    assert "Free to enter, no purchase needed" in prompt.system  # CONTENT_RULES loaded
    assert "{{" not in prompt.system + prompt.user


# ---------------------------------------------------------------- validation


def test_good_pack_passes(plan, settings):
    assert scripts.validate_scripts(good_scripts(plan), plan, settings) == []


def _break(plan, index, **changes):
    pack = good_scripts(plan)
    pack[index].update(changes)
    return pack


@pytest.mark.parametrize(
    "index,changes,expect",
    [
        (0, {"caption": "adidas polos under £40, comment your pick"}, "Brand"),
        (0, {"on_screen_text": ["Nike 50% off"]}, "Brand"),
        (0, {"featured_skus": ["OOS-9"]}, "zero stock"),
        (0, {"featured_skus": ["NOPE"]}, "not in today's stock"),
        (0, {"featured_skus": ["SHOE-LIVE-9"]}, "featured pool"),
        (0, {"caption": "Up to 60% off for members"}, "up to 60%"),
        (0, {"caption": "RRP £60, members pay less", "featured_skus": ["POLO-NORRP"]}, "no RRP"),
        (0, {"shot_list": ["Film it", "Add an AI voiceover"]}, "Real footage"),
        (0, {"hook": "one two three four five six seven eight nine ten eleven twelve"}, "hook"),
        (0, {"caption": "x" * 151}, "max 150"),
        (3, {"featured_skus": ["SHOE-10"]}, "aren't UK9 footwear"),
        (3, {"on_screen_text": ["Size Roulette", "Members only"], "caption": "Every pair left"}, "say the size"),
        (5, {"caption": "Win a pair! Follow and tag a mate."}, "free-entry wording"),
        (6, {"caption": "Motocaddy M5 now £999"}, "Price or discount"),
    ],
)
def test_bad_scripts_fail(plan, settings, index, changes, expect):
    errors = scripts.validate_scripts(_break(plan, index, **changes), plan, settings)
    assert any(expect in e for e in errors), errors


def test_wrong_count_fails(plan, settings):
    errors = scripts.validate_scripts(good_scripts(plan)[:6], plan, settings)
    assert any("Expected 7 scripts" in e for e in errors)


def test_retry_once_with_errors_then_succeed(plan, settings):
    bad = {"scripts": _break(plan, 0, caption="adidas polos £20")}
    llm = FakeLLM(bad, {"scripts": good_scripts(plan)})
    result = scripts.generate(llm, settings, plan, [])
    assert len(result) == 7 and len(llm.calls) == 2
    retry = llm.calls[1]
    assert retry[1]["role"] == "assistant"
    assert "failed our checks" in retry[2]["content"] and "Brand 'adidas'" in retry[2]["content"]


def test_fails_loudly_after_retry(plan, settings):
    bad = {"scripts": _break(plan, 0, caption="adidas polos £20")}
    with pytest.raises(GuardrailError):
        scripts.generate(FakeLLM(bad, bad), settings, plan, [])


# --------------------------------------------------------------------- Claude


class FakeAnthropic:
    def __init__(self, stop_reason="end_turn", text='{"scripts": []}'):
        self.kwargs = None
        response = SimpleNamespace(
            model="claude-sonnet-5-5", stop_reason=stop_reason, stop_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            content=[SimpleNamespace(type="text", text=text)],
        )

        def create(**kwargs):
            self.kwargs = kwargs
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))
        self.messages = SimpleNamespace(create=create)


def test_claude_call_uses_settings_model_schema_and_fallback(settings):
    fake = FakeAnthropic()
    data, _ = ClaudeJSON(settings, "scripts", client=fake).complete_json("sys", [], scripts.SCRIPT_SCHEMA)
    assert data == {"scripts": []}
    assert fake.kwargs["model"] == settings["models"]["scripts"]
    assert fake.kwargs["output_config"]["format"]["schema"] is scripts.SCRIPT_SCHEMA
    assert fake.kwargs["fallbacks"] == "default"


def test_claude_refusal_raises(settings):
    with pytest.raises(LLMError):
        ClaudeJSON(settings, "scripts", client=FakeAnthropic(stop_reason="refusal")).complete_json("s", [], {})


# ------------------------------------------------------------------- the job


@pytest.fixture
def job(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path / "logs")
    url = f"sqlite:///{tmp_path / 'evo.db'}"
    db = Database(url)
    db.migrate()
    db.close()
    data = copy.deepcopy(settings.data)
    data["outputs"]["pack_dir"] = str(tmp_path / "packs")
    s = Settings(data=data, source=settings.source, env={"DATABASE_URL": url})
    posted = []
    monkeypatch.setattr("evo_tiktok.scripts.publish", lambda *a, **k: posted.append(a) or {"published": True})
    return s, url, tmp_path, posted


def _llm_for(settings):
    records = [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]
    plan = scripts.build_plan(settings, stock.build_snapshot(records, settings)[0], [], MONDAY)
    return FakeLLM({"scripts": good_scripts(plan)})


def _run(s, *flags):
    argv = ["--from-jsonl", str(FIXTURE), "--week-start", MONDAY.isoformat(), *flags]
    return scripts.main(argv, llm=_llm_for(s), settings=s)


def _content_log(url):
    db = Database(url)
    try:
        return db.query("SELECT script_no, format, status, roulette_size FROM content_log ORDER BY script_no")
    finally:
        db.close()


def test_dry_run_writes_pack_but_nothing_external(job):
    s, url, tmp, posted = job
    assert _run(s, "--dry-run") == 0
    assert (tmp / "packs" / "2026-10-05" / "filming_pack.md").exists()
    assert (tmp / "packs" / "2026-10-05" / "filming_pack.pdf").stat().st_size > 1000
    assert _content_log(url) == [] and posted == []
    db = Database(url)
    assert db.query("SELECT COUNT(*) FROM run_log")[0][0] == 0
    db.close()


def test_live_run_logs_planned_scripts_and_posts_to_slack(job):
    s, url, tmp, posted = job
    assert _run(s) == 0
    rows = _content_log(url)
    assert len(rows) == 7 and {r[2] for r in rows} == {"planned"}
    assert [r[3] for r in rows if r[1] == "size_roulette"] == ["UK9", "UK10"]
    assert len(posted) == 1
    assert _run(s) == 0  # a re-run replaces the planned rows rather than duplicating them
    assert len(_content_log(url)) == 7
    md = (tmp / "packs" / "2026-10-05" / "filming_pack.md").read_text()
    assert "- **Size Roulette sizes:** UK9 (4 pairs), UK10 (3 pairs)" in md
    assert "Free to enter, no purchase needed" in md
