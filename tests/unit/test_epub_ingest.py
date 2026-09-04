from __future__ import annotations

import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from narranova.application.ingest import ImportBook
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.epub import EpubError, EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository


CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/package.opf"/></rootfiles>
</container>
"""

PACKAGE = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
  <metadata>
    <dc:title>The Example Book</dc:title>
    <dc:creator>Ada Author</dc:creator>
    <dc:creator>Sam Scribe</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier>urn:isbn:123</dc:identifier>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>
    <item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
  </manifest>
  <spine><itemref idref="two"/><itemref idref="one"/></spine>
</package>
"""


def make_epub(
    path: Path,
    *,
    first_document: str | None = None,
    package: str = PACKAGE,
    navigation: str = "<html><body><nav>Contents</nav></body></html>",
) -> None:
    one = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
      <h1 id="title-one">One</h1><p>Hello <em>careful</em> world.</p>
    </body></html>"""
    two = first_document or """<html xmlns="http://www.w3.org/1999/xhtml"><body>
      <h1>Two</h1><section>Loose text.<p id="p2">Second paragraph.</p>Tail text.</section>
    </body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("EPUB/package.opf", package)
        archive.writestr("EPUB/nav.xhtml", navigation)
        archive.writestr("EPUB/one.xhtml", one)
        archive.writestr("EPUB/two.xhtml", two)
        archive.writestr("EPUB/cover.jpg", b"fake-jpeg")


class EpubParserTests(unittest.TestCase):
    def test_reads_navigation_series_subtitle_and_epub2_cover_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.epub"
            package = PACKAGE.replace(
                "<dc:identifier>urn:isbn:123</dc:identifier>",
                """<dc:identifier>urn:isbn:123</dc:identifier>
    <dc:title id="book-subtitle">A Small Tale</dc:title>
    <meta property="title-type" refines="#book-subtitle">subtitle</meta>
    <meta name="calibre:series" content="Example Stories"/>
    <meta name="calibre:series_index" content="2"/>
    <meta name="cover" content="cover"/>""",
            ).replace(' properties="cover-image"', "")
            navigation = """<html><body><nav><ol>
              <li><a href="two.xhtml">The opening</a></li>
              <li><a href="one.xhtml">The conclusion</a></li>
            </ol></nav></body></html>"""
            make_epub(path, package=package, navigation=navigation)

            parsed = EpubParser().parse(path)

            self.assertEqual(parsed.metadata.subtitle, "A Small Tale")
            self.assertEqual(parsed.metadata.series, "Example Stories")
            self.assertEqual(parsed.metadata.series_index, "2")
            self.assertEqual(
                [document.title for document in parsed.documents],
                ["The opening", "The conclusion"],
            )
            self.assertEqual(parsed.cover_path, "EPUB/cover.jpg")

    def test_preserves_metadata_spine_source_mapping_and_all_readable_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "book.epub"
            make_epub(path)

            parsed = EpubParser().parse(path)

        self.assertEqual(parsed.metadata.title, "The Example Book")
        self.assertEqual(parsed.metadata.authors, ("Ada Author", "Sam Scribe"))
        self.assertEqual([document.title for document in parsed.documents], ["Two", "One"])
        self.assertEqual([document.spine_index for document in parsed.documents], [1, 2])
        self.assertEqual(parsed.documents[0].path, "EPUB/two.xhtml")
        all_text = [
            element.display_text
            for document in parsed.documents
            for element in document.elements
        ]
        self.assertEqual(
            all_text,
            ["Two", "Loose text.", "Second paragraph.", "Tail text.", "One", "Hello careful world."],
        )
        self.assertEqual(parsed.documents[0].elements[2].element_id, "p2")
        self.assertEqual(parsed.cover_path, "EPUB/cover.jpg")
        self.assertEqual(parsed.cover_data, b"fake-jpeg")

    def test_marks_headings_and_scene_breaks_without_rewriting_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "structure.epub"
            make_epub(
                path,
                first_document="""<html><body><h1>Chapter One</h1><p>Opening.</p>
                <h2>A turn</h2><p>* * *</p><p>Closing.</p><hr/><p>After.</p></body></html>""",
            )
            parsed = EpubParser().parse(path)

        elements = parsed.documents[0].elements
        self.assertEqual(
            [item.kind for item in elements],
            ["chapter_heading", "paragraph", "section_heading", "scene_break", "paragraph", "scene_break", "paragraph"],
        )
        self.assertEqual(elements[3].display_text, "* * *")
        self.assertEqual(elements[5].display_text, "")

    def test_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape", "bad")

            with self.assertRaisesRegex(EpubError, "Unsafe EPUB archive path"):
                EpubParser().parse(path)

    def test_rejects_dtd_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.epub"
            make_epub(path, first_document='<!DOCTYPE html><html><body><p>Bad</p></body></html>')

            with self.assertRaisesRegex(EpubError, "DTD and entity"):
                EpubParser().parse(path)


class ImportBookTests(unittest.TestCase):
    def test_import_persists_source_and_valid_plan_without_prototype_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.epub"
            make_epub(source)
            data = root / "data"
            layout = ArtifactLayout.at(data)
            layout.initialize()
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            repository = BookRepository(database)
            use_case = ImportBook(EpubParser(), repository, layout, ArtifactStore(data))

            with warnings.catch_warnings():
                warnings.simplefilter("error")
                result = use_case.execute(source)

            source_copy = layout.source_epub(result.book_id)
            plan_path = layout.plan(result.book_id, 1)
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
            books = repository.list_books()

            self.assertEqual(source_copy.read_bytes(), source.read_bytes())
            self.assertEqual(result.chapter_count, 2)
            self.assertEqual(result.unit_count, 6)
            self.assertEqual(len(plan_data["units"]), 6)
            self.assertTrue(all(unit["enabled"] for unit in plan_data["units"]))
            self.assertEqual(books[0].id, result.book_id)
            self.assertFalse((layout.book_root(result.book_id) / "manifest.json").exists())

    def test_duplicate_source_is_rejected_and_new_artifacts_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.epub"
            make_epub(source)
            data = root / "data"
            layout = ArtifactLayout.at(data)
            layout.initialize()
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            use_case = ImportBook(
                EpubParser(), BookRepository(database), layout, ArtifactStore(data)
            )

            use_case.execute(source)
            with self.assertRaisesRegex(ValueError, "already been imported"):
                use_case.execute(source)

            self.assertEqual(len(list(layout.books_root.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
