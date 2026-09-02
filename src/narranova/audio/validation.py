"""Validation for audio promoted into Narranova's artifact store."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WaveInfo:
    channels: int
    sample_width: int
    sample_rate: int
    frames: int
    duration_seconds: float


def validate_wave(
    path: Path,
    *,
    expected_channels: int | None = None,
    expected_sample_width: int | None = None,
    expected_sample_rate: int | None = None,
) -> WaveInfo:
    if not path.is_file() or path.stat().st_size <= 44:
        raise ValueError(f"WAV is missing or empty: {path}")
    try:
        with wave.open(str(path), "rb") as source:
            info = WaveInfo(
                channels=source.getnchannels(),
                sample_width=source.getsampwidth(),
                sample_rate=source.getframerate(),
                frames=source.getnframes(),
                duration_seconds=(source.getnframes() / source.getframerate()),
            )
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"Invalid WAV file: {path}") from exc
    if min(info.channels, info.sample_width, info.sample_rate, info.frames) <= 0:
        raise ValueError(f"WAV contains no usable audio frames: {path}")
    expected = {
        "channels": expected_channels,
        "sample_width": expected_sample_width,
        "sample_rate": expected_sample_rate,
    }
    for attribute, value in expected.items():
        if value is not None and getattr(info, attribute) != value:
            raise ValueError(
                f"WAV {attribute} is {getattr(info, attribute)}; expected {value}"
            )
    return info
