"""Read-only data sources for the weekly report.

- Organic per-post metrics: a CSV export (pilot) or Windsor.ai's TikTok Organic
  connector. Metricool can be added here if the Advanced plan is bought.
- Paid TikTok Ads: Windsor.ai's TikTok connector, or a CSV export.
- Members: the members portal's reporting API (paid starts and cancellations per
  day), Shopify (customer and order searches) or numbers typed in by hand.

Everything here only reads. There is no code that changes ad spend.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from .config import Settings

log = logging.getLogger(__name__)

WINDSOR_URL = "https://connectors.windsor.ai"


@dataclass
class PostMetrics:
    post_id: str
    views: int = 0
    avg_watch_seconds: float | None = None
    completion_rate: float | None = None  # 0-1
    shares: int = 0
    comments: int = 0
    follows: int = 0
    profile_visits: int = 0
    link_clicks: int = 0


@dataclass
class PaidRow:
    post_id: str  # TikTok video ID when the ad is a Spark Ad of our post, else ""
    ad_name: str
    spend: Decimal
    impressions: int
    results: int


@dataclass
class Members:
    new_members: int
    member_orders: int | None
    member_revenue: Decimal | None
    source: str
    # From the members portal only
    cancelled: int | None = None
    paid_at_start: int | None = None
    paid_at_end: int | None = None
    cancel_rate: float | None = None
    covers_to: date | None = None  # last day counted; the portal stops at yesterday


class MembersAPIError(RuntimeError):
    pass


def _num(value, kind=float):
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").replace("£", "").replace("%", "").strip()
    if kind is Decimal:
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    try:
        return kind(float(text))
    except ValueError:
        return None


def _seconds(value) -> float | None:
    """"12.3", "12.3s" or "0:12" to seconds."""
    if value in (None, ""):
        return None
    text = str(value).strip().rstrip("s")
    if ":" in text:
        parts = [float(p) for p in text.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return _num(text)


def _rate(value) -> float | None:
    """Completion as 0-1, from "45%", "45" or "0.45"."""
    n = _num(value)
    if n is None:
        return None
    return n / 100 if (n > 1 or "%" in str(value)) else n


def _post_id(value) -> str:
    m = re.search(r"/video/(\w+)", str(value or ""))
    return m.group(1) if m else str(value or "").strip()


# Header aliases for CSV exports (TikTok Studio, Metricool, hand-made sheets).
ORGANIC_ALIASES = {
    "post_id": {"post_id", "video_id", "video id", "video link", "video url", "post url", "link", "url", "id"},
    "views": {"views", "video views", "video_views", "plays"},
    "avg_watch_seconds": {"avg_watch_seconds", "average watch time", "avg watch time", "average_time_watched",
                          "average time watched"},
    "completion_rate": {"completion_rate", "completion", "full video views %", "watched full video",
                        "full_video_watched_rate", "completion rate"},
    "shares": {"shares"},
    "comments": {"comments"},
    "follows": {"follows", "new followers", "new_followers", "followers gained"},
    "profile_visits": {"profile_visits", "profile views", "profile_views", "profile visits"},
    "link_clicks": {"link_clicks", "link clicks", "bio link clicks"},
}
PAID_ALIASES = {
    "post_id": {"post_id", "tiktok_item_id", "video_id", "video id", "post"},
    "ad_name": {"ad_name", "ad name", "ad"},
    "spend": {"spend", "cost", "amount spent"},
    "impressions": {"impressions"},
    "results": {"results", "conversions", "result"},
}


def _read_csv(path: str, aliases: dict[str, set[str]]) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        mapping = {}
        for field_name, names in aliases.items():
            for h in reader.fieldnames or []:
                if h.strip().lower() in names:
                    mapping[field_name] = h
                    break
        return [{k: row.get(h) for k, h in mapping.items()} for row in reader]


def organic_from_rows(rows: list[dict]) -> list[PostMetrics]:
    out = []
    for r in rows:
        pid = _post_id(r.get("post_id"))
        if not pid:
            continue
        out.append(
            PostMetrics(
                post_id=pid,
                views=_num(r.get("views"), int) or 0,
                avg_watch_seconds=_seconds(r.get("avg_watch_seconds")),
                completion_rate=_rate(r.get("completion_rate")),
                shares=_num(r.get("shares"), int) or 0,
                comments=_num(r.get("comments"), int) or 0,
                follows=_num(r.get("follows"), int) or 0,
                profile_visits=_num(r.get("profile_visits"), int) or 0,
                link_clicks=_num(r.get("link_clicks"), int) or 0,
            )
        )
    return out


def paid_from_rows(rows: list[dict]) -> list[PaidRow]:
    return [
        PaidRow(
            post_id=_post_id(r.get("post_id")),
            ad_name=str(r.get("ad_name") or ""),
            spend=_num(r.get("spend"), Decimal) or Decimal("0"),
            impressions=_num(r.get("impressions"), int) or 0,
            results=_num(r.get("results"), int) or 0,
        )
        for r in rows
    ]


class Windsor:
    """GET-only client for Windsor.ai connector data."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.key = settings.secret("WINDSOR_API_KEY")
        self.cfg = settings["report"]["windsor"]
        self.http = http or httpx.Client(timeout=120)

    def _get(self, connector: str, fields: dict[str, str | None], start: date, end: date) -> list[dict]:
        wanted = {ours: theirs for ours, theirs in fields.items() if theirs}
        resp = self.http.get(
            f"{WINDSOR_URL}/{connector}",
            params={
                "api_key": self.key,
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
                "fields": ",".join(sorted(set(wanted.values()))),
            },
        )
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("data", []) if isinstance(body, dict) else body
        return [{ours: row.get(theirs) for ours, theirs in wanted.items()} for row in rows]

    def organic(self, start: date, end: date) -> list[PostMetrics]:
        rows = self._get(self.cfg["organic_connector"], self.cfg["organic_fields"], start, end)
        return merge_organic(organic_from_rows(rows))

    def paid(self, start: date, end: date) -> list[PaidRow]:
        return paid_from_rows(self._get(self.cfg["paid_connector"], self.cfg["paid_fields"], start, end))


def merge_organic(rows: list[PostMetrics]) -> list[PostMetrics]:
    """One row per post. Daily rows are summed; watch time and completion are
    view-weighted averages."""
    by_post: dict[str, list[PostMetrics]] = {}
    for r in rows:
        by_post.setdefault(r.post_id, []).append(r)
    out = []
    for pid, rs in by_post.items():
        if len(rs) == 1:
            out.append(rs[0])
            continue
        views = sum(r.views for r in rs)

        def weighted(attr):
            pairs = [(getattr(r, attr), r.views) for r in rs if getattr(r, attr) is not None]
            if not pairs:
                return None
            w = sum(v for _, v in pairs)
            return sum(x * v for x, v in pairs) / w if w else sum(x for x, _ in pairs) / len(pairs)

        out.append(
            PostMetrics(
                pid, views, weighted("avg_watch_seconds"), weighted("completion_rate"),
                sum(r.shares for r in rs), sum(r.comments for r in rs), sum(r.follows for r in rs),
                sum(r.profile_visits for r in rs), sum(r.link_clicks for r in rs),
            )
        )
    return out


def load_organic(settings: Settings, args, start: date, end: date) -> list[PostMetrics]:
    if args.organic_csv:
        return merge_organic(organic_from_rows(_read_csv(args.organic_csv, ORGANIC_ALIASES)))
    source = settings["report"]["organic_source"]
    if source == "windsor":
        return Windsor(settings).organic(start, end)
    raise ValueError(f"Organic source '{source}' needs --organic-csv (or set report.organic_source: windsor)")


def load_paid(settings: Settings, args, start: date, end: date) -> list[PaidRow]:
    if args.paid_csv:
        return paid_from_rows(_read_csv(args.paid_csv, PAID_ALIASES))
    source = settings["report"]["paid_source"]
    if source == "windsor":
        return Windsor(settings).paid(start, end)
    if source == "none":
        return []
    raise ValueError(f"Paid source '{source}' needs --paid-csv")


MEMBERS_QUERY = """
query members($customers: String!, $orders: String!, $cAfter: String, $oAfter: String) {
  customers(first: 250, query: $customers, after: $cAfter) { edges { cursor } pageInfo { hasNextPage endCursor } }
  orders(first: 250, query: $orders, after: $oAfter) {
    edges { node { totalPriceSet { shopMoney { amount } } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def load_members(settings: Settings, args, start: date, end: date, shopify=None, http=None) -> Members | None:
    if args.new_members is not None:
        return Members(args.new_members, args.member_orders or 0, Decimal(str(args.member_revenue or 0)), "manual")
    cfg = settings["members"]
    if cfg["source"] == "members_api":
        if not settings.secret("MEMBERS_REPORTING_API_KEY", required=False):
            log.warning("MEMBERS_REPORTING_API_KEY not set: no members numbers this week")
            return None
        return load_members_api(settings, start, end, http)
    if cfg["source"] != "shopify":
        return None
    from .shopify import ShopifyClient

    client = shopify or ShopifyClient(settings)
    window = f"created_at:>={start.isoformat()} created_at:<={end.isoformat()}"
    variables = {"customers": f"{cfg['customer_query']} {window}", "orders": f"{cfg['order_query']} {window}",
                 "cAfter": None, "oAfter": None}
    customers = orders = 0
    revenue = Decimal("0")
    more_c = more_o = True
    while more_c or more_o:
        data = client._graphql(MEMBERS_QUERY, variables)
        if more_c:
            customers += len(data["customers"]["edges"])
            more_c = data["customers"]["pageInfo"]["hasNextPage"]
            variables["cAfter"] = data["customers"]["pageInfo"]["endCursor"]
        if more_o:
            edges = data["orders"]["edges"]
            orders += len(edges)
            revenue += sum(Decimal(e["node"]["totalPriceSet"]["shopMoney"]["amount"]) for e in edges)
            more_o = data["orders"]["pageInfo"]["hasNextPage"]
            variables["oAfter"] = data["orders"]["pageInfo"]["endCursor"]
    return Members(customers, orders, revenue, "shopify")


def load_members_api(settings: Settings, start: date, end: date, http=None, today: date | None = None) -> Members:
    """Paid starts and cancellations for the week from the members portal.

    ``GET /api/reporting/membership-history?days=N`` returns one entry per
    London day, oldest first, ending yesterday. Since 22 Sep 2026 every new
    start lands in annual_pro, so only the totals are used, never the tier mix.
    Cancellation rate = cancellations in the week / paid members at its start.
    """
    from zoneinfo import ZoneInfo

    base = (settings["members"].get("api_url") or "").rstrip("/")
    if not base:
        raise MembersAPIError("Set members.api_url in settings")
    key = settings.secret("MEMBERS_REPORTING_API_KEY")
    today = today or datetime.now(ZoneInfo(settings["timezone"])).date()
    days = max(1, min(365, (today - start).days + 1))  # back to the day before the week starts
    client = http or httpx.Client(timeout=60)
    resp = client.get(
        f"{base}/api/reporting/membership-history",
        params={"days": days},
        headers={"Authorization": f"Bearer {key}"},
    )
    if resp.status_code == 401:
        raise MembersAPIError("Members portal refused the key (401): check MEMBERS_REPORTING_API_KEY")
    if resp.status_code >= 400:
        raise MembersAPIError(f"Members portal error {resp.status_code}: {resp.text[:200]}")
    by_date = {date.fromisoformat(d["date"]): d for d in resp.json().get("days", [])}
    week = [by_date[d] for d in sorted(by_date) if start <= d <= end]
    if not week:
        raise MembersAPIError(f"Members portal has no days between {start} and {end}")
    started = sum(int(d["started"]) for d in week)
    cancelled = sum(int(d["cancelled"]) for d in week)
    before = by_date.get(start - timedelta(days=1))
    first = week[0]
    # Tier changes count as neither a start nor a cancellation, so this rebuilds the day before.
    paid_at_start = int(before["totalPaid"]) if before else int(first["totalPaid"]) - int(first["started"]) + int(
        first["cancelled"]
    )
    paid_at_end = int(week[-1]["totalPaid"])
    return Members(
        new_members=started,
        member_orders=None,
        member_revenue=None,
        source="members portal",
        cancelled=cancelled,
        paid_at_start=paid_at_start,
        paid_at_end=paid_at_end,
        cancel_rate=round(cancelled / paid_at_start, 4) if paid_at_start else None,
        covers_to=date.fromisoformat(week[-1]["date"]),
    )
