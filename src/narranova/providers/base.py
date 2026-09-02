"""Contracts implemented by external TTS provider adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ProviderCapabilities:
    voice_presets: bool = False
    voice_description: bool = False
    reference_audio: bool = False
    stochastic_variants: bool = False
    seed: bool = False
    timestamps: bool = False
    streaming: bool = False
    supported_languages: tuple[str, ...] = ()
    supported_audio_formats: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    destination: Path
    language: str | None = None
    voice_id: str | None = None
    instruction: str | None = None
    reference_audio: Path | None = None
    seed: int | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Synthesis text cannot be empty")
        if not self.destination.name:
            raise ValueError("Synthesis destination must name a file")


@dataclass(frozen=True)
class SynthesisResult:
    audio_path: Path
    audio_sha256: str
    duration_seconds: float
    provider_request_id: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)


class TTSProvider(Protocol):
    def health(self) -> Mapping[str, Any]: ...

    def list_models(self) -> Sequence[Mapping[str, Any]]: ...

    def list_voices(self) -> Sequence[Mapping[str, Any]]: ...

    def capabilities(self) -> ProviderCapabilities: ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult: ...
