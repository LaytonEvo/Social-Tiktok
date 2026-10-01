"""Settings and secrets.

Non-secret settings come from ``config/settings.yaml`` (falling back to
``config/settings.example.yaml`` so a fresh checkout still runs). Secrets come
from environment variables only; see ``config/.env.example``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
DOCS_DIR = ROOT / "docs"

SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "SHOPIFY_STORE",
    "SHOPIFY_ADMIN_TOKEN",
    "METRICOOL_API_TOKEN",
    "METRICOOL_BLOG_ID",
    "WINDSOR_API_KEY",
    "SLACK_BOT_TOKEN",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_DOC_ID",
    "DATABASE_URL",
    "MEMBER_PRICES_API_URL",
    "MEMBER_PRICES_API_TOKEN",
)


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    data: dict[str, Any]
    source: Path
    env: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def secret(self, name: str, required: bool = True) -> str | None:
        value = self.env.get(name) or None
        if required and not value:
            raise ConfigError(f"Missing environment variable {name} (see config/.env.example)")
        return value

    @property
    def restricted_brands(self) -> list[str]:
        brands = list(self.data.get("restricted_brands", []))
        bad = [b for b in brands if not isinstance(b, str)]
        if bad:
            # e.g. a bare TRUE in YAML is read as a boolean, so the brand check would skip it
            raise ConfigError(f"restricted_brands must be quoted strings; got {bad!r}")
        return brands

    @property
    def brand_price_window(self) -> int:
        return int(self.data["price_claims"]["brand_price_window_chars"])

    @property
    def up_to_threshold(self) -> float:
        return float(self.data["price_claims"]["up_to_threshold_share"])


def settings_path() -> Path:
    override = os.environ.get("EVO_SETTINGS")
    if override:
        return Path(override)
    real = CONFIG_DIR / "settings.yaml"
    if real.exists():
        return real
    return CONFIG_DIR / "settings.example.yaml"


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    path = path or settings_path()
    if path.name == "settings.example.yaml":
        log.warning("Using %s; copy it to config/settings.yaml for real runs", path.name)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    env = dict(os.environ if env is None else env)
    secrets = {k: env[k] for k in SECRET_NAMES if env.get(k)}
    return Settings(data=data, source=path, env=secrets)
