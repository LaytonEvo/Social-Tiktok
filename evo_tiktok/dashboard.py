"""Ops dashboard: one set of numbers from the database, shown in a Google Sheet.

    python -m evo_tiktok.dashboard          # refresh now
    python -m evo_tiktok.dashboard --dry-run  # build and log it, write nothing

Every live job run also refreshes it (see runner.py), so the Sheet is never
more than one run out of date.

The data and the look are kept apart so a members portal can take over later:
``build()`` returns plain JSON-ready data, ``save()`` keeps each version in the
``dashboard_snapshot`` table (the portal reads the latest row), and
``sheet_rows()`` is only how the Sheet shows it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import metrics
from .config import Settings
from .runner import JobContext, run_job

log = logging.getLogger(__name__)

JOBS = ("stock", "scripts", "replies", "report")
DAYS = {"MON": "Mondays", "TUE": "Tuesdays", "WED": "Wednesdays", "THU": "Thursdays", "FRI": "Fridays",
        "SAT": "Saturdays", "SUN": "Sundays", "*": "Daily"}
CATEGORY_ORDER = ("Footwear", "Shirts/Polos", "Mid-layers", "Outerwear", "Legwear")


# ------------------------------------------------------------------ data


def _json(value) -> dict:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _when(cron: str | None) -> str:
    """'0 6 * * *' -> 'Daily 06:00 UK'."""
    if not cron:
        return ""
    minute, hour, _, _, dow = cron.split()[:5]
    return f"{DAYS.get(dow.upper(), dow)} {int(hour):02d}:{int(minute):02d} UK"


def _uk(ts, tz: str) -> str:
    if not ts:
        return ""
    when = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(ZoneInfo(tz)).strftime("%a %-d %b %H:%M")


def job_health(db, settings: Settings) -> list[dict]:
    rows = []
    for job in JOBS:
        last = db.query(
            "SELECT finished_at, status, error, summary FROM run_log WHERE job = ? AND dry_run = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (job, False),
        )
        ok = db.query(
            "SELECT finished_at FROM run_log WHERE job = ? AND dry_run = ? AND status = 'ok' "
            "ORDER BY started_at DESC LIMIT 1",
            (job, False),
        )
        finished, status, error, _ = last[0] if last else (None, None, None, None)
        rows.append({
            "job": job,
            "schedule": _when(settings["schedule"].get(job)),
            "last_run": _uk(finished, settings["timezone"]),
            "status": status or "not run yet",
            "last_ok": _uk(ok[0][0], settings["timezone"]) if ok else "",
            "problem": (error or "")[:160] if status == "failed" else "",
        })
    return rows


def stock_numbers(db, settings: Settings) -> dict | None:
    """The latest successful stock run's summary (the job already counts everything)."""
    rows = db.query(
        "SELECT finished_at, summary FROM run_log WHERE job = 'stock' AND status = 'ok' AND dry_run = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (False,),
    )
    if not rows:
        return None
    s = _json(rows[0][1])
    return {
        "as_of": _uk(rows[0][0], settings["timezone"]),
        "lines_in_stock": s.get("lines_in_stock"),
        "units_in_stock": s.get("units_in_stock"),
        "members_routed_lines": s.get("members_routed_lines"),
        "featured_lines": s.get("featured_lines"),
        "featured_units": s.get("featured_units"),
        "featured_by_category": s.get("featured_by_category") or {},
        "featured_footwear_units_by_size": s.get("featured_footwear_units_by_size") or {},
        "members_lines_without_rrp": s.get("routed_but_no_rrp"),
        "variants_without_sku": s.get("no_sku"),
        "member_prices_loaded": s.get("member_prices"),
        "portal": s.get("portal"),
        "portal_variants": s.get("portal_variants"),
        "portal_variants_in_stock": s.get("portal_variants_in_stock"),
        "portal_variants_no_stock": s.get("portal_variants_no_stock"),
    }


def members_numbers(db, settings: Settings, http=None) -> dict | None:
    """Last 7 complete days from the members portal, or the last report's numbers."""
    if settings.secret("MEMBERS_REPORTING_API_KEY", required=False):
        end = datetime.now(ZoneInfo(settings["timezone"])).date() - timedelta(days=1)
        try:
            m = metrics.load_members_api(settings, end - timedelta(days=6), end, http)
            live = metrics.load_customer_metrics(settings, http)
            return {
                "source": "members portal, last 7 days",
                "from": (end - timedelta(days=6)).isoformat(), "to": end.isoformat(),
                "new": m.new_members, "cancelled": m.cancelled, "paid_total": m.paid_at_end,
                "cancel_rate": m.cancel_rate, "paid_now": live.get("paid_now"),
            }
        except metrics.MembersAPIError as exc:
            log.warning("Dashboard: members portal unavailable: %s", exc)
    rows = db.query("SELECT week_start, week_end, feed FROM weekly_report ORDER BY week_end DESC LIMIT 1")
    m = _json(rows[0][2]).get("members") if rows else None
    if not m:
        return None
    return {
        "source": "last weekly report", "from": str(rows[0][0]), "to": str(rows[0][1]),
        "new": m.get("new_members"), "cancelled": m.get("cancelled"), "paid_total": m.get("paid_at_end"),
        "cancel_rate": m.get("cancel_rate"),
    }


def content_numbers(db) -> dict | None:
    latest = db.query("SELECT MAX(week_start) FROM content_log")
    week = latest[0][0] if latest else None
    if not week:
        return None
    rows = db.query(
        "SELECT script_no, format, hook, status, tiktok_post_id, views FROM content_log "
        "WHERE week_start = ? ORDER BY script_no",
        (week,),
    )
    scripts = [
        {"no": r[0], "format": r[1], "hook": r[2] or "", "status": r[3], "posted": bool(r[4]), "views": r[5]}
        for r in rows
    ]
    return {"week_start": str(week), "scripts": scripts,
            "posted": sum(1 for s in scripts if s["posted"]), "planned": len(scripts)}


def reply_numbers(db) -> dict:
    rows = db.query("SELECT status, COUNT(*) FROM reply_queue GROUP BY status")
    counts = {r[0]: r[1] for r in rows}
    check = db.query("SELECT COUNT(*) FROM reply_queue WHERE status = 'drafted' AND needs_human_check = ?", (True,))
    return {"by_status": counts, "waiting": counts.get("drafted", 0), "need_a_closer_look": check[0][0] if check else 0}


def report_numbers(db) -> dict | None:
    rows = db.query(
        "SELECT week_start, week_end, boost_status, boost_post_id, boost_daily_gbp, boost_days FROM weekly_report "
        "ORDER BY week_end DESC LIMIT 1"
    )
    if not rows:
        return None
    r = rows[0]
    return {"week": f"{r[0]} to {r[1]}", "boost_status": r[2], "boost_post": r[3] or "",
            "boost_daily_gbp": None if r[4] is None else str(r[4]), "boost_days": r[5]}


def build(db, settings: Settings, http=None) -> dict:
    return {
        "updated": datetime.now(ZoneInfo(settings["timezone"])).strftime("%a %-d %b %Y %H:%M"),
        "jobs": job_health(db, settings),
        "stock": stock_numbers(db, settings),
        "members": members_numbers(db, settings, http),
        "content": content_numbers(db),
        "replies": reply_numbers(db),
        "report": report_numbers(db),
    }


def save(db, data: dict) -> None:
    db.execute(
        "INSERT INTO dashboard_snapshot (taken_at, data) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), json.dumps(data, default=str)),
    )
    db.commit()


# ------------------------------------------------------------------ sheet


def _n(value) -> str:
    return "–" if value is None else f"{value:,}" if isinstance(value, int) else str(value)


def sheet_rows(data: dict) -> tuple[list[list], list[int]]:
    """The Sheet layout: rows of cells, plus which rows are headings (shown bold)."""
    rows: list[list] = []
    headings: list[int] = []

    def heading(*cells):
        headings.append(len(rows))
        rows.append(list(cells))

    heading("Evo TikTok ops dashboard")
    rows.append([f"Updated {data['updated']} (UK). Refreshes after every job run; don't edit this tab."])
    rows.append([])

    heading("Jobs", "When", "Last run", "Result", "Last OK", "Problem")
    for j in data["jobs"]:
        rows.append([j["job"], j["schedule"], j["last_run"], j["status"], j["last_ok"], j["problem"]])
    rows.append([])

    s = data["stock"]
    heading("Stock", f"as of {s['as_of']}" if s else "no stock run yet")
    if s:
        rows += [
            ["Lines in stock", _n(s["lines_in_stock"])],
            ["Units in stock", _n(s["units_in_stock"])],
            ["Lines for the Members Club", _n(s["members_routed_lines"])],
            ["Featured for filming (lines / units)", f"{_n(s['featured_lines'])} / {_n(s['featured_units'])}"],
        ]
        cats = s["featured_by_category"]
        for cat in [c for c in CATEGORY_ORDER if c in cats] + [c for c in cats if c not in CATEGORY_ORDER]:
            rows.append([f"   {cat}", _n(cats[cat])])
        sizes = s["featured_footwear_units_by_size"]
        if sizes:
            rows.append(["Featured shoe pairs by size", ", ".join(f"{k} {v}" for k, v in sizes.items())])
    rows.append([])

    m = data["members"]
    heading("Members", f"{m['source']} ({m['from']} to {m['to']})" if m else "no numbers yet")
    if m:
        rate = m.get("cancel_rate")
        rows += [
            ["New paid members", _n(m.get("new"))],
            ["Cancelled", _n(m.get("cancelled"))],
            ["Paid members in total", _n(m.get("paid_total"))],
            ["Paid members right now (live)", _n(m.get("paid_now"))],
            ["Weekly cancellation rate", "–" if rate is None else f"{rate:.1%}"],
        ]
    rows.append([])

    c = data["content"]
    heading("This week's scripts", f"week of {c['week_start']}: {c['posted']} of {c['planned']} posted" if c else
            "no filming pack yet")
    if c:
        rows.append(["#", "Format", "Hook", "Status", "Views"])
        for sc in c["scripts"]:
            rows.append([sc["no"], sc["format"], sc["hook"], sc["status"], _n(sc["views"])])
    rows.append([])

    r = data["replies"]
    heading("Replies", f"{r['waiting']} drafts waiting, {r['need_a_closer_look']} need a closer look")
    for status, count in sorted(r["by_status"].items()):
        rows.append([f"   {status}", _n(count)])
    rows.append([])

    rep = data["report"]
    heading("Weekly report", f"week {rep['week']}" if rep else "no report yet")
    if rep:
        boost = rep["boost_status"]
        if rep["boost_post"]:
            boost += f": post {rep['boost_post']}, £{rep['boost_daily_gbp']}/day for {rep['boost_days']} days"
        rows.append(["Boost", boost])
    rows.append([])

    s = data["stock"]
    if s:
        heading("To tidy in Shopify")
        rows += [
            ["Members Club lines with no RRP (can't say 'was £X')", _n(s["members_lines_without_rrp"])],
            ["Variants with no SKU (the jobs can't see them)", _n(s["variants_without_sku"])],
            ["Members portal", s.get("portal") or "–"],
            ["Portal deal variants live", _n(s.get("portal_variants"))],
            ["Portal deals with no stock in Shopify (check before filming)", _n(s.get("portal_variants_no_stock"))],
        ]
    return rows, headings


def sheet_id(settings: Settings) -> str | None:
    return settings.secret("DASHBOARD_SHEET_ID", required=False) or (settings.get("dashboard") or {}).get("sheet_id")


def publish_sheet(settings: Settings, data: dict, sheets=None) -> int:
    from .gsheets import Sheets

    sid = sheet_id(settings)
    tab = (settings.get("dashboard") or {}).get("tab", "Dashboard")
    sheets = sheets or Sheets(settings)
    rows, headings = sheet_rows(data)
    tab_id = sheets.ensure_tab(sid, tab)
    sheets.replace(sid, tab, rows)
    plain = {"repeatCell": {"range": {"sheetId": tab_id}, "cell": {"userEnteredFormat": {"textFormat": {"bold": False}}},
                            "fields": "userEnteredFormat.textFormat.bold"}}
    bold = [
        {"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": i, "endRowIndex": i + 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}}
        for i in headings
    ]
    sheets.batch_update(sid, [plain, *bold])
    return len(rows)


def refresh(settings: Settings, db) -> bool:
    """Rebuild, save and publish. Called after job runs; never raises."""
    if not sheet_id(settings):
        log.warning("Dashboard not refreshed: DASHBOARD_SHEET_ID is empty on this service")
        return False
    try:
        data = build(db, settings)
        save(db, data)
        publish_sheet(settings, data)
        return True
    except Exception as exc:  # the dashboard must never fail a job
        log.warning("Dashboard refresh failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return False


# ------------------------------------------------------------------ CLI


def body(ctx: JobContext) -> None:
    db = ctx.read_db()
    if db is None:
        raise ValueError("DATABASE_URL is needed to build the dashboard")
    data = build(db, ctx.settings)
    ctx.summary["sections"] = [k for k, v in data.items() if v]
    if ctx.dry_run:
        rows, _ = sheet_rows(data)
        log.info("Dry run: dashboard would be\n%s", "\n".join(" | ".join(str(c) for c in r) for r in rows))
        return
    save(db, data)
    if not sheet_id(ctx.settings):
        raise ValueError("Set DASHBOARD_SHEET_ID to the Google Sheet for the dashboard")
    ctx.summary["rows"] = publish_sheet(ctx.settings, data)


def main(argv=None, settings=None) -> int:
    return run_job("dashboard", body, argv, None, settings)


if __name__ == "__main__":
    raise SystemExit(main())
