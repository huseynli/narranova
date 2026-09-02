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
from typing import Any, Mapping, Sequence

from narranova.audio import validate_wave
from narranova.providers.base import (
    ProviderCapabilities,
    SynthesisRequest,
    SynthesisResult,
)


@dataclass(frozen=True)
class OpenMossConfig:
    endpoint_url: str
    max_new_tokens: int = 6000
    stream_chunk_frames: int = 16
    default_sample_rate: int = 48_000
    default_channels: int = 2
    sample_width: int = 2
    timeout_seconds: float | None = None

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
        sampling = dict(request.parameters)
        if request.seed is not None:
            sampling["seed"] = request.seed
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
                        while block := response.read(64 * 1024):
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
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return SynthesisResult(
            audio_path=destination,
            audio_sha256=digest,
            duration_seconds=info.duration_seconds,
            usage={
                "characters": len(request.text),
                "audio_bytes": byte_count,
                "wall_seconds": time.monotonic() - started,
            },
        )
