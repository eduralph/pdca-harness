"""Plan difficulty signal (issue #133, stdlib unittest).

Plan now emits a canonical `- **Difficulty:** low|medium|high` field (blast-radius /
cross-file reach). The brief parser is already generic and the advisory `when =
{field, substring}` consumer already exists, so the producer just has to fill the
field — an advisory leaf gated on difficulty=high then fires with no per-instance
prose. These tests pin the canonical field into the shipped templates and prove the
end-to-end producer→consumer wiring.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import brief, leaves

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


class DifficultyField(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    _seq = 0

    def _brief(self, difficulty: str | None) -> Path:
        DifficultyField._seq += 1
        d = self.tmp / f"issue_{DifficultyField._seq}"
        d.mkdir(parents=True)
        body = "- **Slug:** s\n- **Success criterion:** it works\n"
        if difficulty is not None:
            body += f"- **Difficulty:** {difficulty}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_templates_carry_a_canonical_difficulty_field(self) -> None:
        for tpl in ("brief.md.tpl", "design-proposal.md.tpl", "plan-pointer.md.tpl"):
            text = (TEMPLATES / tpl).read_text(encoding="utf-8")
            self.assertIn("**Difficulty:**", text, f"{tpl} lacks the Difficulty field")
            self.assertIn("blast-radius", text)  # defined for its consumer

    def test_generic_parser_reads_difficulty(self) -> None:
        d = self._brief("high")
        self.assertEqual(brief.field(d / "brief.md", "difficulty"), "high")

    def test_advisory_leaf_fires_on_difficulty_high_without_prose(self) -> None:
        spec = {"when": {"field": "difficulty", "substring": "high"}}
        self.assertTrue(leaves._advisory_applies(spec, self._brief("high")))
        self.assertFalse(leaves._advisory_applies(spec, self._brief("low")))
        # Default-open safety: a missing tag must NOT match a high-gated leaf (the field
        # is absent → no skip-routing decision is silently flipped).
        self.assertFalse(leaves._advisory_applies(spec, self._brief(None)))

    def test_unfilled_placeholder_reads_as_absent(self) -> None:
        # The template placeholder enumerates the values, so its text contains "high".
        # An untouched Difficulty line must NOT match a substring="high" gate, or the
        # absent-is-safe default is defeated (Codex review, PR #145). Covers both a
        # single-line placeholder and a multi-line one (parsed as an unterminated `<` line).
        spec = {"when": {"field": "difficulty", "substring": "high"}}
        for placeholder in ("<`low` | `medium` | `high` — the fix's blast-radius>",
                            "<`low` | `medium` | `high` — blast-radius / cross-file\n"
                            "  reach: ... a wide, cross-cutting change.>"):
            d = self._brief(placeholder)
            self.assertEqual(brief.field(d / "brief.md", "difficulty"), "")
            self.assertFalse(leaves._advisory_applies(spec, d))

    def test_real_shipped_templates_difficulty_placeholder_is_inert(self) -> None:
        # The strongest guard: copy each SHIPPED template verbatim as the brief and confirm
        # its (multi-line) Difficulty placeholder reads as absent — what the hand-written
        # cases above approximate, and what the first fix missed.
        spec = {"when": {"field": "difficulty", "substring": "high"}}
        for tpl in ("brief.md.tpl", "design-proposal.md.tpl", "plan-pointer.md.tpl"):
            d = self.tmp / f"from-{tpl}"
            d.mkdir()
            (d / "brief.md").write_text((TEMPLATES / tpl).read_text(encoding="utf-8"),
                                        encoding="utf-8")
            self.assertEqual(brief.field(d / "brief.md", "difficulty"), "",
                             f"{tpl} difficulty placeholder leaked a value")
            self.assertFalse(leaves._advisory_applies(spec, d),
                             f"{tpl} difficulty placeholder fired a high gate")


if __name__ == "__main__":
    unittest.main()
