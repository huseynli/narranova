"""Coordinated deletion of generated audio, jobs, and books."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from narranova.artifacts import ArtifactLayout, ArtifactStore
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
        if job["status"] in {
            "generating", "pause_requested", "cancel_requested", "assembling"
        }:
            raise ValueError(
                "Pause generation or wait for audiobook assembly before deleting audio"
            )
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
        for relative_path in self.generation.invalidate_job_outputs(
            job_id, chunk.chapter_index
        ):
            try:
                self._artifact(relative_path).unlink(missing_ok=True)
            except OSError:
                pass
        return str(job["book_id"])

    def job(self, job_id: str) -> str:
        job = self.generation.get_job(job_id)
        if job["status"] in {
            "generating", "pause_requested", "cancel_requested", "assembling"
        }:
            raise ValueError(
                "Pause generation or wait for audiobook assembly before deleting it"
            )
        job_root = self.layout.job_root(str(job["book_id"]), job_id)
        staged = self._stage(job_root, "job")
        try:
            book_id = self.generation.delete_job(job_id)
        except Exception:
            self._restore(staged, job_root)
            raise
        self._discard(staged)
        return book_id

    def compact_job(self, job_id: str) -> int:
        """Remove editable chunk masters after the final audiobook is verified."""
        job = self.generation.get_job(job_id)
        if job["status"] in {
            "generating", "pause_requested", "cancel_requested", "assembling"
        }:
            raise ValueError("Wait for active generation or assembly before finalizing")
        audiobook_artifacts = [
            artifact
            for artifact in self.generation.list_job_artifacts(job_id)
            if artifact.kind == "audiobook"
        ]
        if not audiobook_artifacts:
            raise ValueError("Build and verify the audiobook before freeing source space")
        audiobook_artifact = audiobook_artifacts[-1]
        audiobook = self._artifact(audiobook_artifact.relative_path)
        if not audiobook.is_file():
            raise ValueError("The finished audiobook is missing; rebuild it before finalizing")
        if ArtifactStore.sha256(audiobook) != audiobook_artifact.sha256:
            raise ValueError("The finished audiobook failed verification; rebuild it first")
        sources = [
            self._artifact(chunk.audio_artifact_path)
            for chunk in self.generation.list_chunks(job_id)
            if chunk.audio_artifact_path
        ]
        staged: list[tuple[Path | None, Path]] = []
        byte_size = 0
        try:
            for source in sources:
                if source.is_file():
                    byte_size += source.stat().st_size
                staged.append((self._stage(source, "chunk-master"), source))
            self.generation.compact_job_sources(job_id)
        except Exception:
            for temporary, source in reversed(staged):
                self._restore(temporary, source)
            raise
        for temporary, _ in staged:
            self._discard(temporary)
        return byte_size

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

    def voice_profile(self, profile_id: str) -> None:
        self.generation.get_voice_and_provider(profile_id)
        profile_root = self.layout.voice_profile_root(profile_id)
        staged = self._stage(profile_root, "voice-profile")
        try:
            self.generation.delete_voice_profile(profile_id)
        except Exception:
            self._restore(staged, profile_root)
            raise
        self._discard(staged)

    def connection(self, provider_id: str) -> None:
        self.generation.get_provider(provider_id)
        benchmark_root = self.layout.benchmarks_root / provider_id
        staged = self._stage(benchmark_root, "connection-benchmarks")
        try:
            self.generation.delete_provider(provider_id)
        except Exception:
            self._restore(staged, benchmark_root)
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
