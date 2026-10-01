"""Claude API calls and prompt loading.

Prompts live in ``prompts/*.md`` with ``## System`` and ``## User`` sections and
``{{NAME}}`` placeholders. Model names come from settings, never from code.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .config import DOCS_DIR, PROMPTS_DIR, Settings

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class LLMError(RuntimeError):
    pass


@dataclass
class Prompt:
    system: str
    user: str


def load_prompt(name: str, values: dict[str, Any]) -> Prompt:
    """Fill a prompt file. ``CONTENT_RULES`` is always available. Unknown or
    unused placeholders raise, so prompt edits can't silently drop data."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    values = {"CONTENT_RULES": (DOCS_DIR / "CONTENT_RULES.md").read_text(encoding="utf-8"), **values}
    try:
        _, rest = text.split("## System", 1)
        system, user = rest.split("## User", 1)
    except ValueError as exc:
        raise LLMError(f"prompts/{name}.md needs '## System' and '## User' sections") from exc
    wanted = set(PLACEHOLDER_RE.findall(text))
    missing = wanted - values.keys()
    if missing:
        raise LLMError(f"prompts/{name}.md placeholders without values: {sorted(missing)}")

    def fill(part: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), part).strip()

    return Prompt(system=fill(system), user=fill(user))


class JSONModel(Protocol):
    def complete_json(self, system: str, messages: list[dict], schema: dict, max_tokens: int) -> tuple[dict, Any]:
        """Return (parsed JSON, assistant content to append for a follow-up turn)."""


class ClaudeJSON:
    """Structured-output call to Claude with the server-side refusal fallback."""

    def __init__(self, settings: Settings, job: str, client=None):
        import anthropic

        self.model = settings["models"][job]
        self.effort = (settings.get("llm") or {}).get("effort", {}).get(job)
        # Jobs whose model supports the server-side refusal fallback (settings, not code).
        self.fallback = job in ((settings.get("llm") or {}).get("refusal_fallback") or [])
        self.client = client or anthropic.Anthropic(api_key=settings.secret("ANTHROPIC_API_KEY"))

    def complete_json(self, system: str, messages: list[dict], schema: dict, max_tokens: int = 16000):
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config=output_config,
        )
        if self.fallback:
            # If a safety classifier declines, the API retries on a suitable model.
            response = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            response = self.client.messages.create(**kwargs)
        log.info(
            "Claude %s: %s in / %s out tokens, stop=%s",
            response.model, response.usage.input_tokens, response.usage.output_tokens, response.stop_reason,
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"Claude declined the request: {getattr(details, 'category', None)}")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude hit max_tokens before finishing; raise the limit")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Claude returned no text block")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Claude returned invalid JSON: {exc}") from exc
        return data, response.content
