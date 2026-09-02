"""Application configuration loaded at process boundaries."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "Settings":
        configured = data_dir if data_dir is not None else os.getenv("NARRANOVA_DATA_DIR", "/data")
        root = Path(configured).expanduser().resolve()
        return cls(data_dir=root, database_path=root / "narranova.sqlite3")
