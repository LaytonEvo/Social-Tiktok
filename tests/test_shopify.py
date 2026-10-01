import json
from decimal import Decimal
from pathlib import Path

import httpx

from evo_tiktok.config import Settings
from evo_tiktok.shopify import ShopifyClient, parse_bulk_records

FIXTURE = Path(__file__).parent / "fixtures" / "shopify_bulk.jsonl"


def records():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_exclusions_and_fixes(settings):
    lines, stats = parse_bulk_records(records(), settings)
    skus = {l.sku: l for l in lines}
    assert "PERS" not in skus and "HIDDEN" not in skus
    assert stats["excluded"] == 2
    shoe = skus["SHOE-9"]
    assert shoe.units_by_location == {"Warehouse": 3, "Burley Golf Club": 1}
    assert shoe.units_total == 4 and shoe.size == "UK9"
    polo = skus["POLO-M"]
    assert polo.cost_estimated and polo.unit_cost == Decimal("18.62")  # 35 / 1.88
    assert not skus["POLO-NORRP"].has_rrp
    assert stats["cost_estimated"] == 1 and stats["no_rrp"] == 3  # POLO-NORRP and two trolley lines


def test_bulk_export_flow(settings):
    s = Settings(data=settings.data, source=settings.source,
                 env={"SHOPIFY_STORE": "evo.myshopify.com", "SHOPIFY_ADMIN_TOKEN": "t"})
    s.data["shopify"]["bulk_poll_seconds"] = 0
    calls = []

    def api(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body["query"].split("(")[0].strip())
        assert request.headers["X-Shopify-Access-Token"] == "t"
        if "bulkOperationRunQuery" in body["query"]:
            assert "productVariants" in body["variables"]["query"]
            return httpx.Response(200, json={"data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}, "userErrors": []}}})
        status = "RUNNING" if calls.count("query poll") < 2 else "COMPLETED"
        return httpx.Response(200, json={"data": {"node": {
            "id": "gid://shopify/BulkOperation/1", "status": status, "objectCount": "3",
            "url": "https://storage.example/result.jsonl"}}})

    def download(request: httpx.Request):
        assert "X-Shopify-Access-Token" not in request.headers
        return httpx.Response(200, content=FIXTURE.read_bytes())

    client = ShopifyClient(s, http=httpx.Client(transport=httpx.MockTransport(api),
                                                headers={"X-Shopify-Access-Token": "t"}),
                           download_http=httpx.Client(transport=httpx.MockTransport(download)))
    out = list(client.export_variants_jsonl())
    assert len(out) == len(records())
    assert calls[0] == "mutation run" and calls.count("query poll") == 2
