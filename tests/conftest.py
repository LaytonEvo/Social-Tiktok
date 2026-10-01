from decimal import Decimal

import pytest

from evo_tiktok.config import CONFIG_DIR, load_settings
from evo_tiktok.models import StockLine


@pytest.fixture
def settings():
    return load_settings(CONFIG_DIR / "settings.example.yaml", env={})


@pytest.fixture
def make_line():
    def _make(sku="SKU1", units=1, price="50", rrp="100", member=None, **kw) -> StockLine:
        defaults = dict(
            variant_id=f"gid://shopify/ProductVariant/{sku}",
            product_title="Test Golf Polo",
            variant_title="M",
            vendor="Test",
            product_type="Polos",
            tags=["SS26"],
            status="ACTIVE",
            size="M",
            unit_cost=Decimal("20"),
            cost_estimated=False,
        )
        defaults.update(kw)
        return StockLine(
            sku=sku,
            price=Decimal(price),
            compare_at_price=Decimal(rrp) if rrp is not None else None,
            member_price=Decimal(member) if member is not None else None,
            units_by_location={"Warehouse": units},
            **defaults,
        )

    return _make
