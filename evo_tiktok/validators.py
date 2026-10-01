"""Guardrail validators shared by every job (SPEC section 5).

Each check returns a list of error strings; an empty list means it passed.
``require`` turns errors into a ``GuardrailError`` so a job fails loudly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
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
        hits = sum(1 for line in lines if (line.discount_pct or 0) >= pct - 1e-9)
        share = hits / len(lines)
        if share < threshold_share:
            errors.append(
                f"'up to {pct:g}%' needs {threshold_share:.0%} of featured lines at {pct:g}%+; "
                f"only {hits}/{len(lines)} ({share:.0%})"
            )
    return errors


RRP_RE = re.compile(r"\bRRP\b|\bwas\s+£|\bretail\s+price\b|\bnormally\s+£", re.IGNORECASE)


def rrp_check(text: str, skus: Iterable[str], stock: Mapping[str, StockLine]) -> list[str]:
    """Fail if the text shows an RRP for a SKU that has no compareAtPrice in Shopify."""
    if not RRP_RE.search(text):
        return []
    missing = [sku for sku in skus if sku not in stock or not stock[sku].has_rrp]
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


def giveaway_check(text: str, wording: str | None = None) -> list[str]:
    """Fail if a giveaway caption lacks the free-entry wording. "[date]" must be filled."""
    wording = wording or free_entry_wording()
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
