"""Provider-sized synthesis chunks derived from source-mapped narration units."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from narranova.domain.narration import NarrationPlan, NarrationUnit


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+(?=[\"'“‘(]*\S)")


def content_fingerprint(text: str) -> str:
    compact = "".join(text.split())
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NarrationFragment:
    unit_id: str
    document: str
    element_id: str
    text: str
    kind: str = "paragraph"


@dataclass(frozen=True)
class SynthesisChunk:
    id: str
    chapter_index: int
    chunk_index: int
    fragments: tuple[NarrationFragment, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(fragment.text for fragment in self.fragments)

    @property
    def unit_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fragment.unit_id for fragment in self.fragments))


class ChunkPlanner:
    def __init__(self, target_chars: int = 6000, min_chars: int = 4800, max_chars: int = 7200) -> None:
        if not 0 < min_chars <= target_chars <= max_chars:
            raise ValueError("Expected 0 < min_chars <= target_chars <= max_chars")
        self.target_chars = target_chars
        self.min_chars = min_chars
        self.max_chars = max_chars

    def create_chunks(self, plan: NarrationPlan) -> tuple[SynthesisChunk, ...]:
        plan.validate()
        units = {unit.id: unit for unit in plan.units}
        chunks: list[SynthesisChunk] = []
        for chapter in plan.chapters:
            fragments = [
                fragment
                for unit_id in chapter.unit_ids
                if (unit := units[unit_id]).enabled
                for fragment in self._fragments(unit)
            ]
            current: list[NarrationFragment] = []
            current_chars = 0
            chunk_index = 1
            for fragment in fragments:
                separator = 2 if current else 0
                candidate_chars = current_chars + separator + len(fragment.text)
                at_target = current_chars >= self.target_chars and current_chars >= self.min_chars
                if current and (candidate_chars > self.max_chars or at_target):
                    chunks.append(self._chunk(chapter.spine_index, chunk_index, current))
                    chunk_index += 1
                    current = []
                    current_chars = 0
                    separator = 0
                current.append(fragment)
                current_chars += separator + len(fragment.text)
            if current:
                chunks.append(self._chunk(chapter.spine_index, chunk_index, current))

            original = "\n\n".join(units[unit_id].spoken_text for unit_id in chapter.unit_ids if units[unit_id].enabled)
            generated = "\n\n".join(
                chunk.text for chunk in chunks if chunk.chapter_index == chapter.spine_index
            )
            if content_fingerprint(original) != content_fingerprint(generated):
                raise RuntimeError(f"Chunk planning lost narration text in {chapter.document}")
        return tuple(chunks)

    def _fragments(self, unit: NarrationUnit) -> tuple[NarrationFragment, ...]:
        return tuple(
            NarrationFragment(unit.id, unit.document, unit.element_id, text, unit.kind)
            for text in self._split_text(unit.spoken_text)
        )

    def _split_text(self, text: str) -> tuple[str, ...]:
        if len(text) <= self.max_chars:
            return (text,)
        sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(text) if part.strip()]
        pieces: list[str] = []
        for sentence in sentences:
            remaining = sentence
            while len(remaining) > self.max_chars:
                split_at = remaining.rfind(" ", 0, self.max_chars + 1)
                if split_at <= 0:
                    split_at = self.max_chars
                pieces.append(remaining[:split_at].strip())
                remaining = remaining[split_at:].strip()
            if remaining:
                pieces.append(remaining)
        return tuple(pieces)

    @staticmethod
    def _chunk(
        chapter_index: int,
        chunk_index: int,
        fragments: list[NarrationFragment],
    ) -> SynthesisChunk:
        return SynthesisChunk(
            id=f"c{chapter_index:04d}-p{chunk_index:04d}",
            chapter_index=chapter_index,
            chunk_index=chunk_index,
            fragments=tuple(fragments),
        )
