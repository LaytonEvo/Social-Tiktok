"""Weekly reporter (Friday): ``python -m evo_tiktok.report [--dry-run]``.

Pulls organic and paid stats, ranks posts and formats against the account
median, works out cost per member, picks at most one boost candidate and writes
a one-page report. The boost line and the numbers table are computed in code;
Claude writes the narrative around them. Ad spend is never changed: the report
only asks Layton to approve.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from . import metrics, validators
from .config import ROOT, Settings
from .llm import ClaudeJSON, JSONModel, LLMError, load_prompt
from .publish import publish
from .runner import JobContext, run_job
from .scripts import FORMAT_NAMES

log = logging.getLogger(__name__)

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "best_and_worst": {"type": "string"},
        "format_ranking": {"type": "string"},
        "pilot_gate": {"type": "string"},
        "film_next_week": {"type": "array", "items": {"type": "string"}},
        "boost_reasoning": {"type": "string"},
    },
    "required": ["headline", "best_and_worst", "format_ranking", "pilot_gate", "film_next_week", "boost_reasoning"],
    "additionalProperties": False,
}


@dataclass
class Post:
    post_id: str
    format: str | None  # None when the video isn't in content_log
    hook: str
    week_start: str | None
    script_no: int | None
    posted_at: date | None
    m: metrics.PostMetrics

    @property
    def label(self) -> str:
        name = FORMAT_NAMES.get(self.format or "", "Unplanned post")
        return f"{name}: \"{self.hook}\"" if self.hook else f"{name} ({self.post_id})"


@dataclass
class Boost:
    post_id: str
    label: str
    daily_gbp: Decimal
    days: int
    watch_vs_median: float
    shares_vs_median: float


@dataclass
class Analysis:
    start: date
    end: date
    posts: list[Post]
    medians: dict
    median_basis: str
    formats: list[dict]
    totals: dict
    paid: dict
    members: metrics.Members | None
    boost: Boost | None
    boost_reason: str
    gate: dict
    notes: list[str] = field(default_factory=list)
    matched: list[Post] = field(default_factory=list)  # every linked video, for saving metrics


# ------------------------------------------------------------------ analysis


def week_window(today: date, days: int) -> tuple[date, date]:
    return today - timedelta(days=days - 1), today


def history_medians(db, start: date, weeks: int) -> tuple[list[float], list[int]]:
    """Avg watch time and shares of posts in the weeks before this report."""
    if db is None:
        return [], []
    since = start - timedelta(weeks=weeks)
    rows = db.query(
        "SELECT avg_watch_seconds, shares FROM content_log WHERE tiktok_post_id IS NOT NULL "
        "AND avg_watch_seconds IS NOT NULL AND posted_at >= ? AND posted_at < ?",
        (since.isoformat(), start.isoformat()),
    )
    return [float(r[0]) for r in rows], [int(r[1] or 0) for r in rows]


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def match_posts(db, organic: list[metrics.PostMetrics]) -> list[Post]:
    """Every video with metrics, matched to its content_log script where linked."""
    planned: dict[str, tuple] = {}
    if db is not None:
        for pid, fmt, hook, ws, n, posted in db.query(
            "SELECT tiktok_post_id, format, hook, week_start, script_no, posted_at FROM content_log "
            "WHERE tiktok_post_id IS NOT NULL"
        ):
            planned[str(pid)] = (fmt, hook, str(ws), n, _as_date(posted))
    posts = []
    for m in organic:
        fmt, hook, ws, n, posted = planned.get(m.post_id, (None, "", None, None, None))
        posts.append(Post(m.post_id, fmt, hook or "", ws, n, posted, m))
    return posts


def analyse(settings: Settings, db, start: date, end: date, organic, paid, members) -> Analysis:
    cfg = settings["report"]
    notes: list[str] = []
    matched = match_posts(db, organic)
    posts = [p for p in matched if p.format is not None and p.posted_at and start <= p.posted_at <= end]
    unplanned = [p for p in matched if p.format is None]
    if unplanned:
        notes.append(
            f"{len(unplanned)} video(s) in the metrics aren't linked to a script, so they're "
            f"{'included' if cfg.get('include_unlinked_posts') else 'left out of the rankings'} "
            "(link with `python -m evo_tiktok.posts`): " + ", ".join(p.post_id for p in unplanned)
        )
        if cfg.get("include_unlinked_posts"):
            posts += unplanned
    hist_watch, hist_shares = history_medians(db, start, int(cfg.get("median_weeks", 4)))
    if len(hist_watch) >= int(cfg.get("min_history_posts", 5)):
        watch_pool, share_pool = hist_watch, hist_shares
        basis = f"last {cfg.get('median_weeks', 4)} weeks ({len(hist_watch)} posts)"
    else:
        watch_pool = [p.m.avg_watch_seconds for p in posts if p.m.avg_watch_seconds is not None]
        share_pool = [p.m.shares for p in posts]
        basis = f"this week only ({len(watch_pool)} posts); not enough history yet"
        notes.append("Medians use this week's posts because there isn't 4 weeks of history yet.")
    medians = {
        "avg_watch_seconds": statistics.median(watch_pool) if watch_pool else None,
        "shares": statistics.median(share_pool) if share_pool else None,
    }

    # Formats
    by_format: dict[str, list[Post]] = {}
    for p in posts:
        by_format.setdefault(p.format or "unplanned", []).append(p)
    formats = []
    for fmt, ps in by_format.items():
        watch = [p.m.avg_watch_seconds for p in ps if p.m.avg_watch_seconds is not None]
        formats.append(
            {
                "format": FORMAT_NAMES.get(fmt, "Unplanned"),
                "posts": len(ps),
                "avg_watch_seconds": round(statistics.mean(watch), 1) if watch else None,
                "avg_shares": round(statistics.mean(p.m.shares for p in ps), 1),
                "views": sum(p.m.views for p in ps),
            }
        )
    formats.sort(key=lambda f: (-(f["avg_watch_seconds"] or 0), -f["avg_shares"]))

    totals = {
        k: sum(getattr(p.m, k) for p in posts)
        for k in ("views", "shares", "comments", "follows", "profile_visits", "link_clicks")
    }
    totals["posts"] = len(posts)

    spend = sum((r.spend for r in paid), Decimal("0"))
    results = sum(r.results for r in paid)
    paid_summary = {
        "spend_gbp": str(spend.quantize(Decimal("0.01"))),
        "impressions": sum(r.impressions for r in paid),
        "results": results,
        "cost_per_result_gbp": str((spend / results).quantize(Decimal("0.01"))) if results else None,
        "cost_per_member_gbp": (
            str((spend / members.new_members).quantize(Decimal("0.01"))) if members and members.new_members else None
        ),
        "ads": [asdict(r) | {"spend": str(r.spend)} for r in paid],
    }
    if members is None:
        notes.append("No members data: set MEMBERS_REPORTING_API_KEY (members portal) or pass --new-members.")

    boost, reason = pick_boost(settings, posts, medians)
    gate = gate_progress(settings, formats, medians, paid_summary, db, end)
    return Analysis(
        start, end, posts, medians, basis, formats, totals, paid_summary, members, boost, reason, gate, notes, matched
    )


def pick_boost(settings: Settings, posts: list[Post], medians: dict) -> tuple[Boost | None, str]:
    """At most one boost: the post that beats the organic median on BOTH average
    watch time and shares by the widest margin. Otherwise no boost."""
    mw, ms = medians["avg_watch_seconds"], medians["shares"]
    if mw is None or ms is None:
        return None, "no organic medians to compare against"
    beats = [
        p for p in posts
        if p.m.avg_watch_seconds is not None and p.m.avg_watch_seconds > mw and p.m.shares > ms
    ]
    if not beats:
        return None, "no post beat the organic median on both watch time and shares"
    best = max(beats, key=lambda p: (p.m.avg_watch_seconds / mw + p.m.shares / max(ms, 1), p.m.views))
    cfg = settings["report"]["boost"]
    return (
        Boost(
            best.post_id, best.label, Decimal(str(cfg["daily_gbp"])), int(cfg["days"]),
            round(best.m.avg_watch_seconds / mw, 2), round(best.m.shares / max(ms, 1), 2),
        ),
        "beat the organic median on both watch time and shares",
    )


def gate_progress(settings: Settings, formats: list[dict], medians: dict, paid: dict, db, end: date) -> dict:
    g = settings["pilot_gate"]
    mw = medians["avg_watch_seconds"]
    best = next((f for f in formats if f["avg_watch_seconds"] is not None and f["format"] != "Unplanned"), None)
    ratio = round(best["avg_watch_seconds"] / mw, 2) if best and mw else None
    met = ratio is not None and ratio >= float(g["format_beats_median_by"])
    streak = (previous_streak(db, end) + 1) if met else 0
    cpm = paid.get("cost_per_member_gbp")
    return {
        "best_format": best["format"] if best else None,
        "best_format_vs_median": ratio,
        "target_vs_median": g["format_beats_median_by"],
        "format_target_met": met,
        "weeks_in_a_row": streak,
        "weeks_needed": g["weeks_in_a_row"],
        "cost_per_member_gbp": cpm,
        "max_cost_per_member_gbp": g["max_cost_per_member_gbp"],
        "cost_target_met": None if cpm is None else Decimal(cpm) <= Decimal(str(g["max_cost_per_member_gbp"])),
        "max_filming_hours_per_week": g["max_filming_hours_per_week"],
    }


def previous_streak(db, end: date) -> int:
    if db is None:
        return 0
    rows = db.query(
        "SELECT gate FROM weekly_report WHERE week_end < ? ORDER BY week_end DESC LIMIT 1", (end.isoformat(),)
    )
    return int(json.loads(rows[0][0]).get("weeks_in_a_row", 0)) if rows else 0


# ------------------------------------------------------------------ writing


def boost_line(a: Analysis) -> str:
    if a.boost is None:
        return f"Boost recommendation: no boost this week ({a.boost_reason})."
    b = a.boost
    return f"Boost recommendation: {b.label}, £{b.daily_gbp:g}/day, {b.days} days — approve?"


def numbers_table(a: Analysis) -> str:
    def fmt(v, suffix=""):
        return "–" if v is None else f"{v}{suffix}"

    lines = [
        f"| Posts | Views | Shares | Comments | Follows | Profile visits | Link clicks |",
        "|---|---|---|---|---|---|---|",
        "| {posts} | {views} | {shares} | {comments} | {follows} | {profile_visits} | {link_clicks} |".format(**a.totals),
        "",
        "| Post | Format | Views | Avg watch | Completion | Shares |",
        "|---|---|---|---|---|---|",
    ]
    for p in sorted(a.posts, key=lambda p: -(p.m.avg_watch_seconds or 0)):
        completion = None if p.m.completion_rate is None else f"{p.m.completion_rate:.0%}"
        lines.append(
            f"| {p.hook or p.post_id} | {FORMAT_NAMES.get(p.format or '', 'Unplanned')} | {p.m.views} | "
            f"{fmt(p.m.avg_watch_seconds and round(p.m.avg_watch_seconds, 1), 's')} | {fmt(completion)} | {p.m.shares} |"
        )
    mw = a.medians["avg_watch_seconds"]
    lines += [
        "",
        f"Organic median ({a.median_basis}): {fmt(mw and round(mw, 1), 's')} average watch, "
        f"{fmt(a.medians['shares'])} shares.",
        f"Paid: £{a.paid['spend_gbp']} spend, {a.paid['impressions']} impressions, {a.paid['results']} results, "
        f"£{fmt(a.paid['cost_per_result_gbp'])} per result.",
    ]
    if a.members:
        lines.append(
            f"Members ({a.members.source}): {members_line(a.members)}. Cost per new member "
            f"(all paid spend ÷ new members): £{fmt(a.paid['cost_per_member_gbp'])}."
        )
    return "\n".join(lines)


def members_facts(m: metrics.Members) -> dict:
    """The members numbers we have, leaving out what this source doesn't give."""
    facts = {
        "new_paid_members": m.new_members,
        "cancelled": m.cancelled,
        "paid_members_at_start": m.paid_at_start,
        "paid_members_at_end": m.paid_at_end,
        "weekly_cancellation_rate": m.cancel_rate,
        "counted_up_to": m.covers_to,
        "member_orders": m.member_orders,
        "member_revenue_gbp": None if m.member_revenue is None else f"{m.member_revenue:.2f}",
    }
    return {k: v for k, v in facts.items() if v is not None}


def members_line(m: metrics.Members) -> str:
    parts = [f"{m.new_members} new"]
    if m.cancelled is not None:
        parts.append(f"{m.cancelled} cancelled")
    if m.paid_at_end is not None:
        parts.append(f"{m.paid_at_end} paid in total")
    if m.cancel_rate is not None:
        parts.append(f"{m.cancel_rate:.1%} weekly cancellation rate")
    if m.member_orders is not None:
        parts.append(f"{m.member_orders} orders")
    if m.member_revenue is not None:
        parts.append(f"£{m.member_revenue:.2f} revenue")
    text = ", ".join(parts)
    if m.covers_to is not None:
        text += f" (to {m.covers_to:%-d %B}; the portal counts complete days only)"
    return text


AMOUNT_PER_DAY_RE = re.compile(r"£\s?\d+(?:\.\d+)?\s?(?:/|per\s)day", re.IGNORECASE)


def validate_narrative(n: dict, a: Analysis) -> list[str]:
    """The narrative must not make its own spend decision."""
    text = " ".join([n["headline"], n["best_and_worst"], n["format_ranking"], n["pilot_gate"],
                     n["boost_reasoning"], *n["film_next_week"]])
    errors = []
    if AMOUNT_PER_DAY_RE.search(text):
        errors.append("don't state a boost budget; the report appends the boost line itself")
    if re.search(r"\bapprove\b", text, re.IGNORECASE):
        errors.append("don't ask for approval; the report appends the boost line itself")
    if a.boost is None and re.search(r"\b(?:recommend|suggest)\w*\b.{0,40}\bboost", text, re.IGNORECASE) and not re.search(
        r"\bno boost\b", text, re.IGNORECASE
    ):
        errors.append("there is no boost candidate this week; don't recommend one")
    if len(n["film_next_week"]) != 3:
        errors.append("give exactly 3 bullets for what to film next week")
    return errors


def prompt_values(settings: Settings, a: Analysis) -> dict:
    posts = [
        {"post": p.label, "format": FORMAT_NAMES.get(p.format or "", "Unplanned"), **asdict(p.m)}
        for p in a.posts
    ]
    return {
        "WEEK_START": a.start.strftime("%-d %B %Y"),
        "WEEK_END": a.end.strftime("%-d %B %Y"),
        "POSTS_METRICS_JSON": json.dumps(posts, default=str),
        "MEDIANS_JSON": json.dumps({**a.medians, "basis": a.median_basis}, default=str),
        "FORMATS_JSON": json.dumps(a.formats),
        "PAID_JSON": json.dumps({k: v for k, v in a.paid.items() if k != "ads"}),
        "MEMBERS_JSON": json.dumps(members_facts(a.members), default=str) if a.members else "unknown",
        "GATE_JSON": json.dumps(a.gate, default=str),
        "BOOST_JSON": json.dumps(
            {"decision": boost_line(a), "reason": a.boost_reason, **(asdict(a.boost) if a.boost else {})}, default=str
        ),
    }


def write_narrative(llm: JSONModel, settings: Settings, a: Analysis) -> dict | None:
    prompt = load_prompt("weekly_report", prompt_values(settings, a))
    messages: list[dict] = [{"role": "user", "content": prompt.user}]
    errors: list[str] = []
    for _ in (1, 2):
        try:
            data, content = llm.complete_json(prompt.system, messages, NARRATIVE_SCHEMA, 8000)
        except LLMError as exc:
            a.notes.append(f"Narrative unavailable: {exc}")
            return None
        errors = validate_narrative(data, a)
        if not errors:
            return data
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Please fix:\n- " + "\n- ".join(errors)},
        ]
    # The numbers and the boost line still go out; the narrative is dropped.
    a.notes.append("Narrative dropped after failing checks: " + "; ".join(errors))
    return None


def render(a: Analysis, narrative: dict | None) -> str:
    out = [f"# TikTok weekly report: {a.start:%-d %b} to {a.end:%-d %b %Y}", ""]
    if narrative:
        out += [
            f"**{narrative['headline']}**", "",
            "## Best and worst", narrative["best_and_worst"], "",
            "## Formats", narrative["format_ranking"], "",
            "## Pilot gate", narrative["pilot_gate"], "",
            "## Film next week", *[f"- {b}" for b in narrative["film_next_week"]], "",
            "## Boost", narrative["boost_reasoning"], "",
        ]
    out += ["## Numbers", numbers_table(a), ""]
    if a.notes:
        out += ["## Notes", *[f"- {n}" for n in a.notes], ""]
    out.append(boost_line(a))
    return "\n".join(out) + "\n"


def slack_text(a: Analysis, narrative: dict | None) -> str:
    """Short Slack message; the full report is attached (Slack doesn't render tables)."""
    lines = [f"*TikTok weekly report, {a.start:%-d %b} to {a.end:%-d %b}*"]
    if narrative:
        lines.append(narrative["headline"])
    t = a.totals
    lines.append(f"{t['posts']} posts, {t['views']:,} views, {t['shares']} shares, {t['follows']} follows.")
    if a.members:
        lines.append(
            f"Members: {members_line(a.members)}. "
            f"Paid £{a.paid['spend_gbp']}; cost per new member £{a.paid['cost_per_member_gbp'] or '–'}."
        )
    lines += [f"_{n}_" for n in a.notes]
    lines.append(f"*{boost_line(a)}*")
    return "\n".join(lines)


def feed(a: Analysis) -> dict:
    """Machine-readable copy for a dashboard."""
    return {
        "week_start": a.start.isoformat(),
        "week_end": a.end.isoformat(),
        "totals": a.totals,
        "medians": a.medians | {"basis": a.median_basis},
        "formats": a.formats,
        "posts": [{"post_id": p.post_id, "format": p.format, "hook": p.hook, **asdict(p.m)} for p in a.posts],
        "paid": a.paid,
        "members": asdict(a.members) if a.members else None,
        "boost": asdict(a.boost) if a.boost else None,
        "boost_line": boost_line(a),
        "gate": a.gate,
    }


# ------------------------------------------------------------------- the job


def save(db, a: Analysis, report_md: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for p in a.matched:
        if p.format is None:
            continue
        m = p.m
        db.execute(
            "UPDATE content_log SET views = ?, avg_watch_seconds = ?, completion_rate = ?, shares = ?, comments = ?, "
            "follows = ?, profile_visits = ?, link_clicks = ?, metrics_updated_at = ? WHERE tiktok_post_id = ?",
            (m.views, m.avg_watch_seconds, m.completion_rate, m.shares, m.comments, m.follows, m.profile_visits,
             m.link_clicks, now, p.post_id),
        )
    db.execute("DELETE FROM weekly_report WHERE week_end = ?", (a.end.isoformat(),))
    db.execute(
        "INSERT INTO weekly_report (week_start, week_end, report_md, feed, gate, boost_post_id, boost_daily_gbp, "
        "boost_days, boost_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            a.start.isoformat(), a.end.isoformat(), report_md, json.dumps(feed(a), default=str),
            json.dumps(a.gate, default=str), a.boost.post_id if a.boost else None,
            str(a.boost.daily_gbp) if a.boost else None, a.boost.days if a.boost else None,
            "awaiting approval" if a.boost else "none", now,
        ),
    )
    db.commit()


def body(ctx: JobContext) -> None:
    settings = ctx.settings
    cfg = settings["report"]
    tz = ZoneInfo(settings["timezone"])
    today = date.fromisoformat(ctx.args.week_end) if ctx.args.week_end else datetime.now(tz).date()
    start, end = week_window(today, int(cfg.get("window_days", 7)))
    organic = metrics.load_organic(settings, ctx.args, start, end)
    paid = metrics.load_paid(settings, ctx.args, start, end)
    members_error = None
    try:
        members = metrics.load_members(settings, ctx.args, start, end)
    except metrics.MembersAPIError as exc:  # the report still goes out, saying why members are missing
        log.error("Members numbers unavailable: %s", exc)
        members, members_error = None, str(exc)
    db = ctx.read_db()
    a = analyse(settings, db, start, end, organic, paid, members)
    if members_error:
        a.notes.append(f"Members numbers unavailable: {members_error}")
    narrative = write_narrative(ctx.llm or ClaudeJSON(settings, "report"), settings, a)
    report_md = render(a, narrative)
    out_dir = ROOT / settings["outputs"].get("report_dir", "out/reports") / end.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, feed_path = out_dir / "weekly_report.md", out_dir / "feed.json"
    md_path.write_text(report_md, encoding="utf-8")
    feed_path.write_text(json.dumps(feed(a), indent=2, default=str), encoding="utf-8")
    ctx.summary.update(
        week=f"{start} to {end}", posts=len(a.posts), boost=boost_line(a), report=str(md_path), notes=a.notes
    )
    write_db = ctx.db()
    if write_db is None:
        log.info("Dry run: report written to %s; nothing saved or published", md_path)
        return
    save(write_db, a, report_md)
    ctx.summary["published"] = publish(settings, report_md, slack_text(a, narrative), [md_path, feed_path])


def add_args(parser) -> None:
    parser.add_argument("--week-end", help="last day of the report window (YYYY-MM-DD); default today")
    parser.add_argument("--organic-csv", help="per-post organic metrics export (TikTok Studio, Metricool, etc.)")
    parser.add_argument("--paid-csv", help="TikTok Ads export, instead of Windsor")
    parser.add_argument("--new-members", type=int, help="new members this week, if not read from Shopify")
    parser.add_argument("--member-orders", type=int)
    parser.add_argument("--member-revenue", type=float)


def main(argv=None, llm: JSONModel | None = None, settings: Settings | None = None) -> int:
    def _body(ctx: JobContext) -> None:
        ctx.llm = llm
        body(ctx)

    return run_job("report", _body, argv, add_args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
