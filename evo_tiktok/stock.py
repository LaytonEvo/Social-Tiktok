"""Stock snapshot job: ``python -m evo_tiktok.stock [--dry-run] [--from-jsonl FILE]``.

Reads the full catalogue from Shopify (read-only), applies the exclusions and
the allocation rules, and stores a ``stock_snapshot`` the other jobs read.
Dry-run reads Shopify but writes nothing except the local run log.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

from . import allocation
from .member_prices import MemberPriceSource, apply_member_prices
from .models import StockLine
from .runner import JobContext, run_job
from .shopify import ShopifyClient, parse_bulk_records

log = logging.getLogger(__name__)


def load_records(ctx: JobContext):
    if ctx.args.from_jsonl:
        with open(ctx.args.from_jsonl, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    return list(ShopifyClient(ctx.settings).export_variants_jsonl())


def build_snapshot(records, settings) -> tuple[list[StockLine], dict]:
    lines, stats = parse_bulk_records(records, settings)
    for line in lines:
        allocation.allocate(line, settings)
    return lines, stats


def summarise(lines: list[StockLine], stats: dict) -> dict:
    in_stock = [l for l in lines if l.units_total > 0]
    pool = allocation.featured_pool(lines)
    footwear = [l for l in pool if l.category == allocation.FOOTWEAR]
    by_size = Counter()
    for l in footwear:
        by_size[l.size] += l.members_units
    return {
        **stats,
        "lines_in_stock": len(in_stock),
        "units_in_stock": sum(l.units_total for l in in_stock),
        "members_routed_lines": sum(1 for l in lines if l.members_routed),
        "featured_lines": len(pool),
        "featured_units": sum(l.members_units for l in pool),
        "featured_by_category": dict(Counter(l.category for l in pool)),
        "featured_footwear_units_by_size": dict(by_size.most_common()),
        "routed_but_no_rrp": sum(1 for l in lines if l.members_routed and l.units_total > 0 and not l.has_rrp),
    }


def save_snapshot(db, snapshot_id: str, taken_at: str, lines: list[StockLine]) -> None:
    featured = {id(l) for l in allocation.featured_pool(lines)}
    rows = [
        (
            snapshot_id, taken_at, l.sku, l.variant_id, l.product_title, l.variant_title, l.vendor,
            l.product_type, l.status, l.category, l.season, l.age, l.size, l.size_band, l.junior,
            str(l.price), str(l.compare_at_price) if l.compare_at_price is not None else None,
            str(l.member_price) if l.member_price is not None else None, str(l.unit_cost),
            l.cost_estimated, l.units_by_location.get("Warehouse", 0),
            l.units_by_location.get("Burley Golf Club", 0), l.units_total, l.members_units,
            id(l) in featured, "; ".join(l.flags),
        )
        for l in lines
        if l.units_total > 0
    ]
    db.executemany(
        "INSERT INTO stock_snapshot (snapshot_id, taken_at, sku, variant_id, product_title, variant_title, "
        "vendor, product_type, status, category, season, age, size, size_band, junior, price, "
        "compare_at_price, member_price, unit_cost, cost_estimated, units_warehouse, units_burley, "
        "units_total, members_units, featured, flags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()


def body(ctx: JobContext) -> None:
    lines, stats = build_snapshot(load_records(ctx), ctx.settings)
    stats["member_prices"] = apply_member_prices(lines, MemberPriceSource(ctx.settings))
    ctx.summary.update(summarise(lines, stats))
    log.info("Snapshot summary: %s", json.dumps(ctx.summary, indent=2))
    db = ctx.db()
    if db is None:
        log.info("Dry run: snapshot not saved")
        return
    save_snapshot(db, ctx.run_id, datetime.now(timezone.utc).isoformat(), lines)
    ctx.summary["saved_rows"] = sum(1 for l in lines if l.units_total > 0)


def add_args(parser) -> None:
    parser.add_argument("--from-jsonl", help="read a saved bulk export instead of calling Shopify")


def main(argv=None) -> int:
    return run_job("stock", body, argv, add_args)


if __name__ == "__main__":
    raise SystemExit(main())
