"""Build a narration map and chapterized M4B directly from FLAC masters."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from narranova.application.generation import deterministic_chunk_seed
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import (
    AudioMasterInfo,
    FFmpegAudioMasters,
    FFmpegM4BEncoder,
    M4BChapter,
)
from narranova.domain.narration import NarrationPlan
from narranova.epub import EpubParser
from narranova.persistence.generation import GenerationRepository, StoredChunk
from narranova.providers import openmoss_performance_settings


class M4BEncoder(Protocol):
    def encode(
        self,
        chapters: list[M4BChapter],
        destination: Path,
        metadata: dict[str, str],
        workspace: Path,
        cover: Path | None = None,
    ) -> object: ...


class AudioMasters(Protocol):
    def validate(self, path: Path) -> AudioMasterInfo: ...


@dataclass(frozen=True)
class AssemblyResult:
    chapter_count: int
    duration_seconds: float
    audiobook_path: Path
    narration_map_path: Path


class AudioAssembler:
    def __init__(
        self,
        generation: GenerationRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
        encoder: M4BEncoder | None = None,
        epub_parser: EpubParser | None = None,
        masters: AudioMasters | None = None,
    ) -> None:
        self.generation = generation
        self.layout = layout
        self.store = store
        self.encoder = encoder or FFmpegM4BEncoder()
        self.epub_parser = epub_parser or EpubParser()
        self.masters = masters or FFmpegAudioMasters()
        self._claim_lock = threading.Lock()
        self._claim_owners: dict[str, str] = {}
        self._heartbeat_stops: dict[str, threading.Event] = {}

    def prepare(self, job_id: str) -> None:
        owner_id = uuid.uuid4().hex
        self.generation.claim_assembly_work(job_id, owner_id)
        with self._claim_lock:
            self._claim_owners[job_id] = owner_id
            stop = threading.Event()
            self._heartbeat_stops[job_id] = stop
        threading.Thread(
            target=self._heartbeat,
            args=(job_id, owner_id, stop),
            daemon=True,
        ).start()
        try:
            self.generation.begin_assembly(job_id)
        except Exception:
            self._release_claim(job_id)
            raise

    def run(self, job_id: str, *, prepared: bool = False) -> AssemblyResult:
        if not prepared:
            self.prepare(job_id)
        try:
            result = self._assemble(job_id)
            self.generation.complete_job(job_id)
            return result
        except Exception as exc:
            self.generation.fail_job(job_id, str(exc))
            raise
        finally:
            self._release_claim(job_id)

    def _assemble(self, job_id: str) -> AssemblyResult:
        self._renew_claim(job_id)
        job = self.generation.get_job(job_id)
        plan_path = self._artifact(str(job["plan_artifact_path"]))
        if self.store.sha256(plan_path) != job["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        chunks = self.generation.list_chunks(job_id)
        if not chunks or any(chunk.status != "completed" for chunk in chunks):
            raise ValueError("Every audio chunk must be completed before assembly")
        grouped: dict[int, list[StoredChunk]] = {}
        for chunk in chunks:
            grouped.setdefault(chunk.chapter_index, []).append(chunk)
        chapter_titles = {chapter.spine_index: chapter.title for chapter in plan.chapters}
        encoded_chapters: list[M4BChapter] = []
        chapter_records: list[dict[str, object]] = []
        book_offset = 0.0
        for chapter_index, chapter_chunks in grouped.items():
            title = chapter_titles.get(chapter_index, f"Chapter {chapter_index}")
            verified = [
                (chunk, self._verified_chunk(chunk)) for chunk in chapter_chunks
            ]
            duration = sum(info.duration_seconds for _, info in verified)
            chapter_start = book_offset
            chapter_end = chapter_start + duration
            encoded_chapters.append(
                M4BChapter(
                    title,
                    tuple(info.path for _, info in verified),
                    chapter_start,
                    chapter_end,
                )
            )
            chapter_records.append(
                self._chapter_map(
                    plan,
                    str(job["book_id"]),
                    chapter_index,
                    title,
                    chapter_start,
                    verified,
                )
            )
            book_offset = chapter_end

        cover = self._extract_cover(job)
        narration_map = self._narration_map(job, plan, chapter_records, book_offset)
        narration_map_path = self.layout.job_narration_map(job["book_id"], job_id)
        self.store.write_text(
            narration_map_path,
            json.dumps(narration_map, ensure_ascii=False, indent=2) + "\n",
        )
        self._record(
            job,
            "narration_map",
            narration_map_path,
            {"schema_version": 1, "duration_seconds": book_offset},
        )

        audiobook_path = self.layout.job_audiobook(job["book_id"], job_id)
        workspace = self.layout.job_assembly_temporary(job_id)
        try:
            encoded = self.encoder.encode(
                encoded_chapters,
                audiobook_path,
                self._book_metadata(plan, job),
                workspace,
                cover,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        encoded_duration = float(getattr(encoded, "duration_seconds"))
        if abs(encoded_duration - book_offset) > max(1.0, book_offset * 0.01):
            raise RuntimeError("Encoded M4B duration does not match its chapter audio")
        self._record(
            job,
            "audiobook",
            audiobook_path,
            {
                "duration_seconds": encoded_duration,
                "chapter_count": len(encoded_chapters),
                "chunk_sha256s": [
                    str(chunk.audio_sha256)
                    for chapter_chunks in grouped.values()
                    for chunk in chapter_chunks
                ],
            },
        )
        return AssemblyResult(
            len(encoded_chapters), encoded_duration, audiobook_path, narration_map_path
        )

    def _renew_claim(self, job_id: str) -> None:
        with self._claim_lock:
            owner_id = self._claim_owners.get(job_id)
        if owner_id is None:
            raise RuntimeError("Audiobook job has no active worker lease")
        self.generation.renew_assembly_work(job_id, owner_id)

    def _release_claim(self, job_id: str) -> None:
        with self._claim_lock:
            owner_id = self._claim_owners.pop(job_id, None)
            stop = self._heartbeat_stops.pop(job_id, None)
        if stop is not None:
            stop.set()
        if owner_id is not None:
            self.generation.release_job_work(job_id, owner_id)

    def _heartbeat(
        self, job_id: str, owner_id: str, stop: threading.Event
    ) -> None:
        while not stop.wait(60):
            try:
                self.generation.renew_assembly_work(job_id, owner_id)
            except Exception:
                return

    def _verified_chunk(self, chunk: StoredChunk) -> AudioMasterInfo:
        if not chunk.audio_artifact_path or not chunk.audio_sha256:
            raise RuntimeError(f"Chunk {chunk.id} has no verified audio")
        path = self._artifact(chunk.audio_artifact_path)
        info = self.masters.validate(path)
        if self.store.sha256(path) != chunk.audio_sha256:
            raise RuntimeError(f"Chunk {chunk.id} failed hash validation")
        return info

    def _record(
        self,
        job: dict[str, object],
        kind: str,
        path: Path,
        metadata: dict[str, object],
        chapter_index: int | None = None,
    ) -> str:
        return self.generation.record_artifact(
            book_id=str(job["book_id"]),
            job_id=str(job["id"]),
            kind=kind,
            relative_path=path.relative_to(self.layout.root).as_posix(),
            sha256=self.store.sha256(path),
            byte_size=path.stat().st_size,
            metadata=metadata,
            chapter_index=chapter_index,
        )

    def _extract_cover(self, job: dict[str, object]) -> Path | None:
        source = self._artifact(str(job["source_artifact_path"]))
        parsed = self.epub_parser.parse(source)
        if not parsed.cover_data:
            return None
        suffixes = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }
        suffix = suffixes.get(str(parsed.cover_media_type).lower())
        if suffix is None and parsed.cover_path:
            suffix = Path(parsed.cover_path).suffix.lower().lstrip(".")
        if suffix not in {"jpg", "jpeg", "png", "webp"}:
            return None
        cover = self.layout.job_cover(str(job["book_id"]), str(job["id"]), suffix)
        self.store.write_bytes(cover, parsed.cover_data)
        self._record(
            job,
            "cover",
            cover,
            {"media_type": parsed.cover_media_type or f"image/{suffix}"},
        )
        return cover

    def _chapter_map(
        self,
        plan: NarrationPlan,
        book_id: str,
        chapter_index: int,
        title: str,
        book_start: float,
        chunks: list[tuple[StoredChunk, AudioMasterInfo]],
    ) -> dict[str, object]:
        units = {unit.id: unit for unit in plan.units}
        chapter_cursor = 0.0
        mapped_chunks = []
        for chunk, info in chunks:
            duration = info.duration_seconds
            start = chapter_cursor
            end = start + duration
            mapped_chunks.append(
                {
                    "id": chunk.id,
                    "text_sha256": chunk.text_sha256,
                    "synthesis_text_sha256": chunk.synthesis_text_sha256,
                    "audio_sha256": chunk.audio_sha256,
                    "audio_format": "flac",
                    "audio_artifact_path": chunk.audio_artifact_path,
                    "seed": deterministic_chunk_seed(
                        book_id, chunk.chapter_index, chunk.chunk_index
                    ),
                    "chapter_start_seconds": start,
                    "chapter_end_seconds": end,
                    "book_start_seconds": book_start + start,
                    "book_end_seconds": book_start + end,
                    "units": [
                        {
                            "id": unit.id,
                            "document": unit.document,
                            "element_id": unit.element_id,
                            "display_text_sha256": unit.display_text_sha256,
                            "spoken_text_sha256": unit.spoken_text_sha256,
                        }
                        for unit_id in chunk.unit_ids
                        if (unit := units.get(unit_id)) is not None
                    ],
                }
            )
            chapter_cursor = end
        return {
            "spine_index": chapter_index,
            "title": title,
            "master_sha256s": [str(chunk.audio_sha256) for chunk, _ in chunks],
            "book_start_seconds": book_start,
            "book_end_seconds": book_start + chapter_cursor,
            "chunks": mapped_chunks,
        }

    @staticmethod
    def _narration_map(
        job: dict[str, object],
        plan: NarrationPlan,
        chapters: list[dict[str, object]],
        duration: float,
    ) -> dict[str, object]:
        profile = dict(job["profile"])
        connection_configuration = dict(job.get("provider_configuration") or {})
        performance = (
            openmoss_performance_settings(connection_configuration)
            if job.get("provider_kind") == "openmoss"
            else connection_configuration
        )
        return {
            "schema_version": 1,
            "job_id": job["id"],
            "book_id": job["book_id"],
            "plan_revision": job["plan_revision"],
            "plan_sha256": job["plan_sha256"],
            "duration_seconds": duration,
            "metadata": plan.metadata,
            "provider": {
                "id": job.get("provider_instance_id"),
                "name": job.get("provider_name"),
                "kind": job.get("provider_kind"),
                "performance": performance,
            },
            "voice": {
                "name": profile.get("name"),
                "language": profile.get("language"),
                "instruction": profile.get("instruction"),
                "profile_snapshot_sha256": job.get("profile_sha256"),
                "reference_sha256": profile.get("reference_sha256"),
                "sampling": profile.get("sampling") or {},
            },
            "narration_enhancement": job.get("narration_enhancement"),
            "chapters": chapters,
        }

    @staticmethod
    def _book_metadata(
        plan: NarrationPlan, job: dict[str, object]
    ) -> dict[str, str]:
        metadata = plan.metadata
        authors = metadata.get("authors") or []
        author = ", ".join(str(value) for value in authors) or str(
            job.get("book_author") or ""
        )
        profile = dict(job["profile"])
        result = {
            "title": str(metadata.get("title") or job["book_title"]),
            "album": str(metadata.get("title") or job["book_title"]),
            "artist": author,
            "album_artist": author,
            "language": str(metadata.get("language") or job.get("book_language") or ""),
            "narrator": str(profile.get("name") or ""),
            "comment": "Created with Narranova",
        }
        identifiers = metadata.get("identifiers") or []
        if identifiers:
            result["isbn"] = str(identifiers[0])
        for source, target in (
            ("subtitle", "subtitle"),
            ("publisher", "publisher"),
            ("description", "description"),
            ("series", "series"),
            ("series_index", "episode_sort"),
        ):
            if metadata.get(source):
                result[target] = str(metadata[source])
        return result

    def _artifact(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path
