"""Normalize provider WAV output into compact, lossless narration masters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from narranova.audio.validation import validate_wave


@dataclass(frozen=True)
class AudioMasterInfo:
    path: Path
    codec: str
    channels: int
    sample_rate: int
    duration_seconds: float
    byte_size: int


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class FFmpegAudioMasters:
    """Create and validate 48 kHz mono FLAC working masters."""

    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runner = runner

    def normalize(self, source: Path, destination: Path) -> AudioMasterInfo:
        source_info = validate_wave(source)
        self._require_tools()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.part.flac")
        command = [
            self.ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "flac",
            "-compression_level",
            "8",
            str(temporary),
        ]
        try:
            self._run(command)
            info = self.validate(temporary)
            tolerance = max(0.05, source_info.duration_seconds * 0.001)
            if abs(info.duration_seconds - source_info.duration_seconds) > tolerance:
                raise RuntimeError("Normalized FLAC duration does not match provider audio")
            with temporary.open("rb") as output:
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return AudioMasterInfo(
            path=destination,
            codec=info.codec,
            channels=info.channels,
            sample_rate=info.sample_rate,
            duration_seconds=info.duration_seconds,
            byte_size=destination.stat().st_size,
        )

    def validate(self, path: Path) -> AudioMasterInfo:
        self._require_tools()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Audio master is missing or empty: {path}")
        result = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,channels,sample_rate:format=duration",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            payload = json.loads(result.stdout)
            stream = payload["streams"][0]
            codec = str(stream["codec_name"])
            channels = int(stream["channels"])
            sample_rate = int(stream["sample_rate"])
            duration = float(payload["format"]["duration"])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"FFprobe returned no usable audio stream for {path}") from exc
        if codec != "flac" or channels != 1 or sample_rate != 48_000 or duration <= 0:
            raise ValueError(
                "Audio master must be positive-duration 48 kHz mono FLAC; "
                f"received {codec}, {sample_rate} Hz, {channels} channel(s)"
            )
        return AudioMasterInfo(
            path=path,
            codec=codec,
            channels=channels,
            sample_rate=sample_rate,
            duration_seconds=duration,
            byte_size=path.stat().st_size,
        )

    def _require_tools(self) -> None:
        missing = [
            command
            for command in (self.ffmpeg, self.ffprobe)
            if shutil.which(command) is None and not Path(command).is_file()
        ]
        if missing:
            raise RuntimeError(
                "FFmpeg and FFprobe are required to store audio masters; missing: "
                + ", ".join(missing)
            )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Audio processing failed: {message}") from exc
