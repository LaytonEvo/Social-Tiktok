from pathlib import Path

import pytest

from evo_tiktok import allocation as a
from evo_tiktok.config import ROOT


@pytest.mark.parametrize(
    "title,expected",
    [
        ("adidas Adipower Spiked Golf Shoes - Black", a.FOOTWEAR),
        ("Under Armour Drive Winterized Waterproof Golf Shoes", a.FOOTWEAR),
        ("Nike Par 5 Shoe Print Dri Fit Mens Golf Polo Shirt", a.SHIRTS),
        ("Mizuno 2026 City Wind Golf Shoe - Surf the Web / Swim Cap / White", a.FOOTWEAR),
        ("adidas 1/2 Zip Ultimate365 Arctic Jacket", a.OUTERWEAR),
        ("JRB Womens 1/4 Zip Golf Top", a.MID_LAYERS),
        ("Under Armour Drive Pro Waterproof Golf Trousers", a.OUTERWEAR),
        ("adidas Ultimate365 Tapered Golf Trousers", a.LEGWEAR),
        ("Srixon All Weather Ball Marker Golf Glove (3 Pack Bundle)", a.GLOVES),
        ("Mizuno BR-DRI Waterproof Golf Stand Bag - Black", a.NON_APPAREL),
        ("Nike Peak Beanie", a.HEADWEAR),
        ("adidas 3 Pack Crew Golf Socks", a.OTHER_APPAREL),
    ],
)
def test_categorise_titles(title, expected):
    assert a.categorise(title) == expected


def test_product_type_used_when_title_has_no_keyword():
    assert a.categorise("TRUE OG3 Pro - Black", "Golf > Footwear > Mens") == a.FOOTWEAR
    assert a.categorise("Mystery item", "") == a.OTHER_APPAREL


def test_season_and_age(settings):
    seasons = settings["seasons"]
    assert a.season_and_age(["SS26"], seasons) == ("SS26", a.LAST_SEASON)
    assert a.season_and_age(["carryover", "sale"], seasons) == ("carryover", a.LIVE)
    assert a.season_and_age(["SS25", "AW26"], seasons) == ("AW26", a.LIVE)
    assert a.season_and_age([], seasons) == ("Untagged", a.AGED)


@pytest.mark.parametrize(
    "category,size,band",
    [
        (a.FOOTWEAR, "UK9", a.CORE),
        (a.FOOTWEAR, "10.5", a.CORE),
        (a.FOOTWEAR, "UK11", a.EDGE),
        (a.FOOTWEAR, "XL", a.UNKNOWN),
        (a.SHIRTS, "M", a.CORE),
        (a.SHIRTS, "XXL", a.EDGE),
        (a.SHIRTS, "M/L", a.EDGE),
        (a.LEGWEAR, '34"', a.EDGE),
        (a.LEGWEAR, "AGE 9/10", a.EDGE),
        (a.GLOVES, "M", a.NOT_SIZED),
    ],
)
def test_size_band(settings, category, size, band):
    assert a.size_band(category, size, settings["core_sizes"]) == band


def _line(make_line, settings, title, size, tags, units=4, **kw):
    return a.allocate(make_line(units=units, product_title=title, size=size, tags=tags, **kw), settings)


def test_routing_follows_spec(make_line, settings):
    shoe = lambda size, tags: _line(make_line, settings, "adidas Adipower Golf Shoes", size, tags)
    assert shoe("UK9", ["SS26"]).members_units == 4  # core, last season
    assert shoe("UK9", []).members_units == 4  # core, aged
    assert shoe("UK9", ["AW26"]).members_units == 0  # live
    assert shoe("UK11", ["SS26"]).members_units == 0  # edge size
    polo = lambda tags: _line(make_line, settings, "Nike Golf Polo", "XXL", tags)
    assert polo(["SS26"]).members_units == 4
    assert polo([]).members_units == 0  # aged shirts go elsewhere
    jacket = _line(make_line, settings, "adidas Climaproof Jacket", "L", ["AW25"], units=5)
    assert jacket.members_units == 3  # 60% of 5
    assert _line(make_line, settings, "adidas Climaproof Jacket", "L", ["AW25"], units=1).members_units == 1
    junior = _line(make_line, settings, "adidas Junior Performance Golf Polo", "Youth M", ["SS26"])
    assert junior.junior and junior.members_units == 0


def test_featured_pool_needs_stock_rrp_and_active(make_line, settings):
    base = dict(title="Nike Golf Polo", size="M", tags=["SS26"])
    ok = _line(make_line, settings, **base)
    no_rrp = _line(make_line, settings, rrp=None, **base)
    draft = _line(make_line, settings, status="DRAFT", **base)
    empty = _line(make_line, settings, units=0, **base)
    assert a.featured_pool([ok, no_rrp, draft, empty]) == [ok]
    assert "draft listing holding stock" in draft.flags


WORKBOOK = ROOT / "data" / "evo_clearance_allocation.xlsx"


@pytest.fixture(scope="module")
def workbook_rows():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True)
    return list(wb["Lines"].iter_rows(min_row=2, values_only=True))


def test_workbook_categories_mostly_agree(workbook_rows):
    # The workbook was built from product types, which mis-file some lines
    # (bags as Outerwear, a polo as Footwear). Title keywords fix most of those.
    agree = sum(a.categorise(r[1]) == r[3] for r in workbook_rows)
    assert agree / len(workbook_rows) >= 0.98


def test_workbook_ages_agree(settings, workbook_rows):
    for r in workbook_rows:
        tags = [] if r[4] == "Untagged" else [r[4]]
        assert a.season_and_age(tags, settings["seasons"])[1] == r[5], r[1]


def test_workbook_size_bands_agree(settings, workbook_rows):
    diffs = [r for r in workbook_rows if a.size_band(r[3], r[6], settings["core_sizes"]) != r[7]]
    # Two legwear rows sized "34" and "36" are marked Core in the workbook;
    # waist sizes aren't in the M-XL core range.
    assert len(diffs) <= 2
