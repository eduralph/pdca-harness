"""End-to-end vertical slice for the PDCA driver (stdlib unittest — no deps).

Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
Exercises the full control flow on the toy brief with stub leaves/gates:
init → Do → gates → reviewer → assembled SUMMARY → human sign-off → COMPLETE,
plus the C6 accept-gate, the independence contract, and an iterate transition.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import act, assemble, driver, gates, publish, queue, leaves, signoff, state
from pdca_harness.config import DEFAULT_CLOSE_DISPOSITIONS, Config, LeafConfig

TOY_BRIEF = Path(__file__).resolve().parents[1] / "examples" / "toy" / "brief.md"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


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


class VerticalSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("TOY")
        self.d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_to_awaiting_signoff(self) -> None:
        self.assertEqual(state.state(self.d), state.PLANNED)
        final = driver.run_issue(self.d, self.cfg)
        self.assertEqual(final, state.AWAITING_SIGNOFF)
        for name in ("patch.diff", "build-notes.md", "check-gates.json", "check-review.md", "SUMMARY.md", "test_toy.py"):
            self.assertTrue((self.d / name).exists(), f"missing {name}")

    def test_independence_contract(self) -> None:
        # The reviewer's input list must never contain build-notes.md.
        inputs = leaves.reviewer_input_paths(self.d)
        self.assertNotIn(self.d / "build-notes.md", inputs)

    def test_accept_blocked_until_needs_human_cleared(self) -> None:
        driver.run_issue(self.d, self.cfg)
        summary = self.d / "SUMMARY.md"
        # Stub reviewer flags the always-human validation item → §6 is non-empty.
        self.assertTrue(signoff.open_needs_human(summary))
        # Simulate the human clearing §6 (check the box), then accept.
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertFalse(signoff.open_needs_human(summary))
        signoff.record(summary, action="accept", by="tester", date="2026-01-01")
        self.assertEqual(state.state(self.d), state.COMPLETE)

    def test_iterate_to_do_archives_downstream(self) -> None:
        # iterate-do ARCHIVES the prior attempt into iteration-v1/ (never deletes it):
        # the downstream leaves the top level → state PLANNED, brief.md stays, and the
        # attempt (patch + its bundle-local test) is preserved under iteration-v1/.
        driver.run_issue(self.d, self.cfg)
        signoff.record(self.d / "SUMMARY.md", action="iterate-do", by="tester", date="2026-01-01")
        self.assertEqual(state.state(self.d), state.ITERATE_DO)
        driver.advance(self.d, self.cfg)  # archive + rebuild
        self.assertEqual(state.state(self.d), state.PLANNED)
        self.assertFalse((self.d / "patch.diff").exists())          # left the top level
        self.assertFalse((self.d / "test_toy.py").exists())         # the bundle-local test moved too
        self.assertTrue((self.d / "brief.md").exists())             # brief stays for the rebuild
        self.assertTrue((self.d / "iteration-v1" / "patch.diff").exists())   # preserved, not deleted
        self.assertTrue((self.d / "iteration-v1" / "SUMMARY.md").exists())
        self.assertTrue((self.d / "iteration-v1" / "test_toy.py").exists())  # preserved

    def test_iterate_to_plan_archives_attempt(self) -> None:
        # iterate-plan archives the WHOLE attempt incl. the brief → state UNPLANNED;
        # the brief + downstream are preserved under iteration-v1/, never deleted.
        driver.run_issue(self.d, self.cfg)
        signoff.record(self.d / "SUMMARY.md", action="iterate-plan", by="tester", date="2026-01-01")
        driver.advance(self.d, self.cfg)
        self.assertEqual(state.state(self.d), state.UNPLANNED)
        self.assertFalse((self.d / "brief.md").exists())                     # left the top level
        self.assertTrue((self.d / "iteration-v1" / "brief.md").exists())     # preserved
        self.assertTrue((self.d / "iteration-v1" / "patch.diff").exists())   # attempt preserved

    def test_discontinue_derives_discontinued_and_does_not_transition(self) -> None:
        # discontinue is terminal: §9 records `discontinued`, state derives DISCONTINUED, and
        # run_issue performs NO transition (no archive — the attempt stays in place,
        # the bundle just drops out of the active set).
        driver.run_issue(self.d, self.cfg)
        signoff.record(self.d / "SUMMARY.md", action="discontinue", by="tester", date="2026-01-01")
        self.assertEqual(signoff.outcome_token(self.d / "SUMMARY.md"), "discontinued")
        self.assertEqual(state.state(self.d), state.DISCONTINUED)
        self.assertEqual(driver.run_issue(self.d, self.cfg), state.DISCONTINUED)  # no-op
        self.assertFalse((self.d / "iteration-v1").exists())   # nothing archived
        self.assertTrue((self.d / "patch.diff").exists())      # attempt left untouched

    def test_discontinue_not_guarded_by_open_needs_human(self) -> None:
        # Discontinue is a deliberate abandon, independent of §6 — unlike accept (C6), a
        # bundle with open NEEDS-HUMAN items can still be discontinued at the record layer.
        driver.run_issue(self.d, self.cfg)
        self.assertTrue(signoff.open_needs_human(self.d / "SUMMARY.md"))  # §6 still open
        signoff.record(self.d / "SUMMARY.md", action="discontinue", by="tester", date="2026-01-01")
        self.assertEqual(state.state(self.d), state.DISCONTINUED)

    def test_signoff_decision_accepts_discontinue_token(self) -> None:
        # The `discontinue` token was silently dropped before (#42); leaves now recognises it
        # and reads the rationale written below it.
        (self.d / leaves.SIGNOFF_DECISION).write_text(
            "discontinue\nrestructuring task — handled by hand upstream\n", encoding="utf-8")
        self.assertEqual(leaves.signoff_decision(self.d), "discontinue")
        self.assertEqual(leaves.signoff_rationale(self.d),
                         "restructuring task — handled by hand upstream")


class CloseDispositionFastPath(unittest.TestCase):
    """The close-disposition fast path (issue #60): a bundle whose Plan concluded a
    close / no-fix outcome skips the builder + reviewer leaves and routes straight to
    sign-off, where the human confirms or overrides the close."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _close_bundle(self, issue_id: str, disposition: str = "likely-close") -> Path:
        """A bundle whose toy brief carries a CLOSE disposition hint."""
        d = self.cfg.bundle(issue_id)
        d.mkdir(parents=True)
        text = TOY_BRIEF.read_text(encoding="utf-8").replace(
            "- **Disposition hint:** likely-fix",
            f"- **Disposition hint:** {disposition}")
        (d / "brief.md").write_text(text, encoding="utf-8")
        return d

    def test_fast_path_skips_leaves(self) -> None:
        d = self._close_bundle("CLOSE")
        final = driver.run_issue(d, self.cfg)
        self.assertEqual(final, state.AWAITING_SIGNOFF)
        # The builder leaf never ran — no patch, no shipped test.
        self.assertFalse((d / "patch.diff").exists())
        self.assertFalse((d / "test_toy.py").exists())
        # The close marker (the Do artifact) + the audit breadcrumb are present.
        self.assertTrue((d / state.CLOSE_MARKER).exists())
        self.assertEqual((d / state.CLOSE_MARKER).read_text(encoding="utf-8").strip(),
                         "likely-close")
        self.assertIn("Leaves skipped: disposition=likely-close",
                      (d / "build-notes.md").read_text(encoding="utf-8"))
        # The reviewer leaf was skipped (note, not a verdict table).
        self.assertIn("SKIPPED (close disposition)",
                      (d / "check-review.md").read_text(encoding="utf-8"))
        # Gates are N/A → overall pass, no gate command ran.
        gates_json = json.loads((d / "check-gates.json").read_text(encoding="utf-8"))
        self.assertEqual(gates_json["overall"], "pass")
        # The human must consciously confirm the close: §6 has a NEEDS-HUMAN → C6 blocks accept.
        self.assertTrue(signoff.open_needs_human(d / "SUMMARY.md"))

    def test_manual_verification_seeds_stub(self) -> None:
        self.cfg.templates_dir = TEMPLATES_DIR  # the real MANUAL-VERIFICATION.md.tpl
        d = self._close_bundle("MANUAL", "manual-verification")
        driver.run_issue(d, self.cfg)
        self.assertTrue((d / "MANUAL-VERIFICATION.md").exists())
        self.assertIn("Complete MANUAL-VERIFICATION.md",
                      (d / "check-review.md").read_text(encoding="utf-8"))

    def test_accept_then_publish_skips(self) -> None:
        d = self._close_bundle("CLOSE")
        driver.run_issue(d, self.cfg)
        summary = d / "SUMMARY.md"
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        signoff.record(summary, action="accept", by="tester", date="2026-01-01")
        self.assertEqual(state.state(d), state.COMPLETE)
        # Publish has nothing to git-apply: it skips gracefully (return 0), never errors.
        rc = publish.publish(self.cfg, "CLOSE", dry_run=True, skip_if_no_target=True)
        self.assertEqual(rc, 0)

    def test_reopen_does_a_real_build(self) -> None:
        # Reopening a close bundle to a fix path archives the close marker and runs the
        # real builder on the next pass — the fast path is a hint, not a gate.
        d = self._close_bundle("CLOSE")
        driver.run_issue(d, self.cfg)
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="tester", date="2026-01-01")
        self.assertEqual(state.state(d), state.ITERATE_DO)
        driver.advance(d, self.cfg)  # archive the close attempt → PLANNED
        self.assertEqual(state.state(d), state.PLANNED)
        self.assertFalse((d / state.CLOSE_MARKER).exists())              # marker cleared
        self.assertTrue((d / "iteration-v1" / state.CLOSE_MARKER).exists())  # preserved
        driver.advance(d, self.cfg)  # real Do this time (iteration exists → not close)
        self.assertTrue((d / "patch.diff").exists())
        self.assertEqual(state.state(d), state.BUILT)

    def test_config_close_class(self) -> None:
        # Default set classifies the close hints; a non-close hint is "".
        self.assertTrue(all(self.cfg.close_class(c) for c in DEFAULT_CLOSE_DISPOSITIONS))
        self.assertEqual(self.cfg.close_class("likely-fix"), "")
        self.assertEqual(self.cfg.close_class("manual-verification → mac only"),
                         "manual-verification")
        # No-patch-lands-here triage outcomes are close-class (#62), matching the brief
        # template's canonical phrasing; POSSIBLY-FIXED needs verification, so it is NOT.
        self.assertEqual(self.cfg.close_class("UPSTREAM (not this repo's defect)"), "upstream")
        self.assertEqual(self.cfg.close_class("EXTERNAL (not a defect in scope)"), "external")
        self.assertEqual(self.cfg.close_class("POSSIBLY-FIXED → verify first"), "")
        # An instance override is honoured.
        self.cfg.close_dispositions = ["upstream"]
        self.assertEqual(self.cfg.close_class("UPSTREAM"), "upstream")
        self.assertEqual(self.cfg.close_class("likely-close"), "")


class AdvisoryReviewResilience(unittest.TestCase):
    """A failed/interrupted reviewer must degrade to a §6 NEEDS-HUMAN, never crash
    the deterministic spine (the review is advisory, not a gating artifact)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("TOY")
        self.d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_assemble_survives_missing_review(self) -> None:
        # A check-review.md that never landed (reviewer connection dropped) must not
        # crash assemble; the bundle assembles with a §6 NEEDS-HUMAN blocking accept.
        driver.run_issue(self.d, self.cfg)
        (self.d / "check-review.md").unlink()
        assemble.assemble_summary(self.d, self.cfg)  # must not raise
        summary = self.d / "SUMMARY.md"
        self.assertIn("no check-review.md was produced",
                      summary.read_text(encoding="utf-8"))
        self.assertTrue(signoff.open_needs_human(summary))  # accept stays blocked

    def test_sandboxed_review_failure_writes_placeholder(self) -> None:
        # Both failure shapes — the reviewer leaf raises, and it returns 0 but writes
        # no file — leave a re-runnable bundle with a NEEDS-HUMAN placeholder.
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}\n", encoding="utf-8")
        orig = leaves._invoke

        def boom(*a, **k):
            raise RuntimeError("dropped connection")

        leaves._invoke = boom
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)  # must not raise
        finally:
            leaves._invoke = orig
        self.assertIn("NEEDS-HUMAN",
                      (self.d / "check-review.md").read_text(encoding="utf-8"))

        (self.d / "check-review.md").unlink()
        leaves._invoke = lambda *a, **k: None  # returns, writes nothing
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke = orig
        self.assertIn("NOT COMPLETED",
                      (self.d / "check-review.md").read_text(encoding="utf-8"))


class ConfiguredGates(unittest.TestCase):
    """The config-driven, single-sourced gates (docs 04)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, checks: list[dict]) -> Config:
        cfg = _stub_config(self.tmp)
        cfg.gates_checks = checks
        return cfg

    def test_passing_and_failing_repo_gates(self) -> None:
        cfg = self._cfg([
            {"id": "ok", "tier": "T1", "label": "ok", "cmd": "true", "gating": True, "scope": "repo"},
            {"id": "bad", "tier": "T2", "label": "bad", "cmd": "false", "gating": True, "scope": "repo"},
        ])
        result = gates.run_working_tree(cfg)
        self.assertEqual(result["overall"], "fail")  # one gating row failed
        by_id = {r["rule_id"]: r["result"] for r in result["rows"]}
        self.assertEqual(by_id["ok"], "pass")
        self.assertEqual(by_id["bad"], "fail")

    def test_working_tree_skips_bundle_scope(self) -> None:
        cfg = self._cfg([
            {"id": "b", "tier": "C4", "label": "bundle-only", "cmd": "false", "gating": True, "scope": "bundle"},
        ])
        result = gates.run_working_tree(cfg)
        # The bundle-scoped failing check is skipped, so the working tree is green.
        self.assertEqual(result["overall"], "pass")
        self.assertNotIn("b", {r["rule_id"] for r in result["rows"]})


class DelegatedGates(unittest.TestCase):
    """Delegated gates (issue #67): a host runner single-sources the gates; a check's
    bare `subcmd` runs as `<runner> <subcmd>`, so PDCA orchestrates without re-declaring."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, checks: list[dict], runner: str = "") -> Config:
        cfg = _stub_config(self.tmp)
        cfg.gates_checks = checks
        cfg.gates_runner = runner
        return cfg

    def test_subcmd_resolved_against_runner(self) -> None:
        # `subcmd` is run as `<runner> <subcmd>`; the resolved command is the oracle.
        cfg = self._cfg(
            [{"id": "ci", "tier": "T1", "label": "host ci", "subcmd": "ok-step",
              "gating": True, "scope": "repo"}],
            runner="echo")
        result = gates.run_working_tree(cfg)
        row = next(r for r in result["rows"] if r["rule_id"] == "ci")
        self.assertEqual(row["result"], "pass")           # `echo ok-step` exits 0
        self.assertEqual(row["oracle"], "echo ok-step")   # runner prefixed

    def test_missing_runner_is_a_clear_failing_row_not_a_crash(self) -> None:
        cfg = self._cfg(
            [{"id": "x", "tier": "T1", "label": "host ci", "subcmd": "build",
              "gating": True, "scope": "repo"}],
            runner="definitely-not-a-real-binary-zzz xtask")
        result = gates.run_working_tree(cfg)  # must not raise
        row = next(r for r in result["rows"] if r["rule_id"] == "x")
        self.assertEqual(row["result"], "fail")
        self.assertIn("not found on PATH", row["path_line"])

    def test_subcmd_without_runner_is_flagged(self) -> None:
        cfg = self._cfg(
            [{"id": "y", "tier": "T1", "label": "host ci", "subcmd": "build",
              "gating": True, "scope": "repo"}],
            runner="")  # subcmd declared but no runner configured
        result = gates.run_working_tree(cfg)
        row = next(r for r in result["rows"] if r["rule_id"] == "y")
        self.assertEqual(row["result"], "fail")
        self.assertIn("runner is unset", row["path_line"])

    def test_inline_cmd_unaffected_by_runner(self) -> None:
        # A full `cmd` still runs verbatim even when a runner is configured.
        cfg = self._cfg(
            [{"id": "z", "tier": "T1", "label": "inline", "cmd": "true",
              "gating": True, "scope": "repo"}],
            runner="echo")
        row = next(r for r in gates.run_working_tree(cfg)["rows"] if r["rule_id"] == "z")
        self.assertEqual(row["result"], "pass")
        self.assertEqual(row["oracle"], "true")  # not prefixed with the runner


class BuilderGuard(unittest.TestCase):
    """The PreToolUse hook enforcing the builder's STOP discipline."""

    GUARD = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "builder_guard.py"

    def _exit(self, command: str) -> int:
        payload = json.dumps({"tool_input": {"command": command}})
        r = subprocess.run(
            [sys.executable, str(self.GUARD)],
            input=payload, capture_output=True, text=True,
        )
        return r.returncode

    def test_allows_push_and_draft_pr(self) -> None:
        self.assertEqual(self._exit("git push origin feat"), 0)
        self.assertEqual(self._exit("gh pr create --draft --fill"), 0)

    def test_blocks_ready_and_merge(self) -> None:
        self.assertEqual(self._exit("gh pr ready 123"), 2)
        self.assertEqual(self._exit("gh pr merge 123 --squash"), 2)

    def test_blocks_ready_when_chained_after_allowed(self) -> None:
        # Each segment is checked independently; the ready-mark segment is blocked.
        self.assertEqual(self._exit("git push origin feat && gh pr ready 123"), 2)

    def test_blocks_wrapped_ready(self) -> None:
        self.assertEqual(self._exit("timeout 30 gh pr merge 123"), 2)


class SignoffQueue(unittest.TestCase):
    """The cheap-first sign-off burn-down (docs 03 §sign-off queue)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_issue(self, issue_id: str) -> Path:
        d = self.cfg.bundle(issue_id)
        d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, d / "brief.md")
        driver.run_issue(d, self.cfg)
        return d

    def test_cheap_confirms_come_first(self) -> None:
        needs = self._run_issue("NEEDS")  # stub reviewer leaves §6 non-empty
        cheap = self._run_issue("CHEAP")
        # Simulate the human having adjudicated CHEAP's §6 (box checked).
        summ = cheap / "SUMMARY.md"
        summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")

        entries = queue.awaiting_signoff(self.cfg)
        self.assertEqual([e.bundle.name for e in entries], ["issue_CHEAP", "issue_NEEDS"])
        self.assertTrue(entries[0].cheap)
        self.assertFalse(entries[1].cheap)
        self.assertEqual(entries[1].open_needs_human, 1)
        self.assertEqual(needs.name, "issue_NEEDS")


class ActTooling(unittest.TestCase):
    """The L4 Act tooling — bundle index, patterns, act-log scaffold (docs 03 §Act)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _complete(self, issue_id: str, candidate: str) -> Path:
        d = self.cfg.bundle(issue_id)
        d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, d / "brief.md")
        driver.run_issue(d, self.cfg)
        summ = d / "SUMMARY.md"
        t = summ.read_text(encoding="utf-8").replace("- [ ]", "- [x]")  # clear §6
        t = t.replace("- (empty is the common case)", f"- [x] {candidate}")  # add §10 hint
        summ.write_text(t, encoding="utf-8")
        signoff.record(summ, action="accept", by="t", date="2026-06-01")
        return d

    def test_index_only_sees_frozen(self) -> None:
        self._complete("DONE", "spec field X ambiguous")
        # An in-flight bundle (no sign-off) must not appear in the Act index.
        live = self.cfg.bundle("LIVE")
        live.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, live / "brief.md")
        driver.run_issue(live, self.cfg)  # halts at AWAITING_SIGNOFF
        names = [e.bundle.name for e in act.index(self.cfg)]
        self.assertEqual(names, ["issue_DONE"])

    def test_patterns_and_scaffold(self) -> None:
        self._complete("A", "spec field X ambiguous")
        self._complete("B", "spec field X ambiguous")
        entries = act.index(self.cfg)
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.outcome == "merged-wider" for e in entries))
        pats = act.patterns(entries)
        self.assertTrue(pats["act_candidates"], "recurring §10 hint not detected")
        scaffold = act.scaffold_entry(entries, pats, date="2026-06-04")
        self.assertIn("2026-06-04", scaffold)
        self.assertIn("cycles considered: A, B", scaffold)
        self.assertIn("TODO", scaffold)  # deltas left to the human

    def test_append_creates_log(self) -> None:
        self._complete("A", "x")
        entries = act.index(self.cfg)
        log = act.append_entry(self.cfg, act.scaffold_entry(entries, act.patterns(entries), "2026-06-04"))
        self.assertTrue(log.exists())
        self.assertIn("Act review — 2026-06-04", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
