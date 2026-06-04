"""Offline slice for the continuous orchestrator, `flow.flow` (stdlib unittest).

Drives a bundle through Plan → Do → Check → sign-off → Act with **stub** leaves
and **stub** gates (no Claude, no TTY, no Docker), proving the deterministic
control flow and the load-bearing C6 guard. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import brief, driver, flow, leaves, signoff, state
from pdca_harness.config import Config, LeafConfig

DESIGN_TPL = Path(__file__).resolve().parents[1] / "templates" / "design-proposal.md.tpl"


def _stub_config(root: Path) -> Config:
    """All five leaves stubbed, gates empty (all-PASS stub rows)."""
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",  # empty → planner stub uses its fallback brief
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
        planner=LeafConfig(mode="stub", family="claude", interactive=True),
        signoff=LeafConfig(mode="stub", family="claude", interactive=True),
        act=LeafConfig(mode="stub", family="claude", interactive=True),
    )


class FlowSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_flow_reaches_complete(self) -> None:
        # No brief yet: Plan (stub) authors one, then Do→Check→sign-off→COMPLETE.
        final = flow.flow(self.cfg, "FLOW", today="2026-06-04")
        self.assertEqual(final, state.COMPLETE)
        d = self.cfg.bundle("FLOW")
        self.assertTrue((d / "brief.md").exists())          # planner stub authored it
        self.assertTrue((d / "SUMMARY.md").exists())
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "merged-wider")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed

    def test_c6_blocks_accept_with_open_needs_human(self) -> None:
        # A sign-off leaf that accepts WITHOUT clearing §6 must not complete.
        def bad_signoff(d: Path, cfg: Config) -> None:
            (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
            # deliberately leaves §6 NEEDS-HUMAN open

        orig = leaves.run_signoff
        leaves.run_signoff = bad_signoff
        try:
            final = flow.flow(self.cfg, "BLOCKED", today="2026-06-04")
        finally:
            leaves.run_signoff = orig
        self.assertEqual(final, state.AWAITING_SIGNOFF)  # C6 stopped the accept
        d = self.cfg.bundle("BLOCKED")
        self.assertNotEqual(signoff.outcome_token(d / "SUMMARY.md"), "merged-wider")

    def test_iterate_do_then_complete(self) -> None:
        # First sign-off iterates; the flow rebuilds and the second accepts.
        calls = {"n": 0}

        def signoff_iter_then_accept(d: Path, cfg: Config) -> None:
            calls["n"] += 1
            summ = d / "SUMMARY.md"
            if calls["n"] == 1:
                (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
            else:
                summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
                (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        orig = leaves.run_signoff
        leaves.run_signoff = signoff_iter_then_accept
        try:
            final = flow.flow(self.cfg, "ITER", today="2026-06-04")
        finally:
            leaves.run_signoff = orig
        self.assertEqual(final, state.COMPLETE)
        self.assertGreaterEqual(calls["n"], 2)  # iterated at least once

    def test_act_runs_on_complete(self) -> None:
        flow.flow(self.cfg, "ACTME", do_act=True, today="2026-06-04")
        log = self.cfg.process_dir / "act-log.md"
        self.assertTrue(log.exists())  # act stub wrote a dated review entry
        self.assertIn("2026-06-04", log.read_text(encoding="utf-8"))

    def test_batch_plans_many_and_completes_all(self) -> None:
        # The planner stub briefs two issues; the batch flow builds + signs off both.
        results = flow.flow_batch(self.cfg, do_act=True, today="2026-06-04")
        self.assertEqual(set(results), {"BATCH1", "BATCH2"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))
        for iid in ("BATCH1", "BATCH2"):
            self.assertEqual(
                signoff.outcome_token(self.cfg.bundle(iid) / "SUMMARY.md"), "merged-wider"
            )

    def test_batch_iterate_then_complete(self) -> None:
        # One batch member iterates-do on its first sign-off; a later pass rebuilds
        # it and both end COMPLETE — exercises the multi-pass build→sign-off loop.
        iterated = {"done": False}

        def signoff_batch(d: Path, cfg: Config) -> None:
            summ = d / "SUMMARY.md"
            if d.name == "issue_BATCH1" and not iterated["done"]:
                iterated["done"] = True
                (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
                return
            summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
            (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        orig = leaves.run_signoff
        leaves.run_signoff = signoff_batch
        try:
            results = flow.flow_batch(self.cfg, today="2026-06-04", max_passes=4)
        finally:
            leaves.run_signoff = orig
        self.assertTrue(iterated["done"])
        self.assertEqual(set(results), {"BATCH1", "BATCH2"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))


class DesignProposalBrief(unittest.TestCase):
    """A GEPS-style feature brief is a richer Plan artifact, not a separate track."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("GEPS")
        self.d.mkdir(parents=True)
        shutil.copyfile(DESIGN_TPL, self.d / "brief.md")  # the design-proposal template

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_keeps_driver_parsed_fields(self) -> None:
        fields = brief.parse_fields(self.d / "brief.md")
        for label in ("slug", "success criterion", "repo + branch target", "test file"):
            self.assertIn(label, fields, f"design-proposal template lost parsed field: {label}")

    def test_feature_brief_flows_and_renders_goal(self) -> None:
        # Do (stub) + Check (stub gates + reviewer) run normally — there IS code.
        self.assertEqual(driver.run_issue(self.d, self.cfg), state.AWAITING_SIGNOFF)
        summary = (self.d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("Defect / goal:", summary)               # assemble fallback rendered
        self.assertIn("the capability this adds", summary)     # the Goal value, not blank


if __name__ == "__main__":
    unittest.main()
