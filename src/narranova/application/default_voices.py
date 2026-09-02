"""Validated, immutable narrator pairs packaged with Narranova."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from narranova.audio import validate_wave


BUILTIN_VOICE_PREFIX = "builtin:"


@dataclass(frozen=True)
class DefaultVoicePair:
    id: str
    name: str
    gender: str
    instruction: str
    sample_text: str
    audio_path: Path
    audio_sha256: str
    provider_kind: str = "openmoss"

    @property
    def selector_id(self) -> str:
        return f"{BUILTIN_VOICE_PREFIX}{self.id}"

    def profile_snapshot(self) -> dict[str, object]:
        return {
            "kind": self.provider_kind,
            "name": self.name,
            "instruction": self.instruction,
            "language": "English",
            "builtin_voice_id": self.id,
            "sample_text": self.sample_text,
        }


@lru_cache(maxsize=1)
def default_voice_pairs() -> tuple[DefaultVoicePair, ...]:
    root_resource = files("narranova.default_voices")
    manifest_resource = root_resource.joinpath("catalog.json")
    try:
        manifest = json.loads(manifest_resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Built-in narrator catalog is missing or invalid") from exc
    sample_text = _required_text(manifest, "sample_text")
    voices = manifest.get("voices")
    if not isinstance(voices, list) or not voices:
        raise RuntimeError("Built-in narrator catalog has no voices")
    root = Path(str(root_resource)).resolve()
    pairs: list[DefaultVoicePair] = []
    seen: set[str] = set()
    for item in voices:
        if not isinstance(item, dict):
            raise RuntimeError("Built-in narrator catalog contains an invalid voice")
        try:
            voice_id = f"{int(item.get('id')):02d}"
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Built-in narrator catalog contains an invalid id") from exc
        if voice_id in seen:
            raise RuntimeError(f"Duplicate built-in narrator id: {voice_id}")
        seen.add(voice_id)
        filename = _required_text(item, "filename")
        if Path(filename).name != filename or not filename.lower().endswith(".wav"):
            raise RuntimeError(f"Invalid built-in narrator filename: {filename}")
        audio_path = (root / filename).resolve()
        if not audio_path.is_relative_to(root) or not audio_path.is_file():
            raise RuntimeError(f"Built-in narrator audio is missing: {filename}")
        validate_wave(audio_path)
        pairs.append(
            DefaultVoicePair(
                id=voice_id,
                name=_required_text(item, "name"),
                gender=_required_text(item, "gender"),
                instruction=_required_text(item, "style_instruction"),
                sample_text=sample_text,
                audio_path=audio_path,
                audio_sha256=_sha256(audio_path),
            )
        )
    return tuple(pairs)


def default_voice_pair(selector_id: str) -> DefaultVoicePair:
    if not selector_id.startswith(BUILTIN_VOICE_PREFIX):
        raise KeyError(f"Built-in narrator not found: {selector_id}")
    voice_id = selector_id.removeprefix(BUILTIN_VOICE_PREFIX)
    match = next((item for item in default_voice_pairs() if item.id == voice_id), None)
    if match is None:
        raise KeyError(f"Built-in narrator not found: {selector_id}")
    return match


def _required_text(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError(f"Built-in narrator catalog requires {key}")
    return result.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
