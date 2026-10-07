import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from evo_tiktok.config import Settings
from evo_tiktok.shopify import ShopifyClient, ShopifyError, parse_bulk_records

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


def _settings(settings, **env):
    return Settings(data=settings.data, source=settings.source, env={"SHOPIFY_STORE": "evo.myshopify.com", **env})


def test_dev_dashboard_app_swaps_client_credentials_for_a_token(settings):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/admin/oauth/access_token":
            return httpx.Response(200, json={"access_token": "tok123", "scope": "read_products,read_inventory",
                                             "expires_in": 86399})
        return httpx.Response(200, json={"data": {}})

    s = _settings(settings, SHOPIFY_CLIENT_ID="cid", SHOPIFY_CLIENT_SECRET="csecret")
    client = ShopifyClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)))
    token_req = seen[0]
    assert token_req.method == "POST" and token_req.url.host == "evo.myshopify.com"
    assert b"grant_type=client_credentials" in token_req.content and b"client_id=cid" in token_req.content
    assert client.http.headers["X-Shopify-Access-Token"] == "tok123"


def test_legacy_admin_token_still_works(settings):
    client = ShopifyClient(_settings(settings, SHOPIFY_ADMIN_TOKEN="shpat_x"),
                           http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    assert client.http.headers["X-Shopify-Access-Token"] == "shpat_x"


def test_clear_errors_for_bad_or_missing_credentials(settings):
    import pytest

    from evo_tiktok.shopify import ShopifyError

    refuse = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="invalid_client")))
    with pytest.raises(ShopifyError, match="refused the client ID/secret"):
        ShopifyClient(_settings(settings, SHOPIFY_CLIENT_ID="a", SHOPIFY_CLIENT_SECRET="b"), http=refuse)
    with pytest.raises(ShopifyError, match="SHOPIFY_CLIENT_ID"):
        ShopifyClient(_settings(settings), http=refuse)


def test_access_denied_names_the_blocked_field(settings):
    s = _settings(settings, SHOPIFY_ADMIN_TOKEN="t")
    s.data["shopify"]["bulk_poll_seconds"] = 0

    def api(request: httpx.Request):
        query = json.loads(request.content)["query"]
        if "bulkOperationRunQuery" in query:
            return httpx.Response(200, json={"data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}, "userErrors": []}}})
        if "query poll" in query:
            return httpx.Response(200, json={"data": {"node": {"id": "x", "status": "FAILED",
                                                               "errorCode": "ACCESS_DENIED"}}})
        assert "productVariants(first: 1)" in query
        return httpx.Response(200, json={"errors": [{"message": "Access denied for unitCost field."}]})

    client = ShopifyClient(s, http=httpx.Client(transport=httpx.MockTransport(api)))
    with pytest.raises(ShopifyError, match="Access denied for unitCost field"):
        list(client.export_variants_jsonl())


def test_shared_skus_get_the_size_added_and_keep_all_stock(settings):
    recs = records()
    variant = next(r for r in recs if r.get("__parentId") is None and r.get("sku") == "SHOE-9")
    twins = []
    for n, size in enumerate(["UK10", "UK10"]):  # a second size, then a draft copy of it
        twin = json.loads(json.dumps(variant))
        twin["id"] = f"gid://shopify/ProductVariant/77{n}"
        twin["inventoryItem"] = {**(twin.get("inventoryItem") or {}), "id": f"gid://shopify/InventoryItem/77{n}"}
        twin["selectedOptions"] = [{"name": "Size", "value": size}]
        twins += [twin, {"__parentId": twin["id"], "location": {"name": "Warehouse"},
                         "quantities": [{"name": "available", "quantity": 2}]}]
    before, _ = parse_bulk_records(recs, settings)
    lines, stats = parse_bulk_records(recs + twins, settings)
    assert len(lines) == len(before) + 2  # nothing dropped
    keys = [l.sku for l in lines if l.shopify_sku == "SHOE-9"]
    assert len(keys) == len(set(keys)) == 3
    assert "SHOE-9 UK10" in keys and "SHOE-9 UK10 #771" in keys
    assert stats["shared_sku"] == 3
