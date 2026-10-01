"""Read-only Shopify stock export via a bulk operation.

The catalogue has 10,000+ variants, so we use ``bulkOperationRunQuery`` and
download the JSONL result rather than paging. The token needs only
``read_products`` and ``read_inventory``. Nothing here writes to the store.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation

import httpx

from .config import Settings
from .models import StockLine

log = logging.getLogger(__name__)

BULK_QUERY = """
{
  productVariants {
    edges {
      node {
        id
        sku
        title
        price
        compareAtPrice
        selectedOptions { name value }
        inventoryQuantity
        product { id title vendor productType tags status }
        inventoryItem {
          id
          unitCost { amount }
          inventoryLevels {
            edges {
              node {
                id
                location { name }
                quantities(names: ["available"]) { name quantity }
              }
            }
          }
        }
      }
    }
  }
}
"""

RUN_MUTATION = """
mutation run($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""

POLL_QUERY = """
query poll($id: ID!) {
  node(id: $id) {
    ... on BulkOperation { id status errorCode objectCount url partialDataUrl }
  }
}
"""


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        download_http: httpx.Client | None = None,
    ):
        store = settings.secret("SHOPIFY_STORE")
        version = settings["shopify"]["api_version"]
        self.endpoint = f"https://{store}/admin/api/{version}/graphql.json"
        self.poll_seconds = float(settings["shopify"].get("bulk_poll_seconds", 5))
        self.timeout_seconds = float(settings["shopify"].get("bulk_timeout_seconds", 900))
        self.http = http or httpx.Client(
            headers={"X-Shopify-Access-Token": settings.secret("SHOPIFY_ADMIN_TOKEN")},
            timeout=60,
        )
        # The result file is a signed URL on Shopify's storage: no token sent.
        self.download_http = download_http or httpx.Client(timeout=300)

    def _graphql(self, query: str, variables: dict) -> dict:
        resp = self.http.post(self.endpoint, json={"query": query, "variables": variables})
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise ShopifyError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    def export_variants_jsonl(self) -> Iterator[dict]:
        """Start the bulk export, wait for it and yield the JSONL records."""
        data = self._graphql(RUN_MUTATION, {"query": BULK_QUERY})["bulkOperationRunQuery"]
        if data["userErrors"]:
            raise ShopifyError(f"Bulk operation refused: {data['userErrors']}")
        op_id = data["bulkOperation"]["id"]
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            op = self._graphql(POLL_QUERY, {"id": op_id})["node"]
            if op["status"] == "COMPLETED":
                break
            if op["status"] in {"FAILED", "CANCELED", "EXPIRED"}:
                raise ShopifyError(f"Bulk operation {op['status']}: {op.get('errorCode')}")
            if time.monotonic() > deadline:
                raise ShopifyError(f"Bulk operation still {op['status']} after {self.timeout_seconds:.0f}s")
            time.sleep(self.poll_seconds)
        log.info("Bulk export complete: %s objects", op.get("objectCount"))
        if not op.get("url"):  # an empty store returns no file
            return
        with self.download_http.stream("GET", op["url"]) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines():
                if raw.strip():
                    yield json.loads(raw)


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _size(variant: dict) -> str | None:
    for opt in variant.get("selectedOptions") or []:
        if opt.get("name", "").strip().lower() in {"size", "shoe size", "uk size"}:
            return opt.get("value")
    title = variant.get("title") or ""
    return None if title in ("", "Default Title") else title


def parse_bulk_records(records: Iterable[dict], settings: Settings) -> tuple[list[StockLine], dict[str, int]]:
    """Turn bulk JSONL records into StockLines, applying the known data fixes.

    Inventory levels arrive as separate records whose ``__parentId`` is the
    variant (or its inventory item). Returns the lines and a count of what was
    excluded or patched, for the run log.
    """
    exclusions = settings["exclusions"]
    excluded_types = {t.lower() for t in exclusions.get("product_types", [])}
    excluded_titles = {t.lower() for t in exclusions.get("titles", [])}
    divisor = Decimal(str(settings["shopify"].get("estimated_cost_divisor", 1.88)))
    locations = list(settings.get("locations", []))

    variants: dict[str, dict] = {}
    item_to_variant: dict[str, str] = {}
    levels: dict[str, dict[str, int]] = {}
    for rec in records:
        parent = rec.get("__parentId")
        if parent is None and "product" in rec:
            variants[rec["id"]] = rec
            item = rec.get("inventoryItem") or {}
            if item.get("id"):
                item_to_variant[item["id"]] = rec["id"]
        elif parent is not None and "location" in rec:
            qty = sum(q.get("quantity") or 0 for q in rec.get("quantities") or [] if q.get("name") == "available")
            name = (rec.get("location") or {}).get("name", "Unknown")
            levels.setdefault(parent, {})[name] = levels.get(parent, {}).get(name, 0) + qty

    stats = {"variants": len(variants), "excluded": 0, "no_sku": 0, "cost_estimated": 0, "no_rrp": 0}
    lines: list[StockLine] = []
    for vid, v in variants.items():
        product = v.get("product") or {}
        title = product.get("title") or ""
        ptype = product.get("productType") or ""
        if ptype.lower() in excluded_types or title.lower() in excluded_titles:
            stats["excluded"] += 1
            continue
        sku = (v.get("sku") or "").strip()
        if not sku:
            stats["no_sku"] += 1
            continue
        item = v.get("inventoryItem") or {}
        units = levels.get(vid) or levels.get(item.get("id", ""), {})
        units_by_location = {loc: max(units.get(loc, 0), 0) for loc in locations}
        for loc, qty in units.items():  # keep stock at any other location visible
            if loc not in units_by_location:
                units_by_location[loc] = max(qty, 0)
        price = _dec(v.get("price")) or Decimal("0")
        cost = _dec((item.get("unitCost") or {}).get("amount"))
        cost_estimated = cost is None or cost <= 0
        if cost_estimated:
            cost = (price / divisor).quantize(Decimal("0.01"))
            stats["cost_estimated"] += 1
        compare_at = _dec(v.get("compareAtPrice"))
        if compare_at is None or compare_at <= price:
            stats["no_rrp"] += 1
        lines.append(
            StockLine(
                sku=sku,
                variant_id=vid,
                product_title=title,
                variant_title=v.get("title") or "",
                vendor=product.get("vendor") or "",
                product_type=ptype,
                tags=list(product.get("tags") or []),
                status=product.get("status") or "",
                size=_size(v),
                price=price,
                compare_at_price=compare_at,
                unit_cost=cost,
                cost_estimated=cost_estimated,
                units_by_location=units_by_location,
            )
        )
    return lines, stats
