"""Minimal Google Sheets access with a service account.

Share the sheet with the service account's email (Editor). The key goes in
``GOOGLE_SERVICE_ACCOUNT_JSON``, either the JSON itself or a path to it.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from .config import Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(RuntimeError):
    pass


def _service_account_info(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(Path(raw).read_text(encoding="utf-8"))


def authorized_session(settings: Settings, scopes: list[str]):
    """An HTTP session signed in as the service account (Sheets and Docs share it)."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    info = _service_account_info(settings.secret("GOOGLE_SERVICE_ACCOUNT_JSON"))
    return AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=scopes))


class Sheets:
    def __init__(self, settings: Settings, session=None):
        self.session = session or authorized_session(settings, SCOPES)

    def _check(self, resp):
        if resp.status_code >= 400:
            raise SheetsError(f"Sheets API {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def read(self, sheet_id: str, range_: str) -> list[list[str]]:
        resp = self.session.get(f"{API}/{sheet_id}/values/{quote(range_)}")
        return self._check(resp).get("values", [])

    def append(self, sheet_id: str, range_: str, rows: list[list]) -> int:
        if not rows:
            return 0
        resp = self.session.post(
            f"{API}/{sheet_id}/values/{quote(range_)}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": rows},
        )
        self._check(resp)
        return len(rows)
