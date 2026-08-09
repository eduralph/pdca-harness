"""Offline slice for the continuous orchestrator, `flow.flow` (stdlib unittest).

Drives a bundle through Plan → Do → Check → sign-off → publish → Act with **stub**
leaves and **stub** gates (no Claude, no TTY, no Docker), proving the deterministic
control flow, the load-bearing C6 guard, and that publish-on-accept dry-runs when the
publisher leaf is stubbed (never pushes offline). Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import act, brief, cli, driver, flow, leaves, queue, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
DESIGN_TPL = TEMPLATES / "design-proposal.md.tpl"
POINTER_TPL = TEMPLATES / "plan-pointer.md.tpl"
BRIEF_TPL = TEMPLATES / "brief.md.tpl"


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty (all-PASS stub rows)."""
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
        publisher=LeafConfig(mode="stub", family="claude", interactive=True),
        act=LeafConfig(mode="stub", family="claude", interactive=True),
        act_cadence=1,  # most flow tests assert Act runs after a flow; cadence #109 tested separately
        # Hermetic: pin the toy target to a path inside this test's tmp root (absent →
        # worktree isolation legitimately doesn't apply, gates run in place). The sibling
        # convention would resolve to the SHARED `/tmp/example-repo`, where a stray leftover
        # dir with a broken .git flips gates to a fail-closed WorktreeError (#296).
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
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
        # publish-on-accept ran (publisher stub wrote the artifacts) but DRY-RAN —
        # stubbed leaf ⇒ no real git push, so no publish.json is recorded.
        self.assertTrue((d / "commit-msg.txt").exists())
        self.assertFalse((d / "publish.json").exists())

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

    def test_discontinue_disposition_without_c6(self) -> None:
        # A sign-off leaf that discontinues (even with §6 open — independent of C6)
        # ends the flow at DISCONTINUED: terminal, no publish, decision consumed.
        def discontinue_signoff(d: Path, cfg: Config) -> None:
            (d / leaves.SIGNOFF_DECISION).write_text(
                "discontinue\nrestructuring task, handled out-of-band\n", encoding="utf-8")
            # deliberately leaves §6 NEEDS-HUMAN open — discontinue must not be C6-blocked

        orig = leaves.run_signoff
        leaves.run_signoff = discontinue_signoff
        try:
            final = flow.flow(self.cfg, "DISC", today="2026-06-04")
        finally:
            leaves.run_signoff = orig
        self.assertEqual(final, state.DISCONTINUED)
        d = self.cfg.bundle("DISC")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "discontinued")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed
        self.assertFalse((d / "publish.json").exists())           # no publish on a discontinue

    def test_cli_signoff_discontinue_records_discontinued(self) -> None:
        # `pdca signoff <id> --discontinue` records §9 and run_issue performs no transition;
        # the terminal state is in the status queue ordering so `pdca status` renders it.
        d = self.cfg.bundle("DISCCLI")
        self.assertTrue(flow._plan_if_unplanned(self.cfg, d, None))  # planner stub briefs it
        self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)
        args = SimpleNamespace(issue_id="DISCCLI", accept=False, iterate_do=False,
                               iterate_plan=False, discontinue=True, by="tester", delta="")
        self.assertEqual(cli._signoff(self.cfg, args), 0)
        self.assertEqual(state.state(d), state.DISCONTINUED)
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "discontinued")
        self.assertIn(state.DISCONTINUED, cli._STATE_ORDER)

    def test_batch_sweep_excludes_discontinued_bundle(self) -> None:
        # A discontinued (DISCONTINUED) bundle is terminal like COMPLETE: it must stay out
        # of the flow_batch resume set, never re-driven or reported as in-flight.
        d = self.cfg.bundle("DISCONT")
        self.assertTrue(flow._plan_if_unplanned(self.cfg, d, None))
        driver.run_issue(d, self.cfg)
        signoff.record(d / "SUMMARY.md", action="discontinue", by="t", date="2026-06-04")
        self.assertEqual(state.state(d), state.DISCONTINUED)
        results = flow.flow_batch(self.cfg, today="2026-06-04")
        self.assertNotIn("DISCONT", results)                  # excluded from the sweep
        self.assertEqual(state.state(d), state.DISCONTINUED)  # left untouched

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
        # The batch sweep signs off via run_signoff_batch (one session per chunk).
        iterated = {"done": False}

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            for d in bundles:
                summ = d / "SUMMARY.md"
                if d.name == "issue_BATCH1" and not iterated["done"]:
                    iterated["done"] = True
                    (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
                    continue
                summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
                (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        orig = leaves.run_signoff_batch
        leaves.run_signoff_batch = signoff_batch
        try:
            results = flow.flow_batch(self.cfg, today="2026-06-04", max_passes=4)
        finally:
            leaves.run_signoff_batch = orig
        self.assertTrue(iterated["done"])
        self.assertEqual(set(results), {"BATCH1", "BATCH2"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_batch_iterate_plan_then_complete(self) -> None:
        # iterate-plan re-opens a batch member to UNPLANNED (archiving its attempt to
        # iteration-v1/); the sweep must keep looping so a LATER pass re-plans + rebuilds
        # it, rather than break at UNPLANNED when nothing is awaiting sign-off (#105).
        iterated = {"done": False}

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            for d in bundles:
                summ = d / "SUMMARY.md"
                if d.name == "issue_BATCH1" and not iterated["done"]:
                    iterated["done"] = True
                    (d / leaves.SIGNOFF_DECISION).write_text("iterate-plan\n", encoding="utf-8")
                    continue
                summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
                (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        orig = leaves.run_signoff_batch
        leaves.run_signoff_batch = signoff_batch
        try:
            results = flow.flow_batch(self.cfg, today="2026-06-04", max_passes=6)
        finally:
            leaves.run_signoff_batch = orig
        self.assertTrue(iterated["done"])
        self.assertEqual(set(results), {"BATCH1", "BATCH2"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))
        # the re-opened bundle re-planned, rebuilt, and preserved its first attempt
        self.assertTrue((self.cfg.bundle("BATCH1") / "iteration-v1").is_dir())

    def test_iterate_plan_reopens_immediately_iterate_do_deferred(self) -> None:
        # #174: in the batch sweep (apply_now=False), an iterate-plan re-open is applied
        # IMMEDIATELY (archive → UNPLANNED, no rebuild) so the next pass's Plan pre-pass
        # re-plans it BEFORE the deferred iterate-do bundles rebuild. An iterate-do stays
        # deferred (ITERATE_DO, no archive/rebuild) so it can't interrupt the queue review.
        dp = self.cfg.bundle("IPLAN")
        self.assertTrue(flow._plan_if_unplanned(self.cfg, dp, None))
        self.assertEqual(driver.run_issue(dp, self.cfg), state.AWAITING_SIGNOFF)
        (dp / leaves.SIGNOFF_DECISION).write_text(
            "iterate-plan\nneeds a different approach\n", encoding="utf-8")
        self.assertEqual(flow._apply_decision(
            self.cfg, dp, by="t", today="2026-06-04", apply_now=False), "iterate-plan")
        self.assertEqual(state.state(dp), state.UNPLANNED)   # re-opened immediately
        self.assertTrue((dp / "iteration-v1").is_dir())      # attempt archived

        dd = self.cfg.bundle("IDO")
        self.assertTrue(flow._plan_if_unplanned(self.cfg, dd, None))
        self.assertEqual(driver.run_issue(dd, self.cfg), state.AWAITING_SIGNOFF)
        (dd / leaves.SIGNOFF_DECISION).write_text(
            "iterate-do\nfix the off-by-one\n", encoding="utf-8")
        self.assertEqual(flow._apply_decision(
            self.cfg, dd, by="t", today="2026-06-04", apply_now=False), "iterate-do")
        self.assertEqual(state.state(dd), state.ITERATE_DO)  # deferred, NOT yet rebuilt
        self.assertFalse((dd / "iteration-v1").is_dir())     # no archive/rebuild on the spot

    def test_batch_signoff_chunks_into_sessions(self) -> None:
        # The cheap-first queue is signed off in ONE session per chunk of
        # SIGNOFF_BATCH_SIZE (=5): six halted bundles → sessions of 5 then 1, all
        # reaching COMPLETE (testbed issue #2 — batch the interactive sign-off).
        ids = [f"C{i}" for i in range(6)]
        for iid in ids:
            leaves.do_plan(self.cfg.bundle(iid), self.cfg)

        sizes: list[int] = []
        real = leaves.run_signoff_batch

        def counting(cfg: Config, bundles: list[Path]) -> None:
            sizes.append(len(bundles))
            real(cfg, bundles)  # stub loops _stub_signoff → accept + clears §6

        leaves.run_signoff_batch = counting
        try:
            results = flow.flow_ids(self.cfg, ids, today="2026-06-06")
        finally:
            leaves.run_signoff_batch = real
        self.assertEqual(sizes, [flow.SIGNOFF_BATCH_SIZE, 1])   # 6 → 5 + 1, one pass
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_batch_sweep_defers_iteration_to_next_pass(self) -> None:
        # apply_now=False (the batch sweep) records an iterate-do but does NOT drive
        # the rebuild on the spot — so the human reviews the rest of the queue first;
        # the next pass's build-all applies it. Spy on driver.run_issue to prove the
        # sweep call doesn't trigger a transition.
        d = self.cfg.bundle("DEFER")
        leaves.do_plan(d, self.cfg)
        self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)

        def signoff_iter(d: Path, cfg: Config) -> None:
            (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")

        calls = {"n": 0}
        orig_run, orig_signoff = driver.run_issue, leaves.run_signoff
        leaves.run_signoff = signoff_iter
        driver.run_issue = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                            or orig_run(*a, **k))
        try:
            action = flow._signoff_and_apply(
                self.cfg, d, by="t", today="2026-06-04", apply_now=False
            )
        finally:
            driver.run_issue, leaves.run_signoff = orig_run, orig_signoff
        self.assertEqual(action, "iterate-do")
        self.assertEqual(calls["n"], 0)  # deferred — no rebuild during the sweep
        # And the default (single-issue flow) DOES apply immediately.
        leaves.run_signoff = signoff_iter
        driver.run_issue = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                            or orig_run(*a, **k))
        try:
            flow._signoff_and_apply(self.cfg, d, by="t", today="2026-06-04")
        finally:
            driver.run_issue, leaves.run_signoff = orig_run, orig_signoff
        self.assertEqual(calls["n"], 1)  # apply_now default drove the transition

    def test_signoff_survives_a_leaf_that_reset_the_bundle(self) -> None:
        # An over-reaching sign-off leaf deletes the downstream (the iterate-plan bug)
        # so there's no SUMMARY.md to record into. _signoff_and_apply must drop the
        # stale decision and return None — not crash the sweep on a missing file.
        d = self.cfg.bundle("OVERREACH")
        leaves.do_plan(d, self.cfg)
        self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)

        def overreaching_signoff(d: Path, cfg: Config) -> None:
            (d / leaves.SIGNOFF_DECISION).write_text("iterate-plan\n", encoding="utf-8")
            for name in ("SUMMARY.md", "patch.diff", "check-gates.json", "check-review.md"):
                (d / name).unlink(missing_ok=True)

        orig = leaves.run_signoff
        leaves.run_signoff = overreaching_signoff
        try:
            action = flow._signoff_and_apply(self.cfg, d, by="t", today="2026-06-04")
        finally:
            leaves.run_signoff = orig
        self.assertIsNone(action)                              # dropped, not crashed
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # stale token consumed

    def test_batch_resumes_in_flight_bundle_not_briefed_this_session(self) -> None:
        # A bundle briefed in a PRIOR session (RESUME) is in flight; this session's
        # Plan only briefs BATCH1/BATCH2. flow_batch must pick RESUME up too — the
        # resume set is "every in-flight brief", not just the ones planned just now.
        leaves.do_plan(self.cfg.bundle("RESUME"), self.cfg)  # pre-existing brief
        results = flow.flow_batch(self.cfg, today="2026-06-04")
        self.assertEqual(set(results), {"BATCH1", "BATCH2", "RESUME"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_batch_holds_a_stale_dep_bundle_without_aborting(self) -> None:
        # #191: a leftover in-flight bundle with a stale `Depends on: GHOST` must NOT abort the
        # whole resume sweep — it is held (left in-flight), and the rest of the batch still runs.
        bad = self.cfg.bundle("BADDEP")
        leaves.do_plan(bad, self.cfg)                        # pre-briefed leftover (PLANNED)
        bp = bad / "brief.md"
        bp.write_text(bp.read_text(encoding="utf-8") + "- **Depends on:** GHOST\n",
                      encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            results = flow.flow_batch(self.cfg, today="2026-06-04")
        self.assertEqual(set(results), {"BATCH1", "BATCH2"})            # BADDEP held, not driven
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))
        self.assertEqual(state.state(bad), state.PLANNED)              # left in-flight for a re-run
        self.assertIn("issue_BADDEP held this run", err.getvalue())
        self.assertIn("GHOST", err.getvalue())

    def test_batch_leaves_complete_bundle_alone_on_rerun(self) -> None:
        # First run completes BATCH1/BATCH2. A second run re-briefs them (stub) but
        # they are already COMPLETE, so the resume set excludes them → nothing to do.
        first = flow.flow_batch(self.cfg, today="2026-06-04")
        self.assertTrue(all(s == state.COMPLETE for s in first.values()))
        second = flow.flow_batch(self.cfg, today="2026-06-04")
        self.assertEqual(second, {})  # no in-flight briefs left → nothing to do

    def test_batch_nothing_to_do_returns_empty(self) -> None:
        # Plan that briefs nothing + no existing bundles → empty, no crash.
        orig = leaves.do_plan_batch
        leaves.do_plan_batch = lambda cfg, csv=None: None
        try:
            results = flow.flow_batch(self.cfg, today="2026-06-04")
        finally:
            leaves.do_plan_batch = orig
        self.assertEqual(results, {})

    def test_cli_flow_empty_batch_exits_zero(self) -> None:
        # A resumable batch with nothing in flight is success (exit 0), not an error,
        # so re-running `flow --from-csv` resumes cleanly instead of looking failed.
        # Regression guard for cli._flow (the bug returned 1 here).
        args = SimpleNamespace(issue_ids=[], from_csv="anything.csv", from_briefs=None,
                               no_publish=True, no_act=True, by="", lanes=None)
        orig = flow.flow_batch
        flow.flow_batch = lambda cfg, **kw: {}
        try:
            rc = cli._flow(self.cfg, args)
        finally:
            flow.flow_batch = orig
        self.assertEqual(rc, 0)

    def test_flow_ids_drives_prebriefed_to_complete(self) -> None:
        # `pdca batch <ids>`: drive already-briefed bundles with NO Plan beat.
        for iid in ("ID1", "ID2"):
            leaves.do_plan(self.cfg.bundle(iid), self.cfg)  # pre-brief, no plan in flow
        results = flow.flow_ids(self.cfg, ["ID1", "ID2"], today="2026-06-04")
        self.assertEqual(set(results), {"ID1", "ID2"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_flow_ids_skips_unbriefed_and_missing(self) -> None:
        # An id with no brief (UNPLANNED dir) and a non-existent id are both skipped;
        # only the briefed id is driven. Skipped is not ABSENT (#468): every id asked
        # for by name gets a disposition back, so the CLI shapes cannot disagree about
        # one — the skipped ones report UNPLANNED, which is exactly "not driven".
        leaves.do_plan(self.cfg.bundle("HASBRIEF"), self.cfg)
        self.cfg.bundle("NOBRIEF").mkdir(parents=True)  # exists but UNPLANNED
        results = flow.flow_ids(
            self.cfg, ["HASBRIEF", "NOBRIEF", "GHOST"], today="2026-06-04"
        )
        self.assertEqual(results, {"HASBRIEF": state.COMPLETE,
                                   "NOBRIEF": state.UNPLANNED,
                                   "GHOST": state.UNPLANNED})
        self.assertFalse((self.cfg.bundle("GHOST")).exists())  # never even created

    def test_batch_isolates_a_failing_bundle(self) -> None:
        # One bundle's build always raises (a leaf left it half-written). The sweep
        # must isolate it and still drive the others to COMPLETE — never crash the
        # batch and lose the rest's progress (testbed issue #3).
        for iid in ("GOOD", "BAD"):
            leaves.do_plan(self.cfg.bundle(iid), self.cfg)

        real_advance = driver.advance

        def flaky(d: Path, cfg: Config):
            if d.name == "issue_BAD":
                raise RuntimeError("boom: leaf left the bundle half-written")
            return real_advance(d, cfg)

        flow.driver.advance = flaky
        try:
            results = flow.flow_ids(self.cfg, ["GOOD", "BAD"], today="2026-06-06")
        finally:
            flow.driver.advance = real_advance
        self.assertEqual(results["GOOD"], state.COMPLETE)    # other bundle proceeded
        self.assertNotEqual(results["BAD"], state.COMPLETE)  # failing one isolated

    def test_queue_skips_a_bundle_whose_read_raises(self) -> None:
        # A bundle halted at AWAITING_SIGNOFF whose §6 read raises must be skipped by
        # the queue, not take the whole queue computation (and the sweep) down (#3).
        for iid in ("OKAY", "GARBLED"):
            d = self.cfg.bundle(iid)
            leaves.do_plan(d, self.cfg)
            self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)

        real = signoff.open_needs_human

        def boom(p: Path) -> list[str]:
            if "GARBLED" in str(p):
                raise RuntimeError("garbled summary")
            return real(p)

        signoff.open_needs_human = boom
        try:
            names = {e.bundle.name for e in queue.awaiting_signoff(self.cfg)}
        finally:
            signoff.open_needs_human = real
        self.assertIn("issue_OKAY", names)        # healthy bundle still queued
        self.assertNotIn("issue_GARBLED", names)  # broken one skipped, no crash


class BatchPlanPrepass(unittest.TestCase):
    """`pdca batch <ids> --plan` (issue #65): an optional Plan pre-pass briefs the
    UNPLANNED ids in one shared session, making flow_ids the id-seeded analogue of
    flow_batch. Default (no flag) is unchanged — UNPLANNED ids are skipped."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prepass_briefs_unplanned_then_drives(self) -> None:
        # Two seeded-but-UNPLANNED bundles (dir exists, no brief) → the pre-pass briefs
        # both (stub batch plan) and drives them to COMPLETE.
        for iid in ("P1", "P2"):
            self.cfg.bundle(iid).mkdir(parents=True)
        results = flow.flow_ids(self.cfg, ["P1", "P2"], plan_missing=True, today="2026-06-20")
        self.assertEqual(set(results), {"P1", "P2"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_prepass_only_plans_the_unplanned_ones(self) -> None:
        # A mix: one already briefed, one UNPLANNED. The shared Plan session is asked to
        # brief ONLY the UNPLANNED id (the briefed one is not re-planned); both complete.
        leaves.do_plan(self.cfg.bundle("PB"), self.cfg)   # already PLANNED
        self.cfg.bundle("PU").mkdir(parents=True)         # UNPLANNED
        captured = {}
        real = leaves.do_plan_batch

        def spy(cfg, csv=None, ids=None):
            captured["ids"] = ids
            return real(cfg, csv, ids=ids)

        leaves.do_plan_batch = spy
        try:
            results = flow.flow_ids(self.cfg, ["PB", "PU"], plan_missing=True, today="2026-06-20")
        finally:
            leaves.do_plan_batch = real
        self.assertEqual(captured["ids"], ["PU"])  # only the un-briefed id planned
        self.assertEqual(set(results), {"PB", "PU"})
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_prepass_leaves_planner_skipped_id_alone(self) -> None:
        # If the Plan session briefs nothing (planner declined), the id stays UNPLANNED
        # and is left out of the drive set — no crash, nothing driven.
        self.cfg.bundle("SKIP").mkdir(parents=True)
        orig = leaves.do_plan_batch
        leaves.do_plan_batch = lambda cfg, csv=None, ids=None: None  # briefs nothing
        try:
            results = flow.flow_ids(self.cfg, ["SKIP"], plan_missing=True, today="2026-06-20")
        finally:
            leaves.do_plan_batch = orig
        # Reported as UNPLANNED, not dropped from the map (#468) — "left alone" is a
        # disposition the caller must be able to see, not an absence it has to infer.
        self.assertEqual(results, {"SKIP": state.UNPLANNED})
        self.assertEqual(state.state(self.cfg.bundle("SKIP")), state.UNPLANNED)

    def test_default_no_prepass_still_skips_unplanned(self) -> None:
        # Without plan_missing, an UNPLANNED id is skipped exactly as before (no Plan beat)
        # — and says so in the map (#468) rather than vanishing from it.
        self.cfg.bundle("U").mkdir(parents=True)
        leaves.do_plan(self.cfg.bundle("B"), self.cfg)
        results = flow.flow_ids(self.cfg, ["U", "B"], today="2026-06-20")
        self.assertEqual(results, {"U": state.UNPLANNED, "B": state.COMPLETE})

    def test_cli_flow_multi_id_auto_plans(self) -> None:
        # Unified `flow <id> <id>` (#86): several ids → batch with plan_missing=True
        # (auto-plan the unbriefed) wired through to flow_ids — no --plan flag.
        captured = {}
        orig = flow.flow_ids

        def spy(cfg, ids, **kw):
            captured.update(kw)
            captured["ids"] = ids
            return {}

        flow.flow_ids = spy
        try:
            args = SimpleNamespace(issue_ids=["X1", "X2"], from_csv=None, from_briefs=None,
                                   no_publish=True, no_act=True, by="", lanes=None)
            cli._flow(self.cfg, args)
        finally:
            flow.flow_ids = orig
        self.assertTrue(captured["plan_missing"])
        self.assertEqual(captured["ids"], ["X1", "X2"])


class CliSurface(unittest.TestCase):
    """The redesigned CLI surface (#86-89): bare → status, the `act` group, `flow`
    arity/usage, and the `--rehearse` dry-run env. Exercises cli.main() end-to-end
    against a minimal pdca.toml (Config.load walks up from cwd)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n', encoding="utf-8")
        self._cwd = Path.cwd()
        os.chdir(self.tmp)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bare_invocation_runs_status(self) -> None:
        self.assertEqual(cli.main([]), 0)  # no subcommand → status dashboard (#88)

    def test_act_group_routes_index_and_log(self) -> None:
        self.assertEqual(cli.main(["act", "index"]), 0)  # frozen-cycle index (empty is fine)
        # `act log` routes through and reports "no frozen cycles" (return 1) — proves the group.
        self.assertEqual(cli.main(["act", "log", "--date", "2026-01-01"]), 1)

    def _freeze_with_candidate(self, iid: str) -> None:
        d = Path("results") / f"issue_{iid}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            "# Result\n\n## 9. Check sign-off\n- Outcome: accepted\n"
            "- By / date: T / 2026-07-01\n\n## 10. Act candidates\n"
            "- [ ] tighten the repro gate for flaky suites\n", encoding="utf-8")

    def test_act_log_preview_is_read_only_append_writes_ledger(self) -> None:
        # #298 review: the help promises `act log` without --append is a SAFE preview,
        # so the #149 ledger registration must ride --append — a preview that dirties
        # process/act-ledger.json contradicts the printed contract.
        for iid in ("1", "2"):                       # two cycles → a recurring signal
            self._freeze_with_candidate(iid)
        ledger = Path("process") / "act-ledger.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["act", "log", "--date", "2026-07-19"]), 0)
        self.assertFalse(ledger.exists())            # preview wrote nothing
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(
                ["act", "log", "--date", "2026-07-19", "--append"]), 0)
        self.assertTrue(ledger.exists())             # recording registers signals

    def test_act_help_documents_the_out_of_turn_workflow(self) -> None:
        # #298: the CLI help is the operator's contract for the out-of-turn Act review —
        # it must carry the load-bearing facts, not leave them to module docstrings.
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            cli.main(["act", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = buf.getvalue()
        for phrase in ("no cadence gate", "COMPLETE", ".act-reviewed",
                       "log --date", "resolve", "irreducible human work"):
            self.assertIn(phrase, text)

    def test_act_log_help_documents_the_append_side_effect(self) -> None:
        # #298: `--append` also stamps process/.act-reviewed (resets the flow cadence) —
        # omitting that makes an operator fear double-reviewing or hand-edit the marker.
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            cli.main(["act", "log", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(".act-reviewed", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["act", "resolve", "--help"])
        self.assertIn("act-ledger.json", buf.getvalue())

    def test_flow_requires_ids_or_csv(self) -> None:
        self.assertEqual(cli.main(["flow"]), 2)  # no ids and no --from-csv → usage error

    def test_no_pdca_toml_is_clean_error_not_traceback(self) -> None:
        # Run outside a rendered project (no pdca.toml at or above) → one clean line,
        # exit 2, NO Python traceback (issue #92).
        import io
        from contextlib import redirect_stderr
        other = Path(tempfile.mkdtemp())  # under the system temp dir; no pdca.toml above
        try:
            os.chdir(other)
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = cli.main(["status"])
            self.assertEqual(rc, 2)
            self.assertIn("no pdca.toml", buf.getvalue())
            self.assertNotIn("Traceback", buf.getvalue())
        finally:
            os.chdir(self.tmp)
            shutil.rmtree(other, ignore_errors=True)

    def test_rehearse_sets_stub_env_before_load(self) -> None:
        cli.main(["flow", "--rehearse"])  # returns 2 (no ids) but sets the dry-run env first
        self.assertEqual(os.environ.get("PDCA_LEAVES_MODE"), "stub")
        self.assertEqual(os.environ.get("PDCA_GATES_MODE"), "stub")
        self.assertEqual(os.environ.get("PDCA_BUNDLE_ROOT"), ".rehearse")


class NotesFetch(unittest.TestCase):
    """Config-driven notes-fetch (issue #65): `[tracker].notes_cmd` seeds a bundle's
    notes.json before a Plan beat so the planner has the tracker thread. Best-effort
    and idempotent; empty by default (no fetch)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetch_writes_notes_json_before_plan(self) -> None:
        # The command is a .format(id=) template; {id} is substituted, so the scraped
        # notes carry the bundle's id. (Literal braces would be escaped {{ }}, like the
        # branch patterns — none needed here.)
        self.cfg.notes_cmd = 'printf %s "thread-for-{id}" > "$PDCA_BUNDLE/notes.json"'
        d = self.cfg.bundle("N1")
        leaves.do_plan(d, self.cfg)  # stub planner; ensure_notes runs first
        self.assertTrue((d / "notes.json").exists())
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), "thread-for-N1")
        self.assertTrue((d / "brief.md").exists())  # planner stub still briefed

    def test_fetch_skipped_when_notes_present(self) -> None:
        d = self.cfg.bundle("N2")
        d.mkdir(parents=True)
        (d / "notes.json").write_text("ORIGINAL", encoding="utf-8")
        self.cfg.notes_cmd = 'echo OVERWRITTEN > "$PDCA_BUNDLE/notes.json"'
        leaves.ensure_notes(self.cfg, d)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), "ORIGINAL")

    def test_fetch_failure_is_nonfatal(self) -> None:
        self.cfg.notes_cmd = "false"  # exits nonzero, writes nothing
        d = self.cfg.bundle("N3")
        leaves.do_plan(d, self.cfg)  # must not raise
        self.assertFalse((d / "notes.json").exists())
        self.assertTrue((d / "brief.md").exists())  # Plan still proceeded

    def test_no_notes_cmd_is_noop(self) -> None:
        d = self.cfg.bundle("N4")
        d.mkdir(parents=True)
        leaves.ensure_notes(self.cfg, d)  # default empty notes_cmd
        self.assertFalse((d / "notes.json").exists())


class DesignProposalBrief(unittest.TestCase):
    """A GEPS-style feature brief is a richer Plan artifact, not a separate track."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("GEPS")
        self.d.mkdir(parents=True)
        # An authored design-proposal brief: fill the slug so it's a real PLANNED brief,
        # not the raw template (a placeholder-slug template now reads UNPLANNED, #113).
        text = DESIGN_TPL.read_text(encoding="utf-8").replace("<short-kebab-slug>", "geps-feature")
        (self.d / "brief.md").write_text(text, encoding="utf-8")

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


class PlanPointerBrief(unittest.TestCase):
    """A pointer-brief (issue #67): the Plan is a reference to the host's own planning
    artifact (ADR / proposal / spec), not a brief authored here. It carries the same
    parsed-field contract, so the driver treats it as a normal PLANNED brief."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("ADR")
        self.d.mkdir(parents=True)
        # An authored pointer-brief: fill the slug so it's a real PLANNED brief, not the
        # raw template (a placeholder-slug template now reads UNPLANNED, #113). Fill the
        # Planning artifact too — an UNFILLED <…> placeholder now reads as absent (#133),
        # so a real authored pointer must give it a concrete value.
        text = POINTER_TPL.read_text(encoding="utf-8").replace("<short-kebab-slug>", "adr-pointer")
        text = re.sub(r"(\*\*Planning artifact:\*\*) <.*?>", r"\1 docs/adr/0042-thing.md",
                      text, flags=re.DOTALL)
        (self.d / "brief.md").write_text(text, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_keeps_driver_parsed_fields(self) -> None:
        fields = brief.parse_fields(self.d / "brief.md")
        for label in ("slug", "success criterion", "repo + branch target", "test file",
                      "planning artifact"):
            self.assertIn(label, fields, f"plan-pointer template lost parsed field: {label}")

    def test_planning_artifact_reader(self) -> None:
        # brief.planning_artifact reads the pointer; a self-contained brief returns "".
        self.assertTrue(brief.planning_artifact(self.d / "brief.md"))
        plain = self.cfg.bundle("PLAIN")
        plain.mkdir(parents=True)
        (plain / "brief.md").write_text("- **Slug:** x\n", encoding="utf-8")
        self.assertEqual(brief.planning_artifact(plain / "brief.md"), "")

    def test_pointer_brief_flows_to_signoff(self) -> None:
        # A pointer-brief is PLANNED and drives Do→Check→sign-off offline like any brief.
        self.assertEqual(state.state(self.d), state.PLANNED)
        self.assertEqual(driver.run_issue(self.d, self.cfg), state.AWAITING_SIGNOFF)
        self.assertTrue((self.d / "SUMMARY.md").exists())


_TOY_BRIEF = (
    "- **Slug:** {slug}\n"
    "- **Defect:** the count is off by one.\n"
    "- **Success criterion:** a test asserts the right count.\n"
    "- **Repo + branch target:** example-org/example-repo @ main\n"
)

# A real bundle-scoped gate that records the worker's $PDCA_LANE into the bundle.
_LANE_GATE = {
    "id": "LANE", "tier": "C4", "label": "record lane",
    "cmd": "printf '%s' \"${PDCA_LANE:-none}\" > \"$PDCA_BUNDLE/lane.txt\"",
    "scope": "bundle", "gating": True,
}


class LaneParallelism(unittest.TestCase):
    """In-driver lane concurrency (docs 09 / issue #19): the unattended Do+Check band
    fans out across `cfg.lanes` workers, each pinned to a fixed lane slot exposed to
    gate commands as `$PDCA_LANE`; Plan / sign-off / publish / Act stay serial."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        # Hermetic against the ambient environment (#419): gate commands inherit the
        # driver's env (gates._merged_env is {**os.environ, **extra}), so when THIS
        # suite runs under a lane-parallel outer driver's T3 gate — which exports
        # PDCA_LANE for its own lane (gates.py) — the serial-path assertion below
        # would read the OUTER driver's lane, not this test's serial flow.
        env_guard = mock.patch.dict(os.environ)
        env_guard.start()
        self.addCleanup(env_guard.stop)
        for key in [k for k in os.environ if k.startswith("PDCA_")]:
            del os.environ[key]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(_TOY_BRIEF.format(slug=iid.lower()), encoding="utf-8")
        return d

    def test_pooled_drive_completes_all_like_serial(self) -> None:
        # Parity: a 3-lane pool drives every briefed bundle to COMPLETE, same as serial.
        ids = ["L1", "L2", "L3", "L4", "L5"]
        for iid in ids:
            self._brief(iid)
        self.cfg.lanes = 3
        results = flow.flow_ids(self.cfg, ids, do_publish=False, do_act=False,
                                today="2026-06-04")
        self.assertEqual(set(results), set(ids))
        self.assertTrue(all(s == state.COMPLETE for s in results.values()),
                        f"not all COMPLETE under a 3-lane pool: {results}")

    def test_pdca_lane_exposed_to_gates_per_worker_slot(self) -> None:
        # A 2-lane pool over 4 bundles: every gate sees a $PDCA_LANE in {0,1} — the
        # worker-slot id — and writes it into its bundle. Proves the lane contract
        # without timing-flakiness (no assertion on which slot got which bundle).
        self.cfg.gates_checks = [_LANE_GATE]
        ids = ["P1", "P2", "P3", "P4"]
        for iid in ids:
            self._brief(iid)
        self.cfg.lanes = 2
        flow.flow_ids(self.cfg, ids, do_publish=False, do_act=False, today="2026-06-04")
        for iid in ids:
            f = self.cfg.bundle(iid) / "lane.txt"
            self.assertTrue(f.exists(), f"gate did not run for {iid}")
            val = f.read_text(encoding="utf-8").strip()
            self.assertIn(val, {"0", "1"}, f"{iid} got PDCA_LANE={val!r}, not a slot in 0..1")

    def test_serial_path_sets_no_pdca_lane(self) -> None:
        # Backward-compat: lanes=1 takes the serial path → no worker pool → gates see
        # no $PDCA_LANE (the shell default `none`), exactly as before this feature.
        self.cfg.gates_checks = [_LANE_GATE]
        self._brief("S1")
        self.cfg.lanes = 1
        flow.flow_ids(self.cfg, ["S1"], do_publish=False, do_act=False, today="2026-06-04")
        val = (self.cfg.bundle("S1") / "lane.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(val, "none")


class DeclaredOrdering(unittest.TestCase):
    """Declared inter-bundle ordering (docs 09 / issue #36): a brief may declare
    `Depends on:` (topological gate — a dependent isn't driven until its prereq is
    COMPLETE) and `Conflicts with:` (never co-scheduled in one concurrent wave). With
    no fields declared, dispatch is exactly today's sort-by-name pool."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str, *, depends_on: str = "", conflicts_with: str = "",
               depends_on_merged: str = "", stacks_on: str = "") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        body = _TOY_BRIEF.format(slug=iid.lower())
        if depends_on:
            body += f"- **Depends on:** {depends_on}\n"
        if depends_on_merged:
            body += f"- **Depends on (merged):** {depends_on_merged}\n"
        if stacks_on:
            body += f"- **Stacks on:** {stacks_on}\n"
        if conflicts_with:
            body += f"- **Conflicts with:** {conflicts_with}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_dependent_not_driven_until_prereq_complete(self) -> None:
        # AA depends on ZZ. Sort-by-name would build AA first; the gate must hold AA
        # until ZZ is COMPLETE (a later pass), proving ordering is by deps, not name.
        self._brief("AA", depends_on="ZZ")
        self._brief("ZZ")
        seen = {}
        real = driver.advance  # the batch band advances one beat at a time now (#104)

        def spy(d: Path, cfg: Config):
            if d.name == "issue_AA" and "zz_state" not in seen:
                seen["zz_state"] = state.state(cfg.bundle("ZZ"))
            return real(d, cfg)

        driver.advance = spy
        try:
            results = flow.flow_ids(self.cfg, ["AA", "ZZ"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            driver.advance = real
        self.assertEqual(seen.get("zz_state"), state.COMPLETE)  # ZZ done before AA built
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_merge_gated_dependent_completes_after_prereq_in_one_run(self) -> None:
        # `Depends on (merged)` (#107) is SUBSUMED by the wave model: MB lands in the wave
        # after MA, so MA reaches COMPLETE first and MB completes in the SAME run — no human
        # merge between runs. The recorded beat order shows MA's first beat before MB's.
        self._brief("MA")
        self._brief("MB", depends_on_merged="MA")
        order: list[str] = []
        real = driver.advance

        def spy(d: Path, cfg: Config):
            if d.name not in order:
                order.append(d.name)
            return real(d, cfg)

        driver.advance = spy
        try:
            results = flow.flow_ids(self.cfg, ["MA", "MB"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            driver.advance = real
        self.assertEqual(results.get("MA"), state.COMPLETE)
        self.assertEqual(results.get("MB"), state.COMPLETE)              # one run now
        self.assertLess(order.index("issue_MA"), order.index("issue_MB"))  # MA's wave first

    def test_dependent_on_already_complete_prereq_runs_alone(self) -> None:
        # A dependent whose prereq is already COMPLETE on disk (from an earlier run) is
        # driven on its own: the wave leveler accepts an out-of-batch COMPLETE prereq, and
        # _runnable lets the dependent build on the base it's already merged into.
        self._brief("PA")
        flow.flow_ids(self.cfg, ["PA"], do_publish=False, do_act=False, today="2026-06-04")
        self.assertEqual(state.state(self.cfg.bundle("PA")), state.COMPLETE)
        self._brief("PB", depends_on="PA")
        results = flow.flow_ids(self.cfg, ["PB"], do_publish=False, do_act=False,
                                today="2026-06-04")
        self.assertEqual(results.get("PB"), state.COMPLETE)

    def test_stacked_chain_completes_in_one_run(self) -> None:
        # #123: a `Stacks on:` dependent is held until its parent is COMPLETE-with-a-
        # published-branch, then builds + completes in the SAME run (one invocation, not N
        # interleaved with merges). The parent's first beat precedes the dependent's.
        # (All-caps ids: _id_list treats a lowercase-with-no-digit token as prose, #103.)
        self._brief("SPARENT")
        self._brief("SDEP", stacks_on="SPARENT")
        order: list[str] = []
        real_advance = driver.advance

        def spy(d: Path, cfg: Config):
            if d.name not in order:
                order.append(d.name)
            return real_advance(d, cfg)

        def fake_publish(cfg, issue_id, **kw):  # stub publisher writes no publish.json
            (cfg.bundle(issue_id) / "publish.json").write_text(
                f'{{"branch": "fix/{issue_id}-x"}}', encoding="utf-8")
            return 0

        driver.advance = spy
        orig_pub = flow.publish.publish
        flow.publish.publish = fake_publish
        try:
            results = flow.flow_ids(self.cfg, ["SPARENT", "SDEP"], do_act=False,
                                    today="2026-06-04")
        finally:
            driver.advance = real_advance
            flow.publish.publish = orig_pub
        self.assertEqual(results.get("SPARENT"), state.COMPLETE)
        self.assertEqual(results.get("SDEP"), state.COMPLETE)   # chain done in one run
        self.assertLess(order.index("issue_SPARENT"), order.index("issue_SDEP"))  # held until published

    def test_no_deps_keeps_sort_by_name_dispatch(self) -> None:
        # No Depends-on fields → the serial build order is exactly sort-by-name, byte
        # for byte today's behaviour.
        ids = ["N3", "N1", "N2"]
        for iid in ids:
            self._brief(iid)
        order: list[str] = []
        real = driver.advance

        def spy(d: Path, cfg: Config):
            if d.name not in order:
                order.append(d.name)
            return real(d, cfg)

        driver.advance = spy
        try:
            flow.flow_ids(self.cfg, ids, do_publish=False, do_act=False,
                          today="2026-06-04")
        finally:
            driver.advance = real
        # First-touch order is sort-by-name: the first beat-round advances N1, N2, N3.
        self.assertEqual(order, ["issue_N1", "issue_N2", "issue_N3"])

    def test_beats_are_synchronised_across_the_wave(self) -> None:
        # #104: the wave advances one beat at a time — all Dos, then all Checks, then all
        # SUMMARY assembles — not each bundle end-to-end. Record each beat's FROM-state and
        # assert the last Do precedes the first Check precedes the first assemble.
        ids = ["S1", "S2", "S3"]
        for iid in ids:
            self._brief(iid)
        beats: list[str] = []
        real = driver.advance

        def spy(d: Path, cfg: Config):
            beats.append(state.state(d))  # the state this beat acts ON
            return real(d, cfg)

        driver.advance = spy
        try:
            flow.flow_ids(self.cfg, ids, do_publish=False, do_act=False, today="2026-06-04")
        finally:
            driver.advance = real
        do = [i for i, s in enumerate(beats) if s == state.PLANNED]      # Do beat
        check = [i for i, s in enumerate(beats) if s == state.BUILT]     # Check (gates+review)
        assemble = [i for i, s in enumerate(beats) if s == state.CHECKED]  # SUMMARY assemble
        self.assertTrue(do and check and assemble)
        self.assertLess(max(do), min(check))         # every Do before any Check
        self.assertLess(max(check), min(assemble))   # every Check before any assemble

    def test_conflict_pair_never_co_scheduled(self) -> None:
        # C conflicts with D; E/F are free. Under a 2-lane pool, C and D must never be
        # in flight together, while the free bundles still prove the pool parallelises.
        import threading
        import time

        self._brief("C", conflicts_with="D")
        self._brief("D")
        self._brief("E")
        self._brief("F")
        self.cfg.lanes = 2

        active: set[str] = set()
        together: set[tuple[str, str]] = set()
        max_conc = [0]
        lk = threading.Lock()
        real = driver.advance  # one beat is the concurrent unit now (#104)

        def spy(d: Path, cfg: Config):
            with lk:
                active.add(d.name)
                max_conc[0] = max(max_conc[0], len(active))
                for a in active:
                    for b in active:
                        if a < b:
                            together.add((a, b))
            time.sleep(0.05)
            try:
                return real(d, cfg)
            finally:
                with lk:
                    active.discard(d.name)

        driver.advance = spy
        try:
            results = flow.flow_ids(self.cfg, ["C", "D", "E", "F"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            driver.advance = real
        self.assertNotIn(("issue_C", "issue_D"), together)  # conflict respected, every beat
        self.assertEqual(max_conc[0], 2)                     # pool genuinely concurrent
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_dependency_cycle_is_rejected_before_build(self) -> None:
        # A↔B mutual dependency is unschedulable: reject up front, before any build.
        self._brief("CYA", depends_on="CYB")
        self._brief("CYB", depends_on="CYA")
        real = driver.run_issue
        built = {"n": 0}
        driver.run_issue = lambda d, cfg: (built.__setitem__("n", built["n"] + 1)
                                           or real(d, cfg))
        try:
            with self.assertRaises(ValueError):
                flow.flow_ids(self.cfg, ["CYA", "CYB"], do_publish=False,
                              do_act=False, today="2026-06-04")
        finally:
            driver.run_issue = real
        self.assertEqual(built["n"], 0)  # rejected before touching any bundle

    def test_unresolved_dependency_is_rejected(self) -> None:
        # A dep that is neither in the wave nor an existing COMPLETE bundle is a
        # misconfigured brief — a hard error.
        self._brief("DEP1", depends_on="GHOST")
        with self.assertRaises(ValueError):
            flow.flow_ids(self.cfg, ["DEP1"], do_publish=False, do_act=False,
                          today="2026-06-04")


class WaveModel(unittest.TestCase):
    """The wave-based batch driver (#wave-model): a dependent batch runs as an ordered
    sequence of waves, each wave's accepted work folded onto a run-scoped integration
    branch the next builds on; a discontinued prerequisite drops its dependents; an
    undeclared same-wave file overlap is flagged; the final / single wave folds nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str, *, depends_on: str = "") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        body = _TOY_BRIEF.format(slug=iid.lower())
        if depends_on:
            body += f"- **Depends on:** {depends_on}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_integration_fold_runs_once_between_two_waves(self) -> None:
        # WB depends on WA → wave 0 = [WA], wave 1 = [WB]. After wave 0 completes its
        # accepted work folds onto the integration branch (dry-run, stub publisher); the
        # final wave folds nothing. The fold's cumulative `accepted` is exactly [WA].
        self._brief("WA")
        self._brief("WB", depends_on="WA")
        calls: list[list[str]] = []
        real = flow.integrate.fold

        def spy(cfg: Config, accepted: list, *, dry_run: bool = False, locks=None):
            calls.append([d.name for d in accepted])
            return real(cfg, accepted, dry_run=dry_run, locks=locks)

        flow.integrate.fold = spy
        try:
            results = flow.flow_ids(self.cfg, ["WA", "WB"], do_act=False, today="2026-06-04")
        finally:
            flow.integrate.fold = real
        self.assertEqual(results.get("WA"), state.COMPLETE)
        self.assertEqual(results.get("WB"), state.COMPLETE)   # completes in one run
        self.assertEqual(calls, [["issue_WA"]])               # folded once, after wave 0

    def test_single_wave_folds_nothing(self) -> None:
        # No deps → one wave → the last wave, which never folds (STOP discipline holds).
        self._brief("SOLO")
        calls: list[int] = []
        real = flow.integrate.fold
        flow.integrate.fold = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            flow.flow_ids(self.cfg, ["SOLO"], do_act=False, today="2026-06-04")
        finally:
            flow.integrate.fold = real
        self.assertEqual(calls, [])

    def test_no_publish_does_not_fold(self) -> None:
        # --no-publish sequences nothing: every wave drives to COMPLETE but no fold runs.
        self._brief("NA")
        self._brief("NB", depends_on="NA")
        calls: list[int] = []
        real = flow.integrate.fold
        flow.integrate.fold = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            results = flow.flow_ids(self.cfg, ["NA", "NB"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            flow.integrate.fold = real
        self.assertEqual(calls, [])
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_discontinued_prereq_skips_dependent(self) -> None:
        # DA is discontinued in wave 0; DB (depends on DA) can't build on a base missing
        # DA's change, so wave 1 skips DB — it never reaches COMPLETE.
        self._brief("DA")
        self._brief("DB", depends_on="DA")

        def signoff(cfg: Config, chunk: list) -> None:
            for d in chunk:
                tok = "discontinue\nout of scope\n" if d.name == "issue_DA" else "accept\n"
                (d / leaves.SIGNOFF_DECISION).write_text(tok, encoding="utf-8")

        orig = leaves.run_signoff_batch
        leaves.run_signoff_batch = signoff
        try:
            results = flow.flow_ids(self.cfg, ["DA", "DB"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            leaves.run_signoff_batch = orig
        self.assertEqual(results.get("DA"), state.DISCONTINUED)
        self.assertNotEqual(results.get("DB"), state.COMPLETE)   # skipped, never built

    def test_overlap_audit_flags_shared_file(self) -> None:
        # Two same-wave bundles whose patches touch a shared file but declare no conflict —
        # an advisory warning (a Conflicts with the planner missed), never a stop.
        a = self.cfg.bundle("OA")
        a.mkdir(parents=True)
        (a / "patch.diff").write_text(
            "diff --git a/shared.py b/shared.py\n@@ -1 +1 @@\n-x\n+y\n", encoding="utf-8")
        b = self.cfg.bundle("OB")
        b.mkdir(parents=True)
        (b / "patch.diff").write_text(
            "diff --git a/shared.py b/shared.py\n@@ -5 +5 @@\n-p\n+q\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            flow._audit_wave_overlap([a, b])
        self.assertIn("shared.py", err.getvalue())
        self.assertIn("undeclared conflict", err.getvalue())

    def test_overlap_audit_silent_on_disjoint(self) -> None:
        a = self.cfg.bundle("PA")
        a.mkdir(parents=True)
        (a / "patch.diff").write_text("diff --git a/one.py b/one.py\n", encoding="utf-8")
        b = self.cfg.bundle("PB")
        b.mkdir(parents=True)
        (b / "patch.diff").write_text("diff --git a/two.py b/two.py\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            flow._audit_wave_overlap([a, b])
        self.assertEqual(err.getvalue(), "")

    def test_merge_mode_routes_to_merge_wave_not_fold(self) -> None:
        # wave_mode="merge" (opt-in): the driver gh-merges each non-final wave's PRs
        # instead of folding onto an integration branch. merge_wave is stubbed to 0 (no
        # real gh); the next wave then builds on the base (which a real merge would advance).
        self.cfg.wave_mode = "merge"
        self._brief("GA")
        self._brief("GB", depends_on="GA")
        merge_calls: list[list[str]] = []
        fold_calls: list[int] = []
        real_merge, real_fold = flow.merge.merge_wave, flow.integrate.fold
        flow.merge.merge_wave = lambda cfg, bundles, **k: (
            merge_calls.append([d.name for d in bundles]), 0)[1]
        flow.integrate.fold = lambda *a, **k: (fold_calls.append(1), (None, None))[1]
        try:
            results = flow.flow_ids(self.cfg, ["GA", "GB"], do_act=False, today="2026-06-04")
        finally:
            flow.merge.merge_wave, flow.integrate.fold = real_merge, real_fold
        self.assertEqual(results.get("GA"), state.COMPLETE)
        self.assertEqual(results.get("GB"), state.COMPLETE)
        self.assertEqual(merge_calls, [["issue_GA"]])   # merged after wave 0 only
        self.assertEqual(fold_calls, [])                # fold not used in merge mode

    def test_stack_base_file_round_trips(self) -> None:
        # The flow records the integration branch for a wave>0 bundle; worktree + publish
        # read it via publish._stack_base_branch (the generalised stack base).
        d = self._brief("SBX")
        self.assertIsNone(flow.publish._stack_base_branch(self.cfg, d))
        flow.publish.write_stack_base(d, "pdca-integration/main")
        self.assertEqual(flow.publish._stack_base_branch(self.cfg, d), "pdca-integration/main")

    def test_waves_command_prints_plan(self) -> None:
        # `pdca waves` prints the computed wave plan without building (B3 observability).
        self._brief("WX")
        self._brief("WY", depends_on="WX")
        with redirect_stdout(io.StringIO()) as out:
            rc = cli._waves(self.cfg, ["WX", "WY"])
        self.assertEqual(rc, 0)
        self.assertIn("wave 0: WX", out.getvalue())
        self.assertIn("wave 1: WY", out.getvalue())

    def test_waves_command_reports_unschedulable(self) -> None:
        self._brief("CZ1", depends_on="CZ2")
        self._brief("CZ2", depends_on="CZ1")
        with redirect_stderr(io.StringIO()) as err:
            rc = cli._waves(self.cfg, ["CZ1", "CZ2"])
        self.assertEqual(rc, 1)
        self.assertIn("unschedulable", err.getvalue())

    def test_publish_flag_marks_stacked_pr(self) -> None:
        # A stacked PR's status flag shows ↑<integration-branch> so the human merges the
        # stack bottom-up.
        d = self._brief("SF")
        (d / "publish.json").write_text(
            '{"pr_url": "https://gh/pr/9", "base": "pdca-integration/main", '
            '"mode": "stacked-pr"}', encoding="utf-8")
        flag = cli._publish_flag(d)
        self.assertIn("https://gh/pr/9", flag)
        self.assertIn("↑pdca-integration/main", flag)


class ProgName(unittest.TestCase):
    """The CLI's --help command name follows the per-instance console-script name
    (issue #73): resolved from argv[0], with a fallback for module invocation."""

    def test_prog_name_resolution(self) -> None:
        import sys
        orig = sys.argv
        try:
            sys.argv = ["/usr/local/bin/pdca-gramps", "status"]
            self.assertEqual(cli._prog_name(), "pdca-gramps")  # renamed console script
            sys.argv = ["pdca"]
            self.assertEqual(cli._prog_name(), "pdca")          # default console script
            sys.argv = ["/path/to/src/pdca_harness/cli.py"]
            self.assertEqual(cli._prog_name(), "pdca")          # python -m … → file path
            sys.argv = []
            self.assertEqual(cli._prog_name(), "pdca")          # defensive fallback
        finally:
            sys.argv = orig


class PublishOnAccept(unittest.TestCase):
    """Accept → publish by default + publish visibility (issue #97)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accepted_ready(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        leaves.do_plan(d, self.cfg)
        driver.run_issue(d, self.cfg)  # → AWAITING_SIGNOFF (§6 open from the stub reviewer)
        summ = d / "SUMMARY.md"
        summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")  # clear §6
        return d

    def _accept_args(self, iid: str, no_publish: bool = False) -> SimpleNamespace:
        return SimpleNamespace(issue_id=iid, accept=True, iterate_do=False,
                               iterate_plan=False, discontinue=False, by="", delta="",
                               no_publish=no_publish)

    def test_accept_publishes_by_default(self) -> None:
        from pdca_harness import publish
        calls, orig = [], publish.publish
        publish.publish = lambda cfg, iid, **kw: calls.append(iid) or 0
        try:
            self._accepted_ready("ACC")
            self.assertEqual(cli._signoff(self.cfg, self._accept_args("ACC")), 0)
        finally:
            publish.publish = orig
        self.assertEqual(calls, ["ACC"])  # standalone accept publishes (#97)

    def test_no_publish_opts_out(self) -> None:
        from pdca_harness import publish
        calls, orig = [], publish.publish
        publish.publish = lambda cfg, iid, **kw: calls.append(iid) or 0
        try:
            self._accepted_ready("NOP")
            cli._signoff(self.cfg, self._accept_args("NOP", no_publish=True))
        finally:
            publish.publish = orig
        self.assertEqual(calls, [])  # --no-publish ⇒ deliberately unpublished

    def test_accept_publish_failure_is_loud(self) -> None:
        import io
        from contextlib import redirect_stderr
        from pdca_harness import publish
        orig = publish.publish
        publish.publish = lambda cfg, iid, **kw: 1  # publish fails
        try:
            self._accepted_ready("FAILP")
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = cli._signoff(self.cfg, self._accept_args("FAILP"))
        finally:
            publish.publish = orig
        self.assertEqual(rc, 1)                       # failure surfaced as the return
        self.assertIn("NOT", buf.getvalue())          # and printed loudly

    def test_status_publish_flag(self) -> None:
        d = self.cfg.bundle("ST")
        d.mkdir(parents=True)
        (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        self.assertEqual(cli._publish_flag(d), "  [unpublished]")  # no publish.json
        (d / "publish.json").write_text('{"pr_url": "https://x/pr/1"}', encoding="utf-8")
        self.assertEqual(cli._publish_flag(d), "  [PR https://x/pr/1]")
        d2 = self.cfg.bundle("ST2")
        d2.mkdir(parents=True)
        (d2 / "patch.diff").write_text("", encoding="utf-8")  # close/no-fix → no PR expected
        self.assertEqual(cli._publish_flag(d2), "  [close: no PR]")


class InitIssueAndPlaceholderGuard(unittest.TestCase):
    """init-issue is the pre-authored-brief seeder; its no-brief blank-template path is
    dropped, and a still-unfilled template brief reads UNPLANNED so the Plan beat re-plans
    it rather than being silently skipped (issue #113)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_issue_without_from_brief_errors_and_scaffolds_nothing(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli._init_issue(self.cfg, "NEW", None)
        self.assertEqual(rc, 2)
        self.assertFalse(self.cfg.bundle("NEW").exists())   # no content-less PLANNED trap
        self.assertIn("flow NEW", buf.getvalue())           # points to the auto-plan path

    def test_init_issue_with_from_brief_seeds_planned_bundle(self) -> None:
        src = self.tmp / "authored.md"
        src.write_text("- **Slug:** real-fix\n", encoding="utf-8")
        rc = cli._init_issue(self.cfg, "SEED", src)
        self.assertEqual(rc, 0)
        d = self.cfg.bundle("SEED")
        self.assertEqual(state.state(d), state.PLANNED)
        self.assertEqual((d / "brief.md").read_text(encoding="utf-8"), "- **Slug:** real-fix\n")

    def test_unfilled_template_brief_reads_unplanned(self) -> None:
        # The #113 footgun shape: a brief.md that's the raw template (placeholder slug).
        d = self.cfg.bundle("TPL")
        d.mkdir(parents=True)
        shutil.copyfile(BRIEF_TPL, d / "brief.md")
        self.assertTrue(brief.is_placeholder(d / "brief.md"))
        self.assertEqual(state.state(d), state.UNPLANNED)   # planner not skipped

    def test_authored_brief_reads_planned(self) -> None:
        d = self.cfg.bundle("REAL")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** a-real-slug\n", encoding="utf-8")
        self.assertFalse(brief.is_placeholder(d / "brief.md"))
        self.assertEqual(state.state(d), state.PLANNED)


class ActCadence(unittest.TestCase):
    """flow auto-runs Act only when act_cadence cycles have frozen since the last Act —
    counted across flow invocations, not per-run (issue #109)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.cfg.act_cadence = 3

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self) -> Path:
        return self.cfg.process_dir / "act-log.md"

    def test_act_held_below_cadence_then_fires_across_flows(self) -> None:
        # Three separate single-bundle flows: Act must NOT run until the 3rd freezes the
        # third cycle (cadence 3), proving the gate persists across invocations.
        for i in (1, 2):
            flow.flow(self.cfg, f"AC{i}", do_act=True, today="2026-06-04")
            self.assertFalse(self._log().exists(), f"Act ran too early (after flow {i})")
        flow.flow(self.cfg, "AC3", do_act=True, today="2026-06-04")
        self.assertTrue(self._log().exists())          # the 3rd frozen cycle trips cadence

    def test_act_resets_after_running(self) -> None:
        for i in (1, 2, 3):
            flow.flow(self.cfg, f"R{i}", do_act=True, today="2026-06-04")
        self.assertTrue(self._log().exists())
        self.assertEqual(act.cycles_since_review(self.cfg), 0)   # marker reset to frozen count
        before = self._log().read_text(encoding="utf-8")
        flow.flow(self.cfg, "R4", do_act=True, today="2026-06-04")   # only 1 since → below cadence
        self.assertEqual(self._log().read_text(encoding="utf-8"), before)  # no new Act entry


class RunnableMergeGate(unittest.TestCase):
    """`_runnable` keeps the #107 merge-gate for an *out-of-batch* `Depends on (merged)`
    prereq (#186): the wave fold carries only in-batch prereqs into the next base, so an
    out-of-batch one must be genuinely MERGED — COMPLETE-but-PR-open is not enough."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str, body_extra: str = "") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** org/repo @ main\n"
            + body_extra, encoding="utf-8")
        return d

    def _complete(self, iid: str) -> Path:
        d = self._brief(iid)
        (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-06-05")
        self.assertEqual(state.state(d), state.COMPLETE)
        return d

    def test_out_of_batch_depends_on_merged_waits_until_merged(self) -> None:
        # B `Depends on (merged): X`, X from a PRIOR run (not in this batch). Nothing here
        # carries X's diff into the base, so X must be MERGED, not merely COMPLETE (#186).
        b = self._brief("B", "- **Depends on (merged):** X\n")
        with mock.patch("pdca_harness.flow.merged.is_merged", return_value=False) as m:
            self.assertEqual(flow._runnable(self.cfg, [b], {b.name}), [])   # X's PR open → defer
        m.assert_called_once_with(self.cfg, "X")
        with mock.patch("pdca_harness.flow.merged.is_merged", return_value=True):
            self.assertEqual(flow._runnable(self.cfg, [b], {b.name}), [b])  # X merged → runnable

    def test_in_batch_depends_on_merged_rides_the_fold_without_gh(self) -> None:
        # An IN-BATCH `Depends on (merged)` prereq is carried by the fold once COMPLETE — no
        # `gh`/merge check (the regression #186 must not over-correct an in-batch dep into).
        p = self._complete("P")
        b = self._brief("B", "- **Depends on (merged):** P\n")
        with mock.patch("pdca_harness.flow.merged.is_merged") as m:
            runnable = flow._runnable(self.cfg, [b], {b.name, p.name})
        m.assert_not_called()
        self.assertEqual(runnable, [b])

    def test_out_of_batch_plain_depends_on_keeps_complete_bar(self) -> None:
        # A plain out-of-batch `Depends on` keeps the COMPLETE-on-disk bar (#171) — no merge
        # check; only the `(merged)` variant carries the stricter gate.
        self._complete("PRIOR")
        b = self._brief("B", "- **Depends on:** PRIOR\n")
        with mock.patch("pdca_harness.flow.merged.is_merged") as m:
            self.assertEqual(flow._runnable(self.cfg, [b], {b.name}), [b])
        m.assert_not_called()


class PlanBatchUnseededWarning(unittest.TestCase):
    """`do_plan_batch` surfaces a VISIBLE warning (#190) when the CSV/default planner briefs an
    id mid-session without seeded tracker notes — it never lets a CSV-row-only brief flow on
    silently. (The seeders are never auto-run unattended; the human seeds + refines.)"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.cfg.planner = LeafConfig(mode="command", argv=["true"], interactive=True)
        self.cfg.notes_cmd = "fetch-notes"            # a Plan source IS configured
        self.cfg.bundle_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _planner_writes(self, *, notes: bool):
        def _fake(*_a, **_k):                          # simulate the planner choosing an id
            d = self.cfg.bundle("CHOSEN")
            d.mkdir(parents=True, exist_ok=True)
            (d / "brief.md").write_text("- **Slug:** chosen\n", encoding="utf-8")
            if notes:
                (d / "notes.json").write_text("{}", encoding="utf-8")
        return _fake

    def _run_csv_plan(self) -> str:
        err = io.StringIO()
        with redirect_stderr(err):
            leaves.do_plan_batch(self.cfg, csv="export.csv")   # ids=None → the CSV path
        return err.getvalue()

    def test_warns_when_csv_planner_briefs_without_notes(self) -> None:
        with mock.patch("pdca_harness.leaves._invoke",
                        side_effect=self._planner_writes(notes=False)):
            msg = self._run_csv_plan()
        self.assertIn("WITHOUT seeded tracker notes", msg)
        self.assertIn("CHOSEN", msg)

    def test_warns_for_a_brief_added_to_a_preexisting_unplanned_dir(self) -> None:
        # An `issue_<id>` dir can already exist UNPLANNED (no brief); the planner adds brief.md
        # to it mid-session. That's still a NEW brief without notes — must warn (we snapshot
        # which dirs HAD a brief, not just dir names; Codex review on #198).
        self.cfg.bundle("CHOSEN").mkdir(parents=True, exist_ok=True)   # pre-existing empty dir
        with mock.patch("pdca_harness.leaves._invoke",
                        side_effect=self._planner_writes(notes=False)):
            msg = self._run_csv_plan()
        self.assertIn("WITHOUT seeded tracker notes", msg)
        self.assertIn("CHOSEN", msg)

    def test_no_warning_when_the_brief_carries_notes(self) -> None:
        with mock.patch("pdca_harness.leaves._invoke",
                        side_effect=self._planner_writes(notes=True)):
            self.assertNotIn("WITHOUT seeded tracker notes", self._run_csv_plan())

    def test_no_warning_when_no_plan_source_configured(self) -> None:
        self.cfg.notes_cmd = ""                        # no notes_cmd, no plan.sources
        with mock.patch("pdca_harness.leaves._invoke",
                        side_effect=self._planner_writes(notes=False)):
            self.assertNotIn("WITHOUT seeded tracker notes", self._run_csv_plan())


class MaxPassesBudget(unittest.TestCase):
    """#260: the pass budget is configurable, and exhausting it is never silent.

    A bundle signed off `iterate-do` on the LAST allowed pass records the decision with
    `apply_now=False`, deferring its rebuild to "the next pass's build-all" — which never
    comes. It was then left ITERATE_DO while the driver fell out of the loop, published the
    accepted siblings, and reported as if the run had finished cleanly.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _batch1_always_iterates(cfg: Config, bundles: list[Path]) -> None:
        """BATCH1 never accepts (the hard bundle); BATCH2 accepts on its first sign-off."""
        for d in bundles:
            if d.name == "issue_BATCH1":
                (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
                continue
            summ = d / "SUMMARY.md"
            summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
            (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

    def test_cap_exhausted_warns_leaves_iterating_and_still_publishes_sibling(self) -> None:
        orig = leaves.run_signoff_batch
        leaves.run_signoff_batch = self._batch1_always_iterates
        buf = io.StringIO()
        try:
            with redirect_stderr(buf), redirect_stdout(io.StringIO()):
                results = flow.flow_batch(self.cfg, today="2026-06-04", max_passes=1)
        finally:
            leaves.run_signoff_batch = orig
        err = buf.getvalue()

        # the stuck bundle is left iterating — and NAMED, with a resume hint
        self.assertEqual(results["BATCH1"], state.ITERATE_DO)
        self.assertIn("pass budget exhausted after 1 pass(es)", err)
        self.assertIn("issue_BATCH1", err)
        self.assertIn("`pdca flow BATCH1`", err)
        self.assertIn("[driver].max_passes", err)          # how to raise it

        # …while the accepted sibling still publishes (accept/publish routing unchanged)
        self.assertEqual(results["BATCH2"], state.COMPLETE)
        self.assertTrue((self.cfg.bundle("BATCH2") / "commit-msg.txt").exists())
        self.assertFalse((self.cfg.bundle("BATCH1") / "commit-msg.txt").exists())
        # a terminal bundle is never listed as abandoned (stderr also carries beat heartbeats,
        # so match the abandoned-list line shape, not the bare bundle name)
        self.assertNotIn("flow:   issue_BATCH2", err)

    def test_higher_budget_lets_the_hard_bundle_keep_iterating(self) -> None:
        # The same bundle with room to breathe: it keeps iterating instead of being dropped
        # after one pass. Pass 1 records iterate-do (deferred); passes 2 and 3 each rebuild,
        # archiving the prior attempt — so two iteration archives, not zero.
        orig = leaves.run_signoff_batch
        leaves.run_signoff_batch = self._batch1_always_iterates
        buf = io.StringIO()
        try:
            with redirect_stderr(buf), redirect_stdout(io.StringIO()):
                flow.flow_batch(self.cfg, today="2026-06-04", max_passes=3)
        finally:
            leaves.run_signoff_batch = orig
        d = self.cfg.bundle("BATCH1")
        self.assertTrue((d / "iteration-v1").is_dir())
        self.assertTrue((d / "iteration-v2").is_dir())
        self.assertIn("pass budget exhausted after 3 pass(es)", buf.getvalue())

    def test_last_pass_iterate_plan_is_warned_not_silently_reopened(self) -> None:
        """PR-review catch (codex, #267). `iterate-plan` is applied even under
        `apply_now=False` — it only archives → UNPLANNED, no rebuild — so on the LAST allowed
        pass the cap fall-through finds the bundle UNPLANNED. Warning only on
        ITERATE_*/AWAITING_SIGNOFF missed it entirely, and `flow_batch`'s resume set EXCLUDES
        UNPLANNED, so the bundle would vanish from the next unattended sweep too: exactly the
        silent abandonment #260 exists to kill."""
        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            for d in bundles:
                (d / leaves.SIGNOFF_DECISION).write_text("iterate-plan\nrespec\n", encoding="utf-8")

        orig = leaves.run_signoff_batch
        leaves.run_signoff_batch = signoff_batch
        buf = io.StringIO()
        try:
            with redirect_stderr(buf), redirect_stdout(io.StringIO()):
                results = flow.flow_batch(self.cfg, today="2026-06-04", max_passes=1)
        finally:
            leaves.run_signoff_batch = orig
        err = buf.getvalue()

        self.assertEqual(results["BATCH1"], state.UNPLANNED)          # re-opened, mid-flight
        self.assertTrue((self.cfg.bundle("BATCH1") / "iteration-v1").is_dir())  # work archived
        self.assertIn("pass budget exhausted after 1 pass(es)", err)
        self.assertIn("issue_BATCH1 [UNPLANNED]", err)
        self.assertIn("`pdca flow BATCH1`", err)   # the hint re-plans it (single-issue auto-plans)

    def test_no_progress_exit_also_warns(self) -> None:
        # The other silent return: a pass that advances nothing while a bundle still
        # iterates. Freeze `_build_all` so the ITERATE_DO bundle cannot progress.
        d = self.cfg.bundle("STUCK")
        self.assertTrue(flow._plan_if_unplanned(self.cfg, d, None))
        driver.run_issue(d, self.cfg)
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="t", date="2026-06-04")
        self.assertEqual(state.state(d), state.ITERATE_DO)

        buf = io.StringIO()
        with mock.patch.object(flow, "_build_all", lambda cfg, bundles: None):
            with redirect_stderr(buf):
                flow._drive_wave(self.cfg, [d], by="t", today="2026-06-04", max_passes=5)
        err = buf.getvalue()
        self.assertIn("a full pass made no progress", err)
        self.assertIn("issue_STUCK", err)
        self.assertIn("`pdca flow STUCK`", err)

    def test_terminal_wave_exits_quietly(self) -> None:
        # The all-terminal return must stay silent — no false "abandoned" noise.
        buf = io.StringIO()
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            results = flow.flow_batch(self.cfg, today="2026-06-04")
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))
        self.assertNotIn("un-terminal", buf.getvalue())


class MaxPassesConfig(unittest.TestCase):
    """#260: the cap was a hardcoded function default — plumb it like `lanes`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, extra: str = "") -> Config:
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\n[leaves.reviewer]\nmode = "stub"\n' + extra,
            encoding="utf-8")
        return Config.load(self.tmp)

    def test_default_is_twenty(self) -> None:
        # Raised from the old hardcoded 10: the reported real run needed 11 iterations.
        self.assertEqual(self._load().max_passes, 20)

    def test_driver_table_sets_it(self) -> None:
        self.assertEqual(self._load("[driver]\nmax_passes = 40\n").max_passes, 40)

    def test_env_overrides_the_toml(self) -> None:
        with mock.patch.dict(os.environ, {"PDCA_MAX_PASSES": "7"}):
            self.assertEqual(self._load("[driver]\nmax_passes = 40\n").max_passes, 7)

    def test_floor_of_one(self) -> None:
        self.assertEqual(self._load("[driver]\nmax_passes = 0\n").max_passes, 1)

    def _run_cli(self, cfg: Config, *extra: str) -> mock.Mock:
        """Real parser → dispatch → `_flow`; `main(argv)` skips the inhibitor re-exec.
        `flow.flow_ids` — the ONE drive path both CLI shapes route through (issue #468) —
        is stubbed, so only the config plumbing is under test."""
        with mock.patch.object(cli.Config, "load", return_value=cfg), \
             mock.patch.object(cli.flow, "flow_ids",
                               return_value={"ID1": state.COMPLETE}) as driven, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.main(["flow", "ID1", "--no-publish", "--no-act", *extra])
        return driven

    def test_cli_flag_overrides_config(self) -> None:
        cfg = _stub_config(self.tmp)
        cfg.max_passes = 40
        driven = self._run_cli(cfg, "--max-passes", "3")
        self.assertTrue(driven.called)
        self.assertEqual(cfg.max_passes, 3)      # flag beat [driver].max_passes

    def test_cli_flag_absent_leaves_config_value(self) -> None:
        cfg = _stub_config(self.tmp)
        cfg.max_passes = 40
        self._run_cli(cfg)
        self.assertEqual(cfg.max_passes, 40)

    def test_drive_wave_defaults_to_the_configured_budget(self) -> None:
        # No explicit max_passes → cfg.max_passes, not a literal.
        cfg = _stub_config(self.tmp)
        cfg.max_passes = 2
        seen = []
        with mock.patch.object(flow, "_build_all",
                               side_effect=lambda c, b: seen.append(1)), \
             redirect_stderr(io.StringIO()):
            d = cfg.bundle("X")
            d.mkdir(parents=True)
            flow._drive_wave(cfg, [d], by="t", today="2026-06-04")
        self.assertEqual(len(seen), 1)  # UNPLANNED + no progress → returns after one pass


if __name__ == "__main__":
    unittest.main()
