"""Shared data shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class StockLine:
    """One Shopify variant with stock, after exclusions and allocation.

    ``sku`` is this line's unique key. It's the Shopify SKU, except where
    several variants share one (Ecco puts one SKU on every size of a style), when
    the size is added, e.g. "130404-57208 UK9". ``shopify_sku`` is always the
    SKU exactly as Shopify holds it.
    """

    sku: str
    variant_id: str
    product_title: str
    variant_title: str
    vendor: str
    product_type: str
    tags: list[str]
    status: str  # ACTIVE / DRAFT / ARCHIVED
    size: str | None
    price: Decimal
    compare_at_price: Decimal | None
    unit_cost: Decimal
    cost_estimated: bool
    units_by_location: dict[str, int]
    member_price: Decimal | None = None
    # Filled in by allocation.allocate()
    category: str = ""
    season: str = ""
    age: str = ""
    size_band: str = ""
    junior: bool = False
    ladies: bool = False
    members_units: int = 0
    flags: list[str] = field(default_factory=list)
    shopify_sku: str = ""

    def __post_init__(self) -> None:
        self.shopify_sku = self.shopify_sku or self.sku

    @property
    def units_total(self) -> int:
        return sum(self.units_by_location.values())

    @property
    def in_portal(self) -> bool:
        """Live in a Members Club deal (only portal deals carry a member price)."""
        return self.member_price is not None

    @property
    def rrp(self) -> Decimal | None:
        """The "was" price we may show, always one Shopify holds.

        Shopify's compare-at price when it's above the price; for a portal deal
        without one, the live Shopify price itself (what non-members pay).
        """
        if self.compare_at_price is not None and self.compare_at_price > self.price:
            return self.compare_at_price
        if self.member_price is not None and self.price > self.member_price:
            return self.price
        return None

    @property
    def has_rrp(self) -> bool:
        return self.rrp is not None

    @property
    def selling_price(self) -> Decimal:
        """Member price when known, otherwise the public price."""
        return self.member_price if self.member_price is not None else self.price

    @property
    def discount_pct(self) -> float | None:
        """Discount off RRP in percent, or None when Shopify holds no RRP."""
        rrp = self.rrp
        if rrp is None or rrp <= 0:
            return None
        return float((1 - self.selling_price / rrp) * 100)

    @property
    def members_routed(self) -> bool:
        return self.members_units > 0
