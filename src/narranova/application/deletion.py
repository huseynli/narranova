"""Coordinated deletion of generated audio, jobs, and books."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from narranova.artifacts import ArtifactLayout
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository


class DeleteArtifacts:
    def __init__(
        self,
        books: BookRepository,
        generation: GenerationRepository,
        layout: ArtifactLayout,
    ) -> None:
        self.books = books
        self.generation = generation
        self.layout = layout

    def generated_chunk(self, job_id: str, chunk_id: str) -> str:
        job = self.generation.get_job(job_id)
        if job["status"] in {"generating", "pause_requested"}:
            raise ValueError("Pause the generation job before deleting its audio")
        chunk = self.generation.get_chunk(job_id, chunk_id)
        if chunk.status == "generating":
            raise ValueError("An actively generating chunk cannot be deleted")
        audio = self._artifact(chunk.audio_artifact_path) if chunk.audio_artifact_path else None
        staged = self._stage(audio, "chunk")
        try:
            self.generation.delete_generated_chunk(job_id, chunk_id)
        except Exception:
            self._restore(staged, audio)
            raise
        self._discard(staged)
        return str(job["book_id"])

    def job(self, job_id: str) -> str:
        job = self.generation.get_job(job_id)
        if job["status"] in {"generating", "pause_requested"}:
            raise ValueError("Pause the generation job before deleting it")
        job_root = self.layout.job_root(str(job["book_id"]), job_id)
        staged = self._stage(job_root, "job")
        try:
            book_id = self.generation.delete_job(job_id)
        except Exception:
            self._restore(staged, job_root)
            raise
        self._discard(staged)
        return book_id

    def book(self, book_id: str) -> None:
        self.books.get_book(book_id)
        if self.generation.book_has_active_jobs(book_id):
            raise ValueError("Pause active generation jobs before deleting this book")
        book_root = self.layout.book_root(book_id)
        staged = self._stage(book_root, "book")
        try:
            self.books.delete_book(book_id)
        except Exception:
            self._restore(staged, book_root)
            raise
        self._discard(staged)

    def _artifact(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path

    def _stage(self, source: Path | None, kind: str) -> Path | None:
        if source is None or not source.exists():
            return None
        staged = self.layout.temporary_root / f"deleting-{kind}-{uuid.uuid4().hex}"
        os.replace(source, staged)
        return staged

    @staticmethod
    def _restore(staged: Path | None, destination: Path | None) -> None:
        if staged is None or destination is None or not staged.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)

    @staticmethod
    def _discard(staged: Path | None) -> None:
        if staged is None:
            return
        if staged.is_dir():
            shutil.rmtree(staged, ignore_errors=True)
        else:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
