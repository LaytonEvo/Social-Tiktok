"""Post outputs to the team's Slack channel. Slack only: nothing goes to TikTok."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)

API = "https://slack.com/api"


class SlackError(RuntimeError):
    pass


def _call(http: httpx.Client, method: str, **kwargs) -> dict:
    resp = http.post(f"{API}/{method}", **kwargs)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise SlackError(f"Slack {method} failed: {body.get('error')}")
    return body


def post_message_with_files(
    settings: Settings, text: str, files: list[Path], http: httpx.Client | None = None
) -> dict:
    """Post ``text`` to the channel and attach ``files`` in a thread under it."""
    token = settings.secret("SLACK_BOT_TOKEN", required=False)
    outputs = settings["outputs"]
    if not token:
        log.warning("SLACK_BOT_TOKEN not set: skipping Slack")
        return {"posted": False, "reason": "no SLACK_BOT_TOKEN"}
    http = http or httpx.Client(timeout=60)
    auth = {"Authorization": f"Bearer {token}"}
    channel = outputs.get("slack_channel_id") or outputs["slack_channel"]
    msg = _call(http, "chat.postMessage", headers=auth, json={"channel": channel, "text": text})
    result = {"posted": True, "ts": msg["ts"], "files": 0}
    channel_id = outputs.get("slack_channel_id") or msg.get("channel")
    for path in files:
        data = path.read_bytes()
        up = _call(http, "files.getUploadURLExternal", headers=auth, data={"filename": path.name, "length": len(data)})
        http.post(up["upload_url"], content=data).raise_for_status()
        _call(
            http,
            "files.completeUploadExternal",
            headers=auth,
            json={"files": [{"id": up["file_id"], "title": path.name}], "channel_id": channel_id, "thread_ts": msg["ts"]},
        )
        result["files"] += 1
    return result


def post_pack(settings: Settings, plan, files: list[Path], http: httpx.Client | None = None) -> dict:
    from .scripts import FORMAT_NAMES

    lines = [
        f"*Filming pack for the week of {plan.week_start:%-d %B %Y}* is ready for Alex.",
        "Mix: " + ", ".join(f"{n} × {FORMAT_NAMES[f]}" for f, n in plan.mix.items() if n),
    ]
    if plan.roulette_sizes:
        lines.append("Size Roulette: " + ", ".join(s for s, _ in plan.roulette_sizes))
    if plan.giveaway:
        lines.append(f"Giveaway prize: {plan.giveaway.product_title}, closes {plan.giveaway_close:%-d %B}")
    lines += [f"_{n}_" for n in plan.notes]
    return post_message_with_files(settings, "\n".join(lines), files, http)
