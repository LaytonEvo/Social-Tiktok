"""What's live in the Evo Members Club portal, and at what member price.

Luke's reporting API lists every deal live right now:
``GET /api/reporting/member-products`` (same base URL and Bearer key as the
membership history). Each product has variants keyed by Shopify variant ID with
the one member price every member pays.

When the portal answers, it is the truth for what we feature: a line is a
Members Club line if its Shopify variant is in a live deal, and every unit of
it is available there (the portal holds no inventory; Shopify does). When it
doesn't answer, lines keep the allocation rules from the handover workbook and
the run says so.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import httpx

from .config import Settings
from .models import StockLine

log = logging.getLogger(__name__)

VARIANT_GID = "gid://shopify/ProductVariant/"


class MemberPricesError(RuntimeError):
    pass


def _variant_key(value) -> str:
    """'gid://shopify/ProductVariant/123' and '123' both become '123'."""
    return str(value or "").rsplit("/", 1)[-1].strip()


class MemberPriceSource:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.base = ((settings.get("members") or {}).get("api_url") or "").rstrip("/")
        self.key = settings.secret("MEMBERS_REPORTING_API_KEY", required=False)
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(self.base and self.key)

    def fetch(self) -> dict[str, Decimal]:
        """Member price for each Shopify variant in a live deal, keyed by numeric variant ID."""
        client = self.http or httpx.Client(timeout=60)
        resp = client.get(
            f"{self.base}/api/reporting/member-products", headers={"Authorization": f"Bearer {self.key}"}
        )
        if resp.status_code == 401:
            raise MemberPricesError("Members portal refused the key (401): check MEMBERS_REPORTING_API_KEY")
        if resp.status_code >= 400:
            raise MemberPricesError(f"Members portal error {resp.status_code}: {resp.text[:200]}")
        prices: dict[str, Decimal] = {}
        for product in resp.json().get("products", []):
            if product.get("available") is False:
                continue
            for variant in product.get("variants") or []:
                key = _variant_key(variant.get("shopify_variant_id") or variant.get("shopify_variant_gid"))
                try:
                    price = Decimal(str(variant.get("member_price")))
                except (InvalidOperation, TypeError):
                    continue
                if key and price > 0:
                    prices[key] = price
        return prices


def apply_member_prices(lines: list[StockLine], source: MemberPriceSource, stats: dict | None = None) -> int:
    """Mark lines that are live in the portal, with their member price.

    Returns how many stock lines matched a live deal. With the portal loaded,
    a line's Members Club units are all its units if it's in a deal, else none.
    """
    stats = stats if stats is not None else {}
    if not source.configured:
        log.warning("Members portal not configured: featured stock uses the allocation rules")
        stats["portal"] = "not configured"
        return 0
    try:
        prices = source.fetch()
    except (MemberPricesError, httpx.HTTPError, ValueError) as exc:
        log.warning("Members portal unavailable, featured stock uses the allocation rules: %s", exc)
        stats["portal"] = f"unavailable: {exc}"
        return 0
    matched = 0
    seen: set[str] = set()
    for line in lines:
        key = _variant_key(line.variant_id)
        if key in prices:
            line.member_price = prices[key]
            line.members_units = line.units_total
            seen.add(key)
            matched += 1
        else:
            line.member_price = None
            line.members_units = 0
    in_stock = {_variant_key(l.variant_id) for l in lines if l.member_price is not None and l.units_total > 0}
    stats.update(
        portal="live",
        portal_variants=len(prices),
        portal_variants_in_stock=len(in_stock),
        portal_variants_no_stock=len(prices) - len(in_stock),
    )
    log.info(
        "Members portal: %d live deal variants, %d in stock in Shopify, %d with no stock",
        len(prices), len(in_stock), len(prices) - len(in_stock),
    )
    return matched
