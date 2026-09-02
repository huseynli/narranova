"""Narranova command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from narranova import __version__
from narranova.application.ingest import ImportBook
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.config import Settings
from narranova.epub import EpubError, EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository


def _initialize(data_dir: str | Path | None) -> Path:
    settings = Settings.load(data_dir)
    ArtifactLayout.at(settings.data_dir).initialize()
    Database(settings.database_path).initialize()
    return settings.data_dir


def _services(data_dir: str | Path | None) -> tuple[Settings, ArtifactLayout, BookRepository]:
    settings = Settings.load(data_dir)
    layout = ArtifactLayout.at(settings.data_dir)
    layout.initialize()
    database = Database(settings.database_path)
    database.initialize()
    return settings, layout, BookRepository(database)


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

    import_parser = commands.add_parser("import", help="import an EPUB and create its narration plan")
    import_parser.add_argument("source", help="path to a DRM-free EPUB")
    import_parser.add_argument("--data-dir", help="persistent data directory")

    books_parser = commands.add_parser("books", help="list imported books")
    books_parser.add_argument("--data-dir", help="persistent data directory")

    plan_parser = commands.add_parser("plan", help="print the latest narration plan for a book")
    plan_parser.add_argument("book_id", help="Narranova book ID")
    plan_parser.add_argument("--data-dir", help="persistent data directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            data_dir = _initialize(args.data_dir)
            print(f"Narranova data initialized at {data_dir}")
            return 0
        settings, layout, repository = _services(args.data_dir)
        if args.command == "import":
            use_case = ImportBook(
                EpubParser(), repository, layout, ArtifactStore(settings.data_dir)
            )
            result = use_case.execute(Path(args.source))
            print(
                f"Imported {result.title!r} as {result.book_id}: "
                f"{result.chapter_count} chapter(s), {result.unit_count} narration unit(s)"
            )
            return 0
        if args.command == "books":
            books = repository.list_books()
            if not books:
                print("No books imported.")
            for book in books:
                byline = f" — {book.author}" if book.author else ""
                print(f"{book.id}  {book.title}{byline}  [{book.status}]")
            return 0
        if args.command == "plan":
            record = repository.get_plan_record(args.book_id)
            plan_path = (settings.data_dir / record["artifact_path"]).resolve()
            if not plan_path.is_relative_to(settings.data_dir):
                raise RuntimeError("Stored narration plan path escapes the data directory")
            content = plan_path.read_text(encoding="utf-8")
            if ArtifactStore.sha256(plan_path) != record["plan_sha256"]:
                raise RuntimeError("Stored narration plan failed hash validation")
            print(json.dumps(json.loads(content), ensure_ascii=False, indent=2))
            return 0
        raise AssertionError(f"Unhandled command: {args.command}")
    except (EpubError, FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
