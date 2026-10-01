"""Link a posted TikTok to its script, so replies and the report can find it.

    python -m evo_tiktok.posts --week 2026-10-05 --script 3 --post https://www.tiktok.com/@evo/video/7412...

Run it after posting each video by hand. It only updates our content_log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .replies import _post_ref
from .runner import JobContext, run_job

log = logging.getLogger(__name__)


def body(ctx: JobContext) -> None:
    post_id = _post_ref(ctx.args.post)
    if not post_id.isdigit():
        raise ValueError(f"Couldn't find a TikTok video ID in {ctx.args.post!r}")
    db = ctx.read_db()
    if db is None:
        raise ValueError("DATABASE_URL is needed to link a post")
    rows = db.query(
        "SELECT format, hook FROM content_log WHERE week_start = ? AND script_no = ?", (ctx.args.week, ctx.args.script)
    )
    if not rows:
        raise ValueError(f"No script {ctx.args.script} for the week of {ctx.args.week} in content_log")
    ctx.summary.update(post_id=post_id, format=rows[0][0], hook=rows[0][1])
    if ctx.db() is None:
        log.info("Dry run: would link post %s to %s script %s", post_id, ctx.args.week, ctx.args.script)
        return
    ctx.db().execute(
        "UPDATE content_log SET tiktok_post_id = ?, status = 'posted', posted_at = ? WHERE week_start = ? AND script_no = ?",
        (post_id, datetime.now(timezone.utc).isoformat(), ctx.args.week, ctx.args.script),
    )
    ctx.db().commit()


def add_args(parser) -> None:
    parser.add_argument("--week", required=True, help="week_start of the filming pack (YYYY-MM-DD)")
    parser.add_argument("--script", required=True, type=int, help="script number in the pack (1-7)")
    parser.add_argument("--post", required=True, help="TikTok video URL or ID")


def main(argv=None, settings=None) -> int:
    return run_job("posts", body, argv, add_args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
