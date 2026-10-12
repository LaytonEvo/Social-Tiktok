import json
from datetime import date

import pytest

from evo_tiktok import pack, production, scripts, stock
from evo_tiktok.config import Settings
from evo_tiktok.db import Database
from tests.test_scripts import FIXTURE, MONDAY, good_scripts, with_giveaway


class FakeSheets:
    """Holds each tab's values like the Sheets API does (strings back, header in row 1)."""

    def __init__(self):
        self.tabs, self.values, self.requests = {}, {}, []

    def tab_id(self, sheet_id, title):
        return self.tabs.get(title)

    def ensure_tab(self, sheet_id, title):
        return self.tabs.setdefault(title, len(self.tabs) + 7)

    def read(self, sheet_id, range_):
        return [[str(c) for c in row] for row in self.values.get(range_.split("!")[0], [])]

    def replace(self, sheet_id, tab, rows):
        self.values[tab] = [list(r) for r in rows]

    def update(self, sheet_id, range_, rows):
        tab, cells = range_.split("!")
        start = cells.split(":")[0]
        col, row = ord(start[0]) - ord("A"), int(start[1:]) - 1
        for j, value in enumerate(rows[0]):
            self.values[tab][row][col + j] = value

    def batch_update(self, sheet_id, requests):
        self.requests += requests
        return {}

    def column(self, name):
        return [r[production.COL[name]] for r in self.values["Production"][1:]]


@pytest.fixture
def settings(settings):
    return with_giveaway(settings)


@pytest.fixture
def plan(settings):
    records = [json.loads(row) for row in FIXTURE.read_text().splitlines() if row.strip()]
    return scripts.build_plan(settings, stock.build_snapshot(records, settings)[0], [], MONDAY)


@pytest.fixture
def sheet_settings(settings):
    return Settings(data=settings.data, source=settings.source, env={"DASHBOARD_SHEET_ID": "sheet1"})


@pytest.fixture
def pack_scripts(plan):
    return good_scripts(plan)


def test_schedule_films_tuesday_edits_wednesday_and_posts_one_a_day(plan, pack_scripts, settings):
    when = production.schedule(plan, pack_scripts, settings)
    assert {w["film_by"] for w in when} == {date(2026, 10, 6)}
    assert {w["edit_by"] for w in when} == {date(2026, 10, 7)}
    posts = [w["post_on"] for w in when]
    assert len(set(posts)) == 7 and min(posts) == date(2026, 10, 8)
    giveaway = [w for s, w in zip(pack_scripts, when) if s["format"] == "giveaway"][0]
    assert giveaway["post_on"] == plan.giveaway_post  # the entry window was worked out from this day


def test_rows_spell_out_the_outline_and_the_stock_to_pull(plan, pack_scripts, settings):
    rows = production.week_rows(plan, pack_scripts, settings)
    assert len(rows) == 7 and rows[0][:2] == ["2026-10-05", 1]
    shots = rows[0][production.COL["Shots (film / say / on screen)"]]
    assert shots.startswith("1. 0:00–0:03  FILM: ") and 'SAY: "£35 vs £70. Which would you buy?"' in shots
    assert "ON SCREEN: £35 or £70?" in shots
    pull = rows[0][production.COL["Products to pull"]]
    assert "SKU POLO-M" in pull and "Warehouse ×" in pull
    assert rows[0][production.COL["Status"]] == "To film" and rows[0][production.COL["Filmer"]] == "Alex"


def test_rerunning_a_pack_keeps_what_the_team_typed(plan, pack_scripts, sheet_settings):
    sheets = FakeSheets()
    production.publish_week(sheet_settings, plan, pack_scripts, sheets)
    data = sheets.values["Production"]
    assert data[0] == production.HEADER and len(data) == 8
    data[1][production.COL["Status"]] = "Filmed"
    data[1][production.COL["Editor"]] = "Karin"
    data[2][production.COL["Status"]] = "Edited"
    data[3][production.COL["Notes"]] = "Use the black glove"
    # An older week stays put.
    data.append(["2026-09-28", "1", "Trolley", "Old hook", "", "", "", "", "", "", "", "", "", "Posted", "", ""])

    rerun = good_scripts(plan)
    rerun[1]["hook"] = "A brand new hook"  # video 2 was regenerated: a different video
    production.publish_week(sheet_settings, plan, rerun, sheets)
    assert sheets.column("Status")[:3] == ["Filmed", "To film", "To film"]
    assert sheets.column("Editor")[0] == "Karin" and sheets.column("Notes")[2] == "Use the black glove"
    assert sheets.column("Week").count("2026-09-28") == 1 and len(sheets.values["Production"]) == 9
    validation = [r for r in sheets.requests if "setDataValidation" in r][-1]["setDataValidation"]
    assert [v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]][0] == "To film"


def test_sync_copies_status_owners_and_links_into_content_log(plan, pack_scripts, sheet_settings, tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'p.db'}")
    db.migrate()
    scripts.save_content_log(db, plan, pack_scripts, sheet_settings)
    sheets = FakeSheets()
    production.publish_week(sheet_settings, plan, pack_scripts, sheets)
    data = sheets.values["Production"]
    data[1][production.COL["Status"]] = "Posted"
    data[1][production.COL["TikTok link"]] = "https://www.tiktok.com/@evo/video/7412345678901234567"
    data[2][production.COL["Status"]] = "Filmed"
    data[2][production.COL["Editor"]] = "Karin"
    data[3][production.COL["Status"]] = "Nearly there"  # not a status: left alone
    assert production.sync(sheet_settings, db, sheets) == 2
    rows = db.query("SELECT script_no, status, tiktok_post_id, editor, posted_at, film_by, shots FROM content_log "
                    "ORDER BY script_no")
    assert rows[0][1:3] == ("posted", "7412345678901234567") and rows[0][4]
    assert rows[1][1] == "filmed" and rows[1][3] == "Karin"
    assert rows[2][1] == "planned"
    assert rows[0][5] == "2026-10-06" and '"say": "£35 vs £70. Which would you buy?"' in rows[0][6]
    assert production.sync(sheet_settings, db, sheets) == 0  # nothing new
    db.close()


def test_posts_command_marks_the_row_posted(plan, pack_scripts, sheet_settings):
    sheets = FakeSheets()
    production.publish_week(sheet_settings, plan, pack_scripts, sheets)
    link = "https://www.tiktok.com/@evo/video/7412345678901234567"
    assert production.mark_posted(sheet_settings, "2026-10-05", 3, link, sheets)
    assert sheets.column("Status")[2] == "Posted" and sheets.column("TikTok link")[2] == link


def test_pack_has_a_week_pull_list_and_timed_shots(plan, pack_scripts, settings):
    md = pack.render_markdown(plan, pack_scripts, settings=settings)
    assert "## Before you film" in md and "Film everything by **Tuesday 6 October**" in md
    assert "**Pull this stock before you start:**" in md and "**Warehouse**" in md
    assert "**0:00–0:03** Film: Close-up" in md and '**Say:** "£35 vs £70. Which would you buy?"' in md
    assert "posts Thursday 8 October" in md
