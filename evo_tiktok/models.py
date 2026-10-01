"""Shared data shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class StockLine:
    """One Shopify variant with stock, after exclusions and allocation."""

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

    @property
    def units_total(self) -> int:
        return sum(self.units_by_location.values())

    @property
    def has_rrp(self) -> bool:
        return self.compare_at_price is not None and self.compare_at_price > self.price

    @property
    def selling_price(self) -> Decimal:
        """Member price when known, otherwise the public price."""
        return self.member_price if self.member_price is not None else self.price

    @property
    def discount_pct(self) -> float | None:
        """Discount off RRP in percent, or None when Shopify holds no RRP."""
        if self.compare_at_price is None or self.compare_at_price <= 0:
            return None
        return float((1 - self.selling_price / self.compare_at_price) * 100)

    @property
    def members_routed(self) -> bool:
        return self.members_units > 0
