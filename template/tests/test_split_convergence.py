"""The split convergence report (issue #459).

`pdca split <id> --accept` files real, unrevokable tracker sub-issues — and with `--ids`
materialises real bundles — without ever running the size estimate over the staged
children. A split that leaves every child `oversized` was therefore discovered a whole
cycle later, when each child's own guard fired and the planner was pointed back at
`pdca split`. `preflight` now answers that question at the one point BOTH acceptance
shapes reach before anything irreversible happens, and it may never do more than report:
its own output cannot change the exit code or the set of bundles created.

Only the MODULES are imported, never the new symbols. A `from pdca_harness.split import
convergence_report` would raise ImportError on the red leg — 0 tests run, exit 77
`PDCA-UNVERIFIABLE` — instead of a red that proves anything.

Run from template/: PYTHONPATH=src python3 -m unittest tests.test_split_convergence
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, sizing, split
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

_SMALL = "- **Slug:** small\n- **Defect / goal:** a narrow fix\n"
_HEAVY = ("- **Slug:** heavy\n- **Defect / goal:** a wide one\n- **Difficulty:** high\n"
          "- **External dependencies:** `sometool`\n")


def _proposal(*children: str, version: int = 1) -> str:
    body = f"<!-- pdca:split-proposal v{version} -->\n# Split proposal\n\n"
    for i, child in enumerate(children, 1):
        body += (f"<!-- pdca:child child-{i} -->\n{child}\n"
                 f"<!-- pdca:end child-{i} -->\n\n")
    return body


class _BrokenStream:
    """A stream that fails and STAYS failed — what a broken pipe actually does.

    A fake that raises once and then recovers is not a broken pipe; it is a stream that
    hiccuped. The previous attempt at this fix was accepted against exactly that fake and
    shipped an unguarded status write, so with a genuinely broken stderr the acceptance
    still ended in an `OSError` — after both bundles were on disk. Once tripped (by write
    ordinal or by content) every subsequent `write` raises, `flush` included.
    """

    def __init__(self, *, fail_from: int = 1, fail_on: str | None = None) -> None:
        self.fail_from = fail_from
        self.fail_on = fail_on
        self.writes = 0
        self.raised = 0
        self.tripped = False
        self.text: list[str] = []

    def _should_fail(self, s: str) -> bool:
        if self.tripped:
            return True
        if self.fail_on is not None:
            return self.fail_on in s
        return self.writes >= self.fail_from

    def write(self, s: str) -> int:
        self.writes += 1
        if self._should_fail(s):
            self.tripped = True
            self.raised += 1
            raise BrokenPipeError(32, "Broken pipe")
        self.text.append(s)
        return len(s)

    def flush(self) -> None:
        if self.tripped:
            self.raised += 1
            raise BrokenPipeError(32, "Broken pipe")

    def value(self) -> str:
        return "".join(self.text)


class _ExcludingEstimate:
    """What `sizing.estimate` returns once it EXCLUDES a child's sibling conflicts (#457).

    Stands in for the dependency, not for the code under test: the exclusion drops those
    declarations out of `score` and `reasons` and reports the number it removed on
    `sibling_conflicts`. A report that read entanglement off the score or the reasons
    would see this as a perfectly clean child — which is the blinding criterion (c) is
    about — so this is the only shape that can prove where the count is read from.
    """

    def __init__(self, band: str, score: int, sibling_conflicts: int) -> None:
        self.band = band
        self.score = score
        self.reasons = ["difficulty=high"]     # deliberately no "conflict(s) declared"
        self.churn_band = band
        self.patch_band = band
        self.model_band = ""
        self.sibling_conflicts = sibling_conflicts


class ConvergenceReport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text(
            "- **Slug:** parent\n- **Defect / goal:** everything\n"
            "- **Difficulty:** high\n- **External dependencies:** `sometool`\n",
            encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------------------

    def _write(self, *children: str) -> None:
        (self.parent / split.PROPOSAL).write_text(_proposal(*children), encoding="utf-8")

    def _children(self) -> list:
        return split.parse((self.parent / split.PROPOSAL).read_text(encoding="utf-8"))

    def _report(self) -> str:
        return "\n".join(split.convergence_report(self.parent, self._children(), self.cfg))

    def _args(self, ids: str = "") -> SimpleNamespace:
        return SimpleNamespace(issue_id="500", accept=True, ids=ids)

    def _accept_via_cli(self, ids: str = "", stderr=None) -> tuple[int, str]:
        """Run the real `pdca split 500 --accept` code path; return (rc, stderr text)."""
        err = io.StringIO() if stderr is None else stderr
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, self._args(ids))
        return rc, err.getvalue() if isinstance(err, io.StringIO) else ""

    def _bundles(self) -> set[str]:
        root = self.cfg.bundle_root
        return {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()

    # -- (a) BOTH acceptance paths report before anything irreversible ------------------

    def test_the_ids_path_reports_before_it_materialises_anything(self) -> None:
        """`--accept --ids a,b` never reached `preflight` at all: it went straight to
        `accept` and printed nothing but the `pdca flow` follow-up. It is the path the
        docs call *required* for a tracker `pdca` cannot reach — the operator who has
        already paid for the issues by hand, and so most needs the verdict."""
        self._write(_SMALL, _HEAVY)
        seen: dict[str, str] = {}
        err = io.StringIO()
        real_accept = split.accept

        def watched(parent, ids, cfg):
            seen["before"] = err.getvalue()          # the irreversible step on this shape
            return real_accept(parent, ids, cfg)

        with mock.patch("pdca_harness.split.accept", watched):
            rc, out = self._accept_via_cli("601,602", stderr=err)
        self.assertEqual(rc, 0)
        self.assertEqual(self._bundles(), {"issue_500", "issue_601", "issue_602"})
        self.assertIn("convergence report", seen["before"])
        self.assertIn("child-1", seen["before"])
        self.assertIn("child-2", seen["before"])

    def test_the_auto_filing_path_reports_before_a_single_issue_is_filed(self) -> None:
        self._write(_SMALL, _HEAVY)
        seen: dict[str, str] = {}
        err = io.StringIO()

        def filer(parent, children, cfg, **kwargs):
            seen["before"] = err.getvalue()          # the irreversible step on this shape
            return ["601", "602"]

        with mock.patch("pdca_harness.split.file_children", filer):
            rc, out = self._accept_via_cli("", stderr=err)
        self.assertEqual(rc, 0)
        self.assertIn("convergence report", seen["before"])
        self.assertIn("child-1", seen["before"])

    # -- (b) per-child band against the parent's, and the feature that carries it -------

    def test_names_each_childs_band_against_the_parents_and_its_driving_reason(self) -> None:
        self._write(_SMALL, _HEAVY)
        # Cross-checked against the SAME estimator the report calls, so this cannot drift
        # into asserting a band the production code no longer produces.
        parent_est = sizing.estimate(self.parent / "brief.md", self.cfg)
        text = self._report()
        self.assertIn(f"parent bands {parent_est.band}", text)
        self.assertIn(f"child-1: {sizing.OK} (score 0) — LOWER than the parent", text)
        self.assertIn(f"child-2: {parent_est.band} ", text)
        self.assertIn("same band as the parent", text)
        self.assertIn("difficulty=high", text)          # child-2's own driving reason

    def test_says_plainly_when_most_children_do_not_band_lower(self) -> None:
        """Both children inherit the parent's own weighted features, so neither reads any
        smaller — the split that costs two cycles and buys nothing."""
        self._write(_HEAVY, _HEAVY)
        text = self._report()
        self.assertIn("NOT CONVERGED", text)
        self.assertIn("2 of 2 child(ren) do not band lower than issue_500", text)

    def test_a_split_that_does_converge_says_so(self) -> None:
        self._write(_SMALL, _SMALL)
        text = self._report()
        self.assertNotIn("NOT CONVERGED", text)
        self.assertIn("converged — 2 of 2 child(ren) band lower than issue_500", text)

    # -- (c) not blinded by the sibling-conflict exclusion ------------------------------

    def test_the_sibling_conflict_count_is_read_from_the_estimate_not_its_score(self) -> None:
        """With #457's exclusion in force the declarations are gone from `score` and
        `reasons` — the estimate looks clean. The count it EXPOSES is the only place the
        entanglement survives, and the report has to read it there."""
        self._write("- **Slug:** a\n- **Conflicts with:** child-2\n",
                    "- **Slug:** b\n- **Conflicts with:** child-1\n")
        excluding = _ExcludingEstimate(sizing.OK, 3, sibling_conflicts=1)
        with mock.patch("pdca_harness.sizing.estimate", return_value=excluding):
            text = self._report()
        self.assertNotIn("conflict(s) declared —", text)   # nothing in the reasons
        self.assertIn("[1 sibling conflict(s) declared]", text)
        self.assertIn("NOT CONVERGED", text)
        self.assertIn("separated nothing", text)
        self.assertNotIn("exposes no `sibling_conflicts`", text)

    def test_an_estimator_that_exposes_no_count_is_named_not_absorbed(self) -> None:
        """The count is #457's; an estimator without it excludes nothing, so the same
        number is the proposal's own — `_validate_ordering` has just proven every ref
        names a sibling. That substitution is stated in the report rather than absorbed
        silently, so a base missing the prerequisite is visible instead of masked."""
        self._write("- **Slug:** a\n- **Conflicts with:** child-2\n",
                    "- **Slug:** b\n- **Conflicts with:** child-1\n")
        bare = SimpleNamespace(band=sizing.OK, score=0, reasons=[])   # no sibling_conflicts
        with mock.patch("pdca_harness.sizing.estimate", return_value=bare):
            text = self._report()
        self.assertIn("exposes no `sibling_conflicts` count", text)
        self.assertIn("[1 sibling conflict(s) declared]", text)
        self.assertIn("separated nothing", text)

    def test_an_estimate_reporting_no_sibling_conflicts_is_believed(self) -> None:
        """The other direction, so the count is load-bearing rather than decorative: an
        estimator that says none of these declarations name siblings must not have a
        `separated nothing` verdict manufactured from the raw ordering fields."""
        self._write("- **Slug:** a\n- **Conflicts with:** child-2\n",
                    "- **Slug:** b\n- **Conflicts with:** child-1\n")
        clean = _ExcludingEstimate(sizing.OK, 3, sibling_conflicts=0)
        with mock.patch("pdca_harness.sizing.estimate", return_value=clean):
            text = self._report()
        self.assertNotIn("separated nothing", text)

    def test_children_that_all_conflict_pairwise_are_reported_as_not_converged(self) -> None:
        """Through the REAL estimator, whichever of the two it is: every pair declares an
        edge, so the splitter itself is saying the split separated nothing."""
        self._write("- **Slug:** a\n- **Conflicts with:** child-2, child-3\n",
                    "- **Slug:** b\n- **Conflicts with:** child-1, child-3\n",
                    "- **Slug:** c\n- **Conflicts with:** child-1, child-2\n")
        text = self._report()
        self.assertIn("NOT CONVERGED", text)
        self.assertIn("every pair of children declares a `Conflicts with` edge", text)

    def test_a_proposal_with_a_free_pair_is_not_reported_as_separating_nothing(self) -> None:
        self._write("- **Slug:** a\n- **Conflicts with:** child-2\n",
                    "- **Slug:** b\n- **Conflicts with:** child-1\n",
                    "- **Slug:** c\n- **Defect / goal:** independent\n")
        text = self._report()
        self.assertNotIn("separated nothing", text)

    def test_the_staged_estimate_presents_the_sibling_LABELS_as_the_sibling_set(self) -> None:
        """No tracker id exists at `preflight` time — every ordering ref is a sibling
        LABEL — so the staged bundle must carry those labels as its lineage siblings, or
        the estimate applies a different rule from the one the live estimator will apply
        the moment the children land."""
        self._write("- **Slug:** a\n- **Conflicts with:** child-2\n", "- **Slug:** b\n")
        staged: list[tuple[str, dict]] = []
        real = sizing.estimate

        def spy(brief_path, cfg):
            staged.append((brief_path.read_text(encoding="utf-8"),
                           split.read_lineage(brief_path.parent) or {}))
            return real(brief_path, cfg)

        with mock.patch("pdca_harness.sizing.estimate", spy):
            self._report()
        children = [rec for body, rec in staged if rec.get("parent") == "500"]
        self.assertEqual([rec["id"] for rec in children], ["child-1", "child-2"])
        self.assertEqual([rec["siblings"] for rec in children],
                         [["child-2"], ["child-1"]])
        self.assertIn("- **Conflicts with:** child-2", staged[1][0])

    # -- (d) the report's own output can never abort the acceptance ---------------------

    def test_a_persistently_broken_stderr_leaves_preflight_itself_unharmed(self) -> None:
        """Driven at `preflight`, where the escape happened: an `OSError` out of it is read
        by `cli._split` as "this bundle has no split-proposal.md" and refuses a proposal
        that is perfectly fine."""
        self._write(_SMALL, _HEAVY)
        stream = _BrokenStream(fail_from=2)       # print() writes the text, then the "\n"
        before = self._bundles()
        with mock.patch.object(split.sys, "stderr", stream):
            split.preflight(self.parent, self._children(), self.cfg)   # must not raise
        self.assertGreaterEqual(stream.raised, 2, "the stream must have stayed broken")
        self.assertEqual(self._bundles(), before)

    def test_a_broken_stderr_changes_neither_the_exit_code_nor_the_bundles(self) -> None:
        self._write(_SMALL, _HEAVY)
        stream = _BrokenStream(fail_from=1)       # broken from the very first write
        with mock.patch.object(cli.sys, "stderr", stream), \
             mock.patch.object(split.sys, "stderr", stream), \
             redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, self._args("601,602"))
        self.assertEqual(rc, 0)
        self.assertEqual(self._bundles(), {"issue_500", "issue_601", "issue_602"})
        self.assertGreaterEqual(stream.raised, 3,
                                "every line on the path must have been attempted")

    def test_a_stderr_that_breaks_after_the_report_still_exits_zero(self) -> None:
        """The regression the previous attempt shipped: the report was guarded and the
        status write after `accept` returned was not, so a stream that broke later raised
        `OSError` with both bundles already on disk — an advisory line changing the exit
        code of an irreversible command."""
        self._write(_SMALL, _HEAVY)
        stream = _BrokenStream(fail_on="marked split")
        with mock.patch.object(cli.sys, "stderr", stream), \
             mock.patch.object(split.sys, "stderr", stream), \
             redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, self._args("601,602"))
        self.assertEqual(rc, 0)
        self.assertEqual(self._bundles(), {"issue_500", "issue_601", "issue_602"})
        self.assertGreaterEqual(stream.raised, 1, "the status write was never attempted")
        self.assertIn("convergence report", stream.value())

    def test_a_broken_stdout_changes_neither_the_exit_code_nor_the_bundles(self) -> None:
        """`pdca split 500 --accept 2>&1 | head` breaks BOTH streams: the created-bundle
        paths go to stdout, and an unguarded `print` there fails the same way."""
        self._write(_SMALL, _HEAVY)
        stream = _BrokenStream(fail_from=1)
        with mock.patch.object(cli.sys, "stdout", stream), \
             redirect_stderr(io.StringIO()):
            rc = cli._split(self.cfg, self._args("601,602"))
        self.assertEqual(rc, 0)
        self.assertEqual(self._bundles(), {"issue_500", "issue_601", "issue_602"})
        self.assertGreaterEqual(stream.raised, 2)

    def test_a_report_that_cannot_be_produced_does_not_refuse_the_acceptance(self) -> None:
        """Advisory in both directions: the report's own failure is named, not raised."""
        self._write(_SMALL, _HEAVY)
        with mock.patch("pdca_harness.split.convergence_report",
                        side_effect=RuntimeError("boom")):
            rc, out = self._accept_via_cli("601,602")
        self.assertEqual(rc, 0)
        self.assertEqual(self._bundles(), {"issue_500", "issue_601", "issue_602"})
        self.assertIn("could not be produced", out)

    # -- (e) advisory and deterministic -------------------------------------------------

    def test_a_non_converging_split_is_still_accepted_in_full(self) -> None:
        """It reports; it never blocks. The bundles, their briefs and the parent's marker
        are exactly what they were before the report existed."""
        self._write(_HEAVY, _HEAVY + "- **Depends on:** child-1\n")
        rc, out = self._accept_via_cli("601,602")
        self.assertEqual(rc, 0)
        self.assertIn("NOT CONVERGED", out)
        self.assertEqual(self._bundles(), {"issue_500", "issue_601", "issue_602"})
        self.assertIn("- **Depends on:** 601",
                      (self.cfg.bundle("602") / "brief.md").read_text(encoding="utf-8"))
        self.assertTrue((self.cfg.bundle("601") / split.LINEAGE).is_file())

    def test_the_report_writes_nothing_into_the_instance(self) -> None:
        """`preflight` runs BEFORE `file_children`; nothing it added may leave a trace."""
        self._write(_SMALL, _HEAVY)
        before = {p.name for p in self.parent.iterdir()}
        self._report()
        self.assertEqual({p.name for p in self.parent.iterdir()}, before)
        self.assertEqual(self._bundles(), {"issue_500"})

    def test_the_report_is_deterministic(self) -> None:
        self._write(_SMALL, _HEAVY)
        self.assertEqual(self._report(), self._report())


if __name__ == "__main__":
    unittest.main()
