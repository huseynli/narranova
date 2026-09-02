"""Voice profiles, durable synthesis jobs, and restart-safe sequential execution."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Callable

from narranova.application.planning import ChunkPlanner
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import validate_wave
from narranova.domain.narration import NarrationPlan, text_sha256
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from narranova.providers import OpenMossConfig, OpenMossProvider, SynthesisRequest, TTSProvider


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class VoiceProfiles:
    def __init__(
        self,
        repository: GenerationRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
    ) -> None:
        self.repository = repository
        self.layout = layout
        self.store = store

    def add_openmoss_provider(self, name: str, endpoint_url: str) -> str:
        OpenMossConfig(endpoint_url)
        if not name.strip():
            raise ValueError("Provider name cannot be empty")
        return self.repository.add_openmoss_provider(name.strip(), endpoint_url)

    def create_openmoss_profile(
        self,
        *,
        book_id: str,
        provider_id: str,
        reference_audio: Path,
        instruction: str,
        language: str = "English",
    ) -> str:
        if not instruction.strip():
            raise ValueError("Narrator instruction cannot be empty")
        if not reference_audio.is_file():
            raise FileNotFoundError(f"Reference audio not found: {reference_audio}")
        validate_wave(reference_audio)
        profile_id = uuid.uuid4().hex
        destination = self.layout.voice_reference(book_id, profile_id)
        reference_hash = self.store.copy(reference_audio, destination)
        profile: dict[str, object] = {
            "kind": "openmoss",
            "instruction": instruction.strip(),
            "language": language,
            "reference_artifact_path": destination.relative_to(self.layout.root).as_posix(),
            "reference_sha256": reference_hash,
        }
        profile_hash = hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest()
        try:
            self.repository.add_voice_profile(
                profile_id=profile_id,
                book_id=book_id,
                provider_id=provider_id,
                profile=profile,
                profile_sha256=profile_hash,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            shutil.rmtree(destination.parent, ignore_errors=True)
            raise
        return profile_id


class GenerationJobs:
    def __init__(
        self,
        books: BookRepository,
        generation: GenerationRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
        provider_factory: Callable[[dict[str, object]], TTSProvider] | None = None,
    ) -> None:
        self.books = books
        self.generation = generation
        self.layout = layout
        self.store = store
        self.provider_factory = provider_factory or self._openmoss_provider

    def create(self, book_id: str, voice_profile_id: str) -> str:
        voice = self.generation.get_voice_and_provider(voice_profile_id)
        if voice["book_id"] != book_id:
            raise ValueError("Voice profile belongs to a different book")
        plan_record = self.books.get_plan_record(book_id)
        plan_path = self._artifact_path(plan_record["artifact_path"])
        if self.store.sha256(plan_path) != plan_record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        chunks = ChunkPlanner().create_chunks(plan)
        if not chunks:
            raise ValueError("Narration plan has no enabled text")
        job_id = uuid.uuid4().hex
        records = []
        try:
            for chunk in chunks:
                path = self.layout.job_chunk_text(book_id, job_id, chunk.id)
                chunk_hash = self.store.write_text(path, chunk.text)
                records.append(
                    (chunk, path.relative_to(self.layout.root).as_posix(), chunk_hash)
                )
            self.generation.create_job(
                job_id=job_id,
                book_id=book_id,
                plan_id=plan_record["id"],
                voice_profile_id=voice_profile_id,
                chunks=records,
            )
        except Exception:
            shutil.rmtree(self.layout.job_root(book_id, job_id), ignore_errors=True)
            raise
        self._reuse_verified_chunks(book_id, job_id)
        return job_id

    def run(self, job_id: str) -> None:
        job = self.generation.get_job(job_id)
        self.generation.resume(job_id)
        self.generation.recover_interrupted(job_id)
        profile = job["profile"]
        if hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest() != job[
            "profile_sha256"
        ]:
            raise RuntimeError("Voice profile failed hash validation")
        reference = self._artifact_path(profile["reference_artifact_path"])
        if self.store.sha256(reference) != profile["reference_sha256"]:
            raise RuntimeError("Voice reference failed hash validation")
        provider = self.provider_factory(job)
        for chunk in self.generation.list_chunks(job_id):
            if self.generation.job_status(job_id) == "pause_requested":
                self.generation.mark_paused(job_id)
                return
            if chunk.status == "completed" and self._completed_chunk_is_valid(chunk):
                continue
            if chunk.status == "completed":
                self.generation.reset_chunk(job_id, chunk.database_id)
            text_path = self._artifact_path(chunk.text_artifact_path)
            text = text_path.read_text(encoding="utf-8").rstrip("\n")
            if text_sha256(text) != chunk.text_sha256:
                raise RuntimeError(f"Synthesis text failed hash validation: {chunk.id}")
            destination = self.layout.job_chunk_master(job["book_id"], job_id, chunk.id)
            self.generation.begin_chunk(job_id, chunk.database_id)
            try:
                result = provider.synthesize(
                    SynthesisRequest(
                        text=text,
                        destination=destination,
                        language=profile.get("language"),
                        instruction=profile["instruction"],
                        reference_audio=reference,
                    )
                )
                self.generation.complete_chunk(
                    job_id,
                    chunk.database_id,
                    result.audio_path.relative_to(self.layout.root).as_posix(),
                    result.audio_sha256,
                    result.duration_seconds,
                )
            except Exception as exc:
                self.generation.fail_chunk(job_id, chunk.database_id, str(exc))
                raise
        self.generation.complete_job(job_id)

    def _artifact_path(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path

    def _reuse_verified_chunks(self, book_id: str, job_id: str) -> None:
        for chunk in self.generation.list_chunks(job_id):
            reusable = self.generation.find_reusable_chunk(
                book_id=book_id,
                excluding_job_id=job_id,
                logical_id=chunk.id,
                text_sha256=chunk.text_sha256,
            )
            if reusable is None:
                continue
            source = self._artifact_path(reusable.audio_artifact_path)
            destination = self.layout.job_chunk_master(book_id, job_id, chunk.id)
            try:
                validate_wave(source)
                if self.store.sha256(source) != reusable.audio_sha256:
                    continue
                copied_hash = self.store.copy(source, destination)
                if copied_hash != reusable.audio_sha256:
                    destination.unlink(missing_ok=True)
                    continue
                self.generation.complete_chunk(
                    job_id,
                    chunk.database_id,
                    destination.relative_to(self.layout.root).as_posix(),
                    copied_hash,
                    reusable.duration_seconds,
                )
            except (OSError, RuntimeError, ValueError):
                destination.unlink(missing_ok=True)

    def _completed_chunk_is_valid(self, chunk: object) -> bool:
        if not chunk.audio_artifact_path or not chunk.audio_sha256:
            return False
        path = self._artifact_path(chunk.audio_artifact_path)
        try:
            validate_wave(path)
            return self.store.sha256(path) == chunk.audio_sha256
        except (OSError, ValueError):
            return False

    @staticmethod
    def _openmoss_provider(job: dict[str, object]) -> TTSProvider:
        if job["provider_kind"] != "openmoss":
            raise ValueError(f"Unsupported provider kind: {job['provider_kind']}")
        configuration = dict(job["provider_configuration"])
        return OpenMossProvider(OpenMossConfig(str(job["endpoint_url"]), **configuration))
