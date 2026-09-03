"""FFmpeg-backed chapterized M4B encoding."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class M4BChapter:
    title: str
    paths: tuple[Path, ...]
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class EncodedM4B:
    path: Path
    duration_seconds: float


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class FFmpegM4BEncoder:
    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runner = runner

    def encode(
        self,
        chapters: list[M4BChapter],
        destination: Path,
        metadata: dict[str, str],
        workspace: Path,
        cover: Path | None = None,
    ) -> EncodedM4B:
        if not chapters:
            raise ValueError("An M4B requires at least one chapter")
        self._require_tools()
        workspace.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        ffmetadata = workspace / "chapters.ffmetadata"
        ffmetadata.write_text(self._metadata(chapters, metadata), encoding="utf-8")
        temporary = destination.with_name(f".{destination.name}.part.m4b")
        sources = [path for chapter in chapters for path in chapter.paths]
        if not sources:
            raise ValueError("An M4B requires at least one audio master")
        command = [self.ffmpeg, "-nostdin", "-y"]
        for source in sources:
            command.extend(("-i", str(source)))
        metadata_input = len(sources)
        command.extend(("-f", "ffmetadata", "-i", str(ffmetadata)))
        cover_input = metadata_input + 1
        if cover is not None:
            command.extend(("-i", str(cover)))
        if len(sources) == 1:
            command.extend(("-map", "0:a:0"))
        else:
            inputs = "".join(f"[{index}:a:0]" for index in range(len(sources)))
            command.extend(
                (
                    "-filter_complex",
                    f"{inputs}concat=n={len(sources)}:v=0:a=1[audiobook]",
                    "-map",
                    "[audiobook]",
                )
            )
        if cover is not None:
            command.extend(
                (
                    "-map",
                    f"{cover_input}:v:0",
                    "-c:v",
                    "mjpeg",
                    "-frames:v",
                    "1",
                    "-disposition:v:0",
                    "attached_pic",
                )
            )
        command.extend(
            (
                "-map_metadata",
                str(metadata_input),
                "-map_chapters",
                str(metadata_input),
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                "-f",
                "ipod",
                str(temporary),
            )
        )
        try:
            self._run(command)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("FFmpeg did not produce an M4B")
            duration = self.probe_duration(temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return EncodedM4B(destination, duration)

    def probe_duration(self, path: Path) -> float:
        self._require_tools()
        result = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            duration = float(json.loads(result.stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("FFprobe returned no usable audiobook duration") from exc
        if duration <= 0:
            raise RuntimeError("Encoded audiobook contains no audio duration")
        return duration

    def _require_tools(self) -> None:
        missing = [
            command
            for command in (self.ffmpeg, self.ffprobe)
            if shutil.which(command) is None and not Path(command).is_file()
        ]
        if missing:
            raise RuntimeError(
                "FFmpeg and FFprobe are required to build an M4B; missing: "
                + ", ".join(missing)
            )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"FFmpeg failed: {message}") from exc

    @classmethod
    def _metadata(
        cls, chapters: list[M4BChapter], metadata: dict[str, str]
    ) -> str:
        lines = [";FFMETADATA1"]
        lines.extend(
            f"{key}={cls._escape(value)}" for key, value in metadata.items() if value
        )
        for chapter in chapters:
            lines.extend(
                (
                    "[CHAPTER]",
                    "TIMEBASE=1/1000",
                    f"START={round(chapter.start_seconds * 1000)}",
                    f"END={round(chapter.end_seconds * 1000)}",
                    f"title={cls._escape(chapter.title)}",
                )
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _escape(value: str) -> str:
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("=", "\\=")
            .replace(";", "\\;")
            .replace("#", "\\#")
        )
