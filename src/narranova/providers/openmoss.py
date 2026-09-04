"""Dedicated adapter for the non-OpenAI-compatible OpenMOSS streaming API."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, TypedDict, cast

from narranova.audio import validate_wave
from narranova.providers.base import (
    ProviderCapabilities,
    SynthesisCancelled,
    SynthesisRequest,
    SynthesisResult,
)


OPENMOSS_DEFAULT_MAX_NEW_TOKENS = 6000
OPENMOSS_STREAM_FRAME_OPTIONS = (16, 32, 64, 128, 256, 512)


class OpenMossSampling(TypedDict, total=False):
    """Explicit OpenMOSS sampling overrides; absent keys mean engine default."""

    text_temperature: float
    text_top_p: float
    text_top_k: int
    audio_temperature: float
    audio_top_p: float
    audio_top_k: int
    audio_repetition_penalty: float
    seed: int


@dataclass(frozen=True)
class OpenMossSamplingField:
    name: str
    label: str
    help_text: str
    minimum: float
    maximum: float
    integer: bool = False


OPENMOSS_SAMPLING_FIELDS: tuple[OpenMossSamplingField, ...] = (
    OpenMossSamplingField(
        "text_temperature",
        "Text temperature",
        "Controls randomness in text/control-token sampling. Lower is more "
        "predictable; higher adds variation.",
        0.0,
        5.0,
    ),
    OpenMossSamplingField(
        "text_top_p",
        "Text top-p",
        "Restricts text-token sampling to the most probable probability mass. "
        "Lower values are more conservative.",
        0.01,
        1.0,
    ),
    OpenMossSamplingField(
        "text_top_k",
        "Text top-k",
        "Limits text sampling to the top K candidate tokens. Lower values reduce variation.",
        1,
        1000,
        True,
    ),
    OpenMossSamplingField(
        "audio_temperature",
        "Audio temperature",
        "Controls speech variation. Lower values are usually steadier; higher values "
        "may add expression but can increase instability or artifacts.",
        0.0,
        5.0,
    ),
    OpenMossSamplingField(
        "audio_top_p",
        "Audio top-p",
        "Restricts the audio-token probability pool. Lower values are generally "
        "more conservative.",
        0.01,
        1.0,
    ),
    OpenMossSamplingField(
        "audio_top_k",
        "Audio top-k",
        "Limits how many audio-token candidates can be selected. Lower values reduce variation.",
        1,
        1000,
        True,
    ),
    OpenMossSamplingField(
        "audio_repetition_penalty",
        "Audio repetition penalty",
        "Discourages repeated audio-token patterns. Excessive values can sound unnatural.",
        0.1,
        5.0,
    ),
    OpenMossSamplingField(
        "seed",
        "Seed",
        "Reproduces a candidate when text, reference, instruction, and sampling match. "
        "Different seeds may change pacing, emphasis, prosody, or an instruction-only voice.",
        0,
        2_147_483_647,
        True,
    ),
)


def normalize_openmoss_sampling(values: Mapping[str, object] | None) -> OpenMossSampling:
    """Validate explicit sampling overrides without inventing engine defaults."""

    if not values:
        return {}
    specifications = {field.name: field for field in OPENMOSS_SAMPLING_FIELDS}
    unknown = set(values).difference(specifications)
    if unknown:
        raise ValueError(f"Unsupported OpenMOSS sampling setting(s): {', '.join(sorted(unknown))}")
    normalized: dict[str, int | float] = {}
    for name, raw_value in values.items():
        field = specifications[name]
        if isinstance(raw_value, bool):
            raise ValueError(f"{field.label} must be a number")
        try:
            number = float(cast(int | float | str, raw_value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.label} must be a number") from exc
        if not field.minimum <= number <= field.maximum:
            raise ValueError(
                f"{field.label} must be between {field.minimum:g} and {field.maximum:g}"
            )
        if field.integer:
            if not number.is_integer():
                raise ValueError(f"{field.label} must be a whole number")
            normalized[name] = int(number)
        else:
            normalized[name] = number
    return cast(OpenMossSampling, normalized)


def openmoss_sampling_from_form(fields: Mapping[str, str]) -> OpenMossSampling:
    """Parse non-blank VoiceLab fields; blank inputs deliberately remain absent."""

    explicit = {
        field.name: fields[field.name].strip()
        for field in OPENMOSS_SAMPLING_FIELDS
        if fields.get(field.name, "").strip()
    }
    return normalize_openmoss_sampling(explicit)


def openmoss_performance_settings(values: Mapping[str, object] | None) -> dict[str, int]:
    """Read only request-level performance settings from connection configuration."""

    configuration = values or {}
    frames_raw = configuration.get("stream_chunk_frames", 16)
    tokens_raw = configuration.get("max_new_tokens", OPENMOSS_DEFAULT_MAX_NEW_TOKENS)
    if isinstance(frames_raw, bool) or isinstance(tokens_raw, bool):
        raise ValueError("OpenMOSS performance settings must be whole numbers")
    try:
        frames_number = float(cast(int | float | str, frames_raw))
        tokens_number = float(cast(int | float | str, tokens_raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenMOSS performance settings must be whole numbers") from exc
    if not frames_number.is_integer() or not tokens_number.is_integer():
        raise ValueError("OpenMOSS performance settings must be whole numbers")
    frames = int(frames_number)
    tokens = int(tokens_number)
    if frames <= 0 or tokens <= 0:
        raise ValueError("OpenMOSS performance settings must be positive")
    return {"stream_chunk_frames": frames, "max_new_tokens": tokens}


@dataclass(frozen=True)
class OpenMossConfig:
    endpoint_url: str
    max_new_tokens: int = OPENMOSS_DEFAULT_MAX_NEW_TOKENS
    stream_chunk_frames: int = 16
    default_sample_rate: int = 48_000
    default_channels: int = 2
    sample_width: int = 2
    # urllib applies this to connection and individual socket reads.  Keeping a
    # generous finite default prevents a dead endpoint from pinning a worker
    # forever while still allowing slower homelab hardware to synthesize.
    timeout_seconds: float | None = 600.0

    @classmethod
    def from_connection(
        cls,
        endpoint_url: str,
        configuration: Mapping[str, object] | None,
        *,
        timeout_seconds: float | None = 600.0,
    ) -> "OpenMossConfig":
        return cls(
            endpoint_url,
            **openmoss_performance_settings(configuration),
            timeout_seconds=timeout_seconds,
        )

    def __post_init__(self) -> None:
        if not self.endpoint_url.startswith(("http://", "https://")):
            raise ValueError("OpenMOSS endpoint must use HTTP or HTTPS")
        if min(
            self.max_new_tokens,
            self.stream_chunk_frames,
            self.default_sample_rate,
            self.default_channels,
            self.sample_width,
        ) <= 0:
            raise ValueError("OpenMOSS numeric settings must be positive")


class OpenMossProvider:
    def __init__(self, config: OpenMossConfig) -> None:
        self.config = config

    def health(self) -> Mapping[str, Any]:
        info_url = self.config.endpoint_url.rsplit("/tts", 1)[0].rstrip("/") + "/info"
        request = urllib.request.Request(info_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not connect to OpenMOSS: {exc.reason}") from exc
        try:
            result = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("OpenMOSS /info returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("OpenMOSS /info did not return an object")
        return result

    def list_models(self) -> Sequence[Mapping[str, Any]]:
        info = self.health()
        architecture = info.get("architecture")
        return ({"id": str(architecture), "architecture": architecture},) if architecture else ()

    def list_voices(self) -> Sequence[Mapping[str, Any]]:
        return ()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            voice_description=True,
            reference_audio=True,
            stochastic_variants=True,
            seed=True,
            streaming=True,
            supported_audio_formats=("pcm",),
        )

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if request.cancel_requested and request.cancel_requested():
            raise SynthesisCancelled("Synthesis was cancelled")
        if request.reference_audio is not None and not request.reference_audio.is_file():
            raise FileNotFoundError(f"Reference audio not found: {request.reference_audio}")
        if not request.instruction or not request.instruction.strip():
            raise ValueError("OpenMOSS synthesis requires a narrator instruction")
        forbidden = {
            "text", "instruction", "language", "reference_wav_b64", "ref_text",
            "stream", "stream_chunk_frames", "response_format", "max_new_tokens",
        }
        overlap = forbidden.intersection(request.parameters)
        if overlap:
            raise ValueError(f"Reserved OpenMOSS parameter(s): {', '.join(sorted(overlap))}")
        payload: dict[str, Any] = {
            "text": request.text,
            "instruction": request.instruction,
            "max_new_tokens": self.config.max_new_tokens,
            "stream": True,
            "stream_chunk_frames": self.config.stream_chunk_frames,
            "response_format": "pcm",
        }
        if request.reference_audio is not None:
            payload["reference_wav_b64"] = base64.b64encode(
                request.reference_audio.read_bytes()
            ).decode("ascii")
        if request.language:
            payload["language"] = request.language
        sampling = dict(normalize_openmoss_sampling(request.parameters))
        if request.seed is not None:
            sampling["seed"] = request.seed
        sampling = dict(normalize_openmoss_sampling(sampling))
        if sampling:
            payload["sampling"] = sampling
        http_request = urllib.request.Request(
            self.config.endpoint_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        destination = request.destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")
        started = time.monotonic()
        byte_count = 0
        try:
            try:
                response_context = urllib.request.urlopen(
                    http_request, timeout=self.config.timeout_seconds
                )
                with response_context as response:
                    sample_rate = int(
                        response.headers.get("X-MOSS-Sample-Rate", self.config.default_sample_rate)
                    )
                    channels = int(
                        response.headers.get("X-MOSS-Channels", self.config.default_channels)
                    )
                    if sample_rate <= 0 or channels <= 0:
                        raise RuntimeError("OpenMOSS returned an invalid PCM format")
                    with wave.open(str(temporary), "wb") as output:
                        output.setnchannels(channels)
                        output.setsampwidth(self.config.sample_width)
                        output.setframerate(sample_rate)
                        reader = getattr(response, "read1", response.read)
                        first_audio_seconds: float | None = None
                        while block := reader(64 * 1024):
                            if request.cancel_requested and request.cancel_requested():
                                raise SynthesisCancelled("Synthesis was cancelled")
                            if first_audio_seconds is None:
                                first_audio_seconds = time.monotonic() - started
                            output.writeframesraw(block)
                            byte_count += len(block)
            except urllib.error.HTTPError as exc:
                body = exc.read(4096).decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenMOSS HTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Could not connect to OpenMOSS: {exc.reason}") from exc
            if byte_count == 0:
                raise RuntimeError("OpenMOSS stream completed without audio")
            if byte_count % (channels * self.config.sample_width):
                raise RuntimeError("OpenMOSS returned a truncated PCM frame")
            info = validate_wave(
                temporary,
                expected_channels=channels,
                expected_sample_width=self.config.sample_width,
                expected_sample_rate=sample_rate,
            )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        digest_builder = hashlib.sha256()
        with destination.open("rb") as audio:
            for block in iter(lambda: audio.read(1024 * 1024), b""):
                digest_builder.update(block)
        digest = digest_builder.hexdigest()
        return SynthesisResult(
            audio_path=destination,
            audio_sha256=digest,
            duration_seconds=info.duration_seconds,
            usage={
                "characters": len(request.text),
                "audio_bytes": byte_count,
                "wall_seconds": time.monotonic() - started,
                "first_audio_seconds": first_audio_seconds,
            },
        )
