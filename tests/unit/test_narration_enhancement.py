from __future__ import annotations

import hashlib
import unittest

from narranova.application.planning import NarrationFragment, SynthesisChunk
from narranova.domain.enhancement import (
    NarrationEnhancementSettings,
    NarrationEnhancer,
    Pronunciation,
    is_scene_break,
    parse_pronunciations,
)
from narranova.domain.narration import NarrationPlan


class NarrationEnhancementTests(unittest.TestCase):
    def test_adds_only_structural_pauses_and_keeps_input_unchanged(self) -> None:
        fragments = (
            NarrationFragment("one", "chapter.xhtml", "h1", "Chapter One", "chapter_heading"),
            NarrationFragment("two", "chapter.xhtml", "p1", "First paragraph.", "paragraph"),
            NarrationFragment("three", "chapter.xhtml", "h2", "A New Place", "section_heading"),
            NarrationFragment("four", "chapter.xhtml", "p2", "Second paragraph.", "paragraph"),
            NarrationFragment("five", "chapter.xhtml", "hr", "", "scene_break"),
            NarrationFragment("six", "chapter.xhtml", "p3", "Third paragraph.", "paragraph"),
        )
        chunk = SynthesisChunk("c0001-p0001", 1, 1, fragments)
        original = chunk.text

        enhanced = NarrationEnhancer(NarrationEnhancementSettings()).enhance_chunk(chunk)

        self.assertEqual(chunk.text, original)
        self.assertIn("Chapter One\n[pause 1.8s]", enhanced)
        self.assertIn("A New Place\n[pause 1.2s]", enhanced)
        self.assertIn("[pause 1.5s]", enhanced)
        self.assertNotIn("First paragraph.\n[pause", enhanced)

    def test_normalizes_typography_and_applies_longest_ipa_term_first(self) -> None:
        settings = NarrationEnhancementSettings(
            pronunciations=(
                Pronunciation("New York", "nuː jɔːrk"),
                Pronunciation("York", "jɔːrk"),
            )
        )
        fragment = NarrationFragment(
            "one", "chapter.xhtml", "p", "“New\u00a0York…”—York", "paragraph"
        )
        chunk = SynthesisChunk("c0001-p0001", 1, 1, (fragment,))

        enhanced = NarrationEnhancer(settings).enhance_chunk(chunk)

        self.assertEqual(enhanced, '"/nuː jɔːrk/..." — /jɔːrk/')

    def test_recognizes_common_scene_breaks_and_parses_dictionary(self) -> None:
        self.assertTrue(all(is_scene_break(value) for value in ("***", "* * *", "---", "• • •")))
        self.assertFalse(is_scene_break("A normal paragraph."))
        entries = parse_pronunciations("Mara = /ˈmɑːrə/\nNarranova = næɹəˈnoʊvə")
        self.assertEqual(entries[0], Pronunciation("Mara", "ˈmɑːrə"))

    def test_disabled_layer_returns_exact_chunk_text(self) -> None:
        chunk = SynthesisChunk(
            "c0001-p0001",
            1,
            1,
            (NarrationFragment("one", "c", "p", "“Untouched…”", "chapter_heading"),),
        )
        enhanced = NarrationEnhancer(
            NarrationEnhancementSettings(enabled=False)
        ).enhance_chunk(chunk)
        self.assertEqual(enhanced, chunk.text)

    def test_legacy_plans_receive_conservative_heading_metadata(self) -> None:
        def digest(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        units = []
        for identifier, text in (
            ("u1", "Chapter One"),
            ("u2", "A New Place"),
            ("u3", "A sentence."),
        ):
            units.append(
                {
                    "id": identifier,
                    "spine_index": 1,
                    "document": "one.xhtml",
                    "element_id": identifier,
                    "display_text": text,
                    "spoken_text": text,
                    "enabled": True,
                    "display_text_sha256": digest(text),
                    "spoken_text_sha256": digest(text),
                }
            )
        plan = NarrationPlan.from_dict(
            {
                "schema_version": 1,
                "revision": 1,
                "metadata": {},
                "chapters": [
                    {
                        "spine_index": 1,
                        "document": "one.xhtml",
                        "title": "Chapter One",
                        "unit_ids": ["u1", "u2", "u3"],
                    }
                ],
                "units": units,
            }
        )
        self.assertEqual(
            [unit.kind for unit in plan.units],
            ["chapter_heading", "section_heading", "paragraph"],
        )


if __name__ == "__main__":
    unittest.main()
