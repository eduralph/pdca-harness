"""CHECKED-state trap-door recovery (issue #369; stdlib unittest — no deps).

Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests

`check-gates.json` IS the CHECKED marker (state.state), but the BUILT branch runs
gates → reviewer → advisory leaves as ONE indivisible step (driver.advance). A death
in the window between the gate write and a model leaf (Ctrl-C, OOM, a killed session)
used to land the bundle in CHECKED with that leaf never run — and nothing ever ran it
again: assemble filled the missing-review placeholder and the only escape was
hand-deleting check-gates.json, re-paying the entire gate run (observed for real:
wyrd issue_635). These tests build the interrupted states ON DISK — exactly what a
resumed driver sees — and assert `advance`:

* runs a NEVER-RAN reviewer/advisory leaf (artifact AND error log both absent — the
  #138 failed-leaf discriminator) before assembling, preserving the paid gate record;
* does NOT re-run a leaf that RAN AND FAILED (error log present) — today's behaviour;
* words §6 so a skipped reviewer never reads like a failed one;
* is a strict no-op on an uninterrupted bundle (artifacts byte-identical).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import assemble, dependency_halt, driver, gates, state
from pdca_harness.config import Config, LeafConfig

TOY_BRIEF = Path(__file__).resolve().parents[1] / "examples" / "toy" / "brief.md"


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


class CheckResumeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("TRAP")
        self.d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _trapdoor(self) -> None:
        """The interrupted-beat state on disk: brief + patch + a PAID gate record,
        but NO review artifacts — the beat died between the gate write and the
        reviewer leaf. `state.state` reads it as CHECKED (the defect's premise)."""
        (self.d / "patch.diff").write_text("--- a/f\n+++ b/f\n", encoding="utf-8")
        gates.run_gates(self.d, self.cfg)  # the real gate artifact the beat writes
        self.assertEqual(state.state(self.d), state.CHECKED)
        self.assertFalse((self.d / "check-review.md").exists())
        self.assertFalse((self.d / state.REVIEW_ERROR_LOG).exists())


class NeverRanReviewerIsRecovered(CheckResumeBase):
    """Success criterion (a): a never-ran reviewer is run on the next `advance`,
    before assemble — the paid gate record is preserved, the missing leaf recovered."""

    def test_advance_runs_the_reviewer_before_assembling(self) -> None:
        self._trapdoor()
        gates_bytes = (self.d / "check-gates.json").read_bytes()
        driver.advance(self.d, self.cfg)
        # The (stubbed) reviewer leaf RAN: a real verdict table, not a placeholder.
        review = self.d / "check-review.md"
        self.assertTrue(review.exists(), "reviewer leaf was not recovered at CHECKED")
        self.assertIn("Per-item verdicts", review.read_text(encoding="utf-8"))
        # …and assemble consumed the REAL review, not the missing-review placeholder.
        summary = (self.d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertNotIn("no check-review.md was produced", summary)
        # The expensive gate record was preserved, byte-for-byte — Option A's point.
        self.assertEqual((self.d / "check-gates.json").read_bytes(), gates_bytes)

    def test_never_ran_advisory_leaf_is_recovered_too(self) -> None:
        # The death window can also fall between the reviewer and the advisory leaves:
        # check-review.md landed, the configured advisory leaf's artifact did not.
        self.cfg.advisory_leaves = [{"id": "code-review", "role": "x", "mode": "stub"}]
        self._trapdoor()
        (self.d / "check-review.md").write_text("# real review\n", encoding="utf-8")
        driver.advance(self.d, self.cfg)
        self.assertTrue((self.d / "check-advisory-code-review.md").exists(),
                        "never-ran advisory leaf was not recovered at CHECKED")
        # The already-produced reviewer artifact was NOT re-run / overwritten.
        self.assertEqual((self.d / "check-review.md").read_text(encoding="utf-8"),
                         "# real review\n")


class RanAndFailedIsNotRerun(CheckResumeBase):
    """Success criterion (b): an error log (#138) means the leaf RAN and FAILED —
    today's degrade-to-§6 behaviour is unchanged, no silent re-run."""

    def test_failed_reviewer_is_not_rerun(self) -> None:
        self._trapdoor()
        (self.d / state.REVIEW_ERROR_LOG).write_text(
            "----- attempt 1 — exit 1 -----\nboom\n", encoding="utf-8")
        driver.advance(self.d, self.cfg)
        # The stub reviewer would have written a verdict table; it must not have run.
        self.assertFalse((self.d / "check-review.md").exists(),
                         "a ran-and-failed reviewer was re-run")

    def test_failed_advisory_leaf_is_not_rerun(self) -> None:
        self.cfg.advisory_leaves = [{"id": "code-review", "role": "x", "mode": "stub"}]
        self._trapdoor()
        (self.d / "check-review.md").write_text("# real review\n", encoding="utf-8")
        (self.d / "check-advisory-code-review.error.log").write_text(
            "----- attempt 1 — exit 1 -----\nboom\n", encoding="utf-8")
        driver.advance(self.d, self.cfg)
        self.assertFalse((self.d / "check-advisory-code-review.md").exists(),
                         "a ran-and-failed advisory leaf was re-run")


class Section6DistinguishesSkippedFromFailed(CheckResumeBase):
    """Success criterion (c): the record can tell a reviewer that NEVER RAN from one
    that RAN AND FAILED — the sharp edge the issue names. Assembled directly (not via
    `advance`), because the wording is what covers any path that still reaches
    assemble with the review absent."""

    def test_never_ran_wording(self) -> None:
        self._trapdoor()
        assemble.assemble_summary(self.d, self.cfg)
        summary = (self.d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("NEVER RAN", summary)
        self.assertNotIn("RAN AND FAILED", summary)

    def test_ran_and_failed_wording_points_at_the_error_log(self) -> None:
        self._trapdoor()
        (self.d / state.REVIEW_ERROR_LOG).write_text("boom\n", encoding="utf-8")
        assemble.assemble_summary(self.d, self.cfg)
        summary = (self.d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("RAN AND FAILED", summary)
        self.assertIn(state.REVIEW_ERROR_LOG, summary)
        self.assertNotIn("NEVER RAN", summary)


class UninterruptedCycleIsUntouched(CheckResumeBase):
    """Success criterion (d): a bundle whose beat completed normally assembles
    exactly as today — no leaf re-runs, artifacts byte-identical."""

    def test_complete_artifacts_are_not_rewritten(self) -> None:
        self.cfg.advisory_leaves = [{"id": "code-review", "role": "x", "mode": "stub"}]
        self._trapdoor()
        (self.d / "check-review.md").write_text("# sentinel review\n", encoding="utf-8")
        (self.d / "check-advisory-code-review.md").write_text(
            "# sentinel advisory\n", encoding="utf-8")
        driver.advance(self.d, self.cfg)
        # The stubs would have overwritten both with different content had they run.
        self.assertEqual((self.d / "check-review.md").read_text(encoding="utf-8"),
                         "# sentinel review\n")
        self.assertEqual(
            (self.d / "check-advisory-code-review.md").read_text(encoding="utf-8"),
            "# sentinel advisory\n")
        self.assertTrue((self.d / "SUMMARY.md").exists())

    def test_vendor_complement_unselected_leaf_is_not_promoted(self) -> None:
        # Under vendor-complement (#200) exactly ONE of the pool runs, so the
        # unselected leaf's absent artifact is legitimate — resume must re-apply the
        # selection, not read every absence as "missing" (which would run a leaf the
        # policy excluded on every uninterrupted advance).
        self.cfg.advisory_leaves = [
            {"id": "claude-lens", "family": "claude", "role": "x", "mode": "stub"},
            {"id": "codex-lens", "family": "codex", "role": "x", "mode": "stub"},
        ]
        self.cfg.advisory_selection = {"mode": "vendor-complement"}
        self._trapdoor()
        (self.d / "loop-telemetry.json").write_text(
            json.dumps({"attempts": [{"family": "claude"}]}), encoding="utf-8")
        (self.d / "check-review.md").write_text("# real review\n", encoding="utf-8")
        # The selected complement (codex-lens) completed before the interruption…
        (self.d / "check-advisory-codex-lens.md").write_text("# done\n", encoding="utf-8")
        driver.advance(self.d, self.cfg)
        # …so nothing is missing: the unselected same-vendor leaf must not run now.
        self.assertFalse((self.d / "check-advisory-claude-lens.md").exists(),
                         "resume promoted a leaf the selection policy excluded")
        self.assertEqual((self.d / "check-advisory-codex-lens.md")
                         .read_text(encoding="utf-8"), "# done\n")


class DeterministicStandInNotesAreRecovered(unittest.TestCase):
    """The same death window exists on the two no-model BUILT branches (close
    disposition #60, dependency halt #341): N/A gates land, the deterministic
    review stand-in note does not. Resume rewrites the NOTE — it must never run a
    model reviewer over a bundle those paths deliberately kept away from one."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("TRAP")
        self.d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_close_bundle_gets_its_note_back_not_a_reviewer(self) -> None:
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")
        (self.d / state.CLOSE_MARKER).write_text("likely-close\n", encoding="utf-8")
        gates.run_close_gates(self.d, self.cfg)  # died before _close_review_note
        self.assertEqual(state.state(self.d), state.CHECKED)
        driver.advance(self.d, self.cfg)
        review = (self.d / "check-review.md").read_text(encoding="utf-8")
        self.assertIn("SKIPPED (close disposition)", review)   # the note, rewritten
        self.assertNotIn("Per-item verdicts", review)          # never a model review

    def test_dependency_halted_bundle_gets_its_note_back_not_a_reviewer(self) -> None:
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")
        (self.d / "patch.diff").write_text("--- a/f\n+++ b/f\n", encoding="utf-8")
        dependency_halt.record(self.d, [dependency_halt.Verdict(
            "protoc", dependency_halt.CONFIRMED, "registered", "protoc --version",
            127, "apt install protobuf-compiler", "detect cmd exited 127")])
        gates.run_close_gates(self.d, self.cfg)  # died before blocked_review_note
        self.assertEqual(state.state(self.d), state.CHECKED)
        driver.advance(self.d, self.cfg)
        review = (self.d / "check-review.md").read_text(encoding="utf-8")
        self.assertIn("`protoc` confirmed absent", review)     # the note, rewritten
        self.assertNotIn("Per-item verdicts", review)          # never a model review


if __name__ == "__main__":
    unittest.main()
