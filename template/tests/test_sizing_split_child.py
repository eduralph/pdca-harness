"""`sizing.estimate` must not score the split's own scheduling metadata as churn (#457).

`split.materialise` writes a `Conflicts with` entry into every child naming its siblings
(`split.py:493-499`) and `split.rewrite_ordering` turns those proposal-local labels into
the same real ids the lineage record stores under `siblings` (`split.py:333-358`) — the
splitter is told outright that the ordering fields *between* children are the point
(`leaves.py:1261`). Yet the estimator counted a sibling id exactly like an organic one, so
three of its five weighted features were artifacts the process itself installed:
`conflicts_with` (+3), `difficulty_high` inherited from a `high` parent (+3), `ext_deps`
copied down (+3). 9 against a cutoff of 7 — every materialised child banded `oversized`
before anyone read its scope, and `is_plan_pointer` (−2), the one de-escalating term, is
one a split child never has.

Four things are pinned here, one per success criterion:

(a) sibling conflicts no longer carry the score, so a real materialised child lands below
    the cutoff — proven against a child produced by `split.accept` itself;
(b) organic conflicts — any id that is NOT a sibling — still score at full weight, and a
    bundle with no lineage scores exactly what it scored before, asserted by re-running
    the PRE-EXISTING `test_sizing.Structural` fixtures rather than only a fixture invented
    here;
(c) the excluded count is exposed as data, and it counts CONFLICTS rather than merely
    reporting that lineage exists — a report keyed on presence of lineage would score a
    proposal whose children all conflict pairwise as a clean split;
(d) the estimator and `scripts/size-calibrate` mine the same quantity under the shared
    feature name, by calling the same function — not two agreeing copies.

Plus the standing contract the exclusion must not break: the lineage record is a file a
human can hand-edit, and a malformed one must make the estimate ABSTAIN, never crash the
Plan beat (`sizing.estimate`'s docstring, `sizing.py:220-224` pre-fix).

Per the brief: import the MODULE, never the new symbols, so a red run (production hunks
reverted) fails with a real `AttributeError` on `sizing.<name>` rather than an
`ImportError` that would exit the verifier as PDCA-UNVERIFIABLE instead of proving a red.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import brief, sizing, split
from pdca_harness.config import Config, LeafConfig

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"
SCRIPT = HERE.parent / "scripts" / "size-calibrate"

_CFG = SimpleNamespace(sizing={})

#: The shape the defect is about: a child of a `high` parent whose `Conflicts with` names
#: nothing but its siblings, with the parent's difficulty and dependency tokens copied
#: down. Its actual scope is one function; its churn features are all inherited.
_CHILD_1 = ("- **Slug:** child one\n- **Defect / goal:** a\n"
            "- **Difficulty:** high\n"
            "- **External dependencies:** `protoc`\n"
            "- **Conflicts with:** child-2, child-3\n")
_CHILD_2 = "- **Slug:** child two\n- **Defect / goal:** b\n"
_CHILD_3 = "- **Slug:** child three\n- **Defect / goal:** c\n"


def _proposal(*children: str) -> str:
    body = "<!-- pdca:split-proposal v1 -->\n# Split proposal\n\n"
    for i, child in enumerate(children, 1):
        body += (f"<!-- pdca:child child-{i} -->\n{child}\n"
                 f"<!-- pdca:end child-{i} -->\n\n")
    return body


def _load_by_path(path: Path, name: str):
    """Load a file as a module INSIDE a test body, never at import time.

    Two files are loaded this way, for the same reason. `scripts/size-calibrate`
    `sys.exit`s when `pdca_harness` cannot supply what it imports, so on the C4 red leg
    (production hunks reverted, tests kept) a module-level load would kill this module
    before a single case ran — 0 tests, exit 77 PDCA-UNVERIFIABLE, which is the absence of
    a measurement rather than the red it looks like. Inside a test body the same failure is
    an ordinary error on one case. `tests/test_sizing.py` is loaded by path rather than
    imported because how it is IMPORTABLE differs between the two documented invocations
    (`unittest discover -s tests` puts `tests/` on the path, `-m unittest
    tests.test_sizing_split_child` does not), and a fixture that only exists under one of
    them is not a fixture.

    Registered under its own name so a full-suite run does not clobber the peer module's
    already-loaded copy; registration must precede exec, because `@dataclass` resolves a
    field's type through `sys.modules[cls.__module__]`.
    """
    loader = SourceFileLoader(name, str(path))
    spec = spec_from_loader(name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def _load_calibrate():
    return _load_by_path(SCRIPT, "size_calibrate_457")


class SplitChildBase(unittest.TestCase):
    """Every fixture here is a REAL split: the children come out of `split.accept`, which
    runs `materialise` (writes the lineage record) and `rewrite_ordering` (turns the
    sibling labels into ids). A hand-built lineage file would prove the estimator agrees
    with this test's idea of a split child, not with the splitter's."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")

    def _accept(self, *children: str, ids: list[str] | None = None) -> list[Path]:
        children = children or (_CHILD_1, _CHILD_2, _CHILD_3)
        (self.parent / split.PROPOSAL).write_text(_proposal(*children), encoding="utf-8")
        return split.accept(self.parent, ids or ["601", "602", "603"], self.cfg)

    def _rewrite_siblings(self, child: Path, value) -> None:
        """Hand-edit the child's own lineage record — the thing an operator can break."""
        path = child / split.LINEAGE
        record = json.loads(path.read_text(encoding="utf-8"))
        record["siblings"] = value
        path.write_text(json.dumps(record), encoding="utf-8")

    def _lineage_free_brief(self, body: str) -> Path:
        path = Path(tempfile.mkdtemp(dir=self.tmp)) / "brief.md"
        path.write_text(body, encoding="utf-8")
        return path


class SiblingConflictsAreNotChurn(SplitChildBase):
    """(a) The materialised child scores below the `oversized` cutoff it used to band."""

    def test_a_child_whose_only_conflicts_are_siblings_scores_below_the_cutoff(self) -> None:
        child = self._accept()[0]

        # Fixture sanity FIRST: production `split` really produced the case the brief
        # describes — two REAL sibling ids in `Conflicts with`, not proposal-local labels.
        record = split.read_lineage(child)
        self.assertIsNotNone(record, "materialise wrote no lineage record")
        self.assertEqual(sorted(record["siblings"]), ["602", "603"])
        self.assertEqual(sorted(brief.conflicts_with(child / "brief.md")), ["602", "603"],
                         "rewrite_ordering did not turn the sibling labels into real ids")

        est = sizing.estimate(child / "brief.md", _CFG)
        self.assertLess(est.score, sizing.DEFAULT_OVERSIZED,
                        "a child whose only conflicts are its own siblings still scored "
                        f"oversized: {est.reasons}")
        self.assertNotEqual(est.churn_band, sizing.OVERSIZED,
                            "the split's own scheduling metadata still banded this child "
                            "oversized")
        self.assertNotIn("conflict(s) declared", "; ".join(est.reasons),
                         "the excluded conflicts are still quoted as a churn reason")

    def test_the_same_fixture_reaches_the_cutoff_under_the_unfixed_formula(self) -> None:
        """The falsifiability check inline: recompute the pre-#457 score by hand — the
        three weights that fire regardless of which conflicts are organic. Without this,
        a fixture too small to reach 7 would let the assertion above pass vacuously."""
        self._accept()
        w = sizing.DEFAULT_WEIGHTS
        naive = w["difficulty_high"] + w["ext_deps"] + w["conflicts_with"]
        self.assertGreaterEqual(naive, sizing.DEFAULT_OVERSIZED,
                                "the fixture does not reach the oversized cutoff pre-fix, "
                                "so it is not exercising the defect")


class OrganicConflictsStillScore(SplitChildBase):
    """(b) Only the split's own metadata is excluded. Everything else scores as it did."""

    def test_an_organic_conflict_inside_a_split_child_scores_full_weight(self) -> None:
        child = self._accept()[0]
        path = child / "brief.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("602, 603", text, "fixture assumption changed upstream")
        # One more conflict, naming a bundle that is NOT a sibling — organic churn.
        path.write_text(text.replace("602, 603", "602, 603, 999"), encoding="utf-8")

        est = sizing.estimate(path, _CFG)
        self.assertEqual(est.sibling_conflicts, 2)
        baseline = sizing.estimate(
            self._lineage_free_brief("- **Slug:** s\n- **Conflicts with:** 999\n"), _CFG)
        organic = est.score - (sizing.DEFAULT_WEIGHTS["difficulty_high"]
                               + sizing.DEFAULT_WEIGHTS["ext_deps"])
        self.assertEqual(organic, baseline.score,
                         "an organic conflict scored differently inside a split child "
                         "than the same conflict scores standalone")
        self.assertEqual(est.churn_band, sizing.OVERSIZED,
                         "a genuinely conflicted child must still band oversized")

    def test_a_bundle_with_no_lineage_scores_exactly_what_it_scored_before(self) -> None:
        """The synthetic half of (b) — the whole estimate, not only its score, because
        "unchanged" includes the reasons a human reads. The fixture is
        `test_sizing.Structural.test_bands_follow_the_cutoffs`'s `high` brief; that
        existing test is re-run for real in :class:`ThePreExistingFixturesAreUnchanged`.
        """
        path = self._lineage_free_brief("- **Slug:** s\n- **Difficulty:** high\n"
                                        "- **Conflicts with:** 1\n"
                                        "- **External dependencies:** `protoc`\n")
        est = sizing.estimate(path, _CFG)
        self.assertEqual(est.score, 9)
        self.assertEqual(est.churn_band, sizing.OVERSIZED)
        self.assertEqual(est.sibling_conflicts, 0)
        self.assertEqual(est.reasons, ["difficulty=high", "1 conflict(s) declared",
                                       "1 external dependency token(s)"])


class ThePreExistingFixturesAreUnchanged(unittest.TestCase):
    """(b), against fixtures that already existed rather than only ones invented for #457.

    `tests/test_sizing.py` is where #320's calibration is pinned — every weighted feature's
    contribution, the cutoffs, the escalate-only combine. None of those briefs carries a
    lineage record, so all of them must score byte-identically after this change; running
    the real cases is the only way to assert that against the fixtures a reviewer would
    check, instead of against a copy of them that can quietly drift.
    """

    def _run(self, *case_names: str) -> unittest.TestResult:
        peer = _load_by_path(HERE / "test_sizing.py", "test_sizing_457")
        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromTestCase(getattr(peer, name))
            for name in case_names)
        # The peer's own stdout/stderr is swallowed, not forwarded: two of its cases warn
        # deliberately ("sizer escalation binary missing"), and a warning printed twice in
        # one suite run reads like a new one. A failure quotes what it captured, so nothing
        # that matters is lost.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)

    def test_the_calibration_suite_still_passes_unchanged(self) -> None:
        result = self._run("Structural", "Combine", "SecondReviewFixes", "ThirdReviewFixes")
        self.assertGreater(result.testsRun, 0, "the pre-existing fixtures did not run")
        self.assertEqual([], result.failures + result.errors,
                         "excluding sibling conflicts moved something the pre-existing "
                         "estimator fixtures pin: "
                         + "; ".join(f"{t} — {e.strip().splitlines()[-1]}"
                                     for t, e in result.failures + result.errors))


class TheExcludedCountIsExposed(SplitChildBase):
    """(c) The count is data on the estimate — and it counts conflicts, not lineage."""

    def test_children_that_conflict_pairwise_each_expose_a_count(self) -> None:
        """Two children each naming the OTHER: the splitter's own statement that the split
        separated nothing. The score must not reflect it (that is the fix) but the estimate
        must still SAY it, or the convergence report that exists to detect non-convergence
        reads this as a clean split."""
        created = self._accept(
            "- **Slug:** two\n- **Defect / goal:** a\n- **Conflicts with:** child-2\n",
            "- **Slug:** other\n- **Defect / goal:** b\n- **Conflicts with:** child-1\n",
            ids=["701", "702"])

        for bundle in created:
            with self.subTest(bundle=bundle.name):
                est = sizing.estimate(bundle / "brief.md", _CFG)
                self.assertEqual(est.sibling_conflicts, 1,
                                 "the pairwise sibling conflict is invisible on the estimate")
                self.assertNotIn("conflict(s) declared", "; ".join(est.reasons),
                                 "the excluded conflict still moved the score")

    def test_the_count_is_zero_for_a_child_that_declares_no_conflicts(self) -> None:
        """Lineage alone must not raise it. A caller keying wording or a convergence
        verdict on "does this bundle have a parent" would report this quiet child
        identically to the pairwise pair above."""
        quiet = self._accept()[1]                       # child-2 declares no conflicts
        self.assertIsNotNone(split.read_lineage(quiet), "fixture has no lineage to ignore")
        self.assertEqual(sizing.estimate(quiet / "brief.md", _CFG).sibling_conflicts, 0)

    def test_the_count_is_zero_with_no_lineage_at_all(self) -> None:
        est = sizing.estimate(
            self._lineage_free_brief("- **Slug:** s\n- **Conflicts with:** 1\n"), _CFG)
        self.assertEqual(est.sibling_conflicts, 0)

    def test_a_model_escalation_does_not_drop_the_count(self) -> None:
        """`combine` rebuilds the estimate, and the sizer verdict most likely to escalate
        a split child is exactly the one whose non-convergence must stay visible."""
        structural = sizing.SizeEstimate(3, sizing.OK, ["x"], sibling_conflicts=2)
        self.assertEqual(sizing.combine(structural, {"band": "oversized"}).sibling_conflicts,
                         2)


class MalformedLineageAbstains(SplitChildBase):
    """The exclusion must not cost `estimate` its promise never to raise.

    `split.read_lineage` is total over the FILE (`split.py:373-402`) but hands back
    whatever object the JSON held, so the values inside are un-contracted — and the record
    is a bundle file a human edits. Every case here drives the real `sizing.estimate`, the
    production consumer, rather than the helper alone: a helper that abstains is only half
    the contract, and the beat can still die one frame up.
    """

    #: Each maps to "nothing is excluded" — pre-#457 scoring, the direction that
    #: under-corrects rather than silently discarding a conflict someone declared.
    CASES = {
        # The one that bit: a member that cannot be HASHED. It satisfies any "is this a
        # list" guard, and building a set from it raises TypeError out of `estimate` — on a
        # bundle whose brief is perfectly well-formed.
        "a member that is a list": [[]],
        "a member that is an object": [{"id": "602"}],
        "a member that is a set-unhashable nested dict": [{"a": {"b": 1}}],
        # Iterable, but not a list of ids: read as a bare iterable it compares single
        # CHARACTERS against the declared conflict ids.
        "a bare string": "602",
        # `set(mapping)` is its KEYS, so a naive read excludes a real conflict here — the
        # expensive direction, a scoring change with nothing on screen to explain it.
        "a mapping": {"602": 1},
        "a number": 7,
        "a bool": True,
        "null": None,
    }

    def test_estimate_abstains_on_every_malformed_siblings_value(self) -> None:
        child = self._accept()[0]
        unfixed = (sizing.DEFAULT_WEIGHTS["difficulty_high"]
                   + sizing.DEFAULT_WEIGHTS["ext_deps"]
                   + sizing.DEFAULT_WEIGHTS["conflicts_with"])
        for name, value in self.CASES.items():
            with self.subTest(siblings=name):
                self._rewrite_siblings(child, value)
                try:
                    est = sizing.estimate(child / "brief.md", _CFG)
                except Exception as exc:      # pragma: no cover - the assertion IS the point
                    self.fail(f"estimate raised {exc!r} on a `siblings` value that is "
                              f"{name} — a hand-edited record must degrade the hint, "
                              "never crash the Plan beat")
                self.assertEqual(est.sibling_conflicts, 0,
                                 f"{name} was read as a sibling set")
                self.assertEqual(est.score, unfixed,
                                 f"{name} changed the score instead of abstaining")

    def test_estimate_abstains_on_a_record_that_is_not_readable_at_all(self) -> None:
        """The file's own failure modes belong to `read_lineage` (they have their own
        tests); this pins that its abstention reaches THIS consumer as "excluded nothing"
        rather than as an exception or an empty score."""
        child = self._accept()[0]
        (child / split.LINEAGE).write_text("{not json", encoding="utf-8")
        est = sizing.estimate(child / "brief.md", _CFG)
        self.assertEqual(est.sibling_conflicts, 0)
        self.assertEqual(est.churn_band, sizing.OVERSIZED,
                         "an unreadable record must leave the pre-#457 scoring in place")

    def test_a_usable_sibling_beside_junk_is_still_excluded(self) -> None:
        """Tolerance that still does its job. All-or-nothing would be the easy read —
        one bad member and the whole record is discarded — and it would hand a corrupt
        file the power to silently restore the inflation this change removes."""
        child = self._accept()[0]
        self._rewrite_siblings(child, ["602", None, [], {"x": 1}, 603])

        est = sizing.estimate(child / "brief.md", _CFG)
        self.assertEqual(est.sibling_conflicts, 1,
                         "the one usable sibling id was discarded with the junk")
        self.assertIn("1 conflict(s) declared", "; ".join(est.reasons),
                      "603 arrived as a number, so it is not a sibling id this run can "
                      "match — it must keep scoring as organic")

    def test_the_helper_itself_never_raises_on_any_of_them(self) -> None:
        """The same sweep one frame down, so a later caller of the shared helper (the
        calibrator is already one) inherits the guarantee rather than re-deriving it."""
        child = self._accept()[0]
        for name, value in self.CASES.items():
            with self.subTest(siblings=name):
                self._rewrite_siblings(child, value)
                try:
                    count = sizing.sibling_conflict_count(child / "brief.md", ["602", "603"])
                except Exception as exc:      # pragma: no cover - the assertion IS the point
                    self.fail(f"sibling_conflict_count raised {exc!r} on {name}")
                self.assertEqual(count, 0)


class TheCalibratorMinesTheSameQuantity(SplitChildBase):
    """(d) One feature name, one quantity. Otherwise the next Act-cadence retune (#324 /
    #359) fits the `conflicts_with` weight on a number the engine no longer scores."""

    def _reached_do(self, bundle: Path) -> None:
        """`extract` skips a bundle with no evidence it reached Do; a patch is the
        cheapest one."""
        (bundle / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")

    def test_the_calibrator_calls_the_estimator_s_own_exclusion(self) -> None:
        """Asserted by object identity, which holds only if the script imports this very
        function — the discipline `apriori_text` / `AprioriBrief` already set. Two copies
        would agree today and drift silently the first time either side changed."""
        calibrate = _load_calibrate()
        self.assertIs(calibrate.sibling_conflict_count, sizing.sibling_conflict_count,
                      "the calibrator carries its own copy of the sibling exclusion")

    def test_the_two_agree_on_a_real_split_child(self) -> None:
        calibrate = _load_calibrate()
        child = self._accept()[0]
        self._reached_do(child)

        est = sizing.estimate(child / "brief.md", _CFG)
        row = calibrate.extract(child)
        self.assertIsNotNone(row, "the calibrator skipped a bundle the estimator scored")
        self.assertEqual(row.sibling_conflicts, est.sibling_conflicts)
        # What the ENGINE scores here is zero organic conflicts (both declared ids are
        # siblings) — so that is what the column of the same name must hold.
        self.assertEqual(row.conflicts_with, 0,
                         "`conflicts_with` means one thing in the engine and another in "
                         "the corpus a retune would be fitted to")
        self.assertNotIn("conflict(s) declared", "; ".join(est.reasons))

    def test_the_column_is_unchanged_for_a_bundle_with_no_lineage(self) -> None:
        """The other direction of (b), in the calibrator: today's whole published corpus
        is lineage-free, so every one of its `conflicts_with` values must still be the raw
        unique count the figures in `sizing`'s docstring were derived from."""
        calibrate = _load_calibrate()
        organic = self.cfg.bundle("800")
        organic.mkdir(parents=True)
        (organic / "brief.md").write_text("- **Slug:** s\n- **Conflicts with:** 1, 2, 2\n",
                                          encoding="utf-8")
        self._reached_do(organic)

        row = calibrate.extract(organic)
        self.assertIsNone(split.read_lineage(organic), "fixture is not lineage-free")
        self.assertEqual(row.conflicts_with,
                         len(set(brief.conflicts_with(organic / "brief.md"))),
                         "the exclusion moved a column the published calibration used")
        self.assertEqual(row.sibling_conflicts, 0)

    def test_the_excluded_count_is_reported_but_not_correlated(self) -> None:
        """Auditable, not silent — the discipline `carry_forward_bytes` already sets for
        the other quantity the table withholds. And NOT a feature: it is metadata the
        split writes into its own children, so correlating it would fit the process."""
        calibrate = _load_calibrate()
        columns = [f.name for f in calibrate.dataclass_fields(calibrate.Row)]
        self.assertIn("sibling_conflicts", columns)
        self.assertNotIn("sibling_conflicts", calibrate.FEATURES)


if __name__ == "__main__":
    unittest.main()
