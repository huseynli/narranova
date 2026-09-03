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
    def voices_root(self) -> Path:
        return self.root / "voices"

    @property
    def temporary_root(self) -> Path:
        return self.root / "tmp"

    @property
    def voice_studio_root(self) -> Path:
        return self.temporary_root / "voice-studio"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.books_root.mkdir(exist_ok=True)
        self.voices_root.mkdir(exist_ok=True)
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

    def legacy_voice_reference(self, book_id: str, profile_id: str) -> Path:
        safe_profile_id = _validate_id(profile_id, "voice profile id")
        return self.book_root(book_id) / "voice" / safe_profile_id / "reference.wav"

    def voice_profile_root(self, profile_id: str) -> Path:
        return self.voices_root / _validate_id(profile_id, "voice profile id")

    def voice_reference(self, profile_id: str, version_id: str = "reference") -> Path:
        safe_version_id = _validate_id(version_id, "voice reference version id")
        return self.voice_profile_root(profile_id) / f"{safe_version_id}.wav"

    def job_root(self, book_id: str, job_id: str) -> Path:
        safe_job_id = _validate_id(job_id, "job id")
        return self.book_root(book_id) / "jobs" / safe_job_id

    def job_chunk_text(self, book_id: str, job_id: str, chunk_id: str) -> Path:
        safe_chunk_id = _validate_id(chunk_id, "chunk id")
        return self.job_root(book_id, job_id) / "chunks" / f"{safe_chunk_id}.txt"

    def job_chunk_master(self, book_id: str, job_id: str, chunk_id: str) -> Path:
        safe_chunk_id = _validate_id(chunk_id, "chunk id")
        return self.job_root(book_id, job_id) / "chunks" / f"{safe_chunk_id}.wav"

    def job_voice_reference(self, book_id: str, job_id: str) -> Path:
        return self.job_root(book_id, job_id) / "voice" / "reference.wav"

    def job_chapter_audio(self, book_id: str, job_id: str, chapter_index: int) -> Path:
        if chapter_index < 0:
            raise ValueError("Chapter index cannot be negative")
        return (
            self.job_root(book_id, job_id)
            / "output"
            / "chapters"
            / f"{chapter_index:04d}.wav"
        )

    def job_audiobook(self, book_id: str, job_id: str) -> Path:
        return self.job_root(book_id, job_id) / "output" / "audiobook.m4b"

    def job_narration_map(self, book_id: str, job_id: str) -> Path:
        return self.job_root(book_id, job_id) / "output" / "narration-map.json"

    def job_cover(self, book_id: str, job_id: str, suffix: str) -> Path:
        safe_suffix = suffix.lower().lstrip(".")
        if safe_suffix not in {"jpg", "jpeg", "png", "webp"}:
            raise ValueError(f"Unsupported cover format: {suffix}")
        return self.job_root(book_id, job_id) / "output" / f"cover.{safe_suffix}"

    def job_assembly_temporary(self, job_id: str) -> Path:
        return self.temporary_root / f"assembly-{_validate_id(job_id, 'job id')}"

    def voice_studio_draft(self, draft_id: str) -> Path:
        return self.voice_studio_root / _validate_id(draft_id, "voice studio draft id")

    def voice_studio_manifest(self, draft_id: str) -> Path:
        return self.voice_studio_draft(draft_id) / "draft.json"

    def voice_studio_upload(self, draft_id: str) -> Path:
        return self.voice_studio_draft(draft_id) / "uploaded-reference.wav"

    def voice_studio_take(self, draft_id: str, take_id: str) -> Path:
        safe_take_id = _validate_id(take_id, "voice studio take id")
        return self.voice_studio_draft(draft_id) / "takes" / f"{safe_take_id}.wav"
