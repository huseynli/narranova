from __future__ import annotations

import unittest
from dataclasses import replace

from narranova.application.planning import ChunkPlanner, content_fingerprint
from narranova.domain.books import BookMetadata, ParsedBook, SourceDocument, SourceElement
from narranova.domain.narration import plan_book


def example_plan():
    parsed = ParsedBook(
        metadata=BookMetadata(title="Chunks"),
        documents=(
            SourceDocument(
                spine_index=1,
                path="chapter.xhtml",
                title="Chapter",
                elements=(
                    SourceElement(1, "chapter.xhtml", "p1", "First sentence. Second sentence."),
                    SourceElement(1, "chapter.xhtml", "p2", "Third paragraph has more words."),
                    SourceElement(1, "chapter.xhtml", "p3", "Final paragraph."),
                ),
            ),
        ),
    )
    return plan_book(parsed)


class ChunkPlannerTests(unittest.TestCase):
    def test_default_character_budget_targets_shorter_audio(self) -> None:
        planner = ChunkPlanner()

        self.assertEqual(planner.min_chars, 3_840)
        self.assertEqual(planner.target_chars, 4_800)
        self.assertEqual(planner.max_chars, 5_760)

    def test_chunks_on_unit_and_sentence_boundaries_without_content_loss(self) -> None:
        plan = example_plan()
        planner = ChunkPlanner(target_chars=30, min_chars=20, max_chars=35)

        chunks = planner.create_chunks(plan)

        source = "".join(unit.spoken_text for unit in plan.units)
        generated = "".join(chunk.text for chunk in chunks)
        self.assertEqual(content_fingerprint(source), content_fingerprint(generated))
        self.assertTrue(all(len(fragment.text) <= 35 for chunk in chunks for fragment in chunk.fragments))
        self.assertEqual(chunks[0].id, "c0001-p0001")
        self.assertEqual(chunks[0].fragments[0].element_id, "p1")

    def test_disabled_units_are_not_synthesized(self) -> None:
        plan = example_plan()
        disabled = replace(plan.units[1], enabled=False)
        plan = replace(plan, units=(plan.units[0], disabled, plan.units[2]))

        chunks = ChunkPlanner(target_chars=100, min_chars=80, max_chars=120).create_chunks(plan)

        self.assertNotIn("Third paragraph", " ".join(chunk.text for chunk in chunks))
        self.assertNotIn(disabled.id, [unit_id for chunk in chunks for unit_id in chunk.unit_ids])

    def test_rejects_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            ChunkPlanner(target_chars=20, min_chars=30, max_chars=40)


if __name__ == "__main__":
    unittest.main()
