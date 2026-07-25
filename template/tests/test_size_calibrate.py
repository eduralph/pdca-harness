"""Slice-size calibration miner (issue #318) — scripts/size-calibrate.

Covers the three things the miner can get quietly wrong, where "quietly" is the problem: a
wrong number here does not crash, it just sets a threshold nobody can defend.

* **Difficulty is prose, not an enum.** Real briefs write ``high — the widest-surface slice: …``
  and ``**hard** — net-new protocol surface``. An equality test scores nearly all of them as
  undeclared; the engine never does that either (``leaves._when_matches`` substring-matches, and
  the opus auto-route is configured ``substring = ["high", "hard"]``). This is a regression test:
  the first run of the miner used equality and reported 27 of 85 bundles as unset.
* **Field values wrap.** ``brief.parse_fields`` reads the label's own line by contract, so
  measuring how big a field is needs a block reader that ends at the next field or heading.
* **Corpus membership.** A bundle that never reached Do has no outcome to correlate against, but
  one that reached Do and produced an EMPTY patch is a real non-convergence data point and must
  be kept — that is exactly the shape of the worst bundle in the observed corpus.

Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "size-calibrate"

# The script is deliberately extensionless (it is a CLI, not an importable module), so a plain
# import statement cannot reach it — load it by path. It must be registered in sys.modules
# BEFORE exec: @dataclass resolves a field's type through sys.modules[cls.__module__], which is
# None for a module that is merely constructed.
_loader = SourceFileLoader("size_calibrate", str(SCRIPT))
_spec = spec_from_loader(_loader.name, _loader)
assert _spec is not None
sc = module_from_spec(_spec)
sys.modules[_spec.name] = sc
_loader.exec_module(sc)


BRIEF = """# Brief — issue 1 / demo

- **Slug:** demo
- **Defect:** the thing is broken
- **Success criterion:** the observable condition holds
  and it keeps holding under load
- **Difficulty:** {difficulty}
- **Scope:** one logical fix / out of scope: everything else
- **Test file:** tests/test_demo.py

## STOP discipline

Draft only until Check sign-off.
"""


def _bundle(root: Path, name: str, *, brief: str | None = BRIEF.format(difficulty="high"),
            patch: str | None = None, rounds: int = 0) -> Path:
    """A bundle dir on disk with only the pieces a test needs."""
    d = root / name
    d.mkdir(parents=True)
    if brief is not None:
        (d / "brief.md").write_text(brief, encoding="utf-8")
    if patch is not None:
        (d / "patch.diff").write_text(patch, encoding="utf-8")
    for n in range(1, rounds + 1):
        (d / f"iteration-v{n}").mkdir()
    return d


class NormalizeDifficulty(unittest.TestCase):
    def test_bare_values_map_to_their_band(self):
        for raw, want in (("high", "high"), ("medium", "medium"), ("low", "low")):
            self.assertEqual(sc.normalize_difficulty(raw), want)

    def test_trailing_rationale_still_matches(self):
        """The #318 regression: real briefs append prose after the band."""
        self.assertEqual(
            sc.normalize_difficulty("high — the widest-surface m4 slice: the cfg-alias and dst"),
            "high")
        self.assertEqual(sc.normalize_difficulty("**hard** — net-new network protocol surface"),
                         "high")
        self.assertEqual(sc.normalize_difficulty("medium   (a couple of call sites)"), "medium")

    def test_highest_band_wins_when_a_value_hedges(self):
        """A brief naming two bands is scored at the higher one — the same direction of
        caution the builder auto-route takes."""
        self.assertEqual(sc.normalize_difficulty("low blast radius but high cross-file reach"),
                         "high")

    def test_undeclared_reads_as_empty(self):
        self.assertEqual(sc.normalize_difficulty(""), "")
        self.assertEqual(sc.normalize_difficulty("unknown"), "")


class FieldBlock(unittest.TestCase):
    """field_block is pure: text in, value text out."""

    def setUp(self):
        self.text = BRIEF.format(difficulty="high")

    def test_captures_wrapped_continuation_lines(self):
        block = sc.field_block(self.text, "success criterion")
        self.assertIn("the observable condition holds", block)
        self.assertIn("and it keeps holding under load", block)

    def test_returns_the_value_without_the_label_or_markup(self):
        """Including the ``- **Label:**`` opener would make every word count measure
        boilerplate that is identical across briefs."""
        block = sc.field_block(self.text, "success criterion")
        self.assertNotIn("Success criterion", block)
        self.assertNotIn("**", block)
        self.assertTrue(block.startswith("the observable condition holds"))

    def test_stops_at_the_next_field(self):
        self.assertNotIn("Difficulty", sc.field_block(self.text, "success criterion"))

    def test_stops_at_a_heading(self):
        self.assertNotIn("STOP discipline", sc.field_block(self.text, "test file"))

    def test_unindented_prose_after_a_field_is_not_swallowed(self):
        """Continuation membership is indentation; without that a trailing field absorbs
        whatever prose follows it."""
        text = "- **Scope:** one logical fix\n  and only that\nUnrelated trailing prose.\n"
        block = sc.field_block(text, "scope")
        self.assertIn("and only that", block)
        self.assertNotIn("Unrelated trailing prose", block)

    def test_continuation_may_open_with_an_issue_reference(self):
        """A bare ``^#`` heading test would mistake ``#318`` for a section break."""
        text = "- **Scope:** one logical fix\n  #318 is the tracking issue\n- **Slug:** x\n"
        self.assertIn("#318", sc.field_block(text, "scope"))

    def test_compact_field_syntax_is_accepted(self):
        """brief.field accepts ``-**Label:**``; diverging would report a present field as
        an empty block rather than as absent."""
        self.assertEqual(sc.field_block("-**Scope:** tight\n", "scope"), "tight")

    def test_absent_field_is_empty(self):
        self.assertEqual(sc.field_block(self.text, "production reach"), "")


class ValueBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bp = Path(self.tmp.name) / "brief.md"

    def test_unfilled_placeholder_reads_as_absent(self):
        """An untouched template must not score as a long, richly-specified field."""
        text = BRIEF.format(difficulty="<low | medium | high>")
        self.bp.write_text(text, encoding="utf-8")
        self.assertEqual(sc.value_block(self.bp, text, "difficulty"), "")

    def test_filled_field_is_returned(self):
        text = BRIEF.format(difficulty="high")
        self.bp.write_text(text, encoding="utf-8")
        self.assertEqual(sc.value_block(self.bp, text, "difficulty"), "high")


class AprioriText(unittest.TestCase):
    """The outcome-leakage guard: brief features must not see post-Do text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bp = Path(self.tmp.name) / "brief.md"

    def test_carry_forward_is_stripped(self):
        body = BRIEF.format(difficulty="high")
        self.bp.write_text(
            body + "\n## Iteration 2 — carry-forward (from the previous attempt)\n"
                   "- Sign-off rationale: the fix was wrong\n", encoding="utf-8")
        text = sc.apriori_text(self.bp)
        self.assertNotIn("carry-forward", text)
        self.assertNotIn("the fix was wrong", text)
        self.assertIn("the observable condition holds", text)

    def test_carry_forward_bytes_reports_what_was_excluded(self):
        body = BRIEF.format(difficulty="high")
        tail = "\n## Iteration 2 — carry-forward (from the previous attempt)\n- x\n"
        self.bp.write_text(body + tail, encoding="utf-8")
        self.assertEqual(sc.carry_forward_bytes(self.bp), len(tail.lstrip("\n").encode()))

    def test_a_brief_that_never_iterated_reports_zero(self):
        self.bp.write_text(BRIEF.format(difficulty="high"), encoding="utf-8")
        self.assertEqual(sc.carry_forward_bytes(self.bp), 0)


class Clauses(unittest.TestCase):
    def test_absent_field_scores_zero(self):
        self.assertEqual(sc._clauses(""), 0)

    def test_single_clause_scores_one_not_zero(self):
        """Counting bare separators makes a one-clause criterion indistinguishable from a
        field that is not there at all."""
        self.assertEqual(sc._clauses("the thing works"), 1)

    def test_each_separator_adds_a_clause(self):
        self.assertEqual(sc._clauses("a and b; c"), 3)


class IterationRounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_plain_iterate_to_do_rounds_are_counted(self):
        d = _bundle(self.root, "issue_1", patch="", rounds=3)
        self.assertEqual(sc.iteration_rounds(d), (3, 0))

    def test_rounds_before_a_replan_are_not_charged_to_the_current_brief(self):
        """An iterate-to-Plan archives the brief too, so earlier rounds were spent on a
        DIFFERENT brief and must not be attributed to the one on disk now."""
        d = _bundle(self.root, "issue_2", patch="", rounds=4)
        (d / "iteration-v2" / "brief.md").write_text("an older brief", encoding="utf-8")
        self.assertEqual(sc.iteration_rounds(d), (2, 1))  # only v3 and v4 belong to this brief

    def test_no_archives_is_zero(self):
        self.assertEqual(sc.iteration_rounds(_bundle(self.root, "issue_3", patch="")), (0, 0))


class Extract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_briefless_bundle_is_skipped(self):
        self.assertIsNone(sc.extract(_bundle(self.root, "issue_1", brief=None, patch="")))

    def test_bundle_that_never_reached_do_is_skipped(self):
        self.assertIsNone(sc.extract(_bundle(self.root, "issue_2")))

    def test_empty_patch_with_iterations_is_kept(self):
        """The worst real bundle burned 3 rounds and produced a 0-byte patch. Dropping it
        would delete the clearest evidence of the failure being measured."""
        row = sc.extract(_bundle(self.root, "issue_3", patch="", rounds=3))
        self.assertIsNotNone(row)
        self.assertEqual((row.rounds, row.patch_bytes, row.patch_files), (3, 0, 0))
        self.assertEqual(row.has_patch, 1)  # an EMPTY patch, not an absent one

    def test_missing_patch_is_flagged_rather_than_read_as_empty(self):
        """A mid-replan bundle has archives but no live patch; 0 bytes there means absent,
        and averaging it against genuine zeroes would understate the association."""
        row = sc.extract(_bundle(self.root, "issue_7", patch=None, rounds=2))
        self.assertEqual((row.has_patch, row.patch_bytes), (0, 0))

    def test_features_are_read_from_the_brief(self):
        row = sc.extract(_bundle(self.root, "issue_4", patch="", rounds=1))
        self.assertEqual(row.difficulty, "high")
        self.assertEqual(row.difficulty_rank, 3)
        self.assertEqual(row.test_files, 1)
        self.assertEqual(row.has_out_of_scope, 1)
        self.assertGreater(row.success_words, 0)

    def test_brief_features_exclude_carry_forward(self):
        """The leakage guard, end to end: appended iterate text must not grow brief_bytes,
        or the 'a priori' predictor mechanically tracks the outcome it is meant to predict."""
        clean = sc.extract(_bundle(self.root, "issue_8", patch="", rounds=1))
        d = _bundle(self.root, "issue_9", patch="", rounds=1)
        with (d / "brief.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## Iteration 1 — carry-forward (from the previous attempt)\n"
                     + "- Sign-off rationale: " + "x" * 500 + "\n")
        dirty = sc.extract(d)
        self.assertEqual(dirty.brief_bytes, clean.brief_bytes)
        self.assertGreater(dirty.carry_forward_bytes, 500)

    def test_settled_reflects_terminal_state(self):
        """An AWAITING_SIGNOFF bundle may still iterate, so its zero archives are 'unfinished',
        not 'converged' — the correlation must be able to exclude it."""
        row = sc.extract(_bundle(self.root, "issue_10", patch="", rounds=1))
        self.assertIn(row.settled, (0, 1))
        self.assertEqual(row.settled, int(row.state in sc._SETTLED))

    def test_undeclared_difficulty_ranks_zero(self):
        row = sc.extract(_bundle(self.root, "issue_5",
                                 brief=BRIEF.format(difficulty="<low | medium | high>"),
                                 patch="", rounds=1))
        self.assertEqual((row.difficulty, row.difficulty_rank), ("", 0))

    def test_diff_files_are_counted(self):
        patch = ("diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n"
                 "diff --git a/src/b.rs b/src/b.rs\n--- a/src/b.rs\n+++ b/src/b.rs\n")
        row = sc.extract(_bundle(self.root, "issue_6", patch=patch))
        self.assertEqual(row.patch_files, 2)


class Spearman(unittest.TestCase):
    def test_perfect_monotone_association(self):
        self.assertAlmostEqual(sc.spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]), 1.0)
        self.assertAlmostEqual(sc.spearman([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]), -1.0)

    def test_nonlinear_but_monotone_still_scores_one(self):
        """Why rank correlation: patch sizes are heavily skewed, and the detector only ever
        thresholds — it never extrapolates — so monotonicity is the property that matters."""
        self.assertAlmostEqual(sc.spearman([1.0, 2.0, 3.0, 4.0], [1.0, 4.0, 900.0, 9000.0]), 1.0)

    def test_constant_column_is_undefined_not_zero(self):
        self.assertIsNone(sc.spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_ties_are_averaged(self):
        """A binary feature is all ties; ordinal ranking would distort exactly the columns
        most likely to matter."""
        self.assertEqual(sc._ranks([5.0, 5.0, 9.0]), [1.5, 1.5, 3.0])

    def test_too_few_points_is_undefined(self):
        self.assertIsNone(sc.spearman([1.0], [2.0]))


class CsvDestinationGuard(unittest.TestCase):
    """The read-only guarantee is why this can be pointed at a live corpus without ceremony;
    the one write path is checked rather than trusted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundles = self.root / "results"
        self.bundles.mkdir()

    def test_a_path_outside_the_bundle_tree_is_allowed(self):
        dest = self.root / "out.csv"
        self.assertEqual(sc._checked_csv_dest(dest, self.bundles), dest.resolve())

    def test_a_path_inside_the_bundle_tree_is_refused(self):
        with self.assertRaises(SystemExit):
            sc._checked_csv_dest(self.bundles / "issue_1" / "brief.md", self.bundles)

    def test_traversal_back_into_the_tree_is_refused(self):
        """resolve() collapses ``..``, so this is not a way around the guard."""
        with self.assertRaises(SystemExit):
            sc._checked_csv_dest(self.root / "elsewhere" / ".." / "results" / "x.csv",
                                 self.bundles)


if __name__ == "__main__":
    unittest.main()
