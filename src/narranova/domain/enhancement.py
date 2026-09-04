"""Deterministic, provider-facing narration text enhancement."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from narranova.application.planning import SynthesisChunk


_SCENE_BREAK = re.compile(
    r"^\s*(?:(?:\*\s*){3,}|(?:#\s*){3,}|(?:[-_~•·]\s*){3,}|(?:—\s*){2,})\s*$"
)
_HORIZONTAL_SPACE = re.compile(r"[\t\v\f \u00a0\u2007\u202f]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def is_scene_break(text: str) -> bool:
    """Return whether a text-only block is an ornamental scene separator."""
    return bool(_SCENE_BREAK.fullmatch(text))


@dataclass(frozen=True)
class Pronunciation:
    term: str
    ipa: str

    def __post_init__(self) -> None:
        if not self.term.strip() or "\n" in self.term:
            raise ValueError("Pronunciation terms must be non-empty single lines")
        if not self.ipa.strip() or "\n" in self.ipa:
            raise ValueError("IPA pronunciations must be non-empty single lines")


@dataclass(frozen=True)
class NarrationEnhancementSettings:
    enabled: bool = True
    chapter_pause_seconds: float = 1.8
    section_pause_seconds: float = 1.2
    scene_break_pause_seconds: float = 1.5
    normalize_text: bool = True
    pronunciation_enabled: bool = True
    pronunciations: tuple[Pronunciation, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.chapter_pause_seconds,
            self.section_pause_seconds,
            self.scene_break_pause_seconds,
        ):
            if not 0.1 <= value <= 10.0:
                raise ValueError("Narration pauses must be between 0.1 and 10 seconds")
        folded = [item.term.casefold() for item in self.pronunciations]
        if len(folded) != len(set(folded)):
            raise ValueError("Pronunciation terms must be unique")

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["pronunciations"] = [asdict(item) for item in self.pronunciations]
        return result

    @classmethod
    def from_dict(cls, values: dict[str, Any] | None) -> "NarrationEnhancementSettings":
        values = values or {}
        return cls(
            enabled=bool(values.get("enabled", True)),
            chapter_pause_seconds=float(values.get("chapter_pause_seconds", 1.8)),
            section_pause_seconds=float(values.get("section_pause_seconds", 1.2)),
            scene_break_pause_seconds=float(
                values.get("scene_break_pause_seconds", 1.5)
            ),
            normalize_text=bool(values.get("normalize_text", True)),
            pronunciation_enabled=bool(values.get("pronunciation_enabled", True)),
            pronunciations=tuple(
                Pronunciation(str(item["term"]), str(item["ipa"]))
                for item in values.get("pronunciations", [])
            ),
        )


def parse_pronunciations(lines: str) -> tuple[Pronunciation, ...]:
    """Parse one `term = IPA` entry per line for the book settings form."""
    entries: list[Pronunciation] = []
    for line_number, raw in enumerate(lines.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"Pronunciation line {line_number} must use term = IPA")
        term, ipa = (part.strip() for part in line.split("=", 1))
        entries.append(
            Pronunciation(term, ipa.strip().removeprefix("/").removesuffix("/"))
        )
    return tuple(entries)


def format_pronunciations(entries: Iterable[Pronunciation]) -> str:
    return "\n".join(f"{item.term} = {item.ipa}" for item in entries)


class NarrationEnhancer:
    """Build derived OpenMOSS input without mutating source or chunk text."""

    def __init__(self, settings: NarrationEnhancementSettings) -> None:
        self.settings = settings
        ordered = sorted(
            settings.pronunciations, key=lambda item: len(item.term), reverse=True
        )
        self._pronunciation_by_term = {item.term.casefold(): item.ipa for item in ordered}
        self._pronunciation_pattern = (
            re.compile(
                r"(?<!\w)(?:"
                + "|".join(re.escape(item.term) for item in ordered)
                + r")(?!\w)",
                re.IGNORECASE,
            )
            if ordered
            else None
        )

    def enhance_chunk(self, chunk: "SynthesisChunk") -> str:
        if not self.settings.enabled:
            return chunk.text
        output: list[str] = []
        for fragment in chunk.fragments:
            if fragment.kind == "scene_break" or is_scene_break(fragment.text):
                output.append(self._pause(self.settings.scene_break_pause_seconds))
                continue
            text = fragment.text
            if self.settings.normalize_text:
                text = self.normalize(text)
            if self.settings.pronunciation_enabled:
                text = self.apply_pronunciations(text)
            if fragment.kind == "chapter_heading":
                text = f"{text}\n{self._pause(self.settings.chapter_pause_seconds)}"
            elif fragment.kind == "section_heading":
                text = f"{text}\n{self._pause(self.settings.section_pause_seconds)}"
            if text:
                output.append(text)
        return "\n\n".join(output)

    @staticmethod
    def normalize(text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.translate(
            str.maketrans(
                {
                    "“": '"',
                    "”": '"',
                    "„": '"',
                    "‟": '"',
                    "‘": "'",
                    "’": "'",
                    "‚": "'",
                    "‛": "'",
                    "…": "...",
                }
            )
        )
        text = re.sub(r"\s*([—–])\s*", r" \1 ", text)
        lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n")]
        return _EXCESS_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()

    def apply_pronunciations(self, text: str) -> str:
        if self._pronunciation_pattern is None:
            return text
        return self._pronunciation_pattern.sub(
            lambda match: f"/{self._pronunciation_by_term[match.group(0).casefold()]}/",
            text,
        )

    @staticmethod
    def _pause(seconds: float) -> str:
        return f"[pause {seconds:.1f}s]"
