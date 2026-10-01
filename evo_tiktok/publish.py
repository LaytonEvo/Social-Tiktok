"""Where the jobs deliver their output: the shared Google Doc (default) or Slack.

Set ``outputs.destination`` in settings. The Google Doc ID comes from
``outputs.google_doc_id`` or the ``GOOGLE_DOC_ID`` environment variable.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


def publish(settings: Settings, markdown: str, slack_text: str | None = None, files: list[Path] = ()) -> dict:
    outputs = settings["outputs"]
    destination = outputs.get("destination", "google_doc")
    if destination == "google_doc":
        doc_id = outputs.get("google_doc_id") or settings.secret("GOOGLE_DOC_ID", required=False)
        if not doc_id:
            log.warning("No Google Doc configured (outputs.google_doc_id or GOOGLE_DOC_ID): not published")
            return {"published": False, "reason": "no Google Doc ID"}
        from .gdoc import GoogleDoc

        paragraphs = GoogleDoc(settings).prepend(doc_id, markdown)
        return {"published": "google_doc", "doc_id": doc_id, "paragraphs": paragraphs}
    if destination == "slack":
        from . import slack

        return slack.post_message_with_files(settings, slack_text or markdown, list(files))
    if destination == "none":
        return {"published": False, "reason": "outputs.destination is none"}
    raise ValueError(f"Unknown outputs.destination '{destination}' (google_doc, slack or none)")
