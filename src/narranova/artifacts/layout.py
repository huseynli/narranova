"""Canonical paths for persistent Narranova artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def _validate_id(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


@dataclass(frozen=True)
class ArtifactLayout:
    root: Path

    @classmethod
    def at(cls, root: Path) -> "ArtifactLayout":
        return cls(root.expanduser().resolve())

    @property
    def books_root(self) -> Path:
        return self.root / "books"

    @property
    def temporary_root(self) -> Path:
        return self.root / "tmp"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.books_root.mkdir(exist_ok=True)
        self.temporary_root.mkdir(exist_ok=True)

    def book_root(self, book_id: str) -> Path:
        return self.books_root / _validate_id(book_id, "book id")

    def source_epub(self, book_id: str) -> Path:
        return self.book_root(book_id) / "source" / "original.epub"

    def plan(self, book_id: str, revision: int) -> Path:
        if revision < 1:
            raise ValueError("Plan revision must be positive")
        return self.book_root(book_id) / "plan" / f"revision-{revision}.json"

    def chunk_master(self, book_id: str, chunk_id: str) -> Path:
        safe_chunk_id = _validate_id(chunk_id, "chunk id")
        return self.book_root(book_id) / "chunks" / f"{safe_chunk_id}.wav"

    def voice_reference(self, book_id: str, profile_id: str) -> Path:
        safe_profile_id = _validate_id(profile_id, "voice profile id")
        return self.book_root(book_id) / "voice" / safe_profile_id / "reference.wav"

    def job_root(self, book_id: str, job_id: str) -> Path:
        safe_job_id = _validate_id(job_id, "job id")
        return self.book_root(book_id) / "jobs" / safe_job_id

    def job_chunk_text(self, book_id: str, job_id: str, chunk_id: str) -> Path:
        safe_chunk_id = _validate_id(chunk_id, "chunk id")
        return self.job_root(book_id, job_id) / "chunks" / f"{safe_chunk_id}.txt"

    def job_chunk_master(self, book_id: str, job_id: str, chunk_id: str) -> Path:
        safe_chunk_id = _validate_id(chunk_id, "chunk id")
        return self.job_root(book_id, job_id) / "chunks" / f"{safe_chunk_id}.wav"
