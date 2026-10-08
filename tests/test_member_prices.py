import json
from decimal import Decimal

import httpx
import pytest

from evo_tiktok import allocation, dashboard, metrics, validators
from evo_tiktok.config import Settings
from evo_tiktok.member_prices import MemberPriceSource, apply_member_prices

# Trimmed from Luke's production response (8 Oct 2026): sizes share one SKU, key on variant ID.
PORTAL = {
    "generatedAt": "2026-10-08T15:13:00.000Z",
    "count": 2,
    "products": [
        {
            "deal_id": 278, "title": "Nike Dura Feel X Golf Glove - White (3 Glove Bundle)", "available": True,
            "member_price_from": 22.06,
            "variants": [
                {"shopify_variant_id": "50687523520770",
                 "shopify_variant_gid": "gid://shopify/ProductVariant/50687523520770",
                 "title": "Left Hand / S", "sku": "48956-284", "rrp": 25.95, "member_price": 22.06},
                {"shopify_variant_id": "50687523520771",
                 "shopify_variant_gid": "gid://shopify/ProductVariant/50687523520771",
                 "title": "Left Hand / M", "sku": "48956-284", "rrp": 25.95, "member_price": 22.06},
            ],
        },
        {
            "deal_id": 301, "title": "Ecco Biom C4 - White", "available": True,
            "variants": [{"shopify_variant_gid": "gid://shopify/ProductVariant/777", "sku": "130404-57208",
                          "rrp": 180, "member_price": 89.0}],
        },
    ],
}


def _settings(settings, key="k"):
    return Settings(data=settings.data, source=settings.source, env={"MEMBERS_REPORTING_API_KEY": key} if key else {})


def _client(payload=PORTAL, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("customer-metrics"):
            return httpx.Response(200, json={"memberships": {"totalPaid": 247}, "customers": {"total": 62950}})
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_portal_marks_live_deals_by_variant_and_prices_them(settings, make_line):
    seen = []
    glove_s = make_line("48956-284 S", units=3, price="25.95", rrp=None,
                        variant_id="gid://shopify/ProductVariant/50687523520770")
    glove_m = make_line("48956-284 M", units=0, price="25.95", rrp=None,
                        variant_id="gid://shopify/ProductVariant/50687523520771")
    shoe = make_line("130404-57208 UK9", units=2, price="129.95", rrp="180",
                     variant_id="gid://shopify/ProductVariant/777", status="DRAFT")
    polo = make_line("POLO", units=5, price="40", rrp="80", members_units=5)  # routed by the old rules only
    stats = {}
    n = apply_member_prices([glove_s, glove_m, shoe, polo],
                            MemberPriceSource(_settings(settings), _client(seen=seen)), stats)
    assert seen[0].url.path == "/api/reporting/member-products"
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert n == 3 and glove_s.member_price == Decimal("22.06") and polo.member_price is None
    assert glove_s.members_units == 3 and polo.members_units == 0  # the portal is the truth when it answers
    assert stats["portal"] == "live" and stats["portal_variants"] == 3
    assert stats["portal_variants_in_stock"] == 2 and stats["portal_variants_no_stock"] == 1
    # Featured = live in the portal and in stock, even if hidden from the public site.
    assert allocation.featured_pool([glove_s, glove_m, shoe, polo]) == [glove_s, shoe]


def test_was_price_is_always_one_shopify_holds(make_line):
    glove = make_line(price="25.95", rrp=None, member="22.06")
    assert glove.rrp == Decimal("25.95") and round(glove.discount_pct) == 15  # vs the public price
    shoe = make_line(price="129.95", rrp="180", member="89")
    assert shoe.rrp == Decimal("180") and round(shoe.discount_pct) == 51  # vs Shopify's compare-at
    full = make_line(price="40", rrp=None, member=None)
    assert full.rrp is None and not full.has_rrp


def test_portal_down_falls_back_to_the_rules(settings, make_line):
    polo = make_line("POLO", units=5, price="40", rrp="80", members_units=5)
    stats = {}
    assert apply_member_prices([polo], MemberPriceSource(_settings(settings), _client(status=500)), stats) == 0
    assert stats["portal"].startswith("unavailable") and polo.members_units == 5
    assert allocation.featured_pool([polo]) == [polo]
    stats = {}
    assert apply_member_prices([polo], MemberPriceSource(_settings(settings, key=None)), stats) == 0
    assert stats["portal"] == "not configured"


def test_the_claim_matches_the_real_discounts(make_line):
    small = [make_line(price="25.95", rrp=None, member="22.06") for _ in range(4)]  # 15% off
    assert validators.honest_claim(small, 0.30) == "up to 15% off for members"
    big = [make_line(sku=f"S{i}", price="100", rrp=None, member=str(m)) for i, m in enumerate([45, 50, 60, 90])]
    assert validators.honest_claim(big, 0.30) == "up to 50% off for members"  # 2 of 4 lines reach 50%
    assert validators.honest_claim([make_line(price="40", rrp=None, member="38")], 0.30) == "member-only prices"
    assert validators.honest_claim([], 0.30) == "member-only prices"


def test_unsupported_percent_off_claims_fail(make_line):
    pool = [make_line(price="25.95", rrp=None, member="22.06") for _ in range(4)]  # all 15% off
    assert validators.percent_off_check("Members get 40–50% off this week", pool, 0.30)
    assert validators.percent_off_check("Save 15% off for members", pool, 0.30) == []
    assert validators.percent_off_check("up to 15% off for members", pool, 0.30) == []  # up_to check's job


def test_dashboard_shows_live_paid_members(settings, tmp_path):
    from evo_tiktok.db import Database

    db = Database(f"sqlite:///{tmp_path / 'd.db'}")
    db.migrate()
    days = [{"date": f"2026-10-0{d}", "totalPaid": 240 + d, "started": 2, "cancelled": 1} for d in range(1, 9)]

    def handler(request):
        if request.url.path.endswith("customer-metrics"):
            return httpx.Response(200, json={"memberships": {"totalPaid": 247}, "customers": {"total": 1}})
        return httpx.Response(200, json={"days": days})

    m = dashboard.members_numbers(db, _settings(settings), httpx.Client(transport=httpx.MockTransport(handler)))
    db.close()
    assert m["paid_now"] == 247
    rows, _ = dashboard.sheet_rows({"updated": "x", "jobs": [], "stock": None, "members": m, "content": None,
                                    "replies": {"by_status": {}, "waiting": 0, "need_a_closer_look": 0},
                                    "report": None})
    assert ["Paid members right now (live)", "247"] in rows
