"""Lossless, atomic assembly of compatible generated WAV chunks."""

from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from pathlib import Path

from narranova.audio.validation import WaveInfo, validate_wave


@dataclass(frozen=True)
class AssembledWave:
    path: Path
    info: WaveInfo


def assemble_wave(sources: list[Path], destination: Path) -> AssembledWave:
    if not sources:
        raise ValueError("A chapter cannot be assembled without audio chunks")
    expected = validate_wave(sources[0])
    for source in sources[1:]:
        validate_wave(
            source,
            expected_channels=expected.channels,
            expected_sample_width=expected.sample_width,
            expected_sample_rate=expected.sample_rate,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(expected.channels)
            output.setsampwidth(expected.sample_width)
            output.setframerate(expected.sample_rate)
            for source in sources:
                with wave.open(str(source), "rb") as incoming:
                    while frames := incoming.readframes(64 * 1024):
                        output.writeframesraw(frames)
        with temporary.open("rb") as assembled:
            os.fsync(assembled.fileno())
        info = validate_wave(
            temporary,
            expected_channels=expected.channels,
            expected_sample_width=expected.sample_width,
            expected_sample_rate=expected.sample_rate,
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return AssembledWave(destination, info)
