"""Apply database migrations: ``python -m evo_tiktok.migrate``."""

from __future__ import annotations

import logging

from .config import load_settings
from .db import Database


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    db = Database(settings.secret("DATABASE_URL"))
    applied = db.migrate()
    logging.info("Applied migrations: %s", ", ".join(applied) or "none (up to date)")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
