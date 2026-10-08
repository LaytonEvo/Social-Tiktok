"""Reply drafter (daily): ``python -m evo_tiktok.replies [--dry-run] [--from-csv FILE]``.

Reads new TikTok comments (from Karin's inbox sheet tab or a CSV), classifies
them, drafts replies from the stock snapshot only, validates every draft and
writes a review queue. A person posts every reply. Nothing here can post to
TikTok: there is no TikTok write code in this project.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import allocation, stock, validators
from .config import DOCS_DIR, ROOT, Settings
from .llm import ClaudeJSON, JSONModel, load_prompt
from .models import StockLine
from .publish import publish
from .runner import JobContext, run_job

log = logging.getLogger(__name__)

CATEGORIES = ("question-sizing", "question-stock", "question-price", "positive", "complaint", "spam", "other")
LLM_DRAFTED = {"question-sizing", "question-stock", "positive", "other"}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "confident": {"type": "boolean"},
                },
                "required": ["comment_id", "category", "confident"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "needs_human_check": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["reply", "needs_human_check", "reason"],
    "additionalProperties": False,
}

# Safety nets that don't depend on the model.
COMPLAINT_RE = re.compile(
    r"\b(?:refund|faulty|broken|damaged|never (?:arrived|came|received)|not (?:arrived|received)|"
    r"still waiting|wrong (?:size|item|order)|disappointed|rubbish|terrible|awful|scam|rip[- ]?off|"
    r"complain\w*|worst|no reply|ignored|cancel(?:led)? my order|where(?:'s| is) my order)\b",
    re.IGNORECASE,
)
SPAM_RE = re.compile(
    r"https?://|www\.|\b\w+\.(?:com|net|io|xyz|ru)/|\bfollow (?:me|back)\b|\bf4f\b|\bcheck (?:my|out my) "
    r"(?:page|profile|bio)\b|\bcrypto|\bbitcoin|\bforex\b|\binvest(?:ment|ing)\b|\bpromo(?:te)? your\b",
    re.IGNORECASE,
)
RESTOCK_RE = re.compile(
    r"\b(?:back in stock|restock\w*|more (?:coming|arriving)|due in)\b.{0,30}\b(?:on|by|next|in|this|tomorrow|soon)\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"\b(?:sold out|out of stock|don't|do not|haven't|have not|no longer|none left|all gone|gone|not|no|sorry)\b",
    re.IGNORECASE,
)
CLAIMS_STOCK_RE = re.compile(r"\b(?:we've got|we have|in stock|available|still got|got (?:some|a few|plenty))\b", re.IGNORECASE)

FOOT_SIZE_RE = re.compile(
    r"\b(?:uk\s?|size\s?|in an?\s)(\d{1,2}(?:\.5|½)?)\b|\b(\d{1,2}(?:\.5|½)?)s\b", re.IGNORECASE
)
APPAREL_SIZE_RE = re.compile(r"\b(XXS|XS|S|M|L|XL|XXL|2XL|3XL)\b")
APPAREL_WORDS = {"small": "S", "medium": "M", "large": "L", "extra large": "XL", "x-large": "XL"}
APPAREL_WORD_RE = re.compile(r"\b(extra large|x-large|small|medium|large)\b", re.IGNORECASE)


@dataclass
class Comment:
    comment_id: str
    post_id: str
    author: str
    text: str
    created_at: str = ""


@dataclass
class Draft:
    comment: Comment
    category: str
    reply: str = ""
    needs_human_check: bool = False
    reason: str = ""
    status: str = "drafted"  # drafted / hide
    facts: dict = field(default_factory=dict)


# ------------------------------------------------------------------- input

HEADER_ALIASES = {
    "comment_id": {"comment_id", "comment id", "id", "commentid"},
    "post_id": {"post_id", "post id", "video_id", "video id", "post", "video", "post url", "video url", "video link", "post link"},
    "text": {"text", "comment", "comment text", "content", "message"},
    "author": {"author", "username", "user", "handle", "author handle", "commenter"},
    "created_at": {"created_at", "created", "date", "time", "timestamp"},
}


def _post_ref(value: str) -> str:
    m = re.search(r"/video/(\d+)", value or "")
    return m.group(1) if m else (value or "").strip()


def parse_rows(rows: list[list[str]]) -> list[Comment]:
    """Turn a header row plus data rows (CSV or sheet) into comments."""
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    index = {}
    for field_name, aliases in HEADER_ALIASES.items():
        for i, h in enumerate(header):
            if h in aliases:
                index[field_name] = i
                break
    if "text" not in index:
        raise ValueError(f"Comments need a text/comment column; got headers {rows[0]}")
    comments = []
    for row in rows[1:]:
        get = lambda f: (row[index[f]].strip() if f in index and index[f] < len(row) and row[index[f]] else "")
        text = get("text")
        if not text:
            continue
        post = _post_ref(get("post_id"))
        author = get("author").lstrip("@")
        cid = get("comment_id") or "h" + hashlib.sha1(f"{post}|{author}|{text}".encode()).hexdigest()[:16]
        comments.append(Comment(cid, post, author, text, get("created_at")))
    return comments


def load_comments(ctx: JobContext) -> list[Comment]:
    if ctx.args.from_csv:
        with open(ctx.args.from_csv, encoding="utf-8-sig", newline="") as f:
            return parse_rows(list(csv.reader(f)))
    sheet_id = ctx.settings["outputs"].get("reply_sheet_id")
    if not sheet_id:
        raise ValueError("No comments source: pass --from-csv or set outputs.reply_sheet_id (Inbox tab)")
    from .gsheets import Sheets

    return parse_rows(Sheets(ctx.settings).read(sheet_id, ctx.settings["replies"]["inbox_range"]))


def already_queued(db, ids: list[str]) -> set[str]:
    if db is None or not ids:
        return set()
    found: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ", ".join("?" for _ in chunk)
        found |= {r[0] for r in db.query(f"SELECT comment_id FROM reply_queue WHERE comment_id IN ({marks})", chunk)}
    return found


# ------------------------------------------------------------ classification


def classify(llm: JSONModel, comments: list[Comment]) -> dict[str, tuple[str, bool]]:
    """Category and confidence per comment. Strong spam and complaint patterns
    win over the model, and a model-detected complaint always stands."""
    out: dict[str, tuple[str, bool]] = {}
    to_model = []
    for c in comments:
        if SPAM_RE.search(c.text):
            out[c.comment_id] = ("spam", True)
        elif COMPLAINT_RE.search(c.text):
            out[c.comment_id] = ("complaint", True)
        else:
            to_model.append(c)
    for i in range(0, len(to_model), 40):
        batch = to_model[i : i + 40]
        prompt = load_prompt(
            "comment_classifier",
            {"COMMENTS_JSON": json.dumps([{"comment_id": c.comment_id, "text": c.text} for c in batch], ensure_ascii=False)},
        )
        data, _ = llm.complete_json(prompt.system, [{"role": "user", "content": prompt.user}], CLASSIFY_SCHEMA, 4000)
        for r in data["results"]:
            if r["category"] in CATEGORIES:
                out[r["comment_id"]] = (r["category"], bool(r["confident"]))
    for c in comments:
        out.setdefault(c.comment_id, ("other", False))  # the model skipped it
    return out


# ------------------------------------------------------------- stock facts


def extract_sizes(text: str) -> list[str]:
    """Footwear sizes as "UK9.5" and apparel sizes as "M", in order of mention."""
    found: list[str] = []
    for m in FOOT_SIZE_RE.finditer(text):
        raw = (m.group(1) or m.group(2)).replace("½", ".5")
        value = float(raw)
        if 3 <= value <= 14:
            found.append(f"UK{value:g}")
    found += APPAREL_SIZE_RE.findall(text)
    found += [APPAREL_WORDS[w.lower()] for w in APPAREL_WORD_RE.findall(text)]
    return list(dict.fromkeys(found))


def _label(line: StockLine) -> str:
    """Size as extract_sizes() writes it: "UK9.5" for footwear, else the Shopify size."""
    if line.category == allocation.FOOTWEAR:
        value = allocation._foot_size(line.size or "")
        if value is not None:
            return f"UK{value:g}"
    return (line.size or "One size").strip()


def post_lookup(db, post_id: str) -> dict | None:
    if db is None or not post_id:
        return None
    rows = db.query(
        "SELECT format, hook, caption, featured_skus FROM content_log WHERE tiktok_post_id = ? LIMIT 1", (post_id,)
    )
    if not rows:
        return None
    fmt, hook, caption, skus = rows[0]
    return {"format": fmt, "hook": hook, "caption": caption, "featured_skus": json.loads(skus or "[]")}


def stock_facts(comment: Comment, post: dict | None, lines: list[StockLine]) -> dict:
    """What we may say about stock, from the snapshot only."""
    asked = extract_sizes(comment.text)
    if post is None:
        return {"post_known": False, "products": [], "asked_sizes": [{"size": s, "in_stock": None} for s in asked]}
    by_sku = {l.sku: l for l in lines}
    titles = {by_sku[s].product_title for s in post["featured_skus"] if s in by_sku}
    # A featured SKU that has sold out is missing from the snapshot; keep its product by title.
    products: dict[str, dict[str, int]] = defaultdict(dict)
    for line in lines:
        if line.product_title in titles and line.units_total > 0:
            products[line.product_title][_label(line)] = products[line.product_title].get(_label(line), 0) + line.units_total
    asked_facts = []
    for size in asked:
        units = sum(sizes.get(size, 0) for sizes in products.values())
        asked_facts.append({"size": size, "in_stock": units > 0, "units": units})
    return {
        "post_known": True,
        "products": [{"product": t, "sizes_in_stock": dict(sorted(s.items()))} for t, s in sorted(products.items())],
        "asked_sizes": asked_facts,
    }


# --------------------------------------------------------------- validation


def _quoted_rule(prefix: str) -> str:
    """A quoted line from the reply rules in CONTENT_RULES.md, e.g. the price reply."""
    for line in (DOCS_DIR / "CONTENT_RULES.md").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"- {prefix}"):
            m = re.search(r"\"([^\"]+)\"", line)
            if m:
                return m.group(1)
    raise ValueError(f"No quoted '{prefix}' rule in CONTENT_RULES.md")


def validate_reply(reply: str, facts: dict, settings: Settings) -> list[str]:
    cfg = settings["replies"]
    errors = []
    if len(reply) > int(cfg.get("max_chars", 150)):
        errors.append(f"reply is {len(reply)} characters; max {cfg.get('max_chars', 150)}")
    errors += validators.brand_price_check(reply, settings.restricted_brands, settings.brand_price_window)
    errors += validators.no_price_check(reply)
    if RESTOCK_RE.search(reply):
        errors.append("never promise a restock date")
    for name in cfg.get("competitors") or []:
        if re.search(rf"\b{re.escape(name)}\b", reply, re.IGNORECASE):
            errors.append(f"never mention competitors ({name})")
    errors += stock_consistency(reply, facts)
    return errors


def stock_consistency(reply: str, facts: dict) -> list[str]:
    """Sizes in the reply must agree with the snapshot facts."""
    if not facts:
        return []
    asked = {a["size"]: a for a in facts.get("asked_sizes", [])}
    known = set(asked)
    for p in facts.get("products", []):
        known |= set(p["sizes_in_stock"])
    errors = []
    sentences = re.split(r"(?<=[.!?])\s+", reply)
    for sentence in sentences:
        negated = bool(NEGATION_RE.search(sentence))
        for size in extract_sizes(sentence):
            if size not in known:
                errors.append(f"mentions {size}, which isn't in the stock facts")
                continue
            fact = asked.get(size)
            in_stock = fact["in_stock"] if fact else True  # listed under products = in stock
            if in_stock is None and not negated:
                errors.append(f"says {size} is available but we couldn't check this post's stock")
            elif in_stock is False and not negated:
                errors.append(f"implies {size} is available, but the snapshot has none")
            elif in_stock is True and re.search(r"sold out|out of stock|none left|all gone", sentence, re.IGNORECASE):
                errors.append(f"says {size} is sold out, but the snapshot has {fact['units'] if fact else 'some'}")
        if not facts.get("post_known") and CLAIMS_STOCK_RE.search(sentence) and not negated:
            errors.append("claims stock without stock facts for this post")
    return errors


# ------------------------------------------------------------------ drafting


def post_summary(post: dict | None) -> str:
    if post is None:
        return "Unknown post (not matched to our content log)"
    return f"{post['format']} video. Hook: {post['hook']!r}. Caption: {post['caption']!r}"


def draft_one(llm: JSONModel, settings: Settings, comment: Comment, category: str, post: dict | None, facts: dict) -> Draft:
    prompt = load_prompt(
        "reply_drafter",
        {
            "POST_SUMMARY": post_summary(post),
            "AUTHOR": comment.author or "someone",
            "COMMENT_TEXT": comment.text,
            "CATEGORY": category,
            "STOCK_FACTS_JSON": json.dumps(facts, ensure_ascii=False),
        },
    )
    messages: list[dict] = [{"role": "user", "content": prompt.user}]
    errors: list[str] = []
    for _attempt in (1, 2):
        data, content = llm.complete_json(prompt.system, messages, DRAFT_SCHEMA, 1000)
        reply = data["reply"].strip()
        errors = validate_reply(reply, facts, settings)
        if not errors:
            return Draft(comment, category, reply, bool(data["needs_human_check"]), data["reason"], facts=facts)
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "That reply failed our checks:\n- " + "\n- ".join(errors) + "\nWrite it again."},
        ]
    # Don't fail the whole day's queue for one comment: leave it for a person.
    return Draft(comment, category, "", True, "No safe draft: " + "; ".join(errors), facts=facts)


def build_drafts(llm: JSONModel, settings: Settings, comments: list[Comment], lines: list[StockLine], db) -> list[Draft]:
    cfg = settings["replies"]
    price_reply = _quoted_rule("Price questions:")
    for fixed in (price_reply, cfg["complaint_holding_reply"]):  # a bad config edit fails the run
        validators.require(validate_reply(fixed, {}, settings))
    categories = classify(llm, comments)
    drafts = []
    for c in comments:
        category, confident = categories[c.comment_id]
        post = post_lookup(db, c.post_id)
        if category == "spam":
            drafts.append(Draft(c, category, "", False, "Spam: hide the comment, no reply", status="hide"))
            continue
        if category == "complaint":
            drafts.append(Draft(c, category, cfg["complaint_holding_reply"], True,
                                "Complaint: holding reply only. Karin to DM or email and resolve"))
            continue
        if category == "question-price":
            drafts.append(Draft(c, category, price_reply, False, "Price question: standard Members Club reply"))
            continue
        facts = stock_facts(c, post, lines)
        d = draft_one(llm, settings, c, category, post, facts)
        if category in {"question-sizing", "question-stock"} and not facts["post_known"]:
            d.needs_human_check = True
            d.reason = (d.reason + "; " if d.reason else "") + "post not in content log, so stock wasn't checked"
        if not confident:
            d.needs_human_check = True
            d.reason = (d.reason + "; " if d.reason else "") + "category uncertain"
        drafts.append(d)
    return drafts


# -------------------------------------------------------------------- output

QUEUE_HEADER = ["queued_at", "comment_date", "post_id", "author", "comment", "category", "draft_reply",
                "check_before_posting", "reason", "status", "comment_id"]


def queue_rows(drafts: list[Draft], queued_at: str) -> list[list]:
    return [
        [queued_at, d.comment.created_at, d.comment.post_id, "@" + d.comment.author if d.comment.author else "",
         d.comment.text, d.category, d.reply, "YES" if d.needs_human_check else "", d.reason, d.status,
         d.comment.comment_id]
        for d in drafts
    ]


def write_csv(path: Path, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(QUEUE_HEADER)
        w.writerows(rows)


def save_queue(db, drafts: list[Draft], queued_at: str) -> None:
    for d in drafts:
        db.execute(
            "INSERT INTO reply_queue (comment_id, post_id, author, comment_text, category, draft, needs_human_check, "
            "reason, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (comment_id) DO NOTHING",
            (d.comment.comment_id, d.comment.post_id, d.comment.author, d.comment.text, d.category, d.reply,
             d.needs_human_check, d.reason, d.status, queued_at),
        )
    db.commit()


def doc_markdown(settings: Settings, drafts: list[Draft], queued_at: str) -> str:
    """The entry for the shared Google Doc: counts, complaints, and drafts to check."""
    counts = Counter(d.category for d in drafts)
    flagged = [d for d in drafts if d.needs_human_check and d.category != "complaint"]
    complaints = [d for d in drafts if d.category == "complaint"]
    out = [
        f"# Reply drafts: {queued_at}",
        f"**{len(drafts)} new comment(s):** " + ", ".join(f"{n} {c}" for c, n in counts.most_common()) + ".",
        f"**{len(flagged) + len(complaints)} need a person to check before posting.** "
        "Replies are never posted automatically.",
    ]
    if complaints:
        out.append(f"## Complaints for Karin ({len(complaints)})")
        out += [f"- **@{d.comment.author}:** “{d.comment.text}”" for d in complaints]
    if flagged:
        out.append("## Drafts to check")
        out += [
            f"- **@{d.comment.author}:** “{d.comment.text}” → {d.reply or '(no draft)'} ({d.reason})" for d in flagged
        ]
    sheet_id = settings["outputs"].get("reply_sheet_id")
    if sheet_id:
        out.append(f"All drafts are in the Reply queue tab: https://docs.google.com/spreadsheets/d/{sheet_id}")
    return "\n".join(out) + "\n"


def slack_text(settings: Settings, drafts: list[Draft]) -> str:
    counts = Counter(d.category for d in drafts)
    flagged = sum(d.needs_human_check for d in drafts)
    lines = [
        f"*{len(drafts)} new TikTok comment(s)* drafted for review: "
        + ", ".join(f"{n} {c}" for c, n in counts.most_common()),
        f"{flagged} need a person to check before posting. Replies are never posted automatically.",
    ]
    complaints = [d for d in drafts if d.category == "complaint"]
    if complaints:
        who = settings["outputs"].get("complaint_alert_slack_user_id")
        lines.append(f":warning: {'<@' + who + '> ' if who else ''}{len(complaints)} complaint(s) to handle personally:")
        lines += [f"• @{d.comment.author}: “{d.comment.text[:140]}”" for d in complaints]
    return "\n".join(lines)


def body(ctx: JobContext) -> None:
    settings = ctx.settings
    if not ctx.args.from_csv and not settings["outputs"].get("reply_sheet_id"):
        # Nothing to read until the reply Sheet exists; that's a setup step, not a failure.
        ctx.summary["skipped"] = "no reply Sheet yet (set outputs.reply_sheet_id)"
        log.info("No reply Sheet set up yet: nothing to draft")
        return
    comments = load_comments(ctx)
    db = ctx.read_db()
    seen = already_queued(db, [c.comment_id for c in comments])
    new = list({c.comment_id: c for c in comments if c.comment_id not in seen}.values())
    ctx.summary.update(comments_read=len(comments), new_comments=len(new))
    if not new:
        log.info("No new comments")
        return
    lines = stock.current_stock(ctx)
    llm = ctx.llm or ClaudeJSON(settings, "replies")
    drafts = build_drafts(llm, settings, new, lines, db)
    queued_at = datetime.now(ZoneInfo(settings["timezone"])).strftime("%Y-%m-%d %H:%M")
    rows = queue_rows(drafts, queued_at)
    out = ROOT / settings["outputs"].get("reply_dir", "out/replies") / f"reply_queue_{queued_at[:10]}_{ctx.run_id}.csv"
    write_csv(out, rows)
    ctx.summary.update(
        categories=dict(Counter(d.category for d in drafts)),
        needs_human_check=sum(d.needs_human_check for d in drafts),
        complaints=sum(d.category == "complaint" for d in drafts),
        queue_csv=str(out),
    )
    write_db = ctx.db()
    if write_db is None:
        log.info("Dry run: queue written to %s; nothing saved, sent to the sheet or published", out)
        return
    save_queue(write_db, drafts, datetime.now(timezone.utc).isoformat())
    sheet_id = settings["outputs"].get("reply_sheet_id")
    if sheet_id:
        from .gsheets import Sheets

        ctx.summary["sheet_rows"] = Sheets(settings).append(sheet_id, settings["replies"]["queue_range"], rows)
    ctx.summary["published"] = publish(settings, doc_markdown(settings, drafts, queued_at), slack_text(settings, drafts), [out])


def add_args(parser) -> None:
    parser.add_argument("--from-csv", help="read comments from a CSV export instead of the inbox sheet")
    parser.add_argument("--from-jsonl", help="read stock from a saved Shopify bulk export")


def main(argv=None, llm: JSONModel | None = None, settings: Settings | None = None) -> int:
    def _body(ctx: JobContext) -> None:
        ctx.llm = llm
        body(ctx)

    return run_job("replies", _body, argv, add_args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
