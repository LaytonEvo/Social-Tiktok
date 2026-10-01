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
from pathlib import Path
from zoneinfo import ZoneInfo

from . import allocation, stock, validators
from .config import ROOT, Settings
from .llm import ClaudeJSON, JSONModel, load_prompt
from .member_prices import MemberPriceSource, apply_member_prices
from .models import StockLine
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
                    "shot_list": {"type": "array", "items": {"type": "string"}},
                    "on_screen_text": {"type": "array", "items": {"type": "string"}},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "featured_skus": {"type": "array", "items": {"type": "string"}},
                    "cta": {"type": "string"},
                    "est_length_seconds": {"type": "integer"},
                },
                "required": [
                    "format", "hook", "shot_list", "on_screen_text", "caption",
                    "hashtags", "featured_skus", "cta", "est_length_seconds",
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
    giveaway: StockLine | None
    giveaway_close: date | None
    pool: list[StockLine]
    stock_by_sku: dict[str, StockLine]
    trolleys: list[StockLine]
    notes: list[str] = field(default_factory=list)


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


def pick_giveaway(pool: list[StockLine], stock_by_sku: dict[str, StockLine], pinned: str | None) -> StockLine | None:
    if pinned:
        line = stock_by_sku.get(pinned)
        if line is None or line.units_total <= 0:
            raise validators.GuardrailError([f"Pinned giveaway SKU {pinned} is not in stock"])
        return line
    candidates = [l for l in pool if l.members_units > 0]
    return max(candidates, key=lambda l: (l.compare_at_price or 0, l.members_units, l.sku), default=None)


def is_trolley(line: StockLine) -> bool:
    text = f"{line.product_title} {line.product_type}"
    return bool(TROLLEY_RE.search(text)) and not TROLLEY_ACCESSORY_RE.search(line.product_title)


def price_band(price) -> str:
    return f"under £{math.ceil((float(price) + 0.01) / 10) * 10}"


def summarise_pool(pool: list[StockLine], max_products: int) -> list[dict]:
    """Featured lines grouped by product for the prompt. No RRPs or discounts:
    the model only gets member price bands."""
    groups: dict[str, list[StockLine]] = defaultdict(list)
    for line in pool:
        groups[line.product_title].append(line)
    items = []
    for title, lines in groups.items():
        items.append(
            {
                "product": title,
                "category": lines[0].category,
                "member_price_band": price_band(max(l.selling_price for l in lines)),
                "sizes": {
                    (l.size or "One size"): {"sku": l.sku, "units": l.members_units}
                    for l in sorted(lines, key=lambda l: l.size or "")
                },
                "_units": sum(l.members_units for l in lines),
            }
        )
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
    giveaway = None
    close = None
    if mix.get("giveaway", 0):
        giveaway = pick_giveaway(pool, stock_by_sku, (settings.get("giveaway") or {}).get("sku"))
        if giveaway is None:
            mix["value_comparison"] = mix.get("value_comparison", 0) + mix["giveaway"]
            mix["giveaway"] = 0
            notes.append("No featured line available as a giveaway prize; slot became a value comparison.")
        else:
            close = week_start + timedelta(days=int((settings.get("giveaway") or {}).get("closes_after_days", 6)))
    trolleys = [l for l in lines if l.units_total > 0 and is_trolley(l)]
    if not pool:
        raise validators.GuardrailError(["The featured pool is empty: no Members-routed lines in stock with an RRP"])
    order = [f for f in FORMATS for _ in range(mix.get(f, 0))]
    return Plan(week_start, mix, order, sizes, giveaway, close, pool, stock_by_sku, trolleys, notes)


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
    return {
        "N_SCRIPTS": len(plan.order),
        "WEEK_START": plan.week_start.strftime("%A %-d %B %Y"),
        "FORMAT_MIX": ", ".join(f"{n} × {FORMAT_NAMES[f]}" for f, n in plan.mix.items() if n),
        "STOCK_SUMMARY_JSON": json.dumps(summarise_pool(plan.pool, max_products), ensure_ascii=False),
        "TROLLEY_STOCK_JSON": json.dumps(summarise_trolleys(plan.trolleys), ensure_ascii=False),
        "ROULETTE_SIZES": ", ".join(f"{s} ({p} pairs)" for s, p in plan.roulette_sizes) or "none this week",
        "GIVEAWAY_PRODUCT": plan.giveaway.product_title if plan.giveaway else "none this week",
        "GIVEAWAY_SKU": plan.giveaway.sku if plan.giveaway else "n/a",
        "GIVEAWAY_CLOSE_DATE": plan.giveaway_close.strftime("%-d %B %Y") if plan.giveaway_close else "n/a",
        "PRICE_CLAIM": settings["price_claims"]["default_claim"],
        "TOP_POSTS_SUMMARY": top_posts_summary(history),
    }


# -------------------------------------------------------------- validation


def _mentions_size(text: str, label: str) -> bool:
    number = re.escape(label[2:])
    return bool(re.search(rf"\b(?:UK\s?|size\s?){number}\b", text, re.IGNORECASE))


def validate_scripts(scripts: list[dict], plan: Plan, settings: Settings) -> list[str]:
    errors: list[str] = []
    got = Counter(s.get("format") for s in scripts)
    want = Counter(plan.order)
    if len(scripts) != len(plan.order) or got != want:
        errors.append(f"Expected {len(plan.order)} scripts in mix {dict(want)}, got {len(scripts)}: {dict(got)}")
    brands = settings.restricted_brands
    window = settings.brand_price_window
    featured = {l.sku for l in plan.pool}
    roulette = iter(plan.roulette_sizes)
    for i, s in enumerate(scripts, 1):
        fmt = s.get("format")
        tag = f"Script {i} ({FORMAT_NAMES.get(fmt, fmt)})"
        caption = s["caption"]
        screen = s["on_screen_text"]
        public = "\n".join([caption, " ".join(s["hashtags"]), *screen])
        everything = "\n".join([public, s["hook"], s["cta"], *s["shot_list"]])
        e: list[str] = []
        for text in [caption + " " + " ".join(s["hashtags"]), *screen, " ".join(screen)]:
            e += validators.brand_price_check(text, brands, window)
        e += validators.up_to_claim_check(public, plan.pool, settings.up_to_threshold)
        e += validators.rrp_check(public, s["featured_skus"], plan.stock_by_sku)
        e += validators.stock_check(s["featured_skus"], plan.stock_by_sku)
        e += validators.real_footage_check(everything)
        # Structure
        if len(s["hook"].split()) >= 12:
            e.append("hook must be under 12 words")
        if not 3 <= len(s["shot_list"]) <= 6:
            e.append("shot_list needs 3–6 shots")
        if not 1 <= len(screen) <= 3:
            e.append("on_screen_text needs 1–3 lines")
        if fmt != "giveaway" and len(caption) > 150:
            e.append(f"caption is {len(caption)} characters; max 150")
        if not 3 <= len(s["hashtags"]) <= 5 or not all(h.startswith("#") for h in s["hashtags"]):
            e.append("hashtags need 3–5 tags, each starting with #")
        if not 15 <= int(s["est_length_seconds"]) <= 35:
            e.append("est_length_seconds must be 15–35")
        # Format rules
        if fmt in CLEARANCE_FORMATS:
            if not s["featured_skus"]:
                e.append("name the featured SKUs")
            outside = [k for k in s["featured_skus"] if k in plan.stock_by_sku and k not in featured]
            if outside:
                e.append(f"SKUs not in the Members featured pool: {', '.join(outside)}")
        if fmt == "trolley":
            e += validators.no_price_check("\n".join([public, s["hook"]]))
        if fmt == "size_roulette":
            size, pairs = next(roulette, (None, 0))
            if size is None:
                e.append("no Size Roulette size was planned for this script")
            else:
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
            e += validators.giveaway_check(caption)
            if plan.giveaway and plan.giveaway.sku not in s["featured_skus"]:
                e.append(f"giveaway must feature the prize SKU {plan.giveaway.sku}")
            if plan.giveaway_close and plan.giveaway_close.strftime("%-d %B") not in caption:
                e.append(f"giveaway caption must give the closing date {plan.giveaway_close:%-d %B %Y}")
        errors += [f"{tag}: {msg}" for msg in e]
    return errors


def generate(llm: JSONModel, settings: Settings, plan: Plan, history: list[dict]) -> list[dict]:
    """Ask Claude for the scripts; retry once with the validator errors, then fail."""
    prompt = load_prompt("script_generator", prompt_values(settings, plan, history))
    messages: list[dict] = [{"role": "user", "content": prompt.user}]
    errors: list[str] = []
    for attempt in (1, 2):
        data, content = llm.complete_json(prompt.system, messages, SCRIPT_SCHEMA, max_tokens=16000)
        scripts = data["scripts"]
        errors = validate_scripts(scripts, plan, settings)
        if not errors:
            log.info("Scripts passed validation on attempt %d", attempt)
            return scripts
        log.warning("Attempt %d failed validation:\n- %s", attempt, "\n- ".join(errors))
        messages += [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": "These scripts failed our checks:\n- " + "\n- ".join(errors)
                + f"\n\nReturn all {len(plan.order)} scripts again with every problem fixed.",
            },
        ]
    raise validators.GuardrailError(errors)


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


def save_content_log(db, plan: Plan, scripts: list[dict]) -> None:
    created = datetime.now(timezone.utc).isoformat()
    sizes = iter(plan.roulette_sizes)
    db.execute("DELETE FROM content_log WHERE week_start = ? AND status = 'planned'", (plan.week_start.isoformat(),))
    for n, s in enumerate(scripts, 1):
        size = next(sizes)[0] if s["format"] == "size_roulette" else None
        db.execute(
            "INSERT INTO content_log (week_start, script_no, format, hook, on_screen_text, caption, hashtags, "
            "featured_skus, roulette_size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?) "
            "ON CONFLICT (week_start, script_no) DO NOTHING",
            (
                plan.week_start.isoformat(), n, s["format"], s["hook"], "\n".join(s["on_screen_text"]),
                s["caption"], " ".join(s["hashtags"]), json.dumps(s["featured_skus"]), size, created,
            ),
        )
    db.commit()


def body(ctx: JobContext) -> None:
    from . import pack, slack

    settings = ctx.settings
    tz = ZoneInfo(settings["timezone"])
    week_start = (
        date.fromisoformat(ctx.args.week_start) if ctx.args.week_start else week_start_for(datetime.now(tz).date())
    )
    lines, stats = stock.build_snapshot(stock.load_records(ctx), settings)
    apply_member_prices(lines, MemberPriceSource(settings))
    history = history_rows(ctx.read_db(), week_start)
    plan = build_plan(settings, lines, history, week_start)
    ctx.summary.update(
        week_start=week_start.isoformat(),
        mix=plan.mix,
        roulette_sizes=plan.roulette_sizes,
        giveaway_sku=plan.giveaway.sku if plan.giveaway else None,
        featured_lines=len(plan.pool),
        notes=plan.notes,
    )
    scripts = generate(ctx.llm or ClaudeJSON(settings, "scripts"), settings, plan, history)
    out_dir = ROOT / settings["outputs"].get("pack_dir", "out/packs") / week_start.isoformat()
    md_path, pdf_path = pack.write_pack(out_dir, plan, scripts)
    ctx.summary.update(pack_markdown=str(md_path), pack_pdf=str(pdf_path), scripts=len(scripts))
    db = ctx.db()
    if db is None:
        log.info("Dry run: pack written to %s; nothing posted or logged to content_log", out_dir)
        return
    save_content_log(db, plan, scripts)
    ctx.summary["slack"] = slack.post_pack(settings, plan, [md_path, pdf_path])


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
