"""Minimal fal.ai queue client: AI video clips, the voiceover and transcription.

Every call goes through fal's queue (submit, poll, fetch the result). The key
comes from ``FAL_KEY``; on hosts where a proxy signs fal requests it can be left
unset.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)

QUEUE = "https://queue.fal.run"


class FalError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def out_of_credit(self) -> bool:
        """fal locks the account when its balance runs out ('User is locked. Reason: TOP_UP')."""
        text = str(self).lower()
        return self.status in (402, 403) and any(w in text for w in ("top_up", "locked", "balance", "credit"))


class Fal:
    def __init__(self, settings: Settings, http: httpx.Client | None = None, poll_seconds: float = 5,
                 timeout_seconds: float = 900):
        key = settings.secret("FAL_KEY", required=False)
        self.headers = {"Authorization": f"Key {key}"} if key else {}
        self.http = http or httpx.Client(timeout=120)
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def _json(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            raise FalError(f"fal {resp.status_code}: {resp.text[:300]}", resp.status_code)
        return resp.json()

    def submit(self, endpoint: str, body: dict) -> dict:
        return self._json(self.http.post(f"{QUEUE}/{endpoint}", json=body, headers=self.headers))

    def wait(self, job: dict) -> dict:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            status = self._json(self.http.get(job["status_url"], headers=self.headers))
            if status.get("status") == "COMPLETED":
                return self._json(self.http.get(job["response_url"], headers=self.headers))
            time.sleep(self.poll_seconds)
        raise FalError(f"fal job {job.get('request_id')} didn't finish in {self.timeout_seconds:.0f}s")

    def run(self, endpoint: str, body: dict) -> dict:
        return self.wait(self.submit(endpoint, body))

    def run_many(self, calls: dict[str, tuple[str, dict]]) -> dict[str, dict]:
        """Submit everything first, then wait: clips generate in parallel."""
        jobs = {name: self.submit(endpoint, body) for name, (endpoint, body) in calls.items()}
        return {name: self.wait(job) for name, job in jobs.items()}

    def download(self, url: str, path: Path) -> Path:
        resp = self.http.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise FalError(f"download {resp.status_code}: {url}")
        path.write_bytes(resp.content)
        return path
