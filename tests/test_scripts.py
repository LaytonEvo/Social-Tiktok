import copy
from decimal import Decimal
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


# Clubhouse member prices for the fixture's featured stock: polo 15%, jacket 25%,
# shoes 20% off the website price (as Luke's member-products API would give).
MEMBER_PRICES = {"POLO-M": "29.75", "JKT-L": "52.50", "SHOE-9": "71.96", "SHOE-10": "71.96"}


@pytest.fixture
def lines(settings):
    records = [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]
    out = stock.build_snapshot(records, settings)[0]
    for line in out:
        if line.sku in MEMBER_PRICES:
            line.member_price = Decimal(MEMBER_PRICES[line.sku])
            line.members_units = line.units_total
    return out


@pytest.fixture
def plan(settings, lines):
    return scripts.build_plan(settings, lines, [], MONDAY)


# Value comparisons on the fixture's polo and jacket. "{save}" is filled with what
# members save on them (up to 25% with member prices; left out when there are none).
VALUE_COMPARISONS = [
    {
        "hook": "Cold mornings on the course?",
        "on_screen_text": ["Two winter layers", "Members save {save}"],
        "caption": "Two layers for cold rounds. Clubhouse members save {save}, link in bio.",
    },
    {
        "hook": "Need a layer that packs away?",
        "on_screen_text": ["Sizes M and L"],
        "caption": "Light layers for the bag. Clubhouse members save {save}, link in bio.",
    },
    {
        "hook": "Upgrade your winter kit?",
        "on_screen_text": ["Members save {save}"],
        "caption": "Polo or jacket for the cold? Join the Clubhouse, link in bio.",
    },
]


def saving_text(lines) -> str | None:
    """'up to X%' for these products, X rounded down to the nearest 5, or None."""
    best = max((line.member_saving_pct or 0 for line in lines), default=0)
    return f"up to {int(best // 5 * 5)}%" if best >= 5 else None


def with_saving(s: dict, save: str | None) -> dict:
    if save:
        s["on_screen_text"] = [x.replace("{save}", save) for x in s["on_screen_text"]]
        s["caption"] = s["caption"].replace("{save}", save)
    else:
        s["on_screen_text"] = [x for x in s["on_screen_text"] if "{save}" not in x] or ["Winter layers"]
        s["caption"] = s["caption"].replace(" Clubhouse members save {save},", "")
    return s


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
            "cta": "Join the Clubhouse, link in bio",
        }
        if fmt == "value_comparison":
            s.update(copy.deepcopy(next(comparisons)))
            with_saving(s, saving_text(scripts.featured_lines(s, plan)))
        elif fmt == "size_roulette":
            size, _ = next(sizes)
            skus = [l.sku for l in plan.pool if scripts.size_label(l.size) == size]
            save = saving_text([plan.stock_by_sku[k] for k in skus])
            s.update(
                on_screen_text=[f"Size Roulette: {size}", "Members only", "When it's gone, it's gone"],
                caption=f"Every pair we've got left in {size}. Members only"
                        + (f", members save {save}" if save else "") + ", link in bio.",
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
    shots.append({"seconds": 6, "film": "Phone screen showing the Clubhouse page", "say":
                  "Join the Clubhouse, link in bio.", "on_screen": ""})
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
    stock_json = prompt.user.split("{", 1)[1].split("Electric trolleys")[0]
    assert '"members_save_pct": 20' in prompt.user and '"members_save_pct": 25' in prompt.user
    assert "£" not in stock_json and "live_price" not in prompt.user  # videos quote savings, never prices
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
        # videos carry no £ prices; the offer is the member saving
        (0, {"hook": "Polos for £22.75?"}, "No £ prices"),
        (0, {"caption": "Members save up to 30%, link in bio."}, "up to 30%"),
        (0, {"on_screen_text": ["Two winter layers"], "caption": "Two layers for cold rounds, link in bio."},
         "say what Clubhouse members save"),
        # on-screen text adds to the voice, never captions it
        (0, {"on_screen_text": ["Which one would you pick?"]}, "repeats the voiceover"),
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
        (0, {"hook": "Which layer is better?"}, "say the hook word for word"),
        (0, {"on_screen_text": ["Two winter layers", "A line no shot shows"]}, "not placed"),
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


def test_member_saving_is_per_video(plan, settings):
    # The polo saves 15% and the jacket 25% against the website price: "up to 25%" is true
    # for a video with both, "up to 30%" isn't, and the shoes' video can only claim 20%.
    ok = scripts.validate_scripts(_break(plan, 0, caption="Members save up to 25%, link in bio."), plan, settings)
    assert ok == []
    bad = scripts.validate_scripts(_break(plan, 0, caption="Members save up to 30%, link in bio."), plan, settings)
    assert any("up to 30%" in e for e in bad)
    shoes = [i for i, s in enumerate(good_scripts(plan)) if s["format"] == "size_roulette"][0]
    bad = scripts.validate_scripts(_break(plan, shoes, caption="Members only, members save up to 25%."), plan, settings)
    assert any("up to 25%" in e for e in bad)


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
    assert dropped[0]["hook"] == "Polos for £22.75?" and any("No £ prices" in e for e in dropped[0]["errors"])
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

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return response

        def stream(**kwargs):
            self.kwargs = kwargs
            return Stream()

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))
        self.messages = SimpleNamespace(stream=stream)


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
    assert "## Saving checks (Clubhouse member prices)" in md and "| pass |" in md
