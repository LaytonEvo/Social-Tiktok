"""Write job outputs into one shared Google Doc, newest entry first.

Share the Doc with the service account's email (Editor), the same account
the reply sheet uses. Each entry is inserted at the top of the Doc as a
heading followed by its content, converted from the jobs' Markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Settings

API = "https://docs.googleapis.com/v1/documents"
SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/spreadsheets"]

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
NUMBERED_RE = re.compile(r"^\d+\.\s+")
TABLE_RULE_RE = re.compile(r"^\|[\s|:-]+\|$")
DIVIDER = "─" * 30


class DocsError(RuntimeError):
    pass


def u16(text: str) -> int:
    """Length in UTF-16 code units, which is how the Docs API counts indexes."""
    return len(text.encode("utf-16-le")) // 2


@dataclass
class Para:
    text: str
    style: str = "NORMAL_TEXT"
    bullet: str | None = None  # "BULLET" or "NUMBERED"
    italic: bool = False
    bold: list[tuple[int, int]] = field(default_factory=list)  # UTF-16 offsets within the paragraph


def _inline(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip **bold** and `code` markers, returning plain text and bold ranges."""
    text = text.replace("`", "")
    out, bold, pos = "", [], 0
    for m in BOLD_RE.finditer(text):
        out += text[pos : m.start()]
        start = u16(out)
        out += m.group(1)
        bold.append((start, u16(out)))
        pos = m.end()
    return out + text[pos:], bold


def markdown_to_paragraphs(markdown: str) -> list[Para]:
    paras: list[Para] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip() or TABLE_RULE_RE.match(line.strip()):
            continue
        style, bullet, italic = "NORMAL_TEXT", None, False
        if line.startswith("## "):
            style, line = "HEADING_2", line[3:]
        elif line.startswith("# "):
            style, line = "HEADING_1", line[2:]
        elif line.startswith("- "):
            bullet, line = "BULLET", line[2:]
        elif NUMBERED_RE.match(line):
            bullet, line = "NUMBERED", NUMBERED_RE.sub("", line)
        elif line.startswith("> "):
            italic, line = True, line[2:]
        elif line.startswith("|"):
            line = " · ".join(c.strip() for c in line.strip("|").split("|"))
        text, bold = _inline(line)
        paras.append(Para(text, style, bullet, italic, bold))
    paras.append(Para(DIVIDER))
    return paras


def build_requests(paras: list[Para], at: int = 1) -> list[dict]:
    """batchUpdate requests that insert the paragraphs at index ``at`` and style them."""
    full = "\n".join(p.text for p in paras) + "\n"
    end = at + u16(full)
    requests: list[dict] = [
        {"insertText": {"location": {"index": at}, "text": full}},
        # Inserted text inherits the style of the paragraph it lands in; reset it.
        {"deleteParagraphBullets": {"range": {"startIndex": at, "endIndex": end}}},
        {
            "updateTextStyle": {
                "range": {"startIndex": at, "endIndex": end - 1},
                "textStyle": {"bold": False, "italic": False},
                "fields": "bold,italic",
            }
        },
    ]
    starts = []
    pos = at
    for p in paras:
        starts.append(pos)
        pos += u16(p.text) + 1
    for p, start in zip(paras, starts):
        p_end = start + u16(p.text) + 1
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": p_end},
                    "paragraphStyle": {"namedStyleType": p.style},
                    "fields": "namedStyleType",
                }
            }
        )
        if p.italic and p.text:
            requests.append(_text_style(start, start + u16(p.text), {"italic": True}, "italic"))
        for b0, b1 in p.bold:
            if b1 > b0:
                requests.append(_text_style(start + b0, start + b1, {"bold": True}, "bold"))
    # Consecutive bullet paragraphs of the same kind become one list.
    i = 0
    while i < len(paras):
        kind = paras[i].bullet
        if not kind:
            i += 1
            continue
        j = i
        while j + 1 < len(paras) and paras[j + 1].bullet == kind:
            j += 1
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": starts[i], "endIndex": starts[j] + u16(paras[j].text)},
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN" if kind == "NUMBERED" else "BULLET_DISC_CIRCLE_SQUARE",
                }
            }
        )
        i = j + 1
    return requests


def _text_style(start: int, end: int, style: dict, fields: str) -> dict:
    return {"updateTextStyle": {"range": {"startIndex": start, "endIndex": end}, "textStyle": style, "fields": fields}}


class GoogleDoc:
    def __init__(self, settings: Settings, session=None):
        if session is None:
            from .gsheets import authorized_session

            session = authorized_session(settings, SCOPES)
        self.session = session

    def prepend(self, doc_id: str, markdown: str) -> int:
        """Insert the Markdown at the top of the Doc. Returns the number of paragraphs."""
        paras = markdown_to_paragraphs(markdown)
        resp = self.session.post(f"{API}/{doc_id}:batchUpdate", json={"requests": build_requests(paras)})
        if resp.status_code >= 400:
            raise DocsError(f"Docs API {resp.status_code}: {resp.text[:300]}")
        return len(paras)
