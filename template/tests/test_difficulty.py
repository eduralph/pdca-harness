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

    def _brief(self, difficulty: str | None) -> Path:
        d = self.tmp / f"issue_{difficulty}"
        d.mkdir(parents=True)
        body = "- **Slug:** s\n- **Success criterion:** it works\n"
        if difficulty is not None:
            body += f"- **Difficulty:** {difficulty}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_templates_carry_a_canonical_difficulty_field(self) -> None:
        for tpl in ("brief.md.tpl", "design-proposal.md.tpl"):
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


if __name__ == "__main__":
    unittest.main()
