"""Guardrail validators shared by every job (SPEC section 5).

Each check returns a list of error strings; an empty list means it passed.
``require`` turns errors into a ``GuardrailError`` so a job fails loudly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from decimal import Decimal
from pathlib import Path

from .config import DOCS_DIR
from .models import StockLine


class GuardrailError(RuntimeError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Guardrail check failed:\n- " + "\n- ".join(errors))


def require(errors: list[str]) -> None:
    if errors:
        raise GuardrailError(errors)


# A price or discount: "£29", "£ 29.99", "40% off", "40 %", "half price", "30 quid".
PRICE_RE = re.compile(
    r"£\s?\d|\d+(?:\.\d+)?\s?%|%\s?off|\bhalf[- ]price\b|\b\d+(?:\.\d{2})?\s?(?:pounds|quid)\b",
    re.IGNORECASE,
)


def _brand_pattern(brand: str) -> re.Pattern[str]:
    # All-caps brands (TRUE, PXG) match case-sensitively so the word "true"
    # doesn't trip the check. Other brands match in any case.
    flags = 0 if brand.isupper() else re.IGNORECASE
    words = r"\s+".join(re.escape(w) for w in brand.split())
    return re.compile(rf"(?<![\w]){words}(?![\w])", flags)


def brand_price_check(text: str, brands: Iterable[str], window: int = 40) -> list[str]:
    """Fail if a restricted brand and a price or discount sit within ``window`` chars."""
    errors = []
    prices = [m for m in PRICE_RE.finditer(text)]
    if not prices:
        return errors
    for brand in brands:
        for b in _brand_pattern(brand).finditer(text):
            for p in prices:
                gap = max(p.start() - b.end(), b.start() - p.end(), 0)
                if gap <= window:
                    errors.append(
                        f"Brand '{b.group()}' within {gap} characters of price '{p.group()}': {text!r}"
                    )
                    break
    return errors


# Member prices are rounded to the penny, so 22.06 off 25.95 is 14.99%: a claim
# of 15% is fair. Claims may round up by at most this many percentage points.
ROUNDING_PCT = 0.5

UP_TO_RE = re.compile(r"\bup\s+to\s+(\d+(?:\.\d+)?)\s?%", re.IGNORECASE)


def up_to_claim_check(text: str, featured_lines: Iterable[StockLine], threshold_share: float) -> list[str]:
    """Fail if "up to X%" is claimed but too few featured lines reach X% off."""
    claims = [float(m.group(1)) for m in UP_TO_RE.finditer(text)]
    if not claims:
        return []
    lines = list(featured_lines)
    errors = []
    for pct in claims:
        if not lines:
            errors.append(f"'up to {pct:g}%' claimed with no featured lines")
            continue
        hits = sum(1 for line in lines if (line.discount_pct or 0) >= pct - ROUNDING_PCT)
        share = hits / len(lines)
        if share < threshold_share:
            errors.append(
                f"'up to {pct:g}%' needs {threshold_share:.0%} of featured lines at {pct:g}%+; "
                f"only {hits}/{len(lines)} ({share:.0%})"
            )
    return errors


# "40% off", "40-50% off", "40–50% off" (but not "up to 40%", checked above)
PERCENT_OFF_RE = re.compile(
    r"(?<!up to )(?<!up to)\b(\d+(?:\.\d+)?)\s?%?\s?(?:[-–]|to)?\s?(?:(\d+(?:\.\d+)?)\s?)?%\s*off\b",
    re.IGNORECASE,
)


def percent_off_check(text: str, featured_lines: Iterable[StockLine], threshold_share: float) -> list[str]:
    """Fail if "X% off" or "X–Y% off" is claimed but too few featured lines reach X% off."""
    lines = list(featured_lines)
    errors = []
    for m in PERCENT_OFF_RE.finditer(text):
        if re.search(r"up\s+to\s*$", text[: m.start()], re.IGNORECASE):
            continue  # an "up to" claim; up_to_claim_check covers it
        low = float(m.group(1))
        if not lines:
            errors.append(f"'{m.group(0).strip()}' claimed with no featured lines")
            continue
        hits = sum(1 for line in lines if (line.discount_pct or 0) >= low - ROUNDING_PCT)
        if hits / len(lines) < threshold_share:
            errors.append(
                f"'{m.group(0).strip()}' needs {threshold_share:.0%} of featured lines at {low:g}%+ off; "
                f"only {hits}/{len(lines)}"
            )
    return errors


# --------------------------------------------------------- £ figures
#
# Every £ figure in public text must be true at shelf price on the website: the
# live Shopify variant price (never the member price, cost or a rounded band).

POUND_RE = re.compile(r"£\s?(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)")
UNDER_BEFORE_RE = re.compile(r"\b(?:under|below|less\s+than|sub)\s*$", re.IGNORECASE)
PRICE_TIERS = (30, 50, 75, 100, 150, 200)
TIER_MARGIN = 0.10   # every featured item must sit at least 10% below an "under £X" tier
PRICE_GAP_MIN = 0.20  # two quoted prices must differ by at least 20%
PENCE = Decimal("0.50")  # "£26" for £25.95 is fair; "£26" for £24.99 isn't


def pounds(text: str) -> list[tuple[Decimal, bool]]:
    """Each £ figure in ``text`` with whether it's an "under £X" style cap."""
    out = []
    for m in POUND_RE.finditer(text):
        amount = Decimal(m.group(1).replace(",", ""))
        out.append((amount, bool(UNDER_BEFORE_RE.search(text[: m.start()]))))
    return out


def _near(amount: Decimal, value: Decimal) -> bool:
    return abs(amount - value) <= PENCE


def price_claim_check(text: str, lines: Iterable[StockLine]) -> list[str]:
    """Fail any £ figure that isn't true at live website price for the featured lines.

    Allowed: a featured line's live price; its compare-at price when that's above
    the price (a real saving); the gap between two featured live prices, or a
    saving; or "under £X" where X is a fixed tier and every featured line sits at
    least 10% below it. Two quoted live prices must be at least 20% apart.
    """
    lines = list(lines)
    found = pounds(text)
    if not found:
        return []
    if not lines:
        return [f"£ figure(s) {', '.join(f'£{a:g}' for a, _ in found)} quoted with no featured products"]
    live = sorted({line.price for line in lines})
    was = sorted({line.compare_at_price for line in lines
                  if line.compare_at_price is not None and line.compare_at_price > line.price})
    gaps = {abs(a - b) for a in live for b in live if a != b}
    gaps |= {line.compare_at_price - line.price for line in lines if line.compare_at_price in was}
    errors, quoted_live = [], set()
    for amount, capped in found:
        if capped:
            top = max(live)
            if int(amount) != amount or int(amount) not in PRICE_TIERS:
                errors.append(f"'under £{amount:g}' isn't a fixed price tier ({', '.join(f'£{t}' for t in PRICE_TIERS)})")
            elif top > amount * Decimal(str(1 - TIER_MARGIN)):
                errors.append(
                    f"'under £{amount:g}' needs every featured item at least {TIER_MARGIN:.0%} below it; "
                    f"the dearest is £{top} at live price"
                )
            continue
        match = next((p for p in live if _near(amount, p)), None)
        if match is not None:
            quoted_live.add(match)
            continue
        if any(_near(amount, w) for w in was) or any(_near(amount, g) for g in gaps):
            continue
        errors.append(
            f"£{amount:g} isn't a live website price of the featured products "
            f"({', '.join(f'£{p}' for p in live)}), a real saving or the gap between them"
        )
    quoted = sorted(quoted_live)
    for low, high in zip(quoted, quoted[1:]):
        if high < low * Decimal(str(1 + PRICE_GAP_MIN)):
            errors.append(f"£{low} vs £{high} is less than a {PRICE_GAP_MIN:.0%} price gap")
    return errors


def member_discount_check(text: str, lines: Iterable[StockLine]) -> list[str]:
    """Fail a member % claim that this video's own featured products don't get.

    The % is what a Clubhouse member saves against today's website price.
    "up to X%" needs at least one featured product at X%; a flat "X% off"
    (or "X-Y% off") needs every featured product at X% or more.
    """
    lines = list(lines)
    discounts = [line.member_saving_pct or 0 for line in lines]
    errors = []
    for m in UP_TO_RE.finditer(text):
        pct = float(m.group(1))
        best = max(discounts, default=0)
        if best < pct - ROUNDING_PCT:
            errors.append(f"'up to {pct:g}%' but the best discount on this video's products is {best:.0f}%")
    for m in PERCENT_OFF_RE.finditer(text):
        if re.search(r"up\s+to\s*$", text[: m.start()], re.IGNORECASE):
            continue
        low = float(m.group(1))
        worst = min(discounts, default=0)
        if worst < low - ROUNDING_PCT:
            errors.append(f"'{m.group(0).strip()}' but some products on this video get only {worst:.0f}%")
    return errors


def no_pound_check(text: str) -> list[str]:
    """Videos carry no £ prices: the offer is what Clubhouse members save, as a %."""
    return [f"No £ prices in videos ('{m.group()}'); say what Clubhouse members save instead"
            for m in POUND_RE.finditer(text)]


SAVING_CLAIM_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%", re.IGNORECASE)


def has_saving_claim(text: str) -> bool:
    return bool(SAVING_CLAIM_RE.search(text))


def price_hook_type(text: str, lines: Iterable[StockLine]) -> str:
    """Which price hook a video uses: tier, saving, gap or none."""
    lines = list(lines)
    found = pounds(text)
    if any(capped for _, capped in found):
        return "tier"
    was = {line.compare_at_price for line in lines if line.compare_at_price and line.compare_at_price > line.price}
    if any(_near(a, w) for a, _ in found for w in was):
        return "saving"
    live = {p for a, _ in found for p in {line.price for line in lines} if _near(a, p)}
    return "gap" if len(live) >= 2 else "none"


RRP_RE = re.compile(r"\bRRP\b|\bwas\s+£|\bretail\s+price\b|\bnormally\s+£", re.IGNORECASE)


def rrp_check(text: str, skus: Iterable[str], stock: Mapping[str, StockLine]) -> list[str]:
    """Fail if the text shows an RRP for a SKU that has no compareAtPrice in Shopify."""
    if not RRP_RE.search(text):
        return []
    missing = [
        sku for sku in skus
        if sku not in stock or stock[sku].compare_at_price is None or stock[sku].compare_at_price <= stock[sku].price
    ]
    return [f"RRP shown but Shopify holds no RRP for {sku}" for sku in missing]


@lru_cache(maxsize=4)
def free_entry_wording(rules_path: Path = DOCS_DIR / "CONTENT_RULES.md") -> str:
    """The giveaway wording, read from CONTENT_RULES.md so it's edited in one place."""
    text = rules_path.read_text(encoding="utf-8")
    section = text.split("## Giveaway free-entry wording", 1)[1]
    quote = [ln[1:].strip() for ln in section.splitlines() if ln.startswith(">")]
    if not quote:
        raise ValueError("No free-entry wording blockquote in CONTENT_RULES.md")
    return " ".join(quote)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'").replace("–", "-")).strip().lower()


def giveaway_check(text: str, wording: str | None = None, terms_url: str | None = None) -> list[str]:
    """Fail if a giveaway caption lacks the free-entry wording. "[date]" must be
    filled, and "[terms]" is the link to the full T&Cs."""
    wording = wording or free_entry_wording()
    if "[terms]" in wording:
        if not terms_url:
            return ["No T&Cs link configured (giveaway.terms_url), so the giveaway can't run"]
        wording = wording.replace("[terms]", terms_url)
    parts = [re.escape(p) for p in _norm(wording).split("[date]")]
    pattern = r"(?!\[date\])\S.{0,40}?".join(parts)
    if re.search(pattern, _norm(text)):
        return []
    return ["Giveaway is missing the free-entry wording from docs/CONTENT_RULES.md (with a real closing date)"]


def stock_check(skus: Iterable[str], stock: Mapping[str, StockLine]) -> list[str]:
    """Fail if any SKU is unknown or has zero stock."""
    errors = []
    for sku in skus:
        line = stock.get(sku)
        if line is None:
            errors.append(f"SKU {sku} is not in today's stock snapshot")
        elif line.units_total <= 0:
            errors.append(f"SKU {sku} has zero stock")
    return errors


def no_price_check(text: str) -> list[str]:
    """Fail if the text carries any price or discount (trolley content has no clearance pricing)."""
    found = [m.group() for m in PRICE_RE.finditer(text)]
    return [f"Price or discount '{p}' not allowed here: {text!r}" for p in found]


AI_MEDIA_RE = re.compile(
    r"\bAI[- ](?:generated|video|voice(?:over)?|avatar|narrat\w*)\b|\btext[- ]to[- ]speech\b|\bTTS\b"
    r"|\b(?:synthetic|generated|robot) voice\b",
    re.IGNORECASE,
)


def real_footage_check(text: str) -> list[str]:
    """Fail if a script asks for AI video, AI voiceover or text-to-speech."""
    return [f"Real footage and voices only, found '{m.group()}'" for m in AI_MEDIA_RE.finditer(text)]
