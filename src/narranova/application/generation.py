"""Voice profiles, durable synthesis jobs, and restart-safe sequential execution."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Callable, Mapping, Protocol

from narranova.application.default_voices import (
    BUILTIN_VOICE_PREFIX,
    default_voice_pair,
)
from narranova.application.planning import ChunkPlanner
from narranova.application.provider_catalog import provider_type
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import AudioMasterInfo, FFmpegAudioMasters, validate_wave
from narranova.domain.narration import NarrationPlan, text_sha256
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from narranova.providers import (
    OpenMossConfig,
    OpenMossProvider,
    SynthesisRequest,
    TTSProvider,
    normalize_openmoss_sampling,
)


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deterministic_chunk_seed(book_id: str, chapter_index: int, chunk_index: int) -> int:
    """Return a stable positive OpenMOSS seed for one logical audiobook chunk."""

    identity = f"narranova:chunk-seed:v1:{book_id}:{chapter_index}:{chunk_index}"
    seed = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:4], "big")
    return (seed & 0x7FFF_FFFF) or 1


class AudioMasters(Protocol):
    def normalize(self, source: Path, destination: Path) -> AudioMasterInfo: ...

    def validate(self, path: Path) -> AudioMasterInfo: ...


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
        self._migrate_legacy_artifacts()

    def add_provider(self, kind: str, name: str, endpoint_url: str) -> str:
        definition = provider_type(kind)
        if not name.strip():
            raise ValueError("Connection name cannot be empty")
        if definition.id == "openmoss":
            OpenMossConfig(endpoint_url)
        return self.repository.add_provider(definition.id, name.strip(), endpoint_url)

    def add_openmoss_provider(self, name: str, endpoint_url: str) -> str:
        return self.add_provider("openmoss", name, endpoint_url)

    def update_provider(
        self,
        provider_id: str,
        *,
        kind: str,
        name: str,
        endpoint_url: str,
    ) -> None:
        definition = provider_type(kind)
        if not name.strip():
            raise ValueError("Connection name cannot be empty")
        if definition.id == "openmoss":
            OpenMossConfig(endpoint_url)
        self.repository.update_provider(
            provider_id,
            kind=definition.id,
            name=name.strip(),
            endpoint_url=endpoint_url,
        )

    def create_openmoss_profile(
        self,
        *,
        provider_id: str,
        reference_audio: Path,
        instruction: str,
        name: str | None = None,
        language: str = "English",
        sampling: Mapping[str, object] | None = None,
    ) -> str:
        provider = self.repository.get_provider(provider_id)
        if provider["kind"] != "openmoss" or not provider["enabled"]:
            raise ValueError("Choose an enabled OpenMOSS connection")
        if not instruction.strip():
            raise ValueError("Narrator instruction cannot be empty")
        if not reference_audio.is_file():
            raise FileNotFoundError(f"Reference audio not found: {reference_audio}")
        validate_wave(reference_audio)
        OpenMossConfig.from_connection(
            str(provider["endpoint_url"]), provider["configuration"]
        )
        sampling_overrides = normalize_openmoss_sampling(sampling)
        profile_id = uuid.uuid4().hex
        profile_name = (name or "Narrator profile").strip()
        if not profile_name:
            raise ValueError("Voice profile name cannot be empty")
        destination = self.layout.voice_reference(profile_id)
        reference_hash = self.store.copy(reference_audio, destination)
        profile: dict[str, object] = {
            "kind": "openmoss",
            "name": profile_name,
            "instruction": instruction.strip(),
            "language": language,
            "reference_artifact_path": destination.relative_to(self.layout.root).as_posix(),
            "reference_sha256": reference_hash,
        }
        if sampling_overrides:
            profile["sampling"] = dict(sampling_overrides)
        profile_hash = hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest()
        try:
            self.repository.add_voice_profile(
                profile_id=profile_id,
                provider_id=provider_id,
                profile=profile,
                profile_sha256=profile_hash,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            shutil.rmtree(destination.parent, ignore_errors=True)
            raise
        return profile_id

    def update_openmoss_profile(
        self,
        profile_id: str,
        *,
        provider_id: str,
        instruction: str,
        name: str,
        language: str = "English",
        reference_audio: Path | None = None,
        sampling: Mapping[str, object] | None = None,
    ) -> None:
        current = self.repository.get_voice_and_provider(profile_id)
        provider = self.repository.get_provider(provider_id)
        if provider["kind"] != "openmoss":
            raise ValueError("This profile editor currently supports OpenMOSS profiles")
        OpenMossConfig.from_connection(
            str(provider["endpoint_url"]), provider["configuration"]
        )
        if not name.strip():
            raise ValueError("Voice profile name cannot be empty")
        if not instruction.strip():
            raise ValueError("Narrator instruction cannot be empty")
        current_profile = current["profile"]
        sampling_overrides = (
            normalize_openmoss_sampling(sampling)
            if sampling is not None
            else normalize_openmoss_sampling(current_profile.get("sampling"))
        )
        old_reference = self._artifact_path(current_profile["reference_artifact_path"])
        destination = old_reference
        created_reference: Path | None = None
        if reference_audio is not None:
            validate_wave(reference_audio)
            destination = self.layout.voice_reference(
                profile_id, f"reference-{uuid.uuid4().hex}"
            )
            self.store.copy(reference_audio, destination)
            created_reference = destination
        reference_hash = self.store.sha256(destination)
        profile: dict[str, object] = {
            "kind": "openmoss",
            "name": name.strip(),
            "instruction": instruction.strip(),
            "language": language.strip() or "English",
            "reference_artifact_path": destination.relative_to(self.layout.root).as_posix(),
            "reference_sha256": reference_hash,
        }
        if sampling_overrides:
            profile["sampling"] = dict(sampling_overrides)
        profile_hash = hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest()
        try:
            self.repository.update_voice_profile(
                profile_id,
                provider_id=provider_id,
                profile=profile,
                profile_sha256=profile_hash,
            )
        except Exception:
            if created_reference is not None:
                created_reference.unlink(missing_ok=True)
            raise
        if created_reference is not None and old_reference != created_reference:
            old_reference.unlink(missing_ok=True)

    def _migrate_legacy_artifacts(self) -> None:
        for stored in self.repository.list_voice_profiles():
            profile = dict(stored.profile)
            relative = profile.get("reference_artifact_path")
            if not relative:
                continue
            source = self._artifact_path(str(relative))
            destination = self.layout.voice_reference(stored.id)
            if source == destination:
                continue
            if not source.is_file():
                continue
            validate_wave(source)
            source_hash = self.store.sha256(source)
            if source_hash != profile.get("reference_sha256"):
                continue
            copied_hash = self.store.copy(source, destination)
            profile["reference_artifact_path"] = destination.relative_to(
                self.layout.root
            ).as_posix()
            profile["reference_sha256"] = copied_hash
            profile_hash = hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest()
            try:
                self.repository.update_voice_profile(
                    stored.id,
                    provider_id=stored.provider_id,
                    profile=profile,
                    profile_sha256=profile_hash,
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            source.unlink(missing_ok=True)
            shutil.rmtree(source.parent, ignore_errors=True)

    def _artifact_path(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path


class GenerationJobs:
    def __init__(
        self,
        books: BookRepository,
        generation: GenerationRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
        provider_factory: Callable[[dict[str, object]], TTSProvider] | None = None,
        masters: AudioMasters | None = None,
    ) -> None:
        self.books = books
        self.generation = generation
        self.layout = layout
        self.store = store
        self.provider_factory = provider_factory or self._openmoss_provider
        self.masters = masters or FFmpegAudioMasters()
        self._materialize_job_voice_references()
        self._reset_legacy_reused_chunks()

    def create(
        self,
        book_id: str,
        voice_profile_id: str,
        provider_id: str | None = None,
    ) -> str:
        narrator_profile_id: str | None = voice_profile_id
        connection_configuration: dict[str, object]
        if voice_profile_id.startswith(BUILTIN_VOICE_PREFIX):
            if not provider_id:
                raise ValueError("Choose a TTS connection for the built-in narrator")
            provider = self.generation.get_provider(provider_id)
            pair = default_voice_pair(voice_profile_id)
            if provider["kind"] != pair.provider_kind:
                raise ValueError("Built-in narrator is not compatible with this connection")
            voice = {
                "provider_id": provider["id"],
                "provider_kind": provider["kind"],
                "enabled": provider["enabled"],
                "profile": pair.profile_snapshot(),
            }
            source_reference = pair.audio_path
            expected_reference_hash = pair.audio_sha256
            narrator_profile_id = None
            connection_configuration = dict(provider["configuration"])
        else:
            voice = self.generation.get_voice_and_provider(voice_profile_id)
            if provider_id is not None and voice["provider_id"] != provider_id:
                raise ValueError(
                    "Voice profile does not belong to the selected TTS connection"
                )
            source_reference = self._artifact_path(
                voice["profile"]["reference_artifact_path"]
            )
            expected_reference_hash = voice["profile"]["reference_sha256"]
            connection_configuration = dict(voice["provider_configuration"])
        if not voice["enabled"]:
            raise ValueError("The selected TTS connection is disabled")
        plan_record = self.books.get_plan_record(book_id)
        plan_path = self._artifact_path(plan_record["artifact_path"])
        if self.store.sha256(plan_path) != plan_record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        chunks = ChunkPlanner().create_chunks(plan)
        if not chunks:
            raise ValueError("Narration plan has no enabled text")
        job_id = uuid.uuid4().hex
        profile = dict(voice["profile"])
        records = []
        try:
            validate_wave(source_reference)
            if self.store.sha256(source_reference) != expected_reference_hash:
                raise RuntimeError("Voice reference failed hash validation")
            job_reference = self.layout.job_voice_reference(book_id, job_id)
            reference_hash = self.store.copy(source_reference, job_reference)
            profile["reference_artifact_path"] = job_reference.relative_to(
                self.layout.root
            ).as_posix()
            profile["reference_sha256"] = reference_hash
            profile_hash = hashlib.sha256(
                _canonical_json(profile).encode("utf-8")
            ).hexdigest()
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
                voice_profile_id=narrator_profile_id,
                provider_id=voice["provider_id"],
                connection_configuration_snapshot=connection_configuration,
                profile_snapshot=profile,
                profile_snapshot_sha256=profile_hash,
                chunks=records,
            )
        except Exception:
            shutil.rmtree(self.layout.job_root(book_id, job_id), ignore_errors=True)
            raise
        return job_id

    def prepare(self, job_id: str) -> None:
        """Put a job into a visible running state before background execution."""
        self.generation.resume(job_id)
        self.generation.recover_interrupted(job_id)
        self._remove_abandoned_provider_audio(job_id)
        self.generation.start_job(job_id)

    def run(self, job_id: str, *, prepared: bool = False) -> None:
        if not prepared:
            self.prepare(job_id)
        job = self.generation.get_job(job_id)
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
            replacing_completed_audio = chunk.status == "completed"
            if replacing_completed_audio and self._completed_chunk_is_valid(chunk):
                continue
            if replacing_completed_audio:
                self.generation.reset_chunk(job_id, chunk.database_id)
            text_path = self._artifact_path(chunk.text_artifact_path)
            text = text_path.read_text(encoding="utf-8").rstrip("\n")
            if text_sha256(text) != chunk.text_sha256:
                raise RuntimeError(f"Synthesis text failed hash validation: {chunk.id}")
            destination = self.layout.job_chunk_master(job["book_id"], job_id, chunk.id)
            provider_output = self.layout.job_chunk_temporary(
                job_id, chunk.id, uuid.uuid4().hex
            )
            self.generation.begin_chunk(job_id, chunk.database_id)
            try:
                result = provider.synthesize(
                    SynthesisRequest(
                        text=text,
                        destination=provider_output,
                        language=profile.get("language"),
                        instruction=profile["instruction"],
                        reference_audio=reference,
                        seed=deterministic_chunk_seed(
                            str(job["book_id"]), chunk.chapter_index, chunk.chunk_index
                        ),
                        parameters=dict(profile.get("sampling") or {}),
                    )
                )
                if result.audio_path.resolve() != provider_output.resolve():
                    raise RuntimeError("TTS provider returned an unexpected audio path")
                validate_wave(provider_output)
                if self.store.sha256(provider_output) != result.audio_sha256:
                    raise RuntimeError("Provider audio failed hash validation")
                master = self.masters.normalize(provider_output, destination)
                master_hash = self.store.sha256(destination)
                self.generation.complete_chunk(
                    job_id,
                    chunk.database_id,
                    destination.relative_to(self.layout.root).as_posix(),
                    master_hash,
                    master.duration_seconds,
                )
                if replacing_completed_audio:
                    self._invalidate_outputs(job_id, chunk.chapter_index)
            except Exception as exc:
                self.generation.fail_chunk(job_id, chunk.database_id, str(exc))
                raise
            finally:
                provider_output.unlink(missing_ok=True)
        self.generation.complete_job(job_id)

    def prepare_chunk_regeneration(self, job_id: str, chunk_id: str) -> None:
        chunk = self.generation.get_chunk(job_id, chunk_id)
        if chunk.status != "completed" or not self._completed_chunk_is_valid(chunk):
            raise ValueError("Only a verified, completed audio chunk can be regenerated")
        self.generation.begin_chunk_regeneration(job_id, chunk_id)

    def regenerate_chunk(
        self, job_id: str, chunk_id: str, *, prepared: bool = False
    ) -> None:
        """Replace one chunk atomically while leaving every other chunk untouched."""
        if not prepared:
            self.prepare_chunk_regeneration(job_id, chunk_id)
        job = self.generation.get_job(job_id)
        chunk = self.generation.get_chunk(job_id, chunk_id)
        profile = job["profile"]
        destination = self.layout.job_chunk_master(job["book_id"], job_id, chunk.id)
        provider_output = self.layout.job_chunk_temporary(
            job_id, chunk.id, uuid.uuid4().hex
        )
        try:
            if hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest() != job[
                "profile_sha256"
            ]:
                raise RuntimeError("Voice profile failed hash validation")
            reference = self._artifact_path(profile["reference_artifact_path"])
            if self.store.sha256(reference) != profile["reference_sha256"]:
                raise RuntimeError("Voice reference failed hash validation")
            text_path = self._artifact_path(chunk.text_artifact_path)
            text = text_path.read_text(encoding="utf-8").rstrip("\n")
            if text_sha256(text) != chunk.text_sha256:
                raise RuntimeError(f"Synthesis text failed hash validation: {chunk.id}")
            result = self.provider_factory(job).synthesize(
                SynthesisRequest(
                    text=text,
                    destination=provider_output,
                    language=profile.get("language"),
                    instruction=profile["instruction"],
                    reference_audio=reference,
                    seed=deterministic_chunk_seed(
                        str(job["book_id"]), chunk.chapter_index, chunk.chunk_index
                    ),
                    parameters=dict(profile.get("sampling") or {}),
                )
            )
            if result.audio_path.resolve() != provider_output.resolve():
                raise RuntimeError("TTS provider returned an unexpected audio path")
            validate_wave(provider_output)
            if self.store.sha256(provider_output) != result.audio_sha256:
                raise RuntimeError("Regenerated audio failed hash validation")
            master = self.masters.normalize(provider_output, destination)
            regenerated_hash = self.store.sha256(destination)
            self.generation.complete_chunk(
                job_id,
                chunk.database_id,
                destination.relative_to(self.layout.root).as_posix(),
                regenerated_hash,
                master.duration_seconds,
            )
            self._invalidate_outputs(job_id, chunk.chapter_index)
            self.generation.finish_chunk_regeneration(job_id)
        except Exception as exc:
            self.generation.fail_chunk_regeneration(job_id, chunk.database_id, str(exc))
            raise
        finally:
            provider_output.unlink(missing_ok=True)

    def _invalidate_outputs(self, job_id: str, chapter_index: int) -> None:
        for relative_path in self.generation.invalidate_job_outputs(
            job_id, chapter_index
        ):
            try:
                self._artifact_path(relative_path).unlink(missing_ok=True)
            except OSError:
                pass

    def _artifact_path(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path

    def _materialize_job_voice_references(self) -> None:
        """Move legacy job snapshots onto job-owned, independently deletable WAVs."""
        for job in self.generation.list_job_voice_snapshots():
            profile = dict(job["profile"])
            relative = profile.get("reference_artifact_path")
            expected_hash = profile.get("reference_sha256")
            if not relative or not expected_hash:
                continue
            destination = self.layout.job_voice_reference(job["book_id"], job["id"])
            stored_path = self._artifact_path(str(relative))
            if stored_path == destination and self._valid_reference(
                destination, str(expected_hash)
            ):
                continue
            candidates = [stored_path]
            current = job.get("current_profile") or {}
            current_relative = current.get("reference_artifact_path")
            if current_relative:
                candidates.append(self._artifact_path(str(current_relative)))
            source = next(
                (
                    candidate
                    for candidate in candidates
                    if self._valid_reference(candidate, str(expected_hash))
                ),
                None,
            )
            if source is None:
                continue
            copied_hash = self.store.copy(source, destination)
            profile["reference_artifact_path"] = destination.relative_to(
                self.layout.root
            ).as_posix()
            profile["reference_sha256"] = copied_hash
            snapshot_hash = hashlib.sha256(
                _canonical_json(profile).encode("utf-8")
            ).hexdigest()
            self.generation.update_job_voice_snapshot(job["id"], profile, snapshot_hash)

    def _remove_abandoned_provider_audio(self, job_id: str) -> None:
        job = self.generation.get_job(job_id)
        safe_job_id = str(job["id"])
        self.layout.job_root(str(job["book_id"]), safe_job_id)
        for path in self.layout.temporary_root.glob(
            f"generation-{safe_job_id}-*.wav"
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _valid_reference(self, path: Path, expected_hash: str) -> bool:
        try:
            validate_wave(path)
            return self.store.sha256(path) == expected_hash
        except (OSError, ValueError):
            return False

    def _reset_legacy_reused_chunks(self) -> None:
        """Remove cross-job copies created without a synthesis attempt.

        Older builds reused audio by book text alone, which could attach a previous
        narrator's output to a new job. Genuine synthesis always increments attempts
        before completion, so zero-attempt completed chunks identify those copies.
        """
        for chunk in self.generation.list_unattempted_completed_chunks():
            try:
                self._artifact_path(chunk.audio_artifact_path).unlink(missing_ok=True)
            except (OSError, RuntimeError):
                pass
            self.generation.reset_chunk(chunk.job_id, chunk.database_id)

    def _completed_chunk_is_valid(self, chunk: object) -> bool:
        if not chunk.audio_artifact_path or not chunk.audio_sha256:
            return False
        path = self._artifact_path(chunk.audio_artifact_path)
        try:
            self.masters.validate(path)
            return self.store.sha256(path) == chunk.audio_sha256
        except (OSError, ValueError):
            return False

    def verified_chunk_path(self, job_id: str, chunk_id: str) -> Path:
        chunk = self.generation.get_chunk(job_id, chunk_id)
        if not self._completed_chunk_is_valid(chunk):
            raise ValueError("Chunk has no verified editable audio master")
        return self._artifact_path(str(chunk.audio_artifact_path))

    @staticmethod
    def _openmoss_provider(job: dict[str, object]) -> TTSProvider:
        if job["provider_kind"] != "openmoss":
            raise ValueError(f"Unsupported provider kind: {job['provider_kind']}")
        configuration = dict(job["provider_configuration"])
        return OpenMossProvider(
            OpenMossConfig.from_connection(str(job["endpoint_url"]), configuration)
        )
