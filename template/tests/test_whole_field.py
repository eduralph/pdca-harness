"""`brief.whole_field` and the SUMMARY §1 fields it feeds (issue #336).

`brief.parse_fields` is line-based, so every multi-line brief field was cut at its first
line — in the artifact the sign-off leaf and the human read to decide accept or iterate,
and that the C6 accept-guard gates on. Measured over one instance's 85 committed briefs:
88 of 94 success criteria (93%) reached sign-off truncated, and four rendered nothing at
all because the brief wrote the value on the lines *beneath* the label.

`brief.md.tpl` itself writes the Success criterion placeholder across two lines, so this
is the shape the template teaches, not an occasional planner quirk.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import assemble, brief, gates, signoff
from pdca_harness.config import Config, LeafConfig

_PASS_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle",
              "gating": True, "cmd": "true"}


class WholeField(unittest.TestCase):
    def _brief(self, body: str) -> Path:
        f = Path(tempfile.mkdtemp()) / "brief.md"
        f.write_text(body, encoding="utf-8")
        return f

    def test_wrapped_continuation_is_kept(self) -> None:
        f = self._brief("- **Success criterion:** the observable condition\n"
                        "  that means it is fixed\n"
                        "- **Scope:** narrow\n")
        self.assertEqual(brief.whole_field(f, "success criterion"),
                         "the observable condition\nthat means it is fixed")

    def test_value_written_beneath_the_label_is_read(self) -> None:
        """The `issue_256`/`258`/`364`/`366` shape — `field()` reads these as EMPTY, so the
        human was asked "did this work?" against a blank line, with C6 gating on it."""
        f = self._brief("- **Success criterion:**\n"
                        "  the whole value lives on the next line\n"
                        "- **Scope:** narrow\n")
        self.assertEqual(brief.whole_field(f, "success criterion"),
                         "the whole value lives on the next line")
        self.assertEqual(brief.field(f, "success criterion"), "",
                         "field() must keep its single-line contract")

    def test_nested_sub_bullets_do_not_end_the_block(self) -> None:
        """The case that makes indentation, not the field pattern, the terminator.

        `  - **API:** …` matches the field regex exactly. A parser that tests the pattern
        before the indent ends the block at the first nested bullet and yields an EMPTY
        Scope — blanking the field rather than truncating it, which is worse than the bug.
        """
        f = self._brief("- **Scope:**\n"
                        "  - **API:** add the new endpoint\n"
                        "  - **CLI:** preserve the old command\n"
                        "- **Test file:** t.py\n")
        self.assertEqual(brief.whole_field(f, "scope"),
                         "- **API:** add the new endpoint\n- **CLI:** preserve the old command")

    def test_block_ends_at_the_next_field(self) -> None:
        f = self._brief("- **Scope:** only this\n- **Test file:** t.py\n")
        self.assertEqual(brief.whole_field(f, "scope"), "only this")

    def test_block_ends_at_a_heading(self) -> None:
        f = self._brief("- **Scope:** only this\n\n## Iteration 1 — carry-forward\nprose\n")
        self.assertEqual(brief.whole_field(f, "scope"), "only this")

    def test_block_ends_at_unindented_prose(self) -> None:
        """Without this a field near the end of a brief swallows whatever follows it."""
        f = self._brief("- **Scope:** only this\nunindented prose follows\n")
        self.assertEqual(brief.whole_field(f, "scope"), "only this")

    def test_absent_field_returns_the_default(self) -> None:
        f = self._brief("- **Scope:** x\n")
        self.assertEqual(brief.whole_field(f, "success criterion", default="D"), "D")

    def test_labels_resolve_in_priority_order_not_file_order(self) -> None:
        """Same label-fallback contract as `field()`: the first LABEL that exists wins,
        regardless of which appears first in the file."""
        f = self._brief("- **Goal:** the fallback label\n- **Defect:** the primary label\n")
        self.assertEqual(brief.whole_field(f, "defect", "goal"), "the primary label")

    def test_placeholder_is_returned_raw(self) -> None:
        """No `_is_placeholder` filtering: an unfilled field must render as it does today
        rather than silently vanishing from §1."""
        f = self._brief("- **Success criterion:** <the observable condition — must be\n"
                        "  demonstrable by C4-verify\n")
        self.assertTrue(brief.whole_field(f, "success criterion").startswith("<the observable"))

    def test_shipped_template_placeholder_round_trips(self) -> None:
        """The two-line Success criterion in `templates/brief.md.tpl` is the shape that
        produced the 93% rate — assert against the real file, not a copy of it."""
        tpl = Path(__file__).resolve().parents[1] / "templates" / "brief.md.tpl"
        value = brief.whole_field(tpl, "success criterion")
        self.assertIn("\n", value, "the shipped placeholder is multi-line; parser lost it")
        self.assertGreater(len(value), len(brief.field(tpl, "success criterion")))


class SummarySpecFields(unittest.TestCase):
    """§1 must carry the whole value, formatted so it stays one Markdown list item."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub", family="claude"),
            reviewer=LeafConfig(mode="stub", family="codex"),
        )
        self.cfg.gates_checks = [_PASS_GATE]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _summary(self, brief_body: str) -> str:
        d = self.cfg.bundle("336")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(brief_body, encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        gates.run_gates(d, self.cfg)
        assemble.assemble_summary(d, self.cfg)
        return (d / "SUMMARY.md").read_text(encoding="utf-8")

    def test_multi_line_criterion_reaches_summary_whole(self) -> None:
        text = self._summary(
            "- **Slug:** wide-fix\n"
            "- **Defect:** the thing is broken\n"
            "- **Success criterion:** a STREAMING-UNSIGNED-PAYLOAD-TRAILER PUT and a\n"
            "  chunked PUT both round-trip byte-for-byte\n"
            "- **Scope:** the parser only\n"
        )
        self.assertIn("chunked PUT both round-trip byte-for-byte", text,
                      "§1 truncated the success criterion at its first line")

    def test_continuations_stay_inside_the_list_item(self) -> None:
        """A bare continuation would terminate the `- ` list and render as body prose."""
        text = self._summary(
            "- **Slug:** wide-fix\n"
            "- **Success criterion:** first line\n"
            "  second line\n"
            "- **Scope:** narrow\n"
        )
        self.assertIn("- Success criterion: first line\n  second line", text)

    def test_title_slug_is_flattened_to_one_line(self) -> None:
        """The title is a Markdown heading, so a two-space continuation would render as
        literal text and the `#` heading would end mid-value."""
        text = self._summary("- **Slug:**\n  wrapped-slug-value\n- **Scope:** x\n")
        first = text.splitlines()[0]
        self.assertEqual(first, "# Result — issue 336 / wrapped-slug-value")

    def test_section6_and_signoff_still_parse(self) -> None:
        """Widening §1 must not disturb the sections that ARE parsed back out."""
        text = self._summary(
            "- **Slug:** s\n"
            "- **Success criterion:** one\n  two\n"
            "- **Scope:** x\n"
        )
        d = self.cfg.bundle("336")
        self.assertIn("## 6.", text)
        self.assertIsInstance(signoff.open_needs_human(d / "SUMMARY.md"), list)




class NestedListRendering(unittest.TestCase):
    """A value that is a nested list must not be flattened into its label (PR #344 review).

    `- **Scope:**` followed by indented `- **API:** …` bullets is the shape the module
    docstring itself uses as the motivating example. Rendering it inline produces
    `- Scope: - **API:** …` with the *remaining* bullets nested beneath — the first child
    absorbed into the label, the rest one level deep: a different document.
    """

    def test_a_nested_list_value_keeps_its_hierarchy(self) -> None:
        from pdca_harness import assemble
        value = "- **API:** a\n- **CLI:** b"
        rendered = f"- Scope: {assemble._item(value)}"
        self.assertNotIn("- Scope: - **API:**", rendered,
                         "the first sub-bullet was flattened into the label")
        lines = rendered.splitlines()
        self.assertEqual(lines[0].rstrip(), "- Scope:")
        self.assertEqual(lines[1:], ["  - **API:** a", "  - **CLI:** b"])

    def test_an_inline_value_is_unchanged(self) -> None:
        from pdca_harness import assemble
        self.assertEqual(assemble._item("plain value"), "plain value")
        self.assertEqual(assemble._item("first\nsecond"), "first\n  second")


class HierarchyIsPreserved(unittest.TestCase):
    """Round two on #344: a field value keeps the shape the brief gave it."""

    def _brief(self, body: str) -> Path:
        f = Path(tempfile.mkdtemp()) / "brief.md"
        f.write_text(body, encoding="utf-8")
        return f

    def test_relative_indentation_survives(self) -> None:
        """`Scope → API → GET` must not flatten into three siblings. Stripping each line
        independently loses every level, and `_item` then indents them equally — so §1
        states a different specification from the one the brief authored."""
        f = self._brief("- **Scope:**\n  - API:\n    - GET /things\n    - POST /things\n"
                        "  - CLI\n- **Test file:** t.py\n")
        self.assertEqual(brief.whole_field(f, "scope"),
                         "- API:\n  - GET /things\n  - POST /things\n- CLI")

    def test_the_rendered_item_keeps_those_levels(self) -> None:
        from pdca_harness import assemble
        f = self._brief("- **Scope:**\n  - API:\n    - GET /things\n  - CLI\n")
        rendered = f"- Scope: {assemble._item(brief.whole_field(f, 'scope'))}"
        self.assertIn("\n  - API:", rendered)
        self.assertIn("\n    - GET /things", rendered, "a child became a sibling")
        self.assertIn("\n  - CLI", rendered)

    def test_an_ordered_list_is_recognised_too(self) -> None:
        """`1. API` is a valid list marker; an unordered-only test absorbed the first
        ordered child into the label while indenting the rest beneath it."""
        from pdca_harness import assemble
        f = self._brief("- **Scope:**\n  1. API\n  2. CLI\n")
        rendered = f"- Scope: {assemble._item(brief.whole_field(f, 'scope'))}"
        self.assertNotIn("- Scope: 1. API", rendered)
        self.assertIn("\n  1. API", rendered)
        self.assertIn("\n  2. CLI", rendered)

    def test_wrapped_prose_is_unaffected(self) -> None:
        """The common case must not regress: a continuation that is prose, not a list,
        still reads as one wrapped value."""
        from pdca_harness import assemble
        f = self._brief("- **Success criterion:** first line\n  wrapped second\n")
        self.assertEqual(assemble._item(brief.whole_field(f, "success criterion")),
                         "first line\n  wrapped second")


if __name__ == "__main__":
    unittest.main()
