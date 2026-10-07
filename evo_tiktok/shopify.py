"""Read-only Shopify stock export via a bulk operation.

The catalogue has 10,000+ variants, so we use ``bulkOperationRunQuery`` and
download the JSONL result rather than paging. The app needs only
``read_products``, ``read_inventory`` and ``read_locations`` (for location
names). Nothing here writes to the store.
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


# The same fields as a normal query on one variant. A bulk operation that hits
# a field the app can't read just says ACCESS_DENIED; this names the field.
PROBE_QUERY = BULK_QUERY.replace("productVariants {", "productVariants(first: 1) {").replace(
    "inventoryLevels {", "inventoryLevels(first: 5) {"
)


class ShopifyError(RuntimeError):
    pass


def access_token(settings: Settings, store: str, http: httpx.Client) -> str:
    """An Admin API access token.

    Apps made in Shopify's Dev Dashboard (the only kind you can create now) give
    a client ID and secret; we swap them for a token with the client credentials
    grant. Tokens last 24 hours, so every run asks for a fresh one. A legacy
    admin-created app's ``shpat_`` token in SHOPIFY_ADMIN_TOKEN also works.
    """
    client_id = settings.secret("SHOPIFY_CLIENT_ID", required=False)
    client_secret = settings.secret("SHOPIFY_CLIENT_SECRET", required=False)
    if client_id and client_secret:
        resp = http.post(
            f"https://{store}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        )
        if resp.status_code >= 400:
            raise ShopifyError(
                f"Shopify refused the client ID/secret ({resp.status_code}): {resp.text[:200]}. "
                "Check the app is installed on this store and SHOPIFY_STORE is its .myshopify.com address."
            )
        body = resp.json()
        log.info("Shopify token issued, scopes: %s", body.get("scope"))
        return body["access_token"]
    token = settings.secret("SHOPIFY_ADMIN_TOKEN", required=False)
    if token:
        return token
    raise ShopifyError("Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (Dev Dashboard app), or SHOPIFY_ADMIN_TOKEN")


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
        self.http = http or httpx.Client(timeout=60)
        if "X-Shopify-Access-Token" not in self.http.headers:
            self.http.headers["X-Shopify-Access-Token"] = access_token(settings, store, self.http)
        # The result file is a signed URL on Shopify's storage: no token sent.
        self.download_http = download_http or httpx.Client(timeout=300)

    def _graphql(self, query: str, variables: dict) -> dict:
        resp = self.http.post(self.endpoint, json={"query": query, "variables": variables})
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise ShopifyError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    def _access_detail(self) -> str:
        """Why the app was refused, from a one-variant run of the same query."""
        try:
            resp = self.http.post(self.endpoint, json={"query": PROBE_QUERY})
            errors = resp.json().get("errors") or []
        except (httpx.HTTPError, ValueError) as exc:
            return f" (check query failed: {exc})"
        if not errors:
            return " (a normal query of the same fields works, so this is bulk-operation access)"
        messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
        return f" ({messages})"

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
                detail = self._access_detail() if op.get("errorCode") == "ACCESS_DENIED" else ""
                raise ShopifyError(f"Bulk operation {op['status']}: {op.get('errorCode')}{detail}")
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

    stats = {"variants": len(variants), "excluded": 0, "no_sku": 0, "cost_estimated": 0, "no_rrp": 0,
             "duplicate_sku": 0}
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
    return _dedupe_skus(lines, stats), stats


def _dedupe_skus(lines: list[StockLine], stats: dict[str, int]) -> list[StockLine]:
    """Keep one line per SKU: the copy with the most stock.

    Scripts, validators and the snapshot all key on SKU, but Shopify lets two
    variants share one. The extra copies are counted and named in the log so
    they can be fixed in Shopify.
    """
    best: dict[str, StockLine] = {}
    counts: dict[str, int] = {}
    for line in lines:
        counts[line.sku] = counts.get(line.sku, 0) + 1
        kept = best.get(line.sku)
        if kept is None or line.units_total > kept.units_total:
            best[line.sku] = line
    dupes = sorted(sku for sku, n in counts.items() if n > 1)
    if dupes:
        stats["duplicate_sku"] = len(lines) - len(best)
        log.warning(
            "%d SKUs are on more than one Shopify variant; kept the copy with most stock: %s%s",
            len(dupes), ", ".join(dupes[:20]), " ..." if len(dupes) > 20 else "",
        )
    kept_ids = {id(line) for line in best.values()}
    return [line for line in lines if id(line) in kept_ids]
