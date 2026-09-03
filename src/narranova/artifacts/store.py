"""Validated atomic writes beneath the persistent artifact root."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _validate_destination(self, destination: Path) -> Path:
        resolved = destination.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Artifact destination must remain beneath the data directory")
        return resolved

    def copy(self, source: Path, destination: Path) -> str:
        target = self._validate_destination(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self.sha256(target)

    def write_text(self, destination: Path, content: str) -> str:
        target = self._validate_destination(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self.sha256(target)

    def write_bytes(self, destination: Path, content: bytes) -> str:
        target = self._validate_destination(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            with temporary.open("wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self.sha256(target)

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
