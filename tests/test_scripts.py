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


TERMS = "https://www.evolutiongolf.co.uk/pages/giveaway-terms"


def with_giveaway(settings) -> Settings:
    """Settings with a T&Cs link, and a prize allowed with two sizes (the fixture's shoes)."""
    data = copy.deepcopy(settings.data)
    data["giveaway"].update(terms_url=TERMS, min_sizes=2)
    return Settings(data=data, source=settings.source, env=dict(settings.env))


@pytest.fixture
def settings(settings):
    return with_giveaway(settings)


@pytest.fixture
def lines(settings):
    records = [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]
    return stock.build_snapshot(records, settings)[0]


@pytest.fixture
def plan(settings, lines):
    return scripts.build_plan(settings, lines, [], MONDAY)


# The fixture's featured stock: UA polo £35 (was £60), adidas jacket £70 (was £120),
# adidas Adipower shoes £89.95 (was £149.95) in UK9 and UK10.
VALUE_COMPARISONS = [
    {  # price gap
        "hook": "£35 vs £70. Which would you buy?",
        "on_screen_text": ["£35 or £70?", "Up to 40% off for members"],
        "caption": "Polo or jacket? Comment your pick. Members save up to 40%, link in bio.",
    },
    {  # saving (Shopify compare-at)
        "hook": "A £60 polo for £35?",
        "on_screen_text": ["Was £60, now £35"],
        "caption": "Polo or jacket for the cold mornings? Comment your pick, link in bio.",
    },
    {  # fixed tier
        "hook": "Best layers under £100?",
        "on_screen_text": ["Golf layers under £100"],
        "caption": "Two layers, both under £100. Which one? Link in bio.",
    },
]


def good_scripts(plan) -> list[dict]:
    """A pack that passes every check, built from the plan."""
    sizes = iter(plan.roulette_sizes)
    comparisons = iter(VALUE_COMPARISONS)
    out = []
    for fmt in plan.order:
        s = {
            "format": fmt,
            "hook": "Which one would you pick?",
            "setup": "Shop floor counter, plain wall behind, daylight. Hands only, products laid side by side.",
            "on_screen_text": ["Which one?"],
            "caption": "Comment your pick, link in bio.",
            "hashtags": ["#golf", "#golfuk", "#golfclothes"],
            "featured_skus": ["POLO-M", "JKT-L"],
            "cta": "Join the Evo Members Club, link in bio",
        }
        if fmt == "value_comparison":
            s.update(next(comparisons))
        elif fmt == "size_roulette":
            size, _ = next(sizes)
            skus = [l.sku for l in plan.pool if scripts.size_label(l.size) == size]
            s.update(
                on_screen_text=[f"Size Roulette: {size}", "Members only", "When it's gone, it's gone"],
                caption=f"Every pair we've got left in {size}. Members only, link in bio.",
                featured_skus=skus,
            )
        elif fmt == "giveaway":
            wording = (free_entry_wording().replace("[date]", f"{plan.giveaway_close:%-d %B %Y}")
                       .replace("[terms]", plan.terms_url))
            s.update(
                on_screen_text=["WIN these", "Follow, like, tag a mate"],
                caption="Win a pair, and choose your size! Follow, like and tag a mate. " + wording,
                featured_skus=[plan.giveaway_lines[0].sku],
            )
        elif fmt == "trolley":
            s.update(
                on_screen_text=["3 trolley set-up mistakes"],
                caption="Three set-up mistakes we see every week. Which one are you guilty of?",
                featured_skus=["TROLLEY-M5"],
            )
        s["shot_list"] = outline(s)
        out.append(s)
    return out


def outline(s: dict) -> list[dict]:
    """Timed shots that open on the hook and place every on-screen line once."""
    screen = s["on_screen_text"]
    shots = [{"seconds": 3, "film": "Close-up: hand lifts the first product to camera", "say": s["hook"],
              "on_screen": screen[0]}]
    for line in screen[1:]:
        shots.append({"seconds": 6, "film": "Close-up on the label and fabric", "say": "", "on_screen": line})
    while len(shots) < 3:
        shots.append({"seconds": 8, "film": "Wide: both products side by side, hand points to each", "say":
                      "Which one would you pick?", "on_screen": ""})
    shots.append({"seconds": 6, "film": "Phone screen showing the Members Club page", "say":
                  "Join the Members Club, link in bio.", "on_screen": ""})
    return shots


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
    # The prize is the shoe in two sizes, so the winner chooses; never a lone size.
    assert {l.sku for l in plan.giveaway_lines} == {"SHOE-9", "SHOE-10"}
    assert plan.giveaway_post == date(2026, 10, 8)                # Thursday, after filming and editing
    assert plan.giveaway_close == date(2026, 10, 15)              # 7 days from posting
    assert plan.terms_url == TERMS
    assert [l.sku for l in plan.trolleys] == ["TROLLEY-M5"]  # the trolley bag is an accessory


def test_no_giveaway_without_terms_or_a_choosable_prize(settings, lines):
    no_terms = copy.deepcopy(settings.data)
    no_terms["giveaway"]["terms_url"] = None
    p = scripts.build_plan(Settings(no_terms, settings.source), lines, [], MONDAY)
    assert p.giveaway is None and p.mix["giveaway"] == 0 and p.mix["value_comparison"] == 4
    assert any("T&Cs" in n for n in p.notes)
    strict = copy.deepcopy(settings.data)
    strict["giveaway"]["min_sizes"] = 3  # the shoe has only 2 sizes, and the polo and jacket only 1
    p = scripts.build_plan(Settings(strict, settings.source), lines, [], MONDAY)
    assert p.giveaway is None and any("choose" in n for n in p.notes)
    short = copy.deepcopy(settings.data)
    short["giveaway"]["entry_days"] = 6
    with pytest.raises(GuardrailError, match="at least 7 days"):
        scripts.build_plan(Settings(short, settings.source), lines, [], MONDAY)


def test_prompt_has_live_prices_and_ideas_but_no_member_bands(settings, plan, make_line):
    values = scripts.prompt_values(settings, plan, [])
    prompt = load_prompt("script_generator", values)
    assert "SHOE-9" in prompt.user and "TROLLEY-M5" in prompt.user
    assert '"live_price": "£89.95"' in prompt.user and '"was_price": "£149.95"' in prompt.user
    assert "member_price_band" not in prompt.user and "under £90" not in prompt.user
    ideas = scripts.price_ideas(plan.pool)
    assert {"gap", "saving", "tier"} <= set(ideas)
    assert ideas["saving"] and all(i["was"] != i["price"] for i in ideas["saving"])
    assert TERMS in prompt.user and "Thursday 8 October 2026" in prompt.user
    assert "Free to enter, no purchase needed" in prompt.system  # CONTENT_RULES loaded
    assert "{{" not in prompt.system + prompt.user


def test_metrics_note_when_tiktok_isnt_connected(settings, plan):
    assert "No performance metrics" in plan.metrics_note and "WINDSOR_API_KEY" in plan.metrics_note
    from evo_tiktok import pack
    md = pack.render_markdown(plan, good_scripts(plan))
    assert md.splitlines()[2].startswith("**⚠ No performance metrics")


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
        # 1: prices must be live website prices (£22.75 would be a member price)
        (0, {"hook": "Polos for £22.75?"}, "isn't a live website price"),
        # 2: no auto-rounded caps; fixed tiers only, everything 10% below
        (2, {"hook": "Best layers under £80?"}, "isn't a fixed price tier"),
        (2, {"hook": "Best layers under £75?"}, "at least 10% below"),
        # 3: a comparison needs different products, not one product
        (0, {"featured_skus": ["POLO-M"]}, "at least 2 different products"),
        (0, {"featured_skus": ["SHOE-9", "SHOE-10"]}, "colourways"),
        # 6: giveaway caption must link the T&Cs, carry the TikTok line and let the winner choose a size
        (5, {"caption": "Win! " + free_entry_wording().replace("[date]", "15 October 2026")
             .replace("[terms]", "link in bio")}, "T&Cs"),
        (0, {"caption": "RRP £60, members pay less", "featured_skus": ["POLO-NORRP"]}, "no RRP"),
        (0, {"setup": "Film the products, then add an AI voiceover"}, "Real footage"),
        # the outline leaves nothing to guess
        (0, {"setup": " "}, "setup must say"),
        (0, {"hook": "£35 vs £70. Which is better?"}, "say the hook word for word"),
        (0, {"on_screen_text": ["£35 or £70?", "A line no shot shows"]}, "not placed"),
        (0, {"hook": "one two three four five six seven eight nine ten eleven twelve"}, "hook"),
        (0, {"caption": "x" * 151}, "max 150"),
        (3, {"featured_skus": ["SHOE-10"]}, "aren't UK9 footwear"),
        (3, {"on_screen_text": ["Size Roulette", "Members only"], "caption": "Every pair left"}, "say the size"),
        (5, {"caption": "Win a pair! Follow and tag a mate."}, "free-entry wording"),
        (5, {"caption": "Win a pair! " + free_entry_wording().replace("[date]", "15 October 2026")
             .replace("[terms]", TERMS)}, "chooses their size"),
        (6, {"caption": "Motocaddy M5 now £999"}, "Price or discount"),
    ],
)
def test_bad_scripts_fail(plan, settings, index, changes, expect):
    errors = scripts.validate_scripts(_break(plan, index, **changes), plan, settings)
    assert any(expect in e for e in errors), errors


def test_wrong_count_fails(plan, settings):
    errors = scripts.validate_scripts(good_scripts(plan)[:6], plan, settings)
    assert any("Expected 7 scripts" in e for e in errors)


def test_member_discount_is_per_video(plan, settings, make_line):
    # 7: "up to 35%" needs a product in THAT video at 35%; the polo and jacket get ~42%.
    ok = scripts.validate_scripts(_break(plan, 0, caption="Up to 40% off for members, link in bio"), plan, settings)
    assert ok == []
    bad = scripts.validate_scripts(_break(plan, 0, caption="Up to 45% off for members, link in bio"), plan, settings)
    assert any("up to 45%" in e for e in bad)


def test_value_comparisons_must_rotate_hook_types(plan, settings):
    pack = good_scripts(plan)
    for i in (1, 2):
        pack[i].update(VALUE_COMPARISONS[0])  # all three now price gaps
    assert any("same kind of hook" in e for e in scripts.validate_scripts(pack, plan, settings))


def test_retry_once_with_errors_then_succeed(plan, settings):
    bad = {"scripts": _break(plan, 0, caption="adidas polos £20")}
    llm = FakeLLM(bad, {"scripts": good_scripts(plan)})
    result, dropped = scripts.generate(llm, settings, plan, [])
    assert len(result) == 7 and dropped == [] and len(llm.calls) == 2
    retry = llm.calls[1]
    assert retry[1]["role"] == "assistant"
    assert "failed our checks" in retry[2]["content"] and "Brand 'adidas'" in retry[2]["content"]


def test_a_video_that_still_fails_is_dropped_never_shipped(plan, settings):
    bad = {"scripts": _break(plan, 0, hook="Polos for £22.75?")}  # a member price, not the live price
    kept, dropped = scripts.generate(FakeLLM(bad, bad), settings, plan, [])
    assert len(kept) == 6 and len(dropped) == 1
    assert dropped[0]["hook"] == "Polos for £22.75?" and "live website price" in dropped[0]["errors"][0]
    rows = scripts.price_check_rows(kept + dropped, plan)
    assert {r["price_check"][:4] for r in rows if not r["kept"]} == {"FAIL"}
    assert all(r["price_check"] == "pass" for r in rows if r["kept"])


def test_fails_loudly_when_nothing_passes(plan, settings):
    awful = {"scripts": [s | {"caption": "adidas polos £20"} for s in good_scripts(plan)]}
    with pytest.raises(GuardrailError):
        scripts.generate(FakeLLM(awful, awful), settings, plan, [])


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
    data = copy.deepcopy(with_giveaway(settings).data)
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
    assert "## Price checks (live website prices)" in md and "| pass |" in md
