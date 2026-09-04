"""Temporary voice auditions promoted into durable named voice profiles."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from narranova.application.generation import VoiceProfiles
from narranova.application.provider_catalog import provider_type
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import validate_wave
from narranova.persistence.generation import GenerationRepository
from narranova.providers import (
    OpenMossConfig,
    OpenMossProvider,
    SynthesisRequest,
    TTSProvider,
    normalize_openmoss_sampling,
)


INSTRUCTION_PRESETS: tuple[tuple[str, str], ...] = (
    (
        "Natural contemporary fiction",
        "Read as a polished contemporary audiobook narrator: conversational and grounded, "
        "with crisp diction, medium pacing, and natural sentence endings. Keep character "
        "dialogue distinct through subtle rhythm and attitude, not impersonation.",
    ),
    (
        "Expansive science fiction",
        "Narrate with calm authority and a sense of scale. Keep exposition lucid, technical "
        "terms precise, and pacing deliberate without becoming slow. Let wonder, tension, "
        "and dry humor emerge from the prose; use restrained distinctions for dialogue.",
    ),
    (
        "Intimate mystery",
        "Use a close, observant delivery with controlled tension. Favor clean pauses, "
        "purposeful emphasis, and a steady pace. Keep revelations understated and dialogue "
        "believable; avoid trailer-style intensity, whispering, or exaggerated suspense.",
    ),
    (
        "Direct narrative nonfiction",
        "Read with clarity, curiosity, and quiet confidence. Maintain an even, energetic "
        "pace, articulate names and numbers carefully, and use brief pauses to separate "
        "ideas. Sound engaged rather than promotional, academic, or overly solemn.",
    ),
)

DEFAULT_SAMPLE_TEXT = (
    "The rain had stopped, but the city still shimmered beneath the lamps. "
    "She folded the letter once, set it beside the cup, and waited. "
    "By morning, the road would be open—and nothing would feel quite the same."
)


class VoiceStudio:
    def __init__(
        self,
        generation: GenerationRepository,
        profiles: VoiceProfiles,
        layout: ArtifactLayout,
        store: ArtifactStore,
        provider_factory: Callable[[dict[str, Any]], TTSProvider] | None = None,
    ) -> None:
        self.generation = generation
        self.profiles = profiles
        self.layout = layout
        self.store = store
        self.provider_factory = provider_factory or self._openmoss_provider
        self.layout.voice_studio_root.mkdir(parents=True, exist_ok=True)
        self.cleanup_stale()

    def start(self) -> str:
        self.cleanup_stale()
        draft_id = uuid.uuid4().hex
        now = time.time()
        draft: dict[str, Any] = {
            "version": 1,
            "id": draft_id,
            "created_at": now,
            "updated_at": now,
            "name": "",
            "provider_id": "",
            "instruction": INSTRUCTION_PRESETS[0][1],
            "language": "English",
            "sample_text": DEFAULT_SAMPLE_TEXT,
            "sampling": {},
            "uploaded_reference_path": None,
            "uploaded_reference_sha256": None,
            "takes": [],
            "audition_status": "idle",
            "audition_error": None,
        }
        self._write(draft)
        return draft_id

    def get(self, draft_id: str) -> dict[str, Any]:
        path = self.layout.voice_studio_manifest(draft_id)
        if not path.is_file():
            raise KeyError(f"Voice Studio draft not found: {draft_id}")
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Voice Studio draft is corrupt") from exc
        if draft.get("id") != draft_id:
            raise RuntimeError("Voice Studio draft identity mismatch")
        return draft

    def generate_take(
        self,
        draft_id: str,
        *,
        provider_id: str,
        reference_choice: str,
        instruction: str,
        sample_text: str,
        language: str,
        profile_name: str = "",
        uploaded_reference: Path | None = None,
        sampling: Mapping[str, object] | None = None,
    ) -> str:
        draft = self.get(draft_id)
        instruction = instruction.strip()
        sample_text = sample_text.strip()
        if not instruction:
            raise ValueError("Narration instruction cannot be empty")
        if not sample_text:
            raise ValueError("Audition text cannot be empty")
        if len(sample_text) > 2_000:
            raise ValueError("Audition text must be 2,000 characters or fewer")
        provider = self.generation.get_provider(provider_id)
        if not provider["enabled"]:
            raise ValueError("The selected TTS connection is disabled")
        definition = provider_type(str(provider["kind"]))
        sampling_overrides = normalize_openmoss_sampling(sampling)
        if sampling_overrides and not definition.supports_sampling:
            raise ValueError("The selected TTS connection does not support sampling controls")
        if uploaded_reference is not None:
            validate_wave(uploaded_reference)
            upload_path = self.layout.voice_studio_upload(draft_id)
            upload_hash = self.store.copy(uploaded_reference, upload_path)
            draft["uploaded_reference_path"] = upload_path.relative_to(
                self.layout.root
            ).as_posix()
            draft["uploaded_reference_sha256"] = upload_hash
            reference_choice = "uploaded"
        draft.update(
            {
                "name": profile_name.strip(),
                "provider_id": provider_id,
                "instruction": instruction,
                "sample_text": sample_text,
                "language": language.strip() or "English",
                "sampling": dict(sampling_overrides),
                "updated_at": time.time(),
                "audition_status": "generating",
                "audition_error": None,
            }
        )
        self._write(draft)
        reference = self._reference(
            draft,
            reference_choice,
            required=not definition.reference_audio_optional,
        )
        take_id = uuid.uuid4().hex
        destination = self.layout.voice_studio_take(draft_id, take_id)
        owner_id = f"voice-{draft_id}-{take_id}"
        claimed = False
        try:
            self.generation.claim_provider_work(provider_id, owner_id)
            claimed = True
            request_sampling = dict(sampling_overrides)
            seed = request_sampling.pop("seed", None)
            result = self.provider_factory(provider).synthesize(
                SynthesisRequest(
                    text=sample_text,
                    destination=destination,
                    language=language.strip() or "English",
                    instruction=instruction,
                    reference_audio=reference,
                    seed=int(seed) if seed is not None else None,
                    parameters=request_sampling,
                )
            )
            result_path = result.audio_path.resolve()
            if result_path != destination.resolve():
                raise RuntimeError("TTS provider returned an unexpected audition path")
            validate_wave(result_path)
            if self.store.sha256(result_path) != result.audio_sha256:
                raise RuntimeError("Generated audition failed hash validation")
        except Exception as exc:
            destination.unlink(missing_ok=True)
            failed = self.get(draft_id)
            failed["audition_status"] = "failed"
            failed["audition_error"] = str(exc)
            failed["updated_at"] = time.time()
            self._write(failed)
            raise
        finally:
            if claimed:
                self.generation.release_provider_work(owner_id)
        take = {
            "id": take_id,
            "provider_id": provider_id,
            "reference_choice": reference_choice,
            "instruction": instruction,
            "sample_text": sample_text,
            "language": language.strip() or "English",
            "audio_path": result.audio_path.relative_to(self.layout.root).as_posix(),
            "audio_sha256": result.audio_sha256,
            "duration_seconds": result.duration_seconds,
            "sampling": dict(sampling_overrides),
            "created_at": time.time(),
        }
        draft["takes"].append(take)
        draft["audition_status"] = "idle"
        draft["audition_error"] = None
        draft["updated_at"] = time.time()
        self._write(draft)
        return take_id

    def stage_uploaded_reference(self, draft_id: str, source: Path) -> None:
        validate_wave(source)
        destination = self.layout.voice_studio_upload(draft_id)
        digest = self.store.copy(source, destination)
        draft = self.get(draft_id)
        draft["uploaded_reference_path"] = destination.relative_to(
            self.layout.root
        ).as_posix()
        draft["uploaded_reference_sha256"] = digest
        draft["updated_at"] = time.time()
        self._write(draft)

    def mark_audition_queued(self, draft_id: str) -> None:
        draft = self.get(draft_id)
        draft["audition_status"] = "queued"
        draft["audition_error"] = None
        draft["updated_at"] = time.time()
        self._write(draft)

    def save_profile(
        self,
        draft_id: str,
        *,
        name: str,
        provider_id: str,
        reference_choice: str,
        instruction: str,
        language: str,
    ) -> str:
        draft = self.get(draft_id)
        if draft.get("audition_status") in {"queued", "generating"}:
            raise ValueError("Wait for the active audition before saving this profile")
        pair_take = self._pair_take(draft, reference_choice)
        provider_id = str(pair_take["provider_id"])
        instruction = str(pair_take["instruction"])
        language = str(pair_take.get("language") or "English")
        provider = self.generation.get_provider(provider_id)
        if not provider["enabled"]:
            raise ValueError("The selected TTS connection is disabled")
        if provider_type(str(provider["kind"])).id != "openmoss":
            raise ValueError("Saving this provider's voice profiles is not implemented yet")
        if reference_choice.startswith("profile:"):
            raise ValueError(
                "Choose a reference created or uploaded in this Voice Lab draft"
            )
        reference = self._reference(draft, reference_choice, required=True)
        sampling = dict(pair_take.get("sampling") or {})
        profile_id = self.profiles.create_openmoss_profile(
            provider_id=provider_id,
            reference_audio=reference,
            instruction=instruction,
            name=name,
            language=language.strip() or "English",
            sampling=sampling,
        )
        self.discard(draft_id)
        return profile_id

    def take_audio(self, draft_id: str, take_id: str) -> tuple[Path, str]:
        draft = self.get(draft_id)
        take = next((item for item in draft["takes"] if item["id"] == take_id), None)
        if take is None:
            raise KeyError(f"Voice Studio take not found: {take_id}")
        path = self._artifact(str(take["audio_path"]))
        validate_wave(path)
        if self.store.sha256(path) != take["audio_sha256"]:
            raise RuntimeError("Voice Studio audio failed hash validation")
        return path, str(take["audio_sha256"])

    def uploaded_audio(self, draft_id: str) -> tuple[Path, str]:
        draft = self.get(draft_id)
        path = self._reference(draft, "uploaded", required=True)
        assert path is not None
        return path, str(draft["uploaded_reference_sha256"])

    def discard(self, draft_id: str) -> None:
        draft_root = self.layout.voice_studio_draft(draft_id)
        if not draft_root.exists():
            raise KeyError(f"Voice Studio draft not found: {draft_id}")
        draft = self.get(draft_id)
        if draft.get("audition_status") in {"queued", "generating"}:
            raise ValueError("Wait for the active audition before discarding this draft")
        shutil.rmtree(draft_root)

    def cleanup_stale(self, max_age_seconds: int = 24 * 60 * 60) -> None:
        cutoff = time.time() - max_age_seconds
        for path in self.layout.voice_studio_root.iterdir():
            manifest = path / "draft.json"
            modified_at = manifest.stat().st_mtime if manifest.exists() else path.stat().st_mtime
            if path.is_dir() and modified_at < cutoff:
                shutil.rmtree(path, ignore_errors=True)

    def _reference(
        self,
        draft: dict[str, Any],
        choice: str,
        *,
        required: bool = False,
    ) -> Path | None:
        if choice in {"", "none"} and not required:
            return None
        if choice == "uploaded" or choice.startswith("uploaded:"):
            relative = draft.get("uploaded_reference_path")
            expected_hash = draft.get("uploaded_reference_sha256")
        elif choice.startswith("profile:"):
            profile = self.generation.get_voice_and_provider(choice.split(":", 1)[1])
            relative = profile["profile"].get("reference_artifact_path")
            expected_hash = profile["profile"].get("reference_sha256")
        elif choice.startswith("take:"):
            take_id = choice.split(":", 1)[1]
            take = next((item for item in draft["takes"] if item["id"] == take_id), None)
            if take is None:
                raise KeyError(f"Voice Studio take not found: {take_id}")
            relative = take["audio_path"]
            expected_hash = take["audio_sha256"]
        else:
            raise ValueError("Choose or upload the reference WAV to save")
        if not relative or not expected_hash:
            raise ValueError("Choose or upload a reference WAV before generating")
        path = self._artifact(str(relative))
        validate_wave(path)
        if self.store.sha256(path) != expected_hash:
            raise RuntimeError("Reference audio failed hash validation")
        return path

    def _write(self, draft: dict[str, Any]) -> None:
        path = self.layout.voice_studio_manifest(str(draft["id"]))
        self.store.write_text(path, json.dumps(draft, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _pair_take(
        draft: dict[str, Any], reference_choice: str
    ) -> dict[str, Any]:
        if reference_choice.startswith("take:"):
            take_id = reference_choice.split(":", 1)[1]
            take = next(
                (item for item in draft["takes"] if item["id"] == take_id), None
            )
        elif reference_choice.startswith("uploaded:"):
            take_id = reference_choice.split(":", 1)[1]
            take = next(
                (
                    item
                    for item in draft["takes"]
                    if item["id"] == take_id
                    and item.get("reference_choice") == "uploaded"
                ),
                None,
            )
        elif reference_choice == "uploaded":
            take = next(
                (
                    item
                    for item in reversed(draft["takes"])
                    if item.get("reference_choice") == "uploaded"
                ),
                None,
            )
        else:
            take = None
        if take is None:
            raise ValueError("Choose a tested audio and instruction pair to save")
        return take

    def _artifact(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path

    @staticmethod
    def _openmoss_provider(provider: dict[str, Any]) -> TTSProvider:
        if provider["kind"] != "openmoss":
            raise ValueError(f"Unsupported provider kind: {provider['kind']}")
        return OpenMossProvider(
            OpenMossConfig.from_connection(
                str(provider["endpoint_url"]), provider["configuration"]
            )
        )
