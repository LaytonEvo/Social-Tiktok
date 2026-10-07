"""Database access: Postgres on Railway, SQLite locally and in tests.

Queries use ``?`` placeholders; they're translated for Postgres.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .config import ROOT

MIGRATIONS_DIR = ROOT / "migrations"


class Database:
    def __init__(self, url: str):
        self.url = url
        if url.startswith(("postgres://", "postgresql://")):
            import psycopg

            self.kind = "postgres"
            self.conn = psycopg.connect(url)
        elif url.startswith("sqlite:///") or url == "sqlite://":
            self.kind = "sqlite"
            path = url.removeprefix("sqlite:///") if url != "sqlite://" else ":memory:"
            self.conn = sqlite3.connect(path or ":memory:")
        else:
            raise ValueError("DATABASE_URL must start with postgresql:// or sqlite:///")

    def _sql(self, sql: str) -> str:
        if self.kind != "postgres":
            return sql
        sql = sql.replace("%", "%%").replace("?", "%s")
        return sql.replace("INTEGER PRIMARY KEY", "BIGSERIAL PRIMARY KEY")

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        cur = self.conn.cursor()
        cur.executemany(self._sql(sql), rows)

    def query(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        return list(self.execute(sql, params).fetchall())

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    def migrate(self, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
        """Apply any migrations not yet recorded. Returns the ones applied."""
        self.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        done = {row[0] for row in self.query("SELECT version FROM schema_migrations")}
        applied = []
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.stem in done:
                continue
            for statement in _statements(path.read_text(encoding="utf-8")):
                self.execute(statement)
            self.execute("INSERT INTO schema_migrations (version) VALUES (?)", (path.stem,))
            applied.append(path.stem)
        self.commit()
        return applied


def _statements(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    cleaned = "\n".join(ln.split("--", 1)[0] for ln in lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]
