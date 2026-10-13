"""Production tracker: the Production tab in the dashboard Sheet.

The scripts job adds one row per video each Monday: the outline to film from
(setup, timed shots with what to say and what goes on screen), the stock to
pull and where it is, the caption, and the days it's due to be filmed, edited
and posted. The team owns the Filmer, Editor, Status, TikTok link and Notes
columns; re-running a pack keeps them.

Status runs To film -> Filmed -> Edited -> Approved -> Posted (or Dropped).
With AI video on, the video job makes each draft and sets Ready to review,
with its Version and Preview link. Layton either sets Approved or writes notes
in Edit request and sets Needs changes, and the next version answers them. An
approval belongs to the version it was given on (``approved_version``).
Every job run copies the tab's statuses, links and edit requests back to
``content_log`` (see ``sync``), so the dashboard, replies and the Friday report
see them. Nothing here posts to TikTok.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from .config import Settings

log = logging.getLogger(__name__)

STATUSES = [
    ("To film", "planned"),
    ("Filmed", "filmed"),
    ("Edited", "edited"),
    ("Ready to review", "review"),
    ("Needs changes", "changes"),
    ("Approved", "approved"),
    ("Posted", "posted"),
    ("Dropped", "dropped"),
]
LABEL_TO_STATUS = {label.lower(): status for label, status in STATUSES}
STATUS_TO_LABEL = {status: label for label, status in STATUSES}

HEADER = [
    "Week", "Video", "Format", "Hook", "Setup", "Shots (film / say / on screen)", "Products to pull",
    "Caption and hashtags", "Film by", "Edit by", "Post on", "Filmer", "Editor", "Status", "TikTok link", "Notes",
    "Version", "Preview", "Edit request",
]
COL = {name: i for i, name in enumerate(HEADER)}
TEAM_COLUMNS = ["Filmer", "Editor", "Status", "TikTok link", "Notes", "Edit request"]  # the team's; never overwritten
JOB_COLUMNS = ["Version", "Preview"]  # set by the video job; a re-run of the pack keeps them for an unchanged video
WIDTHS = [90, 50, 120, 220, 260, 520, 320, 300, 90, 90, 90, 90, 90, 110, 220, 220, 70, 220, 320]
LAST_COL = chr(ord("A") + len(HEADER) - 1)


def cfg(settings: Settings) -> dict:
    return settings.get("production") or {}


def sheet_id(settings: Settings) -> str | None:
    from . import dashboard

    return cfg(settings).get("sheet_id") or dashboard.sheet_id(settings)


def tab(settings: Settings) -> str:
    return cfg(settings).get("tab", "Production")


def _day(d: date | None) -> str:
    return f"{d:%a %-d %b}" if d else ""


# ------------------------------------------------------------------ content


def schedule(plan, scripts: list[dict], settings: Settings) -> list[dict]:
    """Film-by, edit-by and post-on dates per script.

    Everything is filmed in one session and edited the day after; videos then
    post one a day from ``first_post_day``. The giveaway keeps the posting date
    its entry window was worked out from.
    """
    c = cfg(settings)
    film_by = plan.week_start + timedelta(days=int(c.get("film_day", 1)))
    edit_by = plan.week_start + timedelta(days=int(c.get("edit_by_day", 2)))
    first = plan.week_start + timedelta(days=int(c.get("first_post_day", 3)))
    days = [first + timedelta(days=i) for i in range(len(scripts) + 1)]
    giveaway_day = plan.giveaway_post if any(s["format"] == "giveaway" for s in scripts) else None
    free = iter(d for d in days if d != giveaway_day)
    out = []
    for s in scripts:
        post = giveaway_day if s["format"] == "giveaway" and giveaway_day else next(free)
        out.append({"film_by": film_by, "edit_by": edit_by, "post_on": post})
    return out


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def timed_shots(s: dict) -> list[dict]:
    """The script's shots with their start and end times in the edit."""
    out, t = [], 0
    for shot in s["shot_list"]:
        end = t + int(shot["seconds"])
        out.append(shot | {"from": _clock(t), "to": _clock(end)})
        t = end
    return out


def shots_text(s: dict) -> str:
    parts = []
    for n, shot in enumerate(timed_shots(s), 1):
        lines = [f"{n}. {shot['from']}–{shot['to']}  FILM: {shot['film']}"]
        if shot["say"].strip():
            lines.append(f'    SAY: "{shot["say"]}"')
        if shot["on_screen"].strip():
            lines.append(f"    ON SCREEN: {shot['on_screen']}")
        parts.append("\n".join(lines))
    return "\n".join(parts)


def pull_items(s: dict, plan) -> list[dict]:
    """The stock to have in hand for a script: what it is, the size and where it is."""
    items = []
    for sku in s["featured_skus"]:
        line = plan.stock_by_sku.get(sku)
        if line is None:
            continue
        items.append({
            "sku": sku,
            "product": line.product_title,
            "size": line.size or "",
            "where": {loc: q for loc, q in line.units_by_location.items() if q},
            "price": line.price,
            "url": line.url,
        })
    return items


def where_text(where: dict[str, int]) -> str:
    return ", ".join(f"{loc} ×{q}" for loc, q in where.items()) or "no stock"


def pull_text(s: dict, plan) -> str:
    items = pull_items(s, plan)
    if not items:
        return "Nothing to pull: see Setup."
    return "\n".join(
        f"• {i['product']}" + (f", {i['size']}" if i["size"] else "") + f" ({where_text(i['where'])}) SKU {i['sku']}"
        for i in items
    )


def week_rows(plan, scripts: list[dict], settings: Settings) -> list[list]:
    from .scripts import FORMAT_NAMES

    c = cfg(settings)
    rows = []
    for n, (s, when) in enumerate(zip(scripts, schedule(plan, scripts, settings)), 1):
        rows.append([
            plan.week_start.isoformat(), n, FORMAT_NAMES.get(s["format"], s["format"]), s["hook"], s["setup"],
            shots_text(s), pull_text(s, plan), f"{s['caption']}\n\n{' '.join(s['hashtags'])}",
            _day(when["film_by"]), _day(when["edit_by"]), _day(when["post_on"]),
            c.get("filmer") or "", c.get("editor") or "", "To film", "", "", "", "", "",
        ])
    return rows


# ------------------------------------------------------------------- sheet


def _cell(row: list, name: str) -> str:
    i = COL[name]
    return str(row[i]).strip() if i < len(row) and row[i] is not None else ""


def _key(row: list) -> tuple[str, str]:
    return _cell(row, "Week"), _cell(row, "Video")


def merge(existing: list[list], new: list[list], week: str) -> list[list]:
    """This week's fresh rows, keeping what the team typed; every other week untouched.

    If a video's hook changed (the pack was regenerated), its status and link
    reset: it's a different video. Owners and notes always carry over. A row
    the new pack no longer has is kept if work has started on it.
    """
    from .scripts import _norm

    old = {_key(r): r for r in existing if _cell(r, "Week") and _cell(r, "Video")}
    out = []
    for row in new:
        prev = old.pop(_key(row), None)
        if prev:
            same = _norm(_cell(prev, "Hook")) == _norm(_cell(row, "Hook"))
            for name in TEAM_COLUMNS + JOB_COLUMNS:
                if name in ("Status", "TikTok link", "Version", "Preview", "Edit request") and not same:
                    continue
                if _cell(prev, name):
                    row[COL[name]] = _cell(prev, name)
        out.append(row)
    for key, row in old.items():
        if key[0] == week and _cell(row, "Status").lower() in ("", "to film"):
            continue  # an unstarted video the new pack replaced
        out.append(row)

    def order(r):
        try:
            video = int(_cell(r, "Video"))
        except ValueError:
            video = 999
        return (-date.fromisoformat(_cell(r, "Week")).toordinal() if _valid_week(r) else 0, video)

    return sorted(out, key=order)


def _valid_week(row: list) -> bool:
    try:
        date.fromisoformat(_cell(row, "Week"))
        return True
    except ValueError:
        return False


def _format_requests(tab_id: int, n_rows: int) -> list[dict]:
    status_col = COL["Status"]
    reqs: list[dict] = [
        {"updateSheetProperties": {"properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": tab_id},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP",
                                                       "textFormat": {"bold": False}}},
                        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat.bold)"}},
        {"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"setDataValidation": {
            "range": {"sheetId": tab_id, "startRowIndex": 1, "endRowIndex": max(n_rows, 2),
                      "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": label} for label, _ in STATUSES]},
                     "strict": True, "showCustomUi": True},
        }},
    ]
    reqs += [
        {"updateDimensionProperties": {"range": {"sheetId": tab_id, "dimension": "COLUMNS", "startIndex": i,
                                                 "endIndex": i + 1},
                                       "properties": {"pixelSize": w}, "fields": "pixelSize"}}
        for i, w in enumerate(WIDTHS)
    ]
    return reqs


def read_tab(settings: Settings, sheets) -> list[list]:
    """The tab's data rows (no header), or [] if the tab doesn't exist yet."""
    sid = sheet_id(settings)
    if sheets.tab_id(sid, tab(settings)) is None:
        return []
    rows = sheets.read(sid, f"{tab(settings)}!A:{LAST_COL}")
    return [r for r in rows[1:] if any(str(c).strip() for c in r)]


def publish_week(settings: Settings, plan, scripts: list[dict], sheets=None) -> int:
    """Add or refresh this week's rows in the Production tab. Returns the row count written."""
    from .gsheets import Sheets

    sid = sheet_id(settings)
    if not sid:
        log.warning("Production tab not updated: no Sheet (set DASHBOARD_SHEET_ID or production.sheet_id)")
        return 0
    sheets = sheets or Sheets(settings)
    existing = read_tab(settings, sheets)
    rows = merge(existing, week_rows(plan, scripts, settings), plan.week_start.isoformat())
    tab_id = sheets.ensure_tab(sid, tab(settings))
    sheets.replace(sid, tab(settings), [HEADER, *rows])
    sheets.batch_update(sid, _format_requests(tab_id, len(rows) + 1))
    log.info("Production tab: %d videos for the week of %s (%d rows in all)", len(scripts), plan.week_start, len(rows))
    return len(rows)


def sync(settings: Settings, db, sheets=None) -> int:
    """Copy statuses, owners and TikTok links from the tab into ``content_log``.

    The tab is the record for status. A blank link never clears a post ID we
    already hold. Returns how many videos changed.
    """
    from .gsheets import Sheets
    from .replies import _post_ref

    if not sheet_id(settings):
        return 0
    sheets = sheets or Sheets(settings)
    rows = read_tab(settings, sheets)
    if not rows:
        return 0
    current = {
        (str(r[0]), int(r[1])): r[2:]
        for r in db.query("SELECT week_start, script_no, status, tiktok_post_id, filmer, editor, edit_request, "
                          "approved_version FROM content_log")
    }
    changed = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            key = (date.fromisoformat(_cell(row, "Week")).isoformat(), int(_cell(row, "Video")))
        except ValueError:
            continue
        if key not in current:
            continue
        label = _cell(row, "Status")
        status = LABEL_TO_STATUS.get(label.lower())
        if status is None:
            log.warning("Production tab: week %s video %s has an unknown status %r; left as it was", *key, label)
            status = current[key][0]
        link = _cell(row, "TikTok link")
        post_id = _post_ref(link) if link else ""
        if link and not post_id.isdigit():
            log.warning("Production tab: week %s video %s link %r isn't a TikTok video link; ignored", *key, link)
            post_id = ""
        approved = version_number(_cell(row, "Version")) if status == "approved" else None
        new = (status, post_id or current[key][1], _cell(row, "Filmer") or None, _cell(row, "Editor") or None,
               _cell(row, "Edit request") or None, approved)
        if tuple(current[key]) == new:
            continue
        db.execute(
            "UPDATE content_log SET status = ?, tiktok_post_id = ?, filmer = ?, editor = ?, edit_request = ?, "
            "approved_version = ?, "
            "posted_at = CASE WHEN ? = 'posted' AND posted_at IS NULL THEN ? ELSE posted_at END "
            "WHERE week_start = ? AND script_no = ?",
            (*new, status, now, *key),
        )
        changed += 1
    db.commit()
    if changed:
        log.info("Production tab: %d video(s) updated in content_log", changed)
    return changed


def version_number(text: str) -> int | None:
    """'v3' -> 3."""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


def set_cells(settings: Settings, week: str, script_no: int, values: dict[str, str], sheets=None) -> bool:
    """Write named cells on one video's row. False if there's no Sheet or no such row."""
    from .gsheets import Sheets

    if not sheet_id(settings):
        return False
    sheets = sheets or Sheets(settings)
    sid = sheet_id(settings)
    if sheets.tab_id(sid, tab(settings)) is None:
        return False
    rows = sheets.read(sid, f"{tab(settings)}!A:{LAST_COL}")
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        if _key(row) == (week, str(script_no)):
            for name, value in values.items():
                col = chr(ord("A") + COL[name])
                sheets.update(sid, f"{tab(settings)}!{col}{i}", [[value]])
            return True
    return False


def mark_posted(settings: Settings, week: str, script_no: int, link: str, sheets=None) -> bool:
    """Set a video's row to Posted with its link (used by ``python -m evo_tiktok.posts``)."""
    return set_cells(settings, week, script_no, {"Status": "Posted", "TikTok link": link}, sheets)


def outline_json(s: dict) -> str:
    return json.dumps(timed_shots(s), ensure_ascii=False)
