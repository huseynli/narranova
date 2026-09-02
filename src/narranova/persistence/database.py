"""SQLite connection and forward-only schema migrations."""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            migration_root = files("narranova.persistence.migrations")
            migrations = sorted(
                item for item in migration_root.iterdir() if item.name.endswith(".sql")
            )
            for migration in migrations:
                version_text, _, name = migration.name.partition("_")
                version = int(version_text)
                if version in applied:
                    continue
                sql = migration.read_text(encoding="utf-8")
                migration_name = name.removesuffix(".sql").replace("'", "''")
                script = "\n".join(
                    (
                        "BEGIN IMMEDIATE;",
                        sql,
                        "INSERT INTO schema_migrations(version, name) "
                        f"VALUES ({version}, '{migration_name}');",
                        "COMMIT;",
                    )
                )
                try:
                    connection.executescript(script)
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
