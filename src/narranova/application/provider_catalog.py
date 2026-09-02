"""Capability metadata for pluggable external TTS connection types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderType:
    id: str
    label: str
    description: str
    supports_instructions: bool
    supports_reference_audio: bool
    reference_audio_optional: bool


PROVIDER_TYPES: tuple[ProviderType, ...] = (
    ProviderType(
        id="openmoss",
        label="OpenMOSS",
        description="Natural-language voice direction with optional reference-audio cloning.",
        supports_instructions=True,
        supports_reference_audio=True,
        reference_audio_optional=True,
    ),
)


def provider_type(kind: str) -> ProviderType:
    match = next((item for item in PROVIDER_TYPES if item.id == kind), None)
    if match is None:
        raise ValueError(f"Unsupported TTS connection type: {kind}")
    return match
