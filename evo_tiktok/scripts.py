"""Script generator (Monday): ``python -m evo_tiktok.scripts [--dry-run]``.

Reads live stock, plans the week's mix, asks Claude for 7 scripts, validates
them against the guardrails (one retry with the errors, then fail loudly) and
writes a filming pack. Live runs also post the pack to Slack and log each
script to ``content_log`` as planned. Nothing is ever posted to TikTok.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import allocation, stock, validators
from .config import ROOT, Settings
from .llm import ClaudeJSON, JSONModel, load_prompt
from .member_prices import MemberPriceSource, apply_member_prices
from .models import StockLine
from .publish import publish
from .runner import JobContext, run_job

log = logging.getLogger(__name__)

FORMATS = ("value_comparison", "size_roulette", "giveaway", "trolley")
FORMAT_NAMES = {
    "value_comparison": "Value comparison",
    "size_roulette": "Size Roulette",
    "giveaway": "Giveaway",
    "trolley": "Trolley",
}
CLEARANCE_FORMATS = {"value_comparison", "size_roulette", "giveaway"}

# One shot of the outline: how long it runs, what's in frame and what the person
# does, the exact words said (empty for none) and the on-screen text shown over it.
SHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "seconds": {"type": "integer"},
        "film": {"type": "string"},
        "say": {"type": "string"},
        "on_screen": {"type": "string"},
    },
    "required": ["seconds", "film", "say", "on_screen"],
    "additionalProperties": False,
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "scripts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": list(FORMATS)},
                    "hook": {"type": "string"},
                    "setup": {"type": "string"},
                    "shot_list": {"type": "array", "items": SHOT_SCHEMA},
                    "on_screen_text": {"type": "array", "items": {"type": "string"}},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "featured_skus": {"type": "array", "items": {"type": "string"}},
                    "cta": {"type": "string"},
                },
                "required": [
                    "format", "hook", "setup", "shot_list", "on_screen_text", "caption",
                    "hashtags", "featured_skus", "cta",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scripts"],
    "additionalProperties": False,
}

TROLLEY_RE = re.compile(r"\btrolley", re.IGNORECASE)
TROLLEY_ACCESSORY_RE = re.compile(
    r"\b(?:bag|cover|holder|umbrella|accessor\w*|seat|scorecard|cup|net|mitts?|tee)\b", re.IGNORECASE
)


# ---------------------------------------------------------------- planning


@dataclass
class Plan:
    week_start: date
    mix: dict[str, int]
    order: list[str]
    roulette_sizes: list[tuple[str, int]]  # (size label, pairs)
    giveaway: StockLine | None          # the prize product (any one of its lines)
    giveaway_close: date | None
    pool: list[StockLine]
    stock_by_sku: dict[str, StockLine]
    trolleys: list[StockLine]
    notes: list[str] = field(default_factory=list)
    giveaway_lines: list[StockLine] = field(default_factory=list)  # every size of the prize in stock
    giveaway_post: date | None = None    # planned posting date; entries run at least 7 days from it
    terms_url: str | None = None
    metrics_note: str | None = None      # set when there are no TikTok metrics to learn from


def week_start_for(today: date) -> date:
    """The Monday this pack is for: today if it's Monday, else next Monday."""
    return today + timedelta(days=(7 - today.weekday()) % 7)


def apportion(weights: dict[str, float], total: int, minimum: int) -> dict[str, int]:
    """Split ``total`` across keys in proportion to ``weights`` (largest remainder),
    giving each key at least ``minimum``."""
    counts = {k: minimum for k in weights}
    spare = total - minimum * len(weights)
    if spare <= 0 or not weights:
        return counts
    weight_sum = sum(weights.values()) or 1.0
    raw = {k: spare * w / weight_sum for k, w in weights.items()}
    for k, v in raw.items():
        counts[k] += math.floor(v)
    left = total - sum(counts.values())
    for k in sorted(raw, key=lambda k: (raw[k] - math.floor(raw[k]), weights[k]), reverse=True)[:left]:
        counts[k] += 1
    return counts


def plan_mix(settings: Settings, history: list[dict]) -> tuple[dict[str, int], str]:
    """The week's format counts. Uses the configured mix until ``min_weeks`` of
    metrics exist, then weights flexible formats by average watch time."""
    base = {f: int(n) for f, n in settings["weekly_mix"].items()}
    cfg = settings.get("mix_weighting") or {}
    with_metrics = [h for h in history if h.get("avg_watch_seconds") is not None]
    weeks = {h["week_start"] for h in with_metrics}
    if len(weeks) < int(cfg.get("min_weeks", 2)):
        return base, f"configured mix ({len(weeks)} week(s) of metrics so far)"
    by_format: dict[str, list[float]] = defaultdict(list)
    for h in with_metrics:
        by_format[h["format"]].append(float(h["avg_watch_seconds"]))
    avg = {f: sum(v) / len(v) for f, v in by_format.items()}
    overall = sum(avg.values()) / len(avg)
    fixed = set(cfg.get("fixed", []))
    flexible = {f: n for f, n in base.items() if f not in fixed}
    weights = {f: n * avg.get(f, overall) / overall for f, n in flexible.items()}
    total = sum(flexible.values())
    mix = {f: n for f, n in base.items() if f in fixed}
    mix.update(apportion(weights, total, int(cfg.get("min_per_format", 1))))
    return {f: mix[f] for f in base}, "weighted by average watch time, last 4 weeks"


def size_label(size: str | None) -> str | None:
    value = allocation._foot_size(size or "")
    return None if value is None else f"UK{value:g}"


def pick_roulette_sizes(
    pool: list[StockLine], n: int, recent: set[str], min_pairs: int
) -> list[tuple[str, int]]:
    """Sizes with the most featured footwear pairs, at least ``min_pairs`` each,
    none used in the last few weeks. One size per Size Roulette script."""
    pairs: Counter[str] = Counter()
    for line in pool:
        label = size_label(line.size) if line.category == allocation.FOOTWEAR else None
        if label:
            pairs[label] += line.members_units
    ranked = sorted(
        ((s, p) for s, p in pairs.items() if p >= min_pairs and s not in recent),
        key=lambda sp: (-sp[1], sp[0]),
    )
    return ranked[:n]


ONE_SIZE = {"", "one size", "os", "default title"}


def base_model(title: str) -> str:
    """The model without its colourway: "Ecco Biom C4 - White/Blue" -> "ecco biom c4"."""
    return re.split(r"\s+[-–]\s+", title, maxsplit=1)[0].strip().lower()


def by_product(lines: list[StockLine]) -> dict[str, list[StockLine]]:
    groups: dict[str, list[StockLine]] = defaultdict(list)
    for line in lines:
        groups[line.product_title].append(line)
    return groups


def pick_giveaway(pool: list[StockLine], stock_by_sku: dict[str, StockLine], cfg: dict) -> list[StockLine]:
    """The prize: a one-size product, or one with enough sizes in stock that the
    winner can choose theirs. Never a single size of shoe. Highest RRP wins."""
    min_sizes = int(cfg.get("min_sizes", 3))
    products = by_product([line for line in pool if line.units_total > 0])

    def winner_can_choose(lines: list[StockLine]) -> bool:
        if all((line.size or "").strip().lower() in ONE_SIZE for line in lines):
            return True
        return len({line.size for line in lines}) >= min_sizes

    pinned = cfg.get("sku")
    if pinned:
        line = stock_by_sku.get(pinned)
        if line is None or line.units_total <= 0:
            raise validators.GuardrailError([f"Pinned giveaway SKU {pinned} is not in stock"])
        lines = products.get(line.product_title) or [line]
        if not winner_can_choose(lines):
            raise validators.GuardrailError([
                f"Pinned giveaway {line.product_title} is size-specific with only {len(lines)} size(s) in stock; "
                f"a sized prize needs {min_sizes}+ sizes so the winner can choose"
            ])
        return lines
    eligible = [lines for lines in products.values() if winner_can_choose(lines)]
    return max(
        eligible,
        key=lambda ls: (max(l.rrp or l.price for l in ls), sum(l.units_total for l in ls), ls[0].product_title),
        default=[],
    )


def _money(value) -> str:
    return f"£{Decimal(value):.2f}"


def product_facts(lines: list[StockLine]) -> dict:
    """What the model may say about one product, all at live website prices."""
    prices = sorted({line.price for line in lines})
    was = [line.compare_at_price for line in lines if line.compare_at_price and line.compare_at_price > line.price]
    member = [line.discount_pct for line in lines if line.in_portal and line.discount_pct]
    first = lines[0]
    return {
        "product": first.product_title,
        "brand": first.vendor,
        "model": base_model(first.product_title),
        "category": first.category,
        "live_price": _money(prices[0]) if len(prices) == 1 else f"{_money(prices[0])}–{_money(prices[-1])}",
        "was_price": _money(max(was)) if was else None,
        "member_discount_pct": int(max(member)) if member else None,
    }


def price_ideas(pool: list[StockLine], per_category: int = 3) -> dict:
    """Price hooks that are true at live website prices, worked out in code.

    gap: two different models (different brands or 20%+ apart) at their live prices.
    saving: Shopify compare-at price above the live price.
    tier: a fixed tier (£30/£50/£75/£100/£150/£200) with every item 10%+ below it.
    """
    facts = []
    for lines in by_product(pool).values():
        f = product_facts(lines)
        f["_low"] = min(line.price for line in lines)
        f["_high"] = max(line.price for line in lines)
        f["_was"] = max((line.compare_at_price for line in lines
                         if line.compare_at_price and line.compare_at_price > line.price), default=None)
        facts.append(f)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_cat[f["category"]].append(f)
    gaps, savings, tiers = [], [], []
    gap_min = Decimal(str(1 + validators.PRICE_GAP_MIN))
    margin = Decimal(str(1 - validators.TIER_MARGIN))
    for cat, items in sorted(by_cat.items()):
        items = sorted(items, key=lambda f: (f["_low"], f["product"]))
        pairs = [
            (a, b) for i, a in enumerate(items) for b in items[i + 1:]
            if a["model"] != b["model"] and b["_low"] >= a["_low"] * gap_min
        ]
        pairs.sort(key=lambda ab: (ab[0]["brand"] == ab[1]["brand"], -(ab[1]["_low"] - ab[0]["_low"])))
        for a, b in pairs[:per_category]:
            gaps.append({
                "category": cat,
                "cheaper": {"product": a["product"], "brand": a["brand"], "price": _money(a["_low"])},
                "dearer": {"product": b["product"], "brand": b["brand"], "price": _money(b["_low"])},
                "gap": _money(b["_low"] - a["_low"]),
            })
        for f in sorted((f for f in items if f["_was"]), key=lambda f: f["_low"] - f["_was"])[:per_category]:
            savings.append({"category": cat, "product": f["product"], "brand": f["brand"],
                            "price": _money(f["_low"]), "was": _money(f["_was"]),
                            "saving": _money(f["_was"] - f["_low"])})
        for tier in validators.PRICE_TIERS:
            fits = [f for f in items if f["_high"] <= tier * margin]
            if len({f["model"] for f in fits}) >= 2:
                tiers.append({"category": cat, "tier": f"under £{tier}",
                              "products": [f["product"] for f in fits][:6]})
                break
    return {"gap": gaps, "saving": savings, "tier": tiers}


def metrics_note(settings: Settings, history: list[dict]) -> str | None:
    """Why there are no TikTok metrics, or None when there are some."""
    if any(h.get("avg_watch_seconds") is not None for h in history):
        return None
    cfg = settings["report"]
    reasons = []
    if cfg.get("organic_source") != "windsor":
        reasons.append(f"report.organic_source is '{cfg.get('organic_source')}', not 'windsor'")
    if not settings.secret("WINDSOR_API_KEY", required=False):
        reasons.append("WINDSOR_API_KEY isn't set")
    why = "; ".join(reasons) or "no posts with metrics in the last 4 weeks"
    return (f"No performance metrics: TikTok isn't connected ({why}). "
            "This week's mix is the default and isn't learning from results.")


def is_trolley(line: StockLine) -> bool:
    text = f"{line.product_title} {line.product_type}"
    return bool(TROLLEY_RE.search(text)) and not TROLLEY_ACCESSORY_RE.search(line.product_title)


def summarise_pool(pool: list[StockLine], max_products: int) -> list[dict]:
    """Featured products for the prompt, at live website prices, with their SKUs per size."""
    items = []
    for title, lines in by_product(pool).items():
        item = product_facts(lines)
        item["sizes"] = {
            (l.size or "One size"): {"sku": l.sku, "units": l.members_units}
            for l in sorted(lines, key=lambda l: l.size or "")
        }
        item["_units"] = sum(l.members_units for l in lines)
        items.append(item)
    items.sort(key=lambda i: (-i["_units"], i["product"]))
    for i in items:
        del i["_units"]
    return items[:max_products]


def summarise_trolleys(trolleys: list[StockLine]) -> list[dict]:
    return [
        {"product": l.product_title, "sku": l.sku, "units": l.units_total}
        for l in sorted(trolleys, key=lambda l: (-l.units_total, l.product_title))[:15]
    ]


def build_plan(settings: Settings, lines: list[StockLine], history: list[dict], week_start: date) -> Plan:
    pool = allocation.featured_pool(lines)
    stock_by_sku = {l.sku: l for l in lines}
    mix, how = plan_mix(settings, history)
    notes = [f"Mix: {how}."]
    cfg = settings.get("roulette") or {}
    min_pairs = int(cfg.get("min_pairs", 3))
    cutoff = week_start - timedelta(weeks=int(cfg.get("no_repeat_weeks", 3)))
    recent = {
        h["roulette_size"] for h in history
        if h.get("roulette_size") and h["week_start"] >= cutoff and h["format"] == "size_roulette"
    }
    sizes = pick_roulette_sizes(pool, mix.get("size_roulette", 0), recent, min_pairs)
    short = mix.get("size_roulette", 0) - len(sizes)
    if short > 0:
        mix["size_roulette"] -= short
        mix["value_comparison"] = mix.get("value_comparison", 0) + short
        notes.append(
            f"Only {len(sizes)} footwear size(s) have {min_pairs}+ featured pairs and weren't used in the "
            f"last {cfg.get('no_repeat_weeks', 3)} weeks; {short} Size Roulette slot(s) became value comparisons."
        )
    gcfg = settings.get("giveaway") or {}
    entry_days = int(gcfg.get("entry_days", 7))
    if entry_days < 7:
        raise validators.GuardrailError([f"giveaway.entry_days is {entry_days}; entries must run at least 7 days"])
    prize: list[StockLine] = []
    post = close = None
    if mix.get("giveaway", 0):
        reason = None
        if not gcfg.get("terms_url"):
            reason = "no T&Cs link is set (giveaway.terms_url), and every giveaway caption must link to the full T&Cs"
        else:
            prize = pick_giveaway(pool, stock_by_sku, gcfg)
            if not prize:
                reason = (f"no featured product is one-size or has {gcfg.get('min_sizes', 3)}+ sizes in stock "
                          "for the winner to choose from")
        if reason:
            mix["value_comparison"] = mix.get("value_comparison", 0) + mix["giveaway"]
            mix["giveaway"] = 0
            prize = []
            notes.append(f"No giveaway this week: {reason}. The slot became a value comparison.")
        else:
            post = week_start + timedelta(days=int(gcfg.get("post_after_days", 1)))
            close = post + timedelta(days=entry_days)
    trolleys = [l for l in lines if l.units_total > 0 and is_trolley(l)]
    if not pool:
        raise validators.GuardrailError(["The featured pool is empty: no Members-routed lines in stock with an RRP"])
    order = [f for f in FORMATS for _ in range(mix.get(f, 0))]
    return Plan(
        week_start, mix, order, sizes, prize[0] if prize else None, close, pool, stock_by_sku, trolleys, notes,
        giveaway_lines=prize, giveaway_post=post, terms_url=gcfg.get("terms_url"),
        metrics_note=metrics_note(settings, history),
    )


def top_posts_summary(history: list[dict]) -> str:
    rows = [h for h in history if h.get("avg_watch_seconds") is not None]
    if not rows:
        return "No metrics yet."
    rows.sort(key=lambda h: (-float(h["avg_watch_seconds"]), -(h.get("shares") or 0)))
    return "; ".join(
        f"{FORMAT_NAMES.get(h['format'], h['format'])}: \"{h['hook']}\" "
        f"({float(h['avg_watch_seconds']):.1f}s avg watch, {h.get('shares') or 0} shares)"
        for h in rows[:5]
    )


def prompt_values(settings: Settings, plan: Plan, history: list[dict]) -> dict:
    max_products = int((settings.get("llm") or {}).get("max_featured_products", 60))
    prize = plan.giveaway_lines
    if prize and all((l.size or "").strip().lower() in ONE_SIZE for l in prize):
        sizes = "one size"
    elif prize:
        sizes = "the winner chooses their size from " + ", ".join(sorted({l.size for l in prize if l.size}))
    else:
        sizes = "n/a"
    return {
        "N_SCRIPTS": len(plan.order),
        "WEEK_START": plan.week_start.strftime("%A %-d %B %Y"),
        "FORMAT_MIX": ", ".join(f"{n} × {FORMAT_NAMES[f]}" for f, n in plan.mix.items() if n),
        "STOCK_SUMMARY_JSON": json.dumps(summarise_pool(plan.pool, max_products), ensure_ascii=False),
        "PRICE_IDEAS_JSON": json.dumps(price_ideas(plan.pool), ensure_ascii=False),
        "TROLLEY_STOCK_JSON": json.dumps(summarise_trolleys(plan.trolleys), ensure_ascii=False),
        "ROULETTE_SIZES": ", ".join(f"{s} ({p} pairs)" for s, p in plan.roulette_sizes) or "none this week",
        "GIVEAWAY_PRODUCT": plan.giveaway.product_title if plan.giveaway else "none this week",
        "GIVEAWAY_SKUS": ", ".join(l.sku for l in prize) or "n/a",
        "GIVEAWAY_SIZES": sizes,
        "GIVEAWAY_POST_DATE": plan.giveaway_post.strftime("%A %-d %B %Y") if plan.giveaway_post else "n/a",
        "GIVEAWAY_CLOSE_DATE": plan.giveaway_close.strftime("%-d %B %Y") if plan.giveaway_close else "n/a",
        "TERMS_URL": plan.terms_url or "n/a",
        "TOP_POSTS_SUMMARY": top_posts_summary(history),
    }


# -------------------------------------------------------------- validation


def _mentions_size(text: str, label: str) -> bool:
    number = re.escape(label[2:])
    return bool(re.search(rf"\b(?:UK\s?|size\s?){number}\b", text, re.IGNORECASE))


TIKTOK_DISCLAIMER_RE = re.compile(
    r"not\s+(?:sponsored|endorsed|administered|run|associated)[^.]{0,60}\bTikTok\b", re.IGNORECASE
)
CHOOSE_SIZE_RE = re.compile(r"\b(?:choose|pick)\s+(?:your|their|a|the)\s+(?:own\s+)?size\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9£%]+", " ", (text or "").lower()).strip()


def length_seconds(s: dict) -> int:
    return sum(int(shot.get("seconds") or 0) for shot in s["shot_list"])


def outline_check(s: dict) -> list[str]:
    """The outline must leave nothing to guess: timed shots that open on the hook,
    with every on-screen line placed on a shot and nothing else on screen."""
    e: list[str] = []
    shots, screen = s["shot_list"], s["on_screen_text"]
    if not 3 <= len(shots) <= 6:
        e.append("shot_list needs 3–6 shots")
    if not s.get("setup", "").strip():
        e.append("setup must say where to film, the background and who is on camera")
    if not shots:
        return e
    if any(int(shot["seconds"]) < 1 for shot in shots):
        e.append("every shot needs a length of at least 1 second")
    if any(not shot["film"].strip() for shot in shots):
        e.append("every shot must say what to film")
    total = length_seconds(s)
    if not 15 <= total <= 35:
        e.append(f"shots add up to {total}s; the video must be 15–35s")
    if int(shots[0]["seconds"]) > 3 or _norm(s["hook"]) not in _norm(shots[0]["say"]):
        e.append("shot 1 must be 3 seconds or less and say the hook word for word")
    placed = {_norm(shot["on_screen"]) for shot in shots if shot["on_screen"].strip()}
    lines = {_norm(t) for t in screen}
    unplaced = [t for t in screen if _norm(t) not in placed]
    if unplaced:
        e.append(f"put each on-screen line on a shot; not placed: {'; '.join(unplaced)}")
    extra = [shot["on_screen"] for shot in shots if shot["on_screen"].strip() and _norm(shot["on_screen"]) not in lines]
    if extra:
        e.append(f"shots may only show lines from on_screen_text; also shown: {'; '.join(extra)}")
    return e


def featured_lines(s: dict, plan: Plan) -> list[StockLine]:
    return [plan.stock_by_sku[k] for k in s["featured_skus"] if k in plan.stock_by_sku]


def check_script(s: dict, plan: Plan, settings: Settings, roulette_size: tuple[str, int] | None) -> list[str]:
    """Everything that's wrong with one script; empty means it can ship."""
    fmt = s.get("format")
    caption, screen = s["caption"], s["on_screen_text"]
    public = "\n".join([caption, " ".join(s["hashtags"]), *screen])
    shots = s["shot_list"]
    said = [shot["say"] for shot in shots if shot["say"].strip()]
    claims = "\n".join([s["hook"], *said, *screen, caption, s["cta"]])  # everything said or shown
    everything = "\n".join([public, s["hook"], s["cta"], s["setup"], *(f"{x['film']}\n{x['say']}" for x in shots)])
    lines = featured_lines(s, plan)
    featured = {l.sku for l in plan.pool}
    e: list[str] = []
    for text in [caption + " " + " ".join(s["hashtags"]), *screen, " ".join(screen)]:
        e += validators.brand_price_check(text, settings.restricted_brands, settings.brand_price_window)
    e += validators.price_claim_check(claims, lines)
    e += validators.member_discount_check(claims, lines)
    e += validators.rrp_check(public, s["featured_skus"], plan.stock_by_sku)
    e += validators.stock_check(s["featured_skus"], plan.stock_by_sku)
    e += validators.real_footage_check(everything)
    # Structure
    if len(s["hook"].split()) >= 12:
        e.append("hook must be under 12 words")
    e += outline_check(s)
    if not 1 <= len(screen) <= 3:
        e.append("on_screen_text needs 1–3 lines")
    if fmt != "giveaway" and len(caption) > 150:
        e.append(f"caption is {len(caption)} characters; max 150")
    if not 3 <= len(s["hashtags"]) <= 5 or not all(h.startswith("#") for h in s["hashtags"]):
        e.append("hashtags need 3–5 tags, each starting with #")
    # Format rules
    if fmt in CLEARANCE_FORMATS:
        if not s["featured_skus"]:
            e.append("name the featured SKUs")
        outside = [k for k in s["featured_skus"] if k in plan.stock_by_sku and k not in featured]
        if outside:
            e.append(f"SKUs not in the featured pool (buyable online, Members deal): {', '.join(outside)}")
    if fmt == "value_comparison":
        models = {base_model(l.product_title) for l in lines}
        brands = {l.vendor.strip().lower() for l in lines if l.vendor}
        prices = {l.price.quantize(Decimal("1")) for l in lines}
        if len(models) < 2:
            e.append("a value comparison needs at least 2 different products, not colourways of one model")
        elif len(brands) < 2 and len(prices) < 2:
            e.append("a value comparison needs at least 2 brands or 2 different price points")
    if fmt == "trolley":
        e += validators.no_price_check("\n".join([public, s["hook"]]))
    if fmt == "size_roulette":
        if roulette_size is None:
            e.append("no Size Roulette size was planned for this script")
        else:
            size, pairs = roulette_size
            wrong = [
                k for k in s["featured_skus"]
                if k in plan.stock_by_sku and (
                    plan.stock_by_sku[k].category != allocation.FOOTWEAR
                    or size_label(plan.stock_by_sku[k].size) != size
                )
            ]
            if wrong:
                e.append(f"Size Roulette {size} features SKUs that aren't {size} footwear: {', '.join(wrong)}")
            if not _mentions_size(public, size):
                e.append(f"say the size ({size}) on screen or in the caption")
            if not re.search(r"members[- ]only", public, re.IGNORECASE):
                e.append('say "members only" on screen or in the caption')
            if pairs < int((settings.get("roulette") or {}).get("min_pairs", 3)):
                e.append(f"{size} has only {pairs} pairs")
    if fmt == "giveaway":
        e += validators.giveaway_check(caption, terms_url=plan.terms_url)
        prize_skus = {l.sku for l in plan.giveaway_lines}
        if plan.giveaway and not prize_skus & set(s["featured_skus"]):
            e.append(f"giveaway must feature the prize {plan.giveaway.product_title}")
        if plan.giveaway_close and plan.giveaway_close.strftime("%-d %B") not in caption:
            e.append(f"giveaway caption must give the closing date {plan.giveaway_close:%-d %B %Y}")
        if plan.terms_url and plan.terms_url not in caption:
            e.append(f"giveaway caption must link to the full T&Cs ({plan.terms_url})")
        if not TIKTOK_DISCLAIMER_RE.search(caption):
            e.append("giveaway caption must say the promotion isn't sponsored, endorsed or run by TikTok")
        sized = any((l.size or "").strip().lower() not in ONE_SIZE for l in plan.giveaway_lines)
        if sized and not CHOOSE_SIZE_RE.search(caption):
            e.append("the prize comes in sizes: the caption must say the winner chooses their size")
        if plan.giveaway_post and plan.giveaway_close and (plan.giveaway_close - plan.giveaway_post).days < 7:
            e.append("giveaway entry window is under 7 days from the posting date")
    return e


def validate_each(scripts: list[dict], plan: Plan, settings: Settings) -> tuple[list[str], dict[int, list[str]]]:
    """Pack-level problems, and each script's own problems (by index)."""
    pack: list[str] = []
    got, want = Counter(s.get("format") for s in scripts), Counter(plan.order)
    if len(scripts) != len(plan.order) or got != want:
        pack.append(f"Expected {len(plan.order)} scripts in mix {dict(want)}, got {len(scripts)}: {dict(got)}")
    roulette = iter(plan.roulette_sizes)
    per: dict[int, list[str]] = {}
    for i, s in enumerate(scripts):
        size = next(roulette, None) if s.get("format") == "size_roulette" else None
        s["roulette_size"] = size[0] if size else None
        s["price_hook"] = validators.price_hook_type(
            "\n".join([s["hook"], *s["on_screen_text"], s["caption"]]), featured_lines(s, plan)
        )
        errors = check_script(s, plan, settings, size)
        if errors:
            per[i] = errors
    vc_hooks = [s["price_hook"] for s in scripts if s.get("format") == "value_comparison"]
    if len(vc_hooks) >= 2 and len(set(vc_hooks)) == 1:
        pack.append(f"all {len(vc_hooks)} value comparisons use the same kind of hook ({vc_hooks[0]}); "
                    "rotate between a price gap, a saving and a fixed price tier")
    return pack, per


def validate_scripts(scripts: list[dict], plan: Plan, settings: Settings) -> list[str]:
    pack, per = validate_each(scripts, plan, settings)
    out = list(pack)
    for i, errors in sorted(per.items()):
        s = scripts[i]
        out += [f"Script {i + 1} ({FORMAT_NAMES.get(s.get('format'), s.get('format'))}): {m}" for m in errors]
    return out


def generate(llm: JSONModel, settings: Settings, plan: Plan, history: list[dict]) -> tuple[list[dict], list[dict]]:
    """Ask Claude for the scripts and retry once with the errors.

    Scripts still failing after the retry are dropped, never shipped: returns
    (kept, dropped), each dropped script carrying its ``errors``.
    """
    prompt = load_prompt("script_generator", prompt_values(settings, plan, history))
    messages: list[dict] = [{"role": "user", "content": prompt.user}]
    for attempt in (1, 2):
        data, content = llm.complete_json(prompt.system, messages, SCRIPT_SCHEMA, max_tokens=16000)
        scripts = data["scripts"]
        for s in scripts:
            s["est_length_seconds"] = length_seconds(s)
        errors = validate_scripts(scripts, plan, settings)
        if not errors:
            log.info("Scripts passed validation on attempt %d", attempt)
            return scripts, []
        log.warning("Attempt %d failed validation:\n- %s", attempt, "\n- ".join(errors))
        if attempt == 1:
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "These scripts failed our checks:\n- " + "\n- ".join(errors)
                    + f"\n\nReturn all {len(plan.order)} scripts again with every problem fixed.",
                },
            ]
    pack, per = validate_each(scripts, plan, settings)
    kept = [s for i, s in enumerate(scripts) if i not in per]
    dropped = [s | {"errors": per[i]} for i, s in enumerate(scripts) if i in per]
    for s in dropped:
        log.error("Dropped a %s script (%r) after 2 attempts: %s",
                  FORMAT_NAMES.get(s["format"], s["format"]), s["hook"], "; ".join(s["errors"]))
    if not kept:
        raise validators.GuardrailError(errors)
    return kept, dropped


def price_check_rows(scripts: list[dict], plan: Plan) -> list[dict]:
    """One row per featured product per script: its live price and whether the
    script's price claims passed."""
    rows = []
    for n, s in enumerate(scripts, 1):
        failed = [m for m in s.get("errors", []) if "£" in m or "%" in m]
        result = "FAIL: " + "; ".join(failed) if failed else "pass"
        lines = featured_lines(s, plan) or [None]
        for line in lines:
            rows.append({
                "script": n, "format": FORMAT_NAMES.get(s["format"], s["format"]), "hook": s["hook"],
                "product": f"{line.product_title} {line.size or ''}".strip() if line else "(none)",
                "sku": line.sku if line else "",
                "live_price": _money(line.price) if line else "",
                "price_check": result,
                "kept": "errors" not in s,
            })
    return rows


# ------------------------------------------------------------------- output


def history_rows(db, week_start: date) -> list[dict]:
    if db is None:
        return []
    since = week_start - timedelta(weeks=4)
    cols = ["week_start", "format", "hook", "roulette_size", "avg_watch_seconds", "shares"]
    rows = db.query(
        f"SELECT {', '.join(cols)} FROM content_log WHERE week_start >= ? AND week_start < ?",
        (since.isoformat(), week_start.isoformat()),
    )
    out = []
    for r in rows:
        h = dict(zip(cols, r))
        ws = h["week_start"]
        h["week_start"] = ws if isinstance(ws, date) else date.fromisoformat(str(ws))
        out.append(h)
    return out


def save_content_log(db, plan: Plan, scripts: list[dict], settings: Settings) -> None:
    from . import production

    created = datetime.now(timezone.utc).isoformat()
    c = production.cfg(settings)
    db.execute("DELETE FROM content_log WHERE week_start = ? AND status = 'planned'", (plan.week_start.isoformat(),))
    for n, (s, when) in enumerate(zip(scripts, production.schedule(plan, scripts, settings)), 1):
        size = s.get("roulette_size")
        db.execute(
            "INSERT INTO content_log (week_start, script_no, format, hook, on_screen_text, caption, hashtags, "
            "featured_skus, roulette_size, status, setup, shots, film_by, post_on, filmer, editor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (week_start, script_no) DO NOTHING",
            (
                plan.week_start.isoformat(), n, s["format"], s["hook"], "\n".join(s["on_screen_text"]),
                s["caption"], " ".join(s["hashtags"]), json.dumps(s["featured_skus"]), size,
                s["setup"], production.outline_json(s), when["film_by"].isoformat(), when["post_on"].isoformat(),
                c.get("filmer") or None, c.get("editor") or None, created,
            ),
        )
    db.commit()


def body(ctx: JobContext) -> None:
    from . import pack, production, slack

    settings = ctx.settings
    tz = ZoneInfo(settings["timezone"])
    week_start = (
        date.fromisoformat(ctx.args.week_start) if ctx.args.week_start else week_start_for(datetime.now(tz).date())
    )
    lines, stats = stock.build_snapshot(stock.load_records(ctx), settings)
    apply_member_prices(lines, MemberPriceSource(settings), stats)
    history = history_rows(ctx.read_db(), week_start)
    plan = build_plan(settings, lines, history, week_start)
    if plan.metrics_note:
        log.error(plan.metrics_note)
    ctx.summary.update(
        week_start=week_start.isoformat(),
        mix=plan.mix,
        roulette_sizes=plan.roulette_sizes,
        giveaway=plan.giveaway.product_title if plan.giveaway else None,
        featured_lines=len(plan.pool),
        notes=plan.notes,
        metrics="missing" if plan.metrics_note else "ok",
    )
    scripts, dropped = generate(ctx.llm or ClaudeJSON(settings, "scripts"), settings, plan, history)
    rows = price_check_rows(scripts + dropped, plan)
    for r in rows:
        log.info("PRICE CHECK | script %s | %s | %s | %s | %s | %s",
                 r["script"], r["hook"], r["product"], r["live_price"], r["price_check"],
                 "kept" if r["kept"] else "DROPPED")
    out_dir = ROOT / settings["outputs"].get("pack_dir", "out/packs") / week_start.isoformat()
    md_path, pdf_path = pack.write_pack(out_dir, plan, scripts, dropped, rows, settings)
    ctx.summary.update(pack_markdown=str(md_path), pack_pdf=str(pdf_path), scripts=len(scripts),
                       dropped=[{"hook": s["hook"], "errors": s["errors"]} for s in dropped])
    db = ctx.db()
    if db is None:
        log.info("Dry run: pack written to %s; nothing published or logged to content_log", out_dir)
        return
    save_content_log(db, plan, scripts, settings)
    try:
        ctx.summary["production_rows"] = production.publish_week(settings, plan, scripts)
    except Exception as exc:  # the pack still publishes; say loudly that the tracker didn't update
        log.error("Production tab not updated: %s", exc)
        ctx.summary["production_rows"] = f"failed: {exc}"
    title = pack.title(plan)
    ctx.summary["published"] = publish(
        settings, md_path.read_text(encoding="utf-8"), slack.pack_summary(plan), [md_path, pdf_path],
        replace_heading=title,
    )


def add_args(parser) -> None:
    parser.add_argument("--from-jsonl", help="read a saved Shopify bulk export instead of calling Shopify")
    parser.add_argument("--week-start", help="Monday the pack is for (YYYY-MM-DD); default this/next Monday")


def main(argv=None, llm: JSONModel | None = None, settings: Settings | None = None) -> int:
    def _body(ctx: JobContext) -> None:
        ctx.llm = llm
        body(ctx)

    return run_job("scripts", _body, argv, add_args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
