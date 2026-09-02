"""ZIP and XML defenses for untrusted EPUB input."""

from __future__ import annotations

import stat
import zipfile
from pathlib import PurePosixPath


class UnsafeEpubError(ValueError):
    pass


MAX_ARCHIVE_FILES = 10_000
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000


def validate_archive(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        raise UnsafeEpubError("EPUB contains too many archive members")
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        raise UnsafeEpubError("EPUB contains duplicate archive paths")
    expanded = 0
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
            raise UnsafeEpubError(f"Unsafe EPUB archive path: {member.filename}")
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise UnsafeEpubError(f"EPUB archive links are not allowed: {member.filename}")
        if member.flag_bits & 0x1:
            raise UnsafeEpubError("Encrypted EPUB archive members are not supported")
        if member.file_size > MAX_MEMBER_BYTES:
            raise UnsafeEpubError(f"EPUB member is too large: {member.filename}")
        expanded += member.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise UnsafeEpubError("EPUB expands beyond the configured safety limit")
        if member.file_size and member.compress_size == 0:
            raise UnsafeEpubError(f"Invalid compressed size for {member.filename}")
        if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
            raise UnsafeEpubError(f"Suspicious compression ratio for {member.filename}")


def validate_xml(data: bytes, name: str) -> None:
    prefix = data[:8192].upper()
    if b"<!DOCTYPE" in prefix or b"<!ENTITY" in prefix:
        raise UnsafeEpubError(f"DTD and entity declarations are not allowed in {name}")
