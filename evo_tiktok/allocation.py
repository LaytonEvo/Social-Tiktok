"""Clearance allocation: categoriser, age, size band and Members routing.

Ported from ``data/evo_clearance_allocation.xlsx`` (1 October 2026) and SPEC
section 6, so the TikTok featured pool follows live stock. Routing follows the
rules in SPEC section 6, configured under ``routing`` in settings.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from .models import StockLine

FOOTWEAR = "Footwear"
GLOVES = "Gloves"
OUTERWEAR = "Outerwear"
MID_LAYERS = "Mid-layers"
SHIRTS = "Shirts/Polos"
LEGWEAR = "Legwear"
HEADWEAR = "Headwear"
OTHER_APPAREL = "Other apparel"
NON_APPAREL = "Non-apparel"

LIVE = "Live"
LAST_SEASON = "Last season"
AGED = "Aged"

CORE = "Core"
EDGE = "Edge"
UNKNOWN = "Unknown"
NOT_SIZED = "n/a"

SIZED_APPAREL = {SHIRTS, MID_LAYERS, OUTERWEAR, LEGWEAR}


def _kw(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


# Order matters: the first match wins. For example, "Shoe Print Polo Shirt" is
# a shirt, "Waterproof Golf Shoes" are footwear, "1/2 Zip Jacket" is outerwear
# and "1/4 Zip Golf Top" is a mid-layer.
CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    (GLOVES, _kw(r"gloves?")),
    (NON_APPAREL, _kw(r"bags?", r"ball markers?", r"markers?", r"pens?", r"umbrellas?", r"towels?",
                      r"balls?", r"trolleys?", r"head ?covers?", r"tees")),
    (HEADWEAR, _kw(r"caps?", r"hats?", r"beanies?", r"visors?", r"bucket", r"headwear")),
    (OTHER_APPAREL, _kw(r"socks?", r"belts?", r"snoods?", r"base ?layers?", r"neck ?warmers?",
                        r"accessories")),
    (OUTERWEAR, _kw(r"jackets?", r"anoraks?", r"vests?", r"gilets?", r"capes?", r"smocks?",
                    r"outerwear")),
    (MID_LAYERS, _kw(r"1/4 zip", r"1/2 zip", r"quarter zip", r"half zip")),
    (SHIRTS, _kw(r"polos?", r"t-?shirts?", r"shirts?", r"tops?")),
    (FOOTWEAR, _kw(r"shoes?", r"spiked", r"spikeless", r"boots?", r"trainers?", r"sneakers?",
                   r"footwear")),
    (OUTERWEAR, _kw(r"waterproofs?", r"rain")),
    (MID_LAYERS, _kw(r"full zip", r"mid-?layers?", r"pullovers?", r"sweaters?", r"jumpers?",
                     r"hoodies?", r"fleeces?", r"crew", r"windbreakers?", r"sweatshirts?", r"knit")),
    (LEGWEAR, _kw(r"trousers?", r"shorts?", r"skorts?", r"skirts?", r"joggers?", r"pants?",
                  r"chinos?", r"legwear", r"leggings")),
]
COLOUR_SUFFIX_RE = re.compile(r"\s+[-–]\s+[^-–]*$")

JUNIOR_RE = _kw(r"junior", r"juniors", r"jnr", r"youth", r"kids?", r"boys", r"girls", r"age \d+")
JUNIOR_SIZE_RE = re.compile(r"\b(?:age|youth|years?)\b", re.IGNORECASE)
LADIES_RE = _kw(r"women'?s", r"womens", r"ladies", r"lady")
FOOT_SIZE_RE = re.compile(r"^\s*(?:UK\s*)?(\d{1,2}(?:\.5)?)\s*(?:UK)?\s*$", re.IGNORECASE)
APPAREL_SIZE_RE = re.compile(r"^\s*(XXS|XS|S|M|L|XL|XXL|2XL|XXXL|3XL|4XL)\b", re.IGNORECASE)
WAIST_SIZE_RE = re.compile(r"^\s*\d{2}\s*(?:\"|”|'')?")


def categorise(title: str, product_type: str = "") -> str:
    """Category from title keywords, then product type, else Other apparel."""
    base = COLOUR_SUFFIX_RE.sub("", title or "")  # "... Golf Shoe - Swim Cap / White"
    for text in (base, product_type.replace(">", " ")):
        for category, pattern in CATEGORY_RULES:
            if pattern.search(text or ""):
                return category
    return OTHER_APPAREL


def season_and_age(tags: Iterable[str], seasons: dict[str, list[str]]) -> tuple[str, str]:
    """Return (season tag, age). With several season tags, the newest age wins."""
    by_tag: dict[str, str] = {}
    for age, key in ((LIVE, "live"), (LAST_SEASON, "last_season"), (AGED, "aged")):
        for tag in seasons.get(key, []):
            by_tag[tag.lower()] = age
    rank = {LIVE: 0, LAST_SEASON: 1, AGED: 2}
    found = [(rank[by_tag[t.strip().lower()]], t.strip()) for t in tags if t.strip().lower() in by_tag]
    if not found:
        return "Untagged", AGED
    best_rank, tag = min(found)
    return tag, [LIVE, LAST_SEASON, AGED][best_rank]


def _foot_size(size: str) -> float | None:
    m = FOOT_SIZE_RE.match(size)
    return float(m.group(1)) if m else None


def size_band(category: str, size: str | None, core_sizes: dict) -> str:
    if category == FOOTWEAR:
        value = _foot_size(size or "")
        if value is None:
            return UNKNOWN
        core = {float(s) for s in core_sizes["footwear_uk"]}
        return CORE if value in core else EDGE
    if category in SIZED_APPAREL:
        if not size:
            return UNKNOWN
        m = APPAREL_SIZE_RE.match(size)
        if m:
            core = {s.upper() for s in core_sizes["apparel"]}
            # "M/L" and "S (9-10 Years Old)" style sizes are not core.
            exact = size.strip().upper() in core
            return CORE if exact else EDGE
        if WAIST_SIZE_RE.match(size) or JUNIOR_SIZE_RE.search(size) or size.strip().isdigit():
            return EDGE
        return UNKNOWN
    return NOT_SIZED


def is_junior(title: str, size: str | None) -> bool:
    return bool(JUNIOR_RE.search(title) or (size and JUNIOR_SIZE_RE.search(size)))


def members_units(line: StockLine, routing: dict) -> int:
    """Units of this line routed to the Members Club (the TikTok featured pool)."""
    units = line.units_total
    if units <= 0 or line.category == NON_APPAREL:
        return 0
    if routing.get("exclude_junior", True) and line.junior:
        return 0
    if routing.get("footwear_core_not_live", True) and line.category == FOOTWEAR:
        return units if line.size_band == CORE and line.age != LIVE else 0
    if line.category in routing.get("last_season_categories", []) and line.age == LAST_SEASON:
        return units
    if line.category in (OUTERWEAR, MID_LAYERS) and line.age != LIVE and line.size_band == CORE:
        share = Decimal(str(routing.get("outerwear_midlayer_members_share", 0)))
        return int((units * share).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return 0


def allocate(line: StockLine, settings) -> StockLine:
    """Fill in category, season, age, size band, routing and flags."""
    line.category = categorise(line.product_title, line.product_type)
    line.season, line.age = season_and_age(line.tags, settings["seasons"])
    line.size_band = size_band(line.category, line.size, settings["core_sizes"])
    line.junior = is_junior(line.product_title, line.size)
    line.ladies = bool(LADIES_RE.search(line.product_title))
    line.members_units = members_units(line, settings["routing"])
    flags = line.flags
    if line.cost_estimated:
        flags.append("cost estimated")
    if not line.has_rrp:
        flags.append("no RRP: no discount or RRP claim")
    if line.status.upper() == "DRAFT" and line.units_total > 0:
        flags.append("draft listing holding stock")
    if any(units > 0 for loc, units in line.units_by_location.items() if loc != "Warehouse"):
        flags.append("some units away from Warehouse")
    return line


def featured_pool(lines: Iterable[StockLine]) -> list[StockLine]:
    """SPEC 4.1 step 1: Members-routed, in stock, RRP present, live on site."""
    return [
        line
        for line in lines
        if line.members_routed and line.units_total > 0 and line.has_rrp and line.status.upper() == "ACTIVE"
    ]
