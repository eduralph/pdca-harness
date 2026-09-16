"""A wrapped `- NEEDS-HUMAN` bullet reaches SUMMARY §6 whole, not truncated (#527).

`_needs_human` (`assemble.py`) used to take only a bullet's FIRST physical line — a
`- NEEDS-HUMAN [impl] — src/x.py:12 (`f`):` bullet whose objection continues on
indented lines below lost every word after the first line. The human clearing §6 saw
a question that stopped mid-sentence. Continuation lines (indented deeper than the
bullet) now join the bullet's own text, space-separated, as ONE §6 item — mirroring
the membership rule `brief._block_for` already uses for a wrapped brief field
(`brief.py:70-104`, #336), flattened to one line rather than kept as a block because
each §6 item renders as exactly one `- [ ] …` line.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import assemble, gates, signoff
from pdca_harness.config import Config, LeafConfig

_PASS_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle",
              "gating": True, "cmd": "true"}

# The exact shape from the brief's repro instruction and the real 462/472 cases.
_WRAPPED_IMPL_BULLET = (
    "- NEEDS-HUMAN [impl] — src/x.py:12 (`f`):\n"
    "  the bound counts sleep seconds, not\n"
    "  wall-clock time, so it overshoots.\n"
)


def _stub_config(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, advisory_text: str) -> Path:
        """A bundle that PASSES every gate and has a clean primary review — so §6 is
        fed ONLY by whatever the advisory artifact carries (mirrors
        test_external_dependency_section6.py's `_bundle`)."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** nhml\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        (d / "check-advisory-adversary.md").write_text(advisory_text, encoding="utf-8")
        self.cfg.gates_checks = [_PASS_GATE]
        gates.run_gates(d, self.cfg)
        return d


class WrappedBulletBecomesOneItem(_Base):
    """Criterion (a): first line + every deeper-indented continuation, space-joined,
    one §6 item, never a newline."""

    def test_continuation_text_reaches_the_item(self) -> None:
        d = self._bundle("WRAP1", _WRAPPED_IMPL_BULLET)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 1)
        self.assertIn("overshoots", items[0].text)          # RED pre-fix: word never arrives
        self.assertNotIn("\n", items[0].text)                # one line — the checkbox survives

    def test_summary_section6_carries_the_whole_sentence_as_one_line(self) -> None:
        d = self._bundle("WRAP2", _WRAPPED_IMPL_BULLET)
        assemble.assemble_summary(d, self.cfg)
        summary = (d / "SUMMARY.md").read_text(encoding="utf-8")
        # §5 embeds the raw advisory artifact verbatim (untouched by this fix), so
        # scope the assertion to the §6 checkbox line specifically.
        checkbox_lines = [ln for ln in summary.splitlines()
                          if ln.lstrip().startswith("- [ ]") and "overshoots" in ln]
        self.assertEqual(len(checkbox_lines), 1)
        self.assertIn("the bound counts sleep seconds, not wall-clock time, so it overshoots.",
                      checkbox_lines[0])

    def test_signoff_counts_it_as_one_open_item(self) -> None:
        d = self._bundle("WRAP3", _WRAPPED_IMPL_BULLET)
        assemble.assemble_summary(d, self.cfg)
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        matches = [it for it in open_items if "overshoots" in it]
        self.assertEqual(len(matches), 1)


class ContinuationBoundaries(_Base):
    """Criterion (b): the item ends at a blank line, a same-or-shallower line, a new
    list item at any indent, a heading, a table row, or a code fence."""

    def test_two_consecutive_bullets_stay_two_items(self) -> None:
        text = (
            "- NEEDS-HUMAN first finding\n"
            "  continues onto this line\n"
            "- NEEDS-HUMAN second finding\n"
            "  continues onto this line too\n"
        )
        d = self._bundle("TWOBUL", text)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 2)
        self.assertIn("first finding continues onto this line", items[0].text)
        self.assertIn("second finding continues onto this line too", items[1].text)

    def test_indented_sub_bullet_stays_its_own_item(self) -> None:
        text = (
            "- NEEDS-HUMAN outer finding\n"
            "  more prose\n"
            "  - NEEDS-HUMAN nested finding\n"
        )
        d = self._bundle("SUBBUL", text)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].text, "outer finding more prose")
        self.assertEqual(items[1].text, "nested finding")

    def test_blank_line_ends_the_item(self) -> None:
        text = (
            "- NEEDS-HUMAN a finding\n"
            "  more of it\n"
            "\n"
            "unrelated trailing prose\n"
        )
        d = self._bundle("BLANK", text)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "a finding more of it")

    def test_heading_ends_the_item(self) -> None:
        text = "- NEEDS-HUMAN a finding\n  more of it\n## Next section\nsome other prose\n"
        d = self._bundle("HEAD", text)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "a finding more of it")

    def test_dedented_line_ends_the_item(self) -> None:
        text = "- NEEDS-HUMAN a finding\n  more of it\nback at column zero\n"
        d = self._bundle("DEDENT", text)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "a finding more of it")


class ClassificationUnchanged(_Base):
    """Criterion (c): a multi-line `[impl]` bullet is still one IMPL item, marker
    stripped — mirrors test_autoiterate.py's
    test_advisory_impl_marker_auto_iterates_and_text_is_clean."""

    def test_multiline_impl_bullet_is_one_impl_item_marker_stripped(self) -> None:
        d = self._bundle("IMPLWRAP", _WRAPPED_IMPL_BULLET)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual([i.kind for i in items], [assemble.IMPL])
        self.assertTrue(items[0].text.startswith("src/x.py:12"))  # `[impl] — ` marker gone
        self.assertIn("overshoots", items[0].text)

    def test_single_line_bullet_is_unaffected(self) -> None:
        d = self._bundle("SINGLE", "- NEEDS-HUMAN — a plain one-line finding\n")
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "a plain one-line finding")


if __name__ == "__main__":
    unittest.main()
