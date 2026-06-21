"""Offline slice for the continuous orchestrator, `flow.flow` (stdlib unittest).

Drives a bundle through Plan → Do → Check → sign-off → publish → Act with **stub**
leaves and **stub** gates (no Claude, no TTY, no Docker), proving the deterministic
control flow, the load-bearing C6 guard, and that publish-on-accept dry-runs when the
publisher leaf is stubbed (never pushes offline). Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import brief, cli, driver, flow, leaves, queue, signoff, state
from pdca_harness.config import Config, LeafConfig

DESIGN_TPL = Path(__file__).resolve().parents[1] / "templates" / "design-proposal.md.tpl"
POINTER_TPL = Path(__file__).resolve().parents[1] / "templates" / "plan-pointer.md.tpl"


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
        # only the briefed id is driven.
        leaves.do_plan(self.cfg.bundle("HASBRIEF"), self.cfg)
        self.cfg.bundle("NOBRIEF").mkdir(parents=True)  # exists but UNPLANNED
        results = flow.flow_ids(
            self.cfg, ["HASBRIEF", "NOBRIEF", "GHOST"], today="2026-06-04"
        )
        self.assertEqual(set(results), {"HASBRIEF"})
        self.assertEqual(results["HASBRIEF"], state.COMPLETE)

    def test_batch_isolates_a_failing_bundle(self) -> None:
        # One bundle's build always raises (a leaf left it half-written). The sweep
        # must isolate it and still drive the others to COMPLETE — never crash the
        # batch and lose the rest's progress (testbed issue #3).
        for iid in ("GOOD", "BAD"):
            leaves.do_plan(self.cfg.bundle(iid), self.cfg)

        real_run = driver.run_issue

        def flaky(d: Path, cfg: Config) -> str:
            if d.name == "issue_BAD":
                raise RuntimeError("boom: leaf left the bundle half-written")
            return real_run(d, cfg)

        flow.driver.run_issue = flaky
        try:
            results = flow.flow_ids(self.cfg, ["GOOD", "BAD"], today="2026-06-06")
        finally:
            flow.driver.run_issue = real_run
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
        self.assertEqual(results, {})
        self.assertEqual(state.state(self.cfg.bundle("SKIP")), state.UNPLANNED)

    def test_default_no_prepass_still_skips_unplanned(self) -> None:
        # Without plan_missing, an UNPLANNED id is skipped exactly as before (no Plan beat).
        self.cfg.bundle("U").mkdir(parents=True)
        leaves.do_plan(self.cfg.bundle("B"), self.cfg)
        results = flow.flow_ids(self.cfg, ["U", "B"], today="2026-06-20")
        self.assertEqual(set(results), {"B"})  # U skipped, not briefed

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


class PlanPointerBrief(unittest.TestCase):
    """A pointer-brief (issue #67): the Plan is a reference to the host's own planning
    artifact (ADR / proposal / spec), not a brief authored here. It carries the same
    parsed-field contract, so the driver treats it as a normal PLANNED brief."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("ADR")
        self.d.mkdir(parents=True)
        shutil.copyfile(POINTER_TPL, self.d / "brief.md")  # the plan-pointer template

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

    def _brief(self, iid: str, *, depends_on: str = "", conflicts_with: str = "") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        body = _TOY_BRIEF.format(slug=iid.lower())
        if depends_on:
            body += f"- **Depends on:** {depends_on}\n"
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
        real = driver.run_issue

        def spy(d: Path, cfg: Config):
            if d.name == "issue_AA" and "zz_state" not in seen:
                seen["zz_state"] = state.state(cfg.bundle("ZZ"))
            return real(d, cfg)

        driver.run_issue = spy
        try:
            results = flow.flow_ids(self.cfg, ["AA", "ZZ"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            driver.run_issue = real
        self.assertEqual(seen.get("zz_state"), state.COMPLETE)  # ZZ done before AA built
        self.assertTrue(all(s == state.COMPLETE for s in results.values()))

    def test_no_deps_keeps_sort_by_name_dispatch(self) -> None:
        # No Depends-on fields → the serial build order is exactly sort-by-name, byte
        # for byte today's behaviour.
        ids = ["N3", "N1", "N2"]
        for iid in ids:
            self._brief(iid)
        order: list[str] = []
        real = driver.run_issue

        def spy(d: Path, cfg: Config):
            if d.name not in order:
                order.append(d.name)
            return real(d, cfg)

        driver.run_issue = spy
        try:
            flow.flow_ids(self.cfg, ids, do_publish=False, do_act=False,
                          today="2026-06-04")
        finally:
            driver.run_issue = real
        self.assertEqual(order, ["issue_N1", "issue_N2", "issue_N3"])

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
        real = driver.run_issue

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

        driver.run_issue = spy
        try:
            results = flow.flow_ids(self.cfg, ["C", "D", "E", "F"], do_publish=False,
                                    do_act=False, today="2026-06-04")
        finally:
            driver.run_issue = real
        self.assertNotIn(("issue_C", "issue_D"), together)  # conflict respected
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


if __name__ == "__main__":
    unittest.main()
