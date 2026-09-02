"""Narranova command-line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from narranova import __version__
from narranova.artifacts import ArtifactLayout
from narranova.config import Settings
from narranova.persistence import Database


def _initialize(data_dir: str | Path | None) -> Path:
    settings = Settings.load(data_dir)
    ArtifactLayout.at(settings.data_dir).initialize()
    Database(settings.database_path).initialize()
    return settings.data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narranova",
        description="Create resumable audiobooks from EPUB files.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="initialize persistent application data")
    init_parser.add_argument(
        "--data-dir",
        help="persistent data directory (default: NARRANOVA_DATA_DIR or /data)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        data_dir = _initialize(args.data_dir)
        print(f"Narranova data initialized at {data_dir}")
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")
