"""Slice-size calibration miner (issue #318) — scripts/size-calibrate.

Every test here guards a way the miner can be *quietly* wrong, which is the whole problem: a
wrong number does not crash, it just sets a threshold nobody can defend. Grouped by failure
class rather than by function, because that is what the assertions are really about.

* **Outcome leakage.** The brief is mutated after Do — a rejected attempt appends a
  carry-forward section — so a predictor read from the file on disk partly recovers the outcome
  it claims to predict. The subtle half is that the harness's own field parsers (``brief.field``
  and friends) read the path, not the text, so they leak even when the byte counts do not;
  ``AprioriBrief`` is what closes that, and it refuses the routes that would read around it.
* **An absent OUTCOME is not a measurement.** A bundle with no patch records 0 bytes meaning
  *absent*, and a difficulty band with nothing built in it has no patch size at all. Neither may
  be ranked, medianed, or printed as though it had been observed. This is deliberately NOT the
  same rule as an absent predictor: a brief that declares no ``Scope`` scores 0 words, and that
  zero is a real, Plan-time-knowable fact about the brief which the correlations include on
  purpose. The one predictor filtered instead of counted is ``difficulty_rank``, because it is
  ORDINAL — its 0 would sort below "low" rather than reading as "declared nothing".
* **Corpus membership.** A bundle that never reached Do has no outcome to correlate against.
  But Do is reached three ways — a patch, an iteration archive, or a ``close-disposition``
  marker — and an empty patch is a real Do output, not an absent one. Getting this wrong does
  not error; it silently selects which bundles the threshold gets fitted to.
* **Counting declarations instead of things.** The same prerequisite named in two dependency
  fields is one edge; a ``Difficulty`` of ``high — <rationale prose>`` is one declared band.
  Both inflate a predictor if counted naively. The prose case is a regression test: the first
  run of the miner equality-matched ``Difficulty`` and scored 27 of its 85 bundles as unset.
* **Field values wrap.** ``brief.parse_fields`` reads the label's own line by contract, so
  measuring how big a field is needs a block reader that ends at the next field or heading.

Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from pdca_harness import state

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
            patch: str | None = None, rounds: int = 0, close: str | None = None,
            settle: bool = False) -> Path:
    """A bundle dir on disk with only the pieces a test needs.

    ``settle`` carries it all the way to COMPLETE (the real files, not a forced flag), which
    the render tests need because ``render`` correlates over settled rows only.
    """
    d = root / name
    d.mkdir(parents=True)
    if brief is not None:
        (d / "brief.md").write_text(brief, encoding="utf-8")
    if patch is not None:
        (d / "patch.diff").write_text(patch, encoding="utf-8")
    if close is not None:
        (d / state.CLOSE_MARKER).write_text(close + "\n", encoding="utf-8")
    for n in range(1, rounds + 1):
        (d / f"iteration-v{n}").mkdir()
    if settle:
        (d / "check-gates.json").write_text('{"rows": []}', encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            "# Result\n\n## 9. Check sign-off\n- Outcome: accepted\n", encoding="utf-8")
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

    def test_the_miner_inherits_environment_attribution(self):
        """Issue #436: `iteration_rounds` is ONE definition, imported from the runtime
        backstop — so the miner excludes a round whose archived evidence shows an
        environment fault (a gating red recorded `unverifiable`, clean review) was its
        sole recorded driver, with no second implementation to drift. The bare archives
        in the tests above stay counted (missing evidence must not shrink the signal)."""
        d = _bundle(self.root, "issue_4", patch="", rounds=2)
        (d / "iteration-v1" / "check-gates.json").write_text(json.dumps({"rows": [
            {"check": "C4", "result": "unverifiable", "gating": True},
        ]}), encoding="utf-8")
        (d / "iteration-v1" / "check-review.md").write_text(
            "# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
            "| C1 Spec | PASS | brief.md |\n"
            "| Validation — fitness-to-purpose | NEEDS-HUMAN | human at sign-off |\n",
            encoding="utf-8")
        self.assertEqual(sc.iteration_rounds(d), (1, 0))


class Extract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_briefless_bundle_is_skipped(self):
        self.assertIsNone(sc.extract(_bundle(self.root, "issue_1", brief=None, patch="")))

    def test_bundle_that_never_reached_do_is_skipped(self):
        """Do is reached by a patch, an iteration archive, or a close marker. This bundle has
        none of the three, so it has no outcome to correlate against."""
        self.assertIsNone(sc.extract(_bundle(self.root, "issue_2")))

    def test_empty_patch_with_iterations_is_kept(self):
        """An empty patch is a Do output — Do ran and changed nothing — not a missing one, and
        a bundle that burned rounds to arrive there is the failure being measured, not noise.
        Conflating the two would also corrupt ``has_patch``, which the patch correlations
        subset on."""
        row = sc.extract(_bundle(self.root, "issue_3", patch="", rounds=3))
        self.assertIsNotNone(row)
        self.assertEqual((row.rounds, row.patch_bytes, row.patch_files), (3, 0, 0))
        self.assertEqual(row.has_patch, 1)  # an EMPTY patch, not an absent one

    def test_missing_patch_is_flagged_rather_than_read_as_empty(self):
        """A mid-replan bundle has archives but no live patch; 0 bytes there means absent, and
        ranking it against genuine zeroes would understate the association."""
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

    def test_an_unfinished_bundle_is_not_settled(self):
        """A bundle short of a terminal state may still iterate, so its round count is
        'unfinished', not 'converged'. Counting it would credit an in-flight cycle with an
        outcome it has not earned and bias every correlation toward the calm end."""
        row = sc.extract(_bundle(self.root, "issue_10", patch="", rounds=1))
        self.assertNotIn(row.state, sc._SETTLED)
        self.assertEqual(row.settled, 0)

    def test_an_accepted_bundle_is_settled(self):
        row = sc.extract(_bundle(self.root, "issue_11", patch="", rounds=1, settle=True))
        self.assertEqual((row.state, row.settled), (state.COMPLETE, 1))

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

    def test_a_first_pass_close_reached_do_and_is_kept(self):
        """``close-disposition`` IS the close path's Do artifact — ``state`` reads it as past
        Do. Requiring a patch would drop every first-pass close and bias the corpus toward the
        bundles that needed implementing, dropping valid zero-round outcomes."""
        row = sc.extract(_bundle(self.root, "issue_11", patch=None, close="wont-fix"))
        self.assertIsNotNone(row)
        self.assertEqual((row.rounds, row.has_patch, row.is_close), (0, 0, 1))

    def test_a_patched_bundle_is_not_flagged_as_a_close(self):
        self.assertEqual(sc.extract(_bundle(self.root, "issue_13", patch="")).is_close, 0)

    def test_carry_forward_cannot_declare_a_field_the_brief_never_did(self):
        """The leakage guard where it actually bites: the harness's own field parsers read the
        file, so an absent field appearing in appended gate evidence would switch its predictor
        ON *because the bundle churned*. Carry-forward folds gate evidence in verbatim and that
        evidence can be a multi-line value, so stray bullets genuinely reach the brief."""
        d = _bundle(self.root, "issue_14", patch="", rounds=1)
        with (d / "brief.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## Iteration 1 — carry-forward (from the previous attempt)\n"
                     "- Failing gate: contract build — check the shape below\n"
                     "- **Production reach:** the payment path\n"
                     "- **Depends on:** 42, 43\n"
                     "- **Planning artifact:** docs/adr/0009.md\n"
                     "- **Test file:** tests/test_leaked.py\n"
                     "- **External dependencies:** `protoc`\n"
                     "- **Conflicts with:** 99\n")
        row = sc.extract(d)
        self.assertEqual(
            (row.declares_prod_reach, row.depends_on, row.is_plan_pointer,
             row.ext_deps, row.conflicts_with),
            (0, 0, 0, 0, 0))
        self.assertEqual(row.test_files, 1)  # the brief's own, not the appended one

    def test_fields_declared_above_the_carry_forward_are_still_read(self):
        """The guard narrows what is read; it must not blind the parsers to the real brief."""
        body = BRIEF.format(difficulty="high") + (
            "- **Production reach:** the payment path\n"
            "- **Depends on:** 42\n"
            "- **Planning artifact:** docs/adr/0009.md\n")
        d = _bundle(self.root, "issue_15", brief=body, patch="", rounds=1)
        with (d / "brief.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## Iteration 1 — carry-forward (from the previous attempt)\n- x\n")
        row = sc.extract(d)
        self.assertEqual(
            (row.declares_prod_reach, row.depends_on, row.is_plan_pointer), (1, 1, 1))

    def test_one_prerequisite_named_in_two_dependency_fields_counts_once(self):
        """``waves.declared_deps`` concatenates the three fields because the wave model treats
        them as one edge, and the templates keep the deprecated variants as equivalents — so a
        migrated brief declares the same edge twice. Counting declarations would inflate the
        predictor on exactly those briefs."""
        body = BRIEF.format(difficulty="high") + (
            "- **Depends on:** #5\n"
            "- **Depends on (merged):** issue_5\n"
            "- **Stacks on:** 5, 6\n"
            "- **Conflicts with:** 9, 9\n")
        row = sc.extract(_bundle(self.root, "issue_16", brief=body, patch=""))
        self.assertEqual(row.depends_on, 2)  # {5, 6} — one edge each, however often named
        self.assertEqual(row.conflicts_with, 1)


class AprioriBriefShim(unittest.TestCase):
    """Why a shim rather than a second field parser: the harness's grammar stays the only one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bp = Path(self.tmp.name) / "brief.md"
        self.bp.write_text("on disk", encoding="utf-8")
        self.ap = sc.AprioriBrief(self.bp, "a priori only")

    def test_read_text_yields_the_apriori_text_whatever_arguments_are_passed(self):
        self.assertEqual(self.ap.read_text(), "a priori only")
        self.assertEqual(self.ap.read_text(encoding="utf-8", errors="replace"),
                         "a priori only")

    def test_harness_parsers_see_the_apriori_text(self):
        """sc.brief is the harness module the script itself hands these to."""
        ap = sc.AprioriBrief(self.bp, "- **Slug:** demo\n")
        self.assertEqual(sc.brief.field(ap, "slug"), "demo")

    def test_the_allowlisted_metadata_delegates_to_the_real_path(self):
        self.assertEqual(self.ap.name, "brief.md")
        self.assertEqual(self.ap.suffix, ".md")
        self.assertTrue(self.ap.is_file())
        self.assertTrue(self.ap.exists())

    def test_everything_outside_the_allowlist_is_refused_loudly(self):
        """Why an allowlist and not a blocklist: each of these hands back a real ``Path`` (or
        the file itself), and one ``.read_text()`` later the caller has the FULL brief. There
        are more path-returning methods than a blocklist could chase, so the default is refuse.
        A raised error stops a run; a silently wrong predictor does not."""
        for name in ("open", "read_bytes", "__fspath__",  # read the bytes directly
                     "resolve", "absolute", "with_name", "parent", "expanduser"):  # hand back a Path
            with self.subTest(name=name), self.assertRaises(AttributeError) as caught:
                getattr(self.ap, name)
            self.assertIn("carry-forward", str(caught.exception))

    def test_the_refusal_names_the_way_out(self):
        """A future helper hitting this needs to know what to do, not just that it failed."""
        with self.assertRaises(AttributeError) as caught:
            self.ap.resolve()
        self.assertIn("read_text()", str(caught.exception))

    def test_os_fspath_fails_rather_than_yielding_the_real_file(self):
        """The implicit protocol lookup skips __getattr__, so the guard here is the ABSENCE of
        __fspath__: `open(ap)` must raise, not quietly reopen the unnarrowed file."""
        import os
        with self.assertRaises(TypeError):
            os.fspath(self.ap)


class DifficultySummary(unittest.TestCase):
    """An absent patch is not a 0 KB patch — the one direction this table must never fail in,
    since it would invent a small median for a band a detector gets calibrated against."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _row(self, name, difficulty, *, patch, rounds=0, close=None):
        d = _bundle(self.root, name, brief=BRIEF.format(difficulty=difficulty),
                    patch=patch, rounds=rounds, close=close, settle=True)
        row = sc.extract(d)
        self.assertEqual(row.settled, 1)  # render() drops unsettled rows; the fixture must land
        return row

    def test_a_band_with_no_patches_reports_no_median(self):
        """A close-only band: every patch_bytes is 0 meaning ABSENT, so there is nothing to
        take a median of."""
        rows = [self._row("issue_1", "low", patch=None, close="wont-fix")]
        band, n, med_rounds, n_patched, med_kb = sc.difficulty_summary(rows)[0]
        self.assertEqual((band, n, med_rounds, n_patched), ("low", 1, 0, 0))
        self.assertIsNone(med_kb)

    def test_a_mixed_band_medians_only_the_patched_bundles(self):
        rows = [self._row("issue_2", "high", patch="x" * 2048),
                self._row("issue_3", "high", patch=None, close="wont-fix")]
        band, n, _, n_patched, med_kb = sc.difficulty_summary(rows)[0]
        self.assertEqual((band, n, n_patched), ("high", 2, 1))
        self.assertAlmostEqual(med_kb, 2.0)  # not 1.0 — the absent patch is not a zero

    def test_the_rendered_table_prints_n_a_rather_than_zero(self):
        rows = [self._row("issue_4", "low", patch=None, close="wont-fix")]
        table = sc.render(rows, 0)
        self.assertRegex(table, r"low\s+1\s+0\.0\s+0\s+n/a")

    def test_a_close_bundle_has_no_patch_size_in_the_worst_table(self):
        """A settled row without a patch is a close bundle — nothing else survives ``state``'s
        patch-or-marker test — and it surfaces here whenever fewer than ten bundles burned a
        round. '0.0 KB / 0 files' there would read as a measurement rather than an absence."""
        rows = [self._row("issue_5", "high", patch="x" * 1024),
                self._row("issue_6", "high", patch=None, close="wont-fix")]
        table = sc.render(rows, 0)
        self.assertRegex(table, r"issue_6\s+0\s+n/a\s+n/a")
        self.assertRegex(table, r"issue_5\s+0\s+1\.0\s+0")  # a real patch still prints


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
