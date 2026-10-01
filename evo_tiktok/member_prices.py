"""Member prices from the Evo Members Club portal.

Luke is providing an API for this. Until it lands, ``load_member_prices``
returns nothing and discounts fall back to the public price against RRP
(``StockLine.selling_price``). Implement ``fetch`` once the API is known.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal

from .config import Settings
from .models import StockLine

log = logging.getLogger(__name__)


class MemberPriceSource:
    def __init__(self, settings: Settings):
        self.url = settings.secret("MEMBER_PRICES_API_URL", required=False)
        self.token = settings.secret("MEMBER_PRICES_API_TOKEN", required=False)

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def fetch(self, skus: Iterable[str]) -> dict[str, Decimal]:
        # TODO(M1): call Luke's members portal API once its shape is agreed.
        raise NotImplementedError("Member prices API not wired up yet")


def apply_member_prices(lines: list[StockLine], source: MemberPriceSource) -> int:
    """Set ``member_price`` on lines the portal knows about. Returns how many."""
    if not source.configured:
        log.warning("MEMBER_PRICES_API_URL not set: using public prices for discount checks")
        return 0
    prices = source.fetch(line.sku for line in lines)
    for line in lines:
        if line.sku in prices:
            line.member_price = prices[line.sku]
    return sum(1 for line in lines if line.member_price is not None)
