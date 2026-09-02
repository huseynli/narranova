"""Persistence operations for imported books and narration plans."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from narranova.persistence.database import Database


@dataclass(frozen=True)
class StoredBook:
    id: str
    title: str
    author: str | None
    language: str | None
    status: str
    source_sha256: str


class BookRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add_book_with_plan(
        self,
        *,
        book_id: str,
        title: str,
        author: str | None,
        language: str | None,
        source_sha256: str,
        source_path: str,
        plan_id: str,
        plan_sha256: str,
        plan_path: str,
    ) -> None:
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO books(
                        id, title, author, language, source_sha256, source_artifact_path, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planned')
                    """,
                    (book_id, title, author, language, source_sha256, source_path),
                )
                connection.execute(
                    """
                    INSERT INTO narration_plans(
                        id, book_id, revision, plan_sha256, artifact_path
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (plan_id, book_id, plan_sha256, plan_path),
                )
        except sqlite3.IntegrityError as exc:
            if "books.source_sha256" in str(exc):
                raise ValueError("This EPUB has already been imported") from exc
            raise

    def list_books(self) -> list[StoredBook]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, author, language, status, source_sha256
                FROM books ORDER BY created_at, id
                """
            ).fetchall()
        return [StoredBook(**dict(row)) for row in rows]

    def get_plan_record(self, book_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.artifact_path, p.plan_sha256, p.revision
                FROM narration_plans p
                WHERE p.book_id = ? ORDER BY p.revision DESC LIMIT 1
                """,
                (book_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Book not found or has no narration plan: {book_id}")
        return dict(row)
