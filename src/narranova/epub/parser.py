"""DOM-aware EPUB metadata, spine, cover, and readable-element parser."""

from __future__ import annotations

import html
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from narranova.domain.books import (
    BookMetadata,
    ParsedBook,
    SourceDocument,
    SourceElement,
)
from narranova.epub.safety import UnsafeEpubError, validate_archive, validate_xml
from narranova.domain.enhancement import is_scene_break


class EpubError(ValueError):
    pass


_BLOCK_TAGS = {
    "address", "blockquote", "dd", "dt", "figcaption", "h1", "h2", "h3",
    "h4", "h5", "h6", "li", "p", "pre", "td", "th",
}
_SKIP_TAGS = {"audio", "canvas", "head", "math", "nav", "noscript", "script", "style", "svg"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value).replace("\xa0", " ")).strip()


def _safe_xml(data: bytes, name: str) -> ET.Element:
    validate_xml(data, name)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise EpubError(f"Invalid XML document: {name}") from exc


class _TolerantHTMLTree(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("html")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = ET.SubElement(
            self.stack[-1], tag, {key: value or "" for key, value in attrs}
        )
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ET.SubElement(self.stack[-1], tag, {key: value or "" for key, value in attrs})

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if _local_name(self.stack[index].tag) == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        current = self.stack[-1]
        if len(current):
            child = current[-1]
            child.tail = (child.tail or "") + data
        else:
            current.text = (current.text or "") + data


def _safe_content(data: bytes, name: str, media_type: str) -> ET.Element:
    validate_xml(data, name)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        if media_type.lower() != "text/html":
            raise EpubError(f"Invalid XML document: {name}") from exc
        try:
            parser = _TolerantHTMLTree()
            parser.feed(data.decode("utf-8", errors="replace"))
            parser.close()
            return parser.root
        except Exception as html_exc:
            raise EpubError(f"Invalid HTML document: {name}") from html_exc


def _first(root: ET.Element, name: str) -> ET.Element | None:
    return next((node for node in root.iter() if _local_name(node.tag) == name), None)


def _metadata_values(root: ET.Element, name: str) -> tuple[str, ...]:
    return tuple(
        value
        for node in root.iter()
        if _local_name(node.tag) == name and (value := _normalize_text("".join(node.itertext())))
    )


def _meta_value(root: ET.Element, *keys: str) -> str | None:
    wanted = {key.lower() for key in keys}
    for node in root.iter():
        if _local_name(node.tag) != "meta":
            continue
        key = (node.attrib.get("property") or node.attrib.get("name") or "").lower()
        if key in wanted:
            value = node.attrib.get("content") or _normalize_text("".join(node.itertext()))
            if value:
                return value
    return None


def _subtitle(root: ET.Element) -> str | None:
    title_by_id = {
        node.attrib["id"]: _normalize_text("".join(node.itertext()))
        for node in root.iter()
        if _local_name(node.tag) == "title" and node.attrib.get("id")
    }
    for node in root.iter():
        if (
            _local_name(node.tag) == "meta"
            and node.attrib.get("property", "").lower() == "title-type"
            and _normalize_text("".join(node.itertext())).lower() == "subtitle"
        ):
            reference = node.attrib.get("refines", "").removeprefix("#")
            if title_by_id.get(reference):
                return title_by_id[reference]
    return _meta_value(root, "subtitle")


def _navigation_titles(
    archive: zipfile.ZipFile,
    names: set[str],
    manifest: dict[str, dict[str, str]],
    package_dir: PurePosixPath,
) -> dict[str, str]:
    titles: dict[str, str] = {}
    navigation_items = [
        item
        for item in manifest.values()
        if "nav" in item.get("properties", "").split()
        or item.get("media-type", "").lower() == "application/x-dtbncx+xml"
    ]
    for item in navigation_items:
        navigation_path = _resolve(package_dir, item.get("href", ""))
        if navigation_path not in names:
            continue
        root = _safe_xml(archive.read(navigation_path), navigation_path)
        navigation_dir = PurePosixPath(navigation_path).parent
        for node in root.iter():
            href = node.attrib.get("href") if _local_name(node.tag) == "a" else None
            if href:
                title = _normalize_text("".join(node.itertext()))
                if title:
                    titles.setdefault(_resolve(navigation_dir, href), title)
            if _local_name(node.tag) == "content" and node.attrib.get("src"):
                parent_text = ""
                # NCX labels occur near their content element; the nearest
                # navPoint text is a safe and deterministic approximation.
                for candidate in root.iter():
                    if node in list(candidate):
                        parent_text = _normalize_text("".join(candidate.itertext()))
                        break
                if parent_text:
                    titles.setdefault(
                        _resolve(navigation_dir, node.attrib["src"]), parent_text
                    )
    return titles


def _inline_text(element: ET.Element) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if _local_name(node.tag) in _SKIP_TAGS:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    return _normalize_text(" ".join(parts))


def _readable_elements(root: ET.Element, spine_index: int, document: str) -> tuple[SourceElement, ...]:
    found: list[SourceElement] = []

    seen_heading = False

    def add(
        text: str,
        element: ET.Element,
        suffix: int = 0,
        kind: str = "paragraph",
    ) -> None:
        nonlocal seen_heading
        if not text and kind != "scene_break":
            return
        position = len(found) + 1
        source_id = element.attrib.get("id")
        if source_id and not suffix:
            element_id = source_id
        elif source_id:
            element_id = f"{source_id}-n{suffix}"
        else:
            element_id = f"narration-s{spine_index:04d}-e{position:05d}"
        found.append(
            SourceElement(
                spine_index=spine_index,
                document=document,
                element_id=element_id,
                display_text=text,
                kind=kind,
            )
        )
        if kind in {"chapter_heading", "section_heading"}:
            seen_heading = True

    def walk_container(element: ET.Element) -> None:
        if _local_name(element.tag) in _SKIP_TAGS:
            return
        loose_parts: list[str] = []
        fragment = 0

        def flush() -> None:
            nonlocal fragment
            text = _normalize_text(" ".join(loose_parts))
            loose_parts.clear()
            if text:
                fragment += 1
                add(text, element, fragment)

        if element.text:
            loose_parts.append(element.text)
        for child in element:
            tag = _local_name(child.tag)
            if tag in _SKIP_TAGS:
                pass
            elif tag in _BLOCK_TAGS:
                flush()
                text = _inline_text(child)
                if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                    kind = "section_heading" if seen_heading else "chapter_heading"
                elif is_scene_break(text):
                    kind = "scene_break"
                else:
                    kind = "paragraph"
                add(text, child, kind=kind)
            elif tag == "hr":
                flush()
                add("", child, kind="scene_break")
            elif any(_local_name(desc.tag) in _BLOCK_TAGS for desc in child.iter() if desc is not child):
                flush()
                walk_container(child)
            else:
                loose_parts.append(_inline_text(child))
            if child.tail:
                loose_parts.append(child.tail)
        flush()

    body = _first(root, "body") or root
    walk_container(body)
    return tuple(found)


def _resolve(base: PurePosixPath, href: str) -> str:
    raw = href.split("#", 1)[0]
    resolved = posixpath.normpath((base / raw).as_posix())
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        raise EpubError(f"EPUB reference escapes its package directory: {href}")
    return resolved


class EpubParser:
    def parse(self, path: Path) -> ParsedBook:
        if path.suffix.lower() != ".epub":
            raise EpubError("Narranova currently accepts DRM-free .epub files only")
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise EpubError(f"Cannot open EPUB: {path}") from exc
        try:
            with archive:
                validate_archive(archive)
                return self._parse_archive(archive)
        except UnsafeEpubError as exc:
            raise EpubError(str(exc)) from exc

    def _parse_archive(self, archive: zipfile.ZipFile) -> ParsedBook:
        names = set(archive.namelist())
        try:
            container_data = archive.read("META-INF/container.xml")
        except KeyError as exc:
            raise EpubError("EPUB is missing META-INF/container.xml") from exc
        container = _safe_xml(container_data, "META-INF/container.xml")
        rootfile = _first(container, "rootfile")
        if rootfile is None or not rootfile.attrib.get("full-path"):
            raise EpubError("EPUB container does not identify a package document")
        opf_path = rootfile.attrib["full-path"]
        if opf_path not in names:
            raise EpubError(f"EPUB package document is missing: {opf_path}")
        opf = _safe_xml(archive.read(opf_path), opf_path)
        metadata_node = _first(opf, "metadata")
        manifest_node = _first(opf, "manifest")
        spine_node = _first(opf, "spine")
        if metadata_node is None or manifest_node is None or spine_node is None:
            raise EpubError("EPUB package is missing metadata, manifest, or spine")

        titles = _metadata_values(metadata_node, "title")
        authors = _metadata_values(metadata_node, "creator")
        languages = _metadata_values(metadata_node, "language")
        identifiers = _metadata_values(metadata_node, "identifier")
        publishers = _metadata_values(metadata_node, "publisher")
        descriptions = _metadata_values(metadata_node, "description")
        series = _meta_value(
            metadata_node, "calibre:series", "belongs-to-collection"
        )
        series_index = _meta_value(
            metadata_node, "calibre:series_index", "group-position"
        )
        subtitle = _subtitle(metadata_node)
        metadata = BookMetadata(
            title=titles[0] if titles else "Untitled",
            subtitle=subtitle,
            authors=authors,
            language=languages[0] if languages else None,
            identifiers=identifiers,
            publisher=publishers[0] if publishers else None,
            description=descriptions[0] if descriptions else None,
            series=series,
            series_index=series_index,
        )

        manifest: dict[str, dict[str, str]] = {}
        for item in manifest_node:
            if _local_name(item.tag) == "item" and item.attrib.get("id"):
                manifest[item.attrib["id"]] = dict(item.attrib)
        package_dir = PurePosixPath(opf_path).parent
        navigation_titles = _navigation_titles(
            archive, names, manifest, package_dir
        )
        documents: list[SourceDocument] = []
        for spine_index, itemref in enumerate(spine_node, 1):
            if _local_name(itemref.tag) != "itemref":
                continue
            item = manifest.get(itemref.attrib.get("idref", ""))
            if not item:
                raise EpubError(f"Spine references unknown manifest item: {itemref.attrib.get('idref')}")
            if "nav" in item.get("properties", "").split():
                continue
            if item.get("media-type", "").lower() not in {"application/xhtml+xml", "text/html"}:
                continue
            document_path = _resolve(package_dir, item.get("href", ""))
            if document_path not in names:
                raise EpubError(f"Spine document is missing: {document_path}")
            root = _safe_content(
                archive.read(document_path), document_path, item.get("media-type", "")
            )
            elements = _readable_elements(root, spine_index, document_path)
            if not elements:
                continue
            heading = next(
                (_inline_text(node) for node in root.iter() if _local_name(node.tag) in {"h1", "h2"}),
                "",
            )
            fallback = PurePosixPath(document_path).stem.replace("-", " ").replace("_", " ")
            documents.append(
                SourceDocument(
                    spine_index=spine_index,
                    path=document_path,
                    title=(
                        navigation_titles.get(document_path)
                        or heading
                        or fallback
                        or f"Chapter {spine_index}"
                    ),
                    elements=elements,
                )
            )
        if not documents:
            raise EpubError("EPUB contains no readable spine documents")

        cover_path: str | None = None
        cover_media_type: str | None = None
        cover_data: bytes | None = None
        cover_item = next(
            (item for item in manifest.values() if "cover-image" in item.get("properties", "").split()),
            None,
        )
        if cover_item is None:
            cover_id = _meta_value(metadata_node, "cover")
            if cover_id:
                cover_item = manifest.get(cover_id)
        if cover_item:
            candidate = _resolve(package_dir, cover_item.get("href", ""))
            if candidate in names:
                cover_path = candidate
                cover_media_type = cover_item.get("media-type")
                cover_data = archive.read(candidate)
        return ParsedBook(metadata, tuple(documents), cover_path, cover_media_type, cover_data)
