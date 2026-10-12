"""Where the jobs deliver their output: the shared Google Doc (default) or Slack.

Set ``outputs.destination`` in settings. The Google Doc ID comes from
``outputs.google_doc_id`` or the ``GOOGLE_DOC_ID`` environment variable.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


def publish(
    settings: Settings, markdown: str, slack_text: str | None = None, files: list[Path] = (),
    replace_heading: str | None = None,
) -> dict:
    """Deliver ``markdown``. With ``replace_heading``, any Doc entry with that
    heading is deleted first, so a re-run replaces it rather than adding another."""
    outputs = settings["outputs"]
    destination = outputs.get("destination", "google_doc")
    if destination == "google_doc":
        doc_id = outputs.get("google_doc_id") or settings.secret("GOOGLE_DOC_ID", required=False)
        if not doc_id:
            log.warning("No Google Doc configured (outputs.google_doc_id or GOOGLE_DOC_ID): not published")
            return {"published": False, "reason": "no Google Doc ID"}
        from .gdoc import GoogleDoc

        doc = GoogleDoc(settings)
        removed = doc.remove(doc_id, replace_heading) if replace_heading else 0
        if removed:
            log.info("Removed %d earlier entr%s headed %r", removed, "y" if removed == 1 else "ies", replace_heading)
        paragraphs = doc.prepend(doc_id, markdown)
        result = {"published": "google_doc", "doc_id": doc_id, "paragraphs": paragraphs, "replaced": removed}
        if replace_heading:
            count = len(doc.entries(doc_id, replace_heading))
            result["entries_with_heading"] = count
            (log.info if count == 1 else log.error)("Doc now has %d entr%s headed %r",
                                                   count, "y" if count == 1 else "ies", replace_heading)
        return result
    if destination == "slack":
        from . import slack

        return slack.post_message_with_files(settings, slack_text or markdown, list(files))
    if destination == "none":
        return {"published": False, "reason": "outputs.destination is none"}
    raise ValueError(f"Unknown outputs.destination '{destination}' (google_doc, slack or none)")
