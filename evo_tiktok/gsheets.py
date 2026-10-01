"""Minimal Google Sheets access, and the Google sign-in shared with the Doc writer.

Sign in as a normal Google user via OAuth (client ID, secret and refresh token
from a one-off "Allow"), or with a service-account JSON key where allowed. The
account must have edit access to the sheet and the Doc.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from .config import ConfigError, Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(RuntimeError):
    pass


def _service_account_info(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(Path(raw).read_text(encoding="utf-8"))


OAUTH_VARS = ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")
TOKEN_URI = "https://oauth2.googleapis.com/token"


def authorized_session(settings: Settings, scopes: list[str]):
    """An HTTP session signed in to Google for Sheets and Docs.

    Preferred: a normal Google user who clicked "Allow" once (OAuth client ID,
    secret and refresh token), which needs no service-account key. Fallback: a
    service-account JSON key, where the organisation allows keys.
    """
    from google.auth.transport.requests import AuthorizedSession

    oauth = {name: settings.secret(name, required=False) for name in OAUTH_VARS}
    if all(oauth.values()):
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            token=None,
            refresh_token=oauth["GOOGLE_OAUTH_REFRESH_TOKEN"],
            client_id=oauth["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=oauth["GOOGLE_OAUTH_CLIENT_SECRET"],
            token_uri=TOKEN_URI,
            scopes=scopes,
        )
        return AuthorizedSession(creds)
    if any(oauth.values()):
        missing = [name for name, value in oauth.items() if not value]
        raise ConfigError(f"Google sign-in is half set up: missing {', '.join(missing)}")
    raw = settings.secret("GOOGLE_SERVICE_ACCOUNT_JSON", required=False)
    if not raw:
        raise ConfigError(
            "No Google sign-in: set GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and "
            "GOOGLE_OAUTH_REFRESH_TOKEN (see deploy/railway/README.md), or GOOGLE_SERVICE_ACCOUNT_JSON"
        )
    from google.oauth2 import service_account

    info = _service_account_info(raw)
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
