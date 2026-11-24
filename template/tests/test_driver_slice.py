"""End-to-end vertical slice for the PDCA driver (stdlib unittest — no deps).

Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
Exercises the full control flow on the toy brief with stub leaves/gates:
init → Do → gates → reviewer → assembled SUMMARY → human sign-off → COMPLETE,
plus the C6 accept-gate, the independence contract, and an iterate transition.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pdca_harness import act, assemble, driver, gates, publish, queue, leaves, signoff, state
from pdca_harness.config import DEFAULT_CLOSE_DISPOSITIONS, Config, LeafConfig

TOY_BRIEF = Path(__file__).resolve().parents[1] / "examples" / "toy" / "brief.md"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _js_units(text: str) -> str:
    """``text`` as JavaScript counts it: a character above U+FFFF (an emoji) becomes its two
    UTF-16 surrogates, so a regex `.` matches one code unit, as `[^/]` with no `u` flag does
    in Claude Code's matchers (#935)."""
    return "".join(chr(0xD800 + ((ord(c) - 0x10000) >> 10))
                   + chr(0xDC00 + ((ord(c) - 0x10000) & 0x3FF)) if ord(c) > 0xFFFF else c
                   for c in text)


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


class ReviewerTargetAccess(unittest.TestCase):
    """The reviewer grounds citations on the brief's target checkout via $PDCA_TARGET
    (issue #75) — single-sourced from the brief, so it doesn't wander into other
    checkouts on the machine."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("T")
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(
            "- **Slug:** x\n- **Repo + branch target:** org/myrepo @ main\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolves_target_checkout_when_present(self) -> None:
        self.cfg.repo_checkouts = {"org/myrepo": "target"}  # → <root>/target
        self.assertIsNone(leaves._reviewer_target(self.d, self.cfg))  # not on disk yet
        (self.cfg.root / "target").mkdir()
        self.assertEqual(leaves._reviewer_target(self.d, self.cfg),
                         (self.cfg.root / "target").resolve())

    def test_no_target_when_brief_has_no_target(self) -> None:
        (self.d / "brief.md").write_text("- **Slug:** x\n", encoding="utf-8")
        self.assertIsNone(leaves._reviewer_target(self.d, self.cfg))

    def test_review_prompt_grounds_on_pdca_target(self) -> None:
        # The prompt names $PDCA_TARGET and forbids wandering to other checkouts.
        self.assertIn("$PDCA_TARGET", leaves._REVIEW_PROMPT)
        self.assertIn("do NOT search other checkouts", leaves._REVIEW_PROMPT)

    def test_prefers_worktree_over_sibling(self) -> None:
        # When a per-cycle worktree exists it is the grounding target — pinned to the gate
        # base + patch applied — never the human's (possibly stale) sibling checkout (#120).
        wt = self.tmp / "checkout.pdca-wt"
        wt.mkdir()
        with mock.patch.object(leaves.worktree, "path", return_value=wt):
            self.assertEqual(leaves._reviewer_target(self.d, self.cfg), wt)

    def test_sibling_fallback_fetches_without_touching_working_tree(self) -> None:
        # Worktree off: ground on the sibling, but only `git fetch` it — NEVER reset or
        # checkout the human's working tree (#120). Real git, no network.
        self.cfg.worktree = False
        self.cfg.base_remote = "origin"
        origin = self.tmp / "origin.git"
        repo = self.tmp / "myrepo"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                        capture_output=True)
        run("config", "user.email", "t@e.com"); run("config", "user.name", "T")
        (repo / "f.txt").write_text("base\n", encoding="utf-8")
        run("add", "-A"); run("commit", "-q", "-m", "base"); run("branch", "-M", "main")
        run("push", "-q", "-u", "origin", "main")
        (repo / "f.txt").write_text("human edit\n", encoding="utf-8")   # uncommitted work
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
        self.cfg.repo_checkouts = {"org/myrepo": str(repo)}
        got = leaves._reviewer_target(self.d, self.cfg)
        self.assertEqual(got, repo)
        self.assertEqual((repo / "f.txt").read_text(encoding="utf-8"), "human edit\n")
        self.assertTrue((repo / "untracked.txt").exists())   # fetch is refs-only


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

    def test_sandbox_seeds_project_agents_preserving_independence(self) -> None:
        # #161: the reviewer runs in a temp sandbox cwd; Claude Code (>=2.1.x) resolves
        # `--agent` by walking up from cwd, so the project's .claude/agents must be seeded
        # INTO the sandbox — exposing only the agent definition, never build-notes.md.
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}\n", encoding="utf-8")
        (self.d / "build-notes.md").write_text("builder framing — must not leak\n", encoding="utf-8")
        agents = self.cfg.root / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "reviewer.md").write_text("---\nname: reviewer\n---\nrole\n", encoding="utf-8")
        seen: dict = {}
        orig = leaves._invoke

        def capture(leaf, workdir, prompt, **k):
            wd = Path(workdir)
            seen["agent"] = (wd / ".claude" / "agents" / "reviewer.md").exists()
            seen["build_notes"] = (wd / "build-notes.md").exists()
            (wd / "check-review.md").write_text("ok\n", encoding="utf-8")  # let the run succeed

        leaves._invoke = capture
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke = orig
        self.assertTrue(seen.get("agent"))         # `--agent reviewer` now resolves from the sandbox
        self.assertFalse(seen.get("build_notes"))  # independence held: build-notes.md absent

    def test_sandbox_seeding_skips_dangling_symlink_keeps_good_agent(self) -> None:
        # #161 review: a broken symlink under .claude/agents must not abort Check, and the
        # good agent still seeds (ignore_dangling_symlinks).
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}\n", encoding="utf-8")
        agents = self.cfg.root / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        (agents / "dangling.md").symlink_to(agents / "no-such-target.md")  # broken link
        seen: dict = {}
        orig = leaves._invoke

        def capture(leaf, workdir, prompt, **k):
            seen["agent"] = (Path(workdir) / ".claude" / "agents" / "reviewer.md").exists()
            (Path(workdir) / "check-review.md").write_text("ok\n", encoding="utf-8")

        leaves._invoke = capture
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)  # must NOT raise
        finally:
            leaves._invoke = orig
        self.assertTrue(seen.get("agent"))  # the good agent seeded despite the broken link

    def test_sandbox_seeding_copy_error_does_not_abort_check(self) -> None:
        # #161 review: any copy error (e.g. an unreadable file) under .claude/agents must
        # degrade to a no-op + the §6 placeholder, never crash the deterministic spine.
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}\n", encoding="utf-8")
        agents = self.cfg.root / ".claude" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        orig = leaves._invoke
        leaves._invoke = lambda *a, **k: None  # leaf returns, writes nothing → placeholder
        try:
            with mock.patch.object(leaves.shutil, "copytree",
                                   side_effect=OSError("unreadable agent file")):
                leaves._run_review_sandboxed(self.d, self.cfg)  # must NOT raise
        finally:
            leaves._invoke = orig
        self.assertIn("NOT COMPLETED",
                      (self.d / "check-review.md").read_text(encoding="utf-8"))

    # -- #403: the round's frozen gate evidence must resolve from the sandbox ----------

    _EVIDENCE_GATE = {"id": "T3-log", "tier": "T3", "label": "runtime", "scope": "bundle",
                      "gating": True,
                      "cmd": "echo evidence-first-line; echo evidence-last-line"}

    def _sandbox_probe(self, seen: dict, out_name: str = "check-review.md"):
        """A fake leaf command that records what the sandbox cwd actually holds."""
        def capture(leaf, workdir, prompt, **k):
            wd = Path(workdir)
            rows = json.loads((wd / "check-gates.json").read_text(encoding="utf-8"))["rows"]
            seen["logged_rows"] = [r for r in rows if r.get("log")]
            seen["resolved"] = {r["log"]: (wd / r["log"]).is_file() for r in seen["logged_rows"]}
            seen["texts"] = {r["log"]: (wd / r["log"]).read_text(encoding="utf-8")
                             for r in seen["logged_rows"] if (wd / r["log"]).is_file()}
            seen["build_notes"] = (wd / "build-notes.md").exists()
            (wd / out_name).write_text("ok\n", encoding="utf-8")
        return capture

    def _real_gate_round(self) -> None:
        """Run a REAL bundle-scoped gate so check-gates.json + gate-logs/ are the
        production artifacts (gates.run_gates → gates.py:157/544), not hand-written."""
        (self.d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.cfg.gates_checks = [self._EVIDENCE_GATE]
        gates.run_gates(self.d, self.cfg)
        (self.d / "build-notes.md").write_text("builder framing — must not leak\n",
                                               encoding="utf-8")
        self.assertTrue((self.d / state.GATE_LOGS_DIR / "T3-log.log").exists())

    def test_sandbox_seeds_gate_logs_so_every_row_log_resolves(self) -> None:
        # #403: each check-gates.json row carries `log: gate-logs/<rule_id>.log` (the full
        # captured output, gates.py:544) — the one artifact that lets the reviewer
        # adjudicate a row whose oracle it cannot re-run. Seeding copied file NAMES only,
        # so the row referenced a path that did not resolve in the leaf's cwd.
        self._real_gate_round()
        seen: dict = {}
        orig = leaves._invoke
        leaves._invoke = self._sandbox_probe(seen)
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke = orig
        self.assertTrue(seen.get("logged_rows"), "no row carried a `log` key to resolve")
        # EVERY path a frozen row references resolves inside the leaf's cwd…
        self.assertTrue(all(seen["resolved"].values()), seen.get("resolved"))
        # …and it is the real evidence: header + verbatim output, not an empty stand-in.
        text = seen["texts"]["gate-logs/T3-log.log"]
        self.assertIn("# cmd: ", text)
        self.assertIn("evidence-first-line\nevidence-last-line\n", text)
        # …while independence still holds: the builder's rationale stays out.
        self.assertFalse(seen.get("build_notes"))

    def test_advisory_sandbox_seeds_gate_logs_too(self) -> None:
        # Both seeding call sites stay in step (leaves.py:1890/2205): the advisory leaves
        # share the reviewer's sandbox contract, so they get the same evidence.
        self._real_gate_round()
        seen: dict = {}
        orig = leaves._invoke
        leaves._invoke = self._sandbox_probe(seen, out_name="check-advisory-lens.md")
        try:
            leaves._run_advisory_sandboxed(
                self.d, self.cfg, LeafConfig(mode="command", family="codex"),
                {"id": "lens"}, "lens")
        finally:
            leaves._invoke = orig
        self.assertTrue(seen.get("logged_rows"))
        self.assertTrue(all(seen["resolved"].values()), seen.get("resolved"))
        self.assertFalse(seen.get("build_notes"))

    def test_gate_log_seed_failure_does_not_abort_check(self) -> None:
        # Best-effort, exactly like the agents seed (#161): an unreadable/failing copy
        # degrades to a no-op + the §6 placeholder — an OSError must never abort Check.
        self._real_gate_round()
        orig = leaves._invoke
        leaves._invoke = lambda *a, **k: None  # returns, writes nothing → placeholder
        try:
            with mock.patch.object(leaves.shutil, "copytree",
                                   side_effect=OSError("unreadable gate log")):
                with redirect_stderr(io.StringIO()):
                    leaves._run_review_sandboxed(self.d, self.cfg)  # must NOT raise
        finally:
            leaves._invoke = orig
        self.assertIn("NOT COMPLETED",
                      (self.d / "check-review.md").read_text(encoding="utf-8"))

    def test_sandbox_without_gate_logs_is_a_no_op(self) -> None:
        # An older bundle / a stub gate round has no gate-logs/: the seed is a no-op and
        # the leaf runs exactly as before (no crash, no empty directory invented).
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text('{"rows": []}\n', encoding="utf-8")
        seen: dict = {}
        orig = leaves._invoke
        leaves._invoke = self._sandbox_probe(seen)
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke = orig
        self.assertEqual(seen.get("logged_rows"), [])
        self.assertIn("ok", (self.d / "check-review.md").read_text(encoding="utf-8"))

    def test_reviewer_contract_routes_unrepeatable_gate_to_its_log(self) -> None:
        # The contract text must stop being false about the sandbox contents, and must
        # send a row it cannot re-run to gate-logs/ instead of an automatic escalation —
        # the driver-side prompt and the vendored role body saying the same thing.
        prompt = leaves._REVIEW_PROMPT
        self.assertIn("gate-logs/", prompt)
        self.assertNotIn("You have ONLY patch.diff, brief.md and check-gates.json", prompt)
        self.assertIn("gate-logs/", leaves._advisory_prompt({}, "lens"))
        agents = Path(__file__).resolve().parents[1] / "agents"
        # The role body ships as `.md.jinja` in the template repo and as the rendered
        # `.md` in an instance — assert on whichever this checkout carries.
        role_path = next((p for p in (agents / "reviewer.md.jinja", agents / "reviewer.md")
                          if p.exists()), None)
        self.assertIsNotNone(role_path, f"no reviewer role body under {agents}")
        role = role_path.read_text(encoding="utf-8")
        self.assertIn("gate-logs/", role)
        self.assertIn("$PDCA_WORKTREE`-scoped by design", role)

    def _exempt(self, *commands: str) -> None:
        """Grant a leaf sandbox exemption, on a family that can actually be BOUNDED.

        Only a family with `settings_scope_argv` (claude: `--setting-sources project`) can be
        confined to the harness's own settings; without that, the operator's user-scope
        `excludedCommands` concatenate into the leaf and the list is a floor, not a ceiling.
        So the exemption is claude-only by construction (#288 review) — the default stub
        reviewer here is codex, which is refused (asserted separately below).
        """
        self.cfg.reviewer = LeafConfig(mode="stub", family="claude")
        self.cfg.leaf_unsandboxed_commands = list(commands)

    def _project_settings(self, payload: dict) -> None:
        cdir = self.cfg.root / ".claude"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def _capture_sandbox_settings(self) -> dict:
        """Run the sandboxed reviewer, returning what landed at <sandbox>/.claude/settings.json.

        Against a fake host (:meth:`_fake_host`) unless the test set one up itself: since #935
        every seed lists the operator's credential locations, and no test reads the CI host's."""
        if getattr(self, "_host", None) is None:
            self._fake_host()
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}\n", encoding="utf-8")
        seen: dict = {}
        orig = leaves._invoke

        def capture(leaf, workdir, prompt, **k):
            f = Path(workdir) / ".claude" / "settings.json"
            seen["exists"] = f.exists()
            seen["content"] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
            seen["extra_argv"] = list(k.get("extra_argv") or [])
            (Path(workdir) / "check-review.md").write_text("ok\n", encoding="utf-8")

        leaves._invoke = capture
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke = orig
        return seen

    def test_sandbox_seeds_project_sandbox_settings(self) -> None:
        # #261: Claude Code loads project settings from `.claude/settings.json` relative to
        # the subprocess cwd. The reviewer runs in a temp cwd, so the project's sandbox
        # policy — notably sandbox.network.allowLocalBinding — never applied, and every
        # loopback-socket runtime test failed to bind before its assertion ran.
        self._project_settings({
            "sandbox": {"network": {"allowLocalBinding": True}},
            "permissions": {"allow": ["Edit", "Write"]},
        })
        seen = self._capture_sandbox_settings()
        self.assertTrue(seen["exists"])
        self.assertIs(seen["content"]["sandbox"]["network"]["allowLocalBinding"], True)
        # ONLY the loopback grant travels — the project's Edit/Write allow-list must not
        # widen the reviewer's surface past what its `tools:` frontmatter grants.
        self.assertEqual(self._grants(seen["content"]),
                         {"sandbox": {"network": {"allowLocalBinding": True}}})
        # …and beside it, the harness's own credential deny (#935): deny rules only.
        content = seen["content"]
        self.assertTrue(self._deny_read(content))
        self.assertTrue(self._env_entries(content))
        self.assertEqual(set(content.get("permissions", {})), {"deny"})
        self.assertIn("Read(//proc/**)", self._deny_rules(content))
        self.assertFalse([r for r in self._deny_rules(content)
                          if not r.startswith("Read(")])

    def _grants(self, content: dict) -> dict:
        """The seeded file with the harness's credential deny (#935) set aside: what is left is
        exactly what was GRANTED, for the tests that pin the grant allow-list. The deny itself
        is pinned by the `test_seed_*` cases below; here it is held to being deny-only — a
        `filesystem` with nothing but `denyRead`, `credentials` with nothing but `deny`
        entries, `permissions` with nothing but `Read(...)` denies — so no grant can hide in it.
        """
        content = json.loads(json.dumps(content))      # a copy; the caller's stays whole
        sandbox = content.get("sandbox", {})
        self.assertLessEqual(set(sandbox.pop("filesystem", {})), {"denyRead"})
        credentials = sandbox.pop("credentials", {})
        self.assertLessEqual(set(credentials), {"envVars"})
        self.assertTrue(all(e.get("mode") == "deny" for e in credentials.get("envVars", [])))
        permissions = content.pop("permissions", {})
        self.assertLessEqual(set(permissions), {"deny"})
        self.assertTrue(all(r.startswith("Read(") for r in permissions.get("deny", [])))
        return content

    # Lookups that come back empty, not KeyError, on a seed with no deny: the red leg then
    # fails on the assertion that names the missing rule.
    @staticmethod
    def _deny_read(content: dict) -> list:
        return content.get("sandbox", {}).get("filesystem", {}).get("denyRead", [])

    @staticmethod
    def _deny_rules(content: dict) -> list:
        return content.get("permissions", {}).get("deny", [])

    @staticmethod
    def _env_entries(content: dict) -> list:
        return content.get("sandbox", {}).get("credentials", {}).get("envVars", [])

    # ---- #935: every seeded policy denies the operator's credentials ----------------------
    # The brief's named minimum, written out HERE rather than read from `leaves`, so these
    # tests pin the list itself and not whatever the code holds. (path under a home, is a dir)
    _HOME_LOCATIONS = (
        (".claude.json", False), (".codex", True), (".gemini", True), (".config/gh", True),
        (".config/glab-cli", True), (".ssh", True), (".git-credentials", False),
        (".config/git/credentials", False), (".netrc", False), (".aws", True), (".kube", True),
        (".docker/config.json", False), (".dockercfg", False),
        (".config/containers/auth.json", False), (".npmrc", False),
        (".cargo/credentials.toml", False), (".cargo/credentials", False), (".pypirc", False),
        (".gem/credentials", False), (".local/share/gem/credentials", False),
        (".cache/huggingface/stored_tokens", False),
        (".terraform.d/credentials.tfrc.json", False), (".terraformrc", False),
        (".config/gcloud", True), (".azure", True),
    )
    # (variable, path under it, is a dir)
    _VAR_LOCATIONS = (
        ("CODEX_HOME", "", True), ("GH_CONFIG_DIR", "", True), ("XDG_CONFIG_HOME", "gh", True),
        ("XDG_CONFIG_HOME", "glab-cli", True), ("XDG_CONFIG_HOME", "git/credentials", False),
        ("XDG_CONFIG_HOME", "containers/auth.json", False), ("XDG_CONFIG_HOME", "gcloud", True),
        ("GLAB_CONFIG_DIR", "", True), ("NETRC", "", False),
        ("AWS_SHARED_CREDENTIALS_FILE", "", False), ("AWS_CONFIG_FILE", "", False),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "", False),
        ("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "", False),
        ("DOCKER_CONFIG", "config.json", False), ("REGISTRY_AUTH_FILE", "", False),
        ("XDG_RUNTIME_DIR", "containers/auth.json", False), ("NPM_CONFIG_USERCONFIG", "", False),
        ("CARGO_HOME", "credentials.toml", False), ("CARGO_HOME", "credentials", False),
        ("XDG_DATA_HOME", "gem/credentials", False), ("HF_HOME", "stored_tokens", False),
        ("XDG_CACHE_HOME", "huggingface/stored_tokens", False),
        ("TF_CLI_CONFIG_FILE", "", False), ("CLOUDSDK_CONFIG", "", True),
        ("GOOGLE_APPLICATION_CREDENTIALS", "", False), ("AZURE_CONFIG_DIR", "", True),
        ("AZURE_CLIENT_CERTIFICATE_PATH", "", False), ("AZURE_FEDERATED_TOKEN_FILE", "", False),
    )
    _CLAUDE_CONFIG_FILES = (".credentials.json", "settings.json", "settings.local.json",
                            "history.jsonl", "daemon.json")
    _CLAUDE_CONFIG_DIRS = ("file-history", "paste-cache", "session-env", "shell-snapshots",
                           "sessions", "todos", "plans", "ide", "debug", "backups", "jobs",
                           "daemon")
    _DENIED_VARS = (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN", "NPM_TOKEN",
        "CARGO_REGISTRY_TOKEN", "DOCKER_AUTH_CONFIG", "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_RUNTIME_TOKEN", "GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "GL_TOKEN",
        "CI_JOB_TOKEN", "TWINE_PASSWORD", "GEM_HOST_API_KEY", "COMPOSER_AUTH", "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN", "VAULT_TOKEN", "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_CERTIFICATE_PASSWORD", "AZURE_PASSWORD",
    )

    def _fake_host(self, **env: str) -> dict:
        """Point every seed at a FAKE host for the rest of the test, never the CI host's own.

        The environment is cleared (only PATH kept, so `git` still runs) and holds just
        `HOME`, `CLAUDE_CONFIG_DIR` and ``env``. `HOME` is a symlink to the real home dir, so
        every home location has two spellings; the password database says the real one. The
        home's `.claude` holds the CLI's login, an entry the CLI's own list does not name, and
        another session's `projects/` folder with its transcript — plus a folder whose name
        carries pattern characters. `$CLAUDE_CONFIG_DIR` is a second config dir. Nothing
        under the home but `.claude` exists: a missing location must still be denied."""
        base = self.tmp.resolve() / "host"
        real = base / "home"
        claude = real / ".claude"
        (claude / "projects" / "-tmp-old-session").mkdir(parents=True)
        (claude / "projects" / "-tmp-old-session" / "f00d.jsonl").write_text("{}\n")
        (claude / "projects" / "we[ir]d name").mkdir()
        (claude / ".credentials.json").write_text("{}\n")
        (claude / "statsig").mkdir()
        link = base / "home-link"
        link.symlink_to(real, target_is_directory=True)
        config = base / "claude-config"
        (config / "projects" / "-tmp-other").mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", ""), "HOME": str(link),
            "CLAUDE_CONFIG_DIR": str(config), **env}, clear=True))
        self.enterContext(mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=str(real))))
        self._host = {"base": base, "real": real, "link": link, "config": config}
        return self._host

    @staticmethod
    def _as_written(path: Path | str) -> str:
        """``path`` as a rule writes it: each glob character (`* ? [ ] { } \\`) as `?`, every
        other character as it is. (A NAME read off the disk is written stricter, per the brief;
        the tests that read one spell its rule out.) The identity on every path these tests
        build under an ordinary temp dir; the two path-character cases pin it."""
        return re.sub(r"[][*?{}\\]", "?", str(path))

    @staticmethod
    def _rule_covers(rule: str, path: Path | str) -> bool:
        """Whether the Read rule ``rule`` could match ``path``, read generously: `*` and `?`
        match across `/` too, case is not told apart, a rule matches everything under what it
        names, and it matches through its "physical twin" too — Claude Code 2.1.322 applies
        each rule again with the part before its first wildcard resolved through symlinks. A
        rule that misses here misses under any stricter reading, the vendor's included. Like
        the vendor's, a `?` is one UTF-16 code unit, so a character above U+FFFF takes two.
        A rule for `/`, or one whose twin is `/` (it names a link to the root), covers every
        path."""
        pattern = rule[len("Read(/"):-1].removesuffix("/**").rstrip("/")
        parts = pattern.split("/")
        cut = next((i for i, part in enumerate(parts) if "*" in part or "?" in part),
                   len(parts))
        twins = [pattern] + (["/".join([os.path.realpath("/".join(parts[:cut])).rstrip("/"),
                                        *parts[cut:]])]
                             if cut > 1 else [])
        return any(re.fullmatch("".join(".*" if c == "*" else "." if c == "?" else re.escape(c)
                                        for c in _js_units(twin)) + "(/.*)?",
                                _js_units(str(path)), re.IGNORECASE)
                   for twin in twins)

    def _covering(self, deny: list, path: Path) -> list:
        """The Read rules in ``deny`` that could match ``path`` (:meth:`_rule_covers`)."""
        return [rule for rule in deny if self._rule_covers(rule, path)]

    def _assert_denied(self, content: dict, path: Path, is_dir: bool) -> None:
        """``path`` is denied to the leaf's commands AND to its Read tool, absolute, in the
        spelling given (here) — callers pass each spelling they need."""
        written = self._as_written(path)
        rule = f"Read(/{written}/**)" if is_dir else f"Read(/{written})"
        self.assertIn(written, self._deny_read(content))
        self.assertIn(rule, self._deny_rules(content))

    def test_seed_denies_every_named_credential_location_under_the_home(self) -> None:
        # (b) The exemption seed — written AND enabled on this code — lists each named home
        # location in `denyRead` and as a Read deny, in the given (`$HOME`, a symlink) and the
        # resolved spelling, whether or not it exists; the containers logins under /run always;
        # and `Read(//proc/**)`, written to `permissions.deny` only (the brief). Whether Claude
        # Code then hides `/proc` from the commands as well is the live check's to settle. Red
        # before #935: no `filesystem`, no `Read(...)`.
        h = self._fake_host()
        self._exempt("cargo xtask fdb-conformance")
        content = self._capture_sandbox_settings()["content"]
        for rel, is_dir in self._HOME_LOCATIONS:
            for home in (h["link"], h["real"]):
                with self.subTest(home=str(home), location=rel):
                    self.assertFalse((home / rel).exists())     # missing, and denied anyway
                    self._assert_denied(content, home / rel, is_dir)
        uid = os.getuid()
        for path in (f"/run/user/{uid}/containers/auth.json", f"/run/containers/{uid}/auth.json"):
            self._assert_denied(content, Path(path), False)     # no $XDG_RUNTIME_DIR set
        self.assertIn("Read(//proc/**)", self._deny_rules(content))
        self.assertFalse([p for p in self._deny_read(content)
                          if p == "/proc" or p.startswith("/proc/")])   # not in `denyRead`
        self.assertTrue(all(os.path.isabs(p) for p in self._deny_read(content)))

    def test_seed_denies_the_locations_the_variables_name(self) -> None:
        # (b) A variable-named location is denied when the variable is set and non-empty —
        # each `$KUBECONFIG` entry too — and an empty one names nothing (it is not the cwd).
        base = self.tmp.resolve() / "vars"
        env = {var: str(base / var) for var, _, _ in self._VAR_LOCATIONS}
        env["KUBECONFIG"] = os.pathsep.join([str(base / "kube-a"), str(base / "kube-b")])
        h = self._fake_host(**env)
        self._exempt("cargo xtask fdb-conformance")
        content = self._capture_sandbox_settings()["content"]
        for var, under, is_dir in self._VAR_LOCATIONS:
            with self.subTest(var=var, under=under):
                self._assert_denied(content, base / var / under if under else base / var,
                                    is_dir)
        for entry in ("kube-a", "kube-b"):
            self._assert_denied(content, base / entry, False)
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": "", "NETRC": ""}):
            content = self._capture_sandbox_settings()["content"]
        deny_read = self._deny_read(content)
        self.assertNotIn(self._as_written(os.getcwd()), deny_read)
        self.assertNotIn(self._as_written(base / "GH_CONFIG_DIR"), deny_read)
        self.assertIn(self._as_written(h["real"] / ".netrc"), deny_read)  # the default still is

    def test_seed_denies_a_variable_path_with_a_tilde_or_a_variable_where_it_expands(
            self) -> None:
        # (b) A value with a literal `~` or `$VAR` in it — a systemd `Environment=` line, a
        # `docker --env-file` or a quoted `.env` export leaves it unexpanded — names the file a
        # tool that expands it reads: botocore runs `expandvars` then `expanduser` on its file
        # paths, npm expands a leading `~/`. So that spelling is denied, in both home
        # spellings, beside the value as written (a tool that takes it literally reads it
        # against its cwd). Each `$KUBECONFIG` entry the same. Red on round 5's code, which
        # denied only `<cwd>/~/work/aws-creds`, and `aws` in the re-run read the real file.
        h = self._fake_host(
            AWS_SHARED_CREDENTIALS_FILE="~/work/aws-creds",
            GOOGLE_APPLICATION_CREDENTIALS="$HOME/keys/sa.json",
            GH_CONFIG_DIR="${HOME}/gh-alt", DOCKER_CONFIG="~/docker-alt",
            KUBECONFIG=os.pathsep.join(["~/kube/a", "$HOME/kube/b"]))
        self._exempt("cargo xtask fdb-conformance")
        content = self._capture_sandbox_settings()["content"]
        for rel, is_dir in (("work/aws-creds", False), ("keys/sa.json", False),
                            ("gh-alt", True), ("docker-alt/config.json", False),
                            ("kube/a", False), ("kube/b", False)):
            for home in (h["link"], h["real"]):
                with self.subTest(home=str(home), location=rel):
                    self._assert_denied(content, home / rel, is_dir)
        for literal in ("~/work/aws-creds", "$HOME/keys/sa.json", "~/kube/a"):
            with self.subTest(as_written=literal):
                self._assert_denied(content, Path.cwd() / literal, False)

    def test_seed_still_denies_a_variable_that_names_the_home_entry_by_entry(self) -> None:
        # (b) Fail closed: a path a variable names is a credential location on the operator's
        # own word, even when it is the home itself (`$GH_CONFIG_DIR` set to it: `gh` keeps
        # `hosts.yml` there). Unlike a LINK to the home, which the walk passes by, it is
        # denied: whole to the commands, and entry by entry to the Read tool, so its login is
        # refused while the config dir in it keeps its own rules and the leaf its outputs.
        h = self._fake_host()
        (h["real"] / "hosts.yml").write_text("not a token\n")
        self._exempt("cargo xtask fdb-conformance")
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(h["link"])}):
            content = self._capture_sandbox_settings()["content"]
        deny_read, deny = self._deny_read(content), self._deny_rules(content)
        for home in (h["link"], h["real"]):
            with self.subTest(home=str(home)):
                self.assertIn(self._as_written(home), deny_read)
                self.assertIn(f"Read(/{self._as_written(home)}/hosts.yml)", deny)
                self.assertEqual(self._covering(deny, home / ".claude" / self._LEAF_OUTPUT), [])

    def test_seed_denies_the_claude_config_dir_whole_to_commands_and_by_entry_to_read(
            self) -> None:
        # (b) `~/.claude` and `$CLAUDE_CONFIG_DIR`: whole in `denyRead`; to the Read tool the
        # CLI's own names (existing or not) and every entry there now, `projects` never whole —
        # its existing folders by name and the session history by pattern — so the leaf's
        # own session folder, made after the seed, keeps its `tool-results/` readable.
        h = self._fake_host()
        self._exempt("cargo xtask fdb-conformance")
        content = self._capture_sandbox_settings()["content"]
        deny = self._deny_rules(content)
        for given in (h["link"] / ".claude", h["real"] / ".claude", h["config"]):
            config = self._as_written(given)
            with self.subTest(config=config):
                self.assertIn(config, self._deny_read(content))
                self.assertNotIn(f"Read(/{config}/**)", deny)
                for name in self._CLAUDE_CONFIG_FILES:
                    self.assertIn(f"Read(/{config}/{name})", deny)
                for name in self._CLAUDE_CONFIG_DIRS:
                    self.assertIn(f"Read(/{config}/{name}/**)", deny)
                projects = f"{config}/projects"
                self.assertNotIn(f"Read(/{projects}/**)", deny)
                self.assertNotIn(f"Read(/{projects})", deny)
                under = sorted(r for r in deny if r.startswith(f"Read(/{projects}/"))
                existing = (["-tmp-other"] if given == h["config"]
                            else ["-tmp-old-session", "we?ir?d?name"])
                self.assertEqual(under, sorted(
                    [f"Read(/{projects}/{name}/**)" for name in existing]
                    + [f"Read(/{projects}/*/*.jsonl)", f"Read(/{projects}/*/*/subagents/**)",
                       f"Read(/{projects}/*/memory/**)"]))
        for given in (h["link"] / ".claude", h["real"] / ".claude"):
            self.assertIn(f"Read(/{self._as_written(given)}/statsig/**)", deny)  # there now

    def test_seed_denies_where_a_link_in_a_denied_directory_points(self) -> None:
        # (b) Both spellings hold INSIDE a denied directory too. The CLI's login linked to a
        # file elsewhere, `projects` moved to another disk and linked back, a session folder
        # in it linked from a third place, a key linked into `~/.ssh`: denying the directory
        # hides each link, not what it points at, which the leaf's commands and its Read tool
        # could still open by its own path. So each target is denied too, and the moved
        # `projects` the way `projects` is: to the Read tool folder by folder and by pattern,
        # never whole, so the leaf's own session folder made there during the run stays
        # readable. Red on round 1's code, which resolved the config dir but not its entries.
        h = self._fake_host()
        claude, data = h["real"] / ".claude", h["base"] / "data"
        login = data / "secret" / "cred.json"
        login.parent.mkdir(parents=True)
        login.write_text("{}\n")
        (claude / ".credentials.json").unlink()
        (claude / ".credentials.json").symlink_to(login)
        moved = data / "cc-projects"
        (claude / "projects").rename(moved)
        (claude / "projects").symlink_to(moved, target_is_directory=True)
        linked_session = data / "old-session"
        linked_session.mkdir()
        (moved / "-tmp-linked").symlink_to(linked_session, target_is_directory=True)
        key = data / "keys" / "id_ed25519"
        key.parent.mkdir()
        key.write_text("not a key\n")
        (h["real"] / ".ssh").mkdir()
        (h["real"] / ".ssh" / "id_ed25519").symlink_to(key)
        self._exempt("cargo xtask fdb-conformance")
        content = self._capture_sandbox_settings()["content"]
        deny = self._deny_rules(content)
        self._assert_denied(content, login, False)
        self._assert_denied(content, key, False)
        self._assert_denied(content, linked_session, True)
        projects = self._as_written(moved)
        self.assertIn(projects, self._deny_read(content))       # whole, to the commands
        self.assertNotIn(f"Read(/{projects}/**)", deny)
        self.assertNotIn(f"Read(/{projects})", deny)
        self.assertEqual(
            sorted(r for r in deny if r.startswith(f"Read(/{projects}/")),
            sorted([f"Read(/{projects}/{name}/**)"
                    for name in ("-tmp-old-session", "-tmp-linked", "we?ir?d?name")]
                   + [f"Read(/{projects}/*/*.jsonl)", f"Read(/{projects}/*/*/subagents/**)",
                      f"Read(/{projects}/*/memory/**)"]))
        for given in (h["link"] / ".claude", claude):          # the links' own spellings too
            config = self._as_written(given)
            self.assertIn(f"Read(/{config}/.credentials.json)", deny)
            self.assertIn(f"Read(/{config}/projects/-tmp-linked/**)", deny)
            self.assertNotIn(f"Read(/{config}/projects/**)", deny)

    # Paths a leaf reads that no credential rule may take: a tool, the inputs in its temp cwd,
    # an ordinary file in the home (the target often sits there).
    def _ordinary(self, h: dict) -> list:
        return [Path("/usr/bin/env"), self.tmp.resolve() / "pdca-review-leaf" / "brief.md",
                h["real"] / "src" / "main.py"]

    def test_seed_does_not_deny_a_link_to_root_or_to_a_home_where_it_points(self) -> None:
        # (b) A link in a denied directory to `/` (`~/.ssh/r`), to the home (`~/.ssh/h`) or to
        # a directory holding it (`~/.ssh/up`) is left out, with a warning. Denied where it
        # points, it would take every file there from the leaf — its toolchain, the target,
        # the inputs in its cwd — and the credential locations it holds are denied under their
        # own names anyway. `~/.ssh` itself stays denied, and a link to an ordinary place is
        # still followed. Red on round 5's code, which put `/` in `denyRead` and wrote
        # `Read(///**)`, refusing the leaf's commands and its Read tool every file.
        h = self._fake_host()
        ssh = h["real"] / ".ssh"
        ssh.mkdir()
        (ssh / "r").symlink_to("/", target_is_directory=True)
        (ssh / "h").symlink_to(h["link"], target_is_directory=True)
        (ssh / "up").symlink_to(h["base"], target_is_directory=True)
        key = h["base"] / "keys" / "id_ed25519"
        key.parent.mkdir()
        key.write_text("not a key\n")
        (ssh / "id_ed25519").symlink_to(key)
        self._exempt("cargo xtask fdb-conformance")
        err = io.StringIO()
        with redirect_stderr(err):
            content = self._capture_sandbox_settings()["content"]
        deny_read, deny = self._deny_read(content), self._deny_rules(content)
        for target in ("/", h["real"], h["link"], h["base"]):
            with self.subTest(target=str(target)):
                self.assertNotIn(self._as_written(target), deny_read)
        for path in self._ordinary(h):
            with self.subTest(ordinary=str(path)):
                self.assertEqual(self._covering(deny, path), [])
        with self.subTest(deny_read="only the fake host and /run"):
            self.assertEqual([p for p in deny_read if not p.startswith(str(h["base"]))
                              and not p.startswith("/run/")], [])
        for home in (h["link"], h["real"]):
            self._assert_denied(content, home / ".ssh", True)
        self._assert_denied(content, key, False)            # an ordinary link is still followed
        for name in ("r", "h", "up"):
            self.assertIn(f"{ssh / name} links to", err.getvalue())

    def test_seed_writes_a_pattern_character_in_a_path_as_a_wildcard(self) -> None:
        # (b) The vendor reads a path that holds a glob character as a pattern, so in every
        # path — a home, a path a variable names, a `$KUBECONFIG` entry — each of
        # `* ? [ ] { } \` is written as `?`. Written raw, `home[1]` is a character class that
        # matches `home1`, never the real `home[1]`; written `home?1?`, the rule matches it.
        h = self._fake_host()
        odd = h["base"] / "odd"
        home = odd / "home[1]"
        (home / ".ssh").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home), "GH_CONFIG_DIR": str(odd / "gh{x}"),
                                          "KUBECONFIG": str(odd / "kube*")}):
            self._exempt("cargo xtask fdb-conformance")
            content = self._capture_sandbox_settings()["content"]
        deny_read, deny = self._deny_read(content), self._deny_rules(content)
        written = f"{self._as_written(odd)}/home?1?"
        for path, rule in ((f"{written}/.ssh", f"Read(/{written}/.ssh/**)"),
                           (f"{written}/.claude.json", f"Read(/{written}/.claude.json)"),
                           (f"{self._as_written(odd)}/gh?x?",
                            f"Read(/{self._as_written(odd)}/gh?x?/**)"),
                           (f"{self._as_written(odd)}/kube?",
                            f"Read(/{self._as_written(odd)}/kube?)")):
            with self.subTest(path=path):
                self.assertIn(path, deny_read)
                self.assertIn(rule, deny)
        self.assertIn(f"{written}/.claude", deny_read)
        self.assertIn(f"Read(/{written}/.claude/.credentials.json)", deny)
        # No path in the file carries a pattern of its own: no `[ ] { } \` anywhere, no `*`
        # in `denyRead`, and in a Read rule only the `*` of `/**` and of the session-history
        # patterns (longest first, so `…/subagents/**)` is not cut at its `/**)`).
        tails = ("/*/*/subagents/**)", "/*/memory/**)", "/*/*.jsonl)", "/**)")
        for path in deny_read:
            self.assertNotRegex(path, r"[][{}\\*]")
        for rule in deny:
            head = next((rule[:-len(t)] for t in tails if rule.endswith(t)), rule)
            self.assertNotRegex(head, r"[][{}\\*]")
        # …and as a glob the written spelling matches the real path, where the raw one misses.
        self.assertTrue(fnmatch.fnmatchcase(str(home / ".ssh"), f"{written}/.ssh"))
        self.assertFalse(fnmatch.fnmatchcase(str(home / ".ssh"), str(home / ".ssh")))

    def test_seed_writes_an_ordinary_character_in_a_path_as_it_is(self) -> None:
        # (b) Only a glob character is rewritten. A space, `@`, `+`, `~` or `,` in a home or
        # in a path a variable names is written as it is, so the vendor keeps reading the path
        # as a literal path — the handling the by-hand check (h) runs — not as a glob, whose
        # branch skips a link that does not resolve and denies a directory only where it finds
        # files in it. (An SSSD home is `/home/jdoe@corp.example`.) Red on round 2's code,
        # which wrote every such character as `?`.
        h = self._fake_host()
        odd = h["base"] / "odd"
        home = odd / "jdoe@corp.example"
        (home / ".claude" / "projects" / "-tmp-old").mkdir(parents=True)
        gh = odd / "john smith" / "gh+1~,x"
        with mock.patch.dict(os.environ, {"HOME": str(home), "GH_CONFIG_DIR": str(gh)}):
            self._exempt("cargo xtask fdb-conformance")
            content = self._capture_sandbox_settings()["content"]
        deny_read, deny = self._deny_read(content), self._deny_rules(content)
        for path, is_dir in ((home / ".ssh", True), (home / ".netrc", False),
                             (home / ".claude.json", False), (gh, True)):
            with self.subTest(path=str(path)):
                self._assert_denied(content, path, is_dir)
                self.assertIn(str(path), deny_read)                 # written as it is
        claude = self._as_written(home / ".claude")
        self.assertIn(claude, deny_read)
        self.assertIn(f"Read(/{claude}/.credentials.json)", deny)
        self.assertIn(f"Read(/{claude}/projects/-tmp-old/**)", deny)
        self.assertIn(f"Read(/{claude}/projects/*/*.jsonl)", deny)
        # Nothing under those two paths is written with a `?`.
        self.assertEqual([p for p in deny_read + deny
                          if (str(home) in p or str(gh) in p) and "?" in p], [])

    def test_seed_denies_all_of_projects_when_it_cannot_be_listed(self) -> None:
        # (b) Fail closed: a `projects` that exists but cannot be listed may hold any number
        # of other sessions' folders, none of which can be told from the leaf's own, so the
        # Read tool is denied all of it, plain and `/**` — whether `projects` is a file or a
        # directory the harness may not list (root lists one anyway, so that leg needs a
        # non-root run). The listable `projects` beside it is still denied folder by folder.
        h = self._fake_host()
        projects = h["config"] / "projects"
        shutil.rmtree(projects)
        projects.write_text("not a directory\n")
        self._exempt("cargo xtask fdb-conformance")
        cases = [("a file", self._capture_sandbox_settings()["content"])]
        if os.geteuid() != 0:
            projects.unlink()
            (projects / "-tmp-other").mkdir(parents=True)
            projects.chmod(0)
            try:
                cases.append(("a directory it may not list",
                              self._capture_sandbox_settings()["content"]))
            finally:
                projects.chmod(0o755)
        written = self._as_written(projects)
        listable = self._as_written(h["real"] / ".claude" / "projects")
        for case, content in cases:
            with self.subTest(projects=case):
                deny = self._deny_rules(content)
                self.assertIn(f"Read(/{written})", deny)
                self.assertIn(f"Read(/{written}/**)", deny)
                self.assertIn(f"Read(/{written}/*/*.jsonl)", deny)
                self.assertNotIn(f"Read(/{listable}/**)", deny)
                self.assertIn(f"Read(/{listable}/-tmp-old-session/**)", deny)

    # Where the CLI saves one of the LEAF's oversized outputs, under a config dir: a folder
    # named after the leaf's fresh temp cwd, so made only once the leaf runs.
    _LEAF_OUTPUT = "projects/-tmp-pdca-review-leaf/0b1e2c3d/tool-results/toolu_01.txt"

    def _profiles(self, h: dict) -> Path:
        """A profile layout inside the fake home's `.claude`: `profiles/work`, a config dir
        with its own login and another session's folder; `profiles/personal`, another
        profile's login; and a file beside them. Returns `profiles`."""
        profiles = h["real"] / ".claude" / "profiles"
        (profiles / "work" / "projects" / "-tmp-old-work").mkdir(parents=True)
        (profiles / "work" / "projects" / "-tmp-old-work" / "5e55.jsonl").write_text("{}\n")
        (profiles / "work" / ".credentials.json").write_text("{}\n")
        (profiles / "personal").mkdir()
        (profiles / "personal" / ".credentials.json").write_text("{}\n")
        (profiles / "notes.txt").write_text("x\n")
        return profiles

    def test_seed_leaves_a_config_dir_inside_another_to_its_own_rules(self) -> None:
        # (b) The leaf can still Read its own saved output when `$CLAUDE_CONFIG_DIR` sits
        # inside `~/.claude` — a profile layout, `~/.claude/profiles/work`: no rule of the
        # outer dir covers the inner one. `profiles` is denied entry by entry instead, so the
        # profile and the file beside `work` stay denied, and `work` keeps its own rules. The
        # same when `profiles` is a link out of `~/.claude` (its target is opened up the same
        # way), and for an inner dir in a folder of `projects`. Red on round 2's code, which
        # denied `~/.claude/profiles/**` whole.
        h = self._fake_host()
        profiles = self._profiles(h)
        self._exempt("cargo xtask fdb-conformance")
        w = self._as_written
        work = h["link"] / ".claude" / "profiles" / "work"
        # As given, and through the real home: resolved, until `profiles` is a link below.
        inner = (work, h["real"] / ".claude" / "profiles" / "work")

        def seed(config: Path) -> tuple[list, list]:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                content = self._capture_sandbox_settings()["content"]
            return self._deny_read(content), self._deny_rules(content)

        covering = self._covering
        deny_read, deny = seed(work)
        for spelling in inner:
            with self.subTest(layout="profiles a directory", inner=str(spelling)):
                self.assertEqual(covering(deny, spelling / self._LEAF_OUTPUT), [])
                self.assertIn(w(spelling), deny_read)
                self.assertIn(f"Read(/{w(spelling)}/.credentials.json)", deny)
                self.assertIn(f"Read(/{w(spelling)}/projects/-tmp-old-work/**)", deny)
                self.assertTrue(covering(deny, spelling / "projects/-tmp-old-work/5e55.jsonl"))
        for home in (h["link"], h["real"]):
            outer = w(home / ".claude" / "profiles")
            with self.subTest(layout="profiles a directory", outer=outer):
                self.assertIn(w(home / ".claude"), deny_read)
                self.assertNotIn(f"Read(/{outer}/**)", deny)
                self.assertNotIn(f"Read(/{outer}/work/**)", deny)
                self.assertIn(f"Read(/{outer}/personal/**)", deny)
                self.assertIn(f"Read(/{outer}/notes.txt)", deny)

        data = h["base"] / "data" / "profiles"         # moved to another disk, linked back
        data.parent.mkdir()
        profiles.rename(data)
        profiles.symlink_to(data, target_is_directory=True)
        deny_read, deny = seed(work)
        for spelling in (*inner, data / "work"):
            with self.subTest(layout="profiles linked out", inner=str(spelling)):
                self.assertEqual(covering(deny, spelling / self._LEAF_OUTPUT), [])
        self.assertIn(w(data), deny_read)                      # whole, to the commands
        self.assertNotIn(f"Read(/{w(data)}/**)", deny)
        self.assertIn(f"Read(/{w(data)}/personal/**)", deny)
        self.assertIn(f"Read(/{w(data)}/notes.txt)", deny)
        self.assertIn(f"Read(/{w(data)}/work/.credentials.json)", deny)
        self.assertTrue(covering(deny, data / "personal" / ".credentials.json"))

        session = h["link"] / ".claude" / "projects" / "-tmp-old-session"
        (session / "cfg" / "projects").mkdir(parents=True)
        deny_read, deny = seed(session / "cfg")
        for home in (h["link"], h["real"]):
            folder = home / ".claude" / "projects" / "-tmp-old-session"
            with self.subTest(layout="in a projects folder", folder=str(folder)):
                self.assertEqual(covering(deny, folder / "cfg" / self._LEAF_OUTPUT), [])
                self.assertNotIn(f"Read(/{w(folder)}/**)", deny)
                self.assertIn(f"Read(/{w(folder)}/f00d.jsonl)", deny)

    def test_seed_denies_a_dir_holding_a_config_dir_whole_when_it_cannot_be_listed(
            self) -> None:
        # (b) Fail closed: a directory that holds a config dir but cannot be listed (here
        # `profiles` at mode 0311, passable but not listable) has entries the policy cannot
        # name, so the Read tool is denied all of it, the inner dir's saved outputs with it.
        # The inner dir's own rules are still written. Root lists it anyway.
        if os.geteuid() == 0:
            self.skipTest("root lists a directory whatever its mode")
        h = self._fake_host()
        profiles = self._profiles(h)
        self._exempt("cargo xtask fdb-conformance")
        work = h["link"] / ".claude" / "profiles" / "work"
        profiles.chmod(0o311)
        try:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(work)}):
                deny = self._deny_rules(self._capture_sandbox_settings()["content"])
        finally:
            profiles.chmod(0o755)
        for home in (h["link"], h["real"]):
            self.assertIn(f"Read(/{self._as_written(home / '.claude' / 'profiles')}/**)", deny)
        self.assertIn(f"Read(/{self._as_written(work)}/.credentials.json)", deny)

    def test_seed_keeps_the_active_profile_clear_of_a_rule_written_for_a_sibling(
            self) -> None:
        # (b) Claude Code matches a Read rule as a pattern, case aside (2.1.322: gitignore
        # rules, matched case-insensitively), so a rule written for a SIBLING of the active
        # config dir can match the active one: `prod@us`, a name read off the disk and so
        # written `prod?us`, matches `prod-us`, and so does `PROD-US`. Denied whole, the sibling
        # would refuse the leaf the outputs it saves under `prod-us/projects`. It is denied
        # entry by entry instead, so its login and its sessions stay denied. Red on round 3's
        # code, which compared the sibling's path, not the rule written for it.
        h = self._fake_host()
        profiles = h["real"] / ".claude" / "profiles"
        (profiles / "prod-us" / "projects" / "-tmp-old-prod").mkdir(parents=True)
        (profiles / "prod-us" / ".credentials.json").write_text("{}\n")
        self._exempt("cargo xtask fdb-conformance")
        active = h["link"] / ".claude" / "profiles" / "prod-us"
        for sibling, written in (("prod@us", "prod?us"), ("PROD-US", "PROD-US")):
            with self.subTest(sibling=sibling):
                if (profiles / sibling).exists():
                    self.skipTest("a case-blind filesystem: `PROD-US` is `prod-us` there")
                (profiles / sibling / "projects" / "-tmp-old-other").mkdir(parents=True)
                (profiles / sibling / ".credentials.json").write_text("{}\n")
                try:
                    with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(active)}):
                        deny = self._deny_rules(self._capture_sandbox_settings()["content"])
                finally:
                    shutil.rmtree(profiles / sibling)
                for spelling in (active, profiles / "prod-us"):
                    self.assertEqual(self._covering(deny, spelling / self._LEAF_OUTPUT), [])
                for home in (h["link"], h["real"]):
                    other = f"{self._as_written(home / '.claude' / 'profiles')}/{written}"
                    self.assertNotIn(f"Read(/{other}/**)", deny)
                    self.assertIn(f"Read(/{other}/.credentials.json)", deny)
                    self.assertIn(f"Read(/{other}/projects/-tmp-old-other/**)", deny)

    def test_seed_keeps_a_config_dir_clear_of_a_rule_applied_through_a_link(self) -> None:
        # (b) Claude Code applies each Read rule twice: as written, and as its "physical twin",
        # the part before its first wildcard resolved through symlinks (2.1.322). Here `$HOME`
        # is a link and `$CLAUDE_CONFIG_DIR` names the real path, so a rule written for
        # `<link>/.claude/profiles` is applied to `<real home>/.claude/profiles` too, which
        # holds the active config dir. That entry is denied entry by entry by the link as well,
        # so the other profile and the file beside it stay denied. Red on round 3's code, which
        # wrote `Read(//<link>/.claude/profiles/**)`.
        h = self._fake_host()
        self._profiles(h)
        self._exempt("cargo xtask fdb-conformance")
        work = h["real"] / ".claude" / "profiles" / "work"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(work)}):
            deny = self._deny_rules(self._capture_sandbox_settings()["content"])
        for spelling in (work, h["link"] / ".claude" / "profiles" / "work"):
            with self.subTest(inner=str(spelling)):
                self.assertEqual(self._covering(deny, spelling / self._LEAF_OUTPUT), [])
        for home in (h["link"], h["real"]):
            outer = self._as_written(home / ".claude" / "profiles")
            with self.subTest(outer=outer):
                self.assertNotIn(f"Read(/{outer}/**)", deny)
                self.assertIn(f"Read(/{outer}/personal/**)", deny)
                self.assertIn(f"Read(/{outer}/notes.txt)", deny)
        self.assertIn(f"Read(/{self._as_written(work)}/.credentials.json)", deny)

    def test_seed_denies_whole_what_reaches_a_config_dir_but_cannot_be_opened_up(
            self) -> None:
        # (b) Fail closed: a path whose rule would reach the active config dir's `projects`
        # but that has no entries to deny one by one — a FILE `prod@us` (written `prod?us`)
        # beside an active `prod-us`; a `$GH_CONFIG_DIR` holding `$CLAUDE_CONFIG_DIR`, neither
        # made yet — is denied whole, the leaf's own saved outputs with it. A credential
        # location that does not exist is still listed. The second is red on round 3's code,
        # which wrote no Read rule at all for a missing directory that held a config dir.
        h = self._fake_host()
        profiles = h["real"] / ".claude" / "profiles"
        (profiles / "prod-us" / "projects").mkdir(parents=True)
        (profiles / "prod@us").write_text("not a directory\n")
        active = h["link"] / ".claude" / "profiles" / "prod-us"
        gh = h["base"] / "gh"
        self._exempt("cargo xtask fdb-conformance")
        for case, env in (("a file", {"CLAUDE_CONFIG_DIR": str(active)}),
                          ("missing", {"CLAUDE_CONFIG_DIR": str(gh / "claude"),
                                       "GH_CONFIG_DIR": str(gh)})):
            with self.subTest(case=case), mock.patch.dict(os.environ, env):
                content = self._capture_sandbox_settings()["content"]
                deny_read, deny = self._deny_read(content), self._deny_rules(content)
                if case == "a file":
                    for home in (h["link"], h["real"]):
                        other = f"{self._as_written(home / '.claude' / 'profiles')}/prod?us"
                        self.assertIn(f"Read(/{other})", deny)
                else:
                    self.assertFalse(gh.exists())
                    self.assertIn(self._as_written(gh), deny_read)
                    self.assertIn(f"Read(/{self._as_written(gh)}/**)", deny)

    # How long a seed may take before a test calls it hung. One takes milliseconds here.
    _SEED_SECONDS = 10

    def _seed_in_bounded_time(self) -> dict:
        """The seeded policy (:meth:`_capture_sandbox_settings`), failing the test when the
        seed runs past :data:`_SEED_SECONDS` instead of hanging the suite (where the platform
        has SIGALRM and this is the main thread; elsewhere it just runs). The alarm's
        exception is no `Exception` at all, so the seed's own fallback, which takes any
        `Exception`, cannot swallow it."""

        class Hung(BaseException):
            pass

        def hung(signum, frame):
            raise Hung

        try:
            previous = signal.signal(signal.SIGALRM, hung)
        except (AttributeError, ValueError):     # no SIGALRM here, or not the main thread
            return self._capture_sandbox_settings()["content"]
        signal.alarm(self._SEED_SECONDS)
        try:
            return self._capture_sandbox_settings()["content"]
        except Hung:
            self.fail(f"the seed was still running after {self._SEED_SECONDS}s")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_seed_stops_at_a_link_back_to_a_config_dir(self) -> None:
        # (b) A link inside a config dir that leads back to a config dir — `~/.claude/alias-0`
        # to `~/.claude` by its real path, `alias-1` to it as `.`, and one in
        # `$CLAUDE_CONFIG_DIR` to `~/.claude` by the symlinked home — IS that config dir,
        # known by where it resolves. It gets no rule of its own: the config dir's own rules
        # deny what it holds, and Claude Code checks a path it reads in its resolved spelling
        # too. So the seed is written, the logins stay denied, and the leaf can still read its
        # own saved outputs. Red on round 4's code, which walked the config dir again through
        # each link, without end once there were two.
        h = self._fake_host()
        claude = h["real"] / ".claude"
        (claude / "alias-0").symlink_to(claude, target_is_directory=True)
        (claude / "alias-1").symlink_to(".", target_is_directory=True)
        (h["config"] / "home-claude").symlink_to(h["link"] / ".claude",
                                                 target_is_directory=True)
        self._exempt("cargo xtask fdb-conformance")
        deny = self._deny_rules(self._seed_in_bounded_time())
        for config in (h["link"] / ".claude", claude, h["config"]):
            with self.subTest(config=str(config)):
                self.assertIn(f"Read(/{self._as_written(config)}/.credentials.json)", deny)
                self.assertEqual(self._covering(deny, config / self._LEAF_OUTPUT), [])
        for link in (claude / "alias-0", claude / "alias-1", h["link"] / ".claude" / "alias-0",
                     h["config"] / "home-claude"):
            with self.subTest(link=str(link)):
                self.assertEqual([r for r in deny if f"{self._as_written(link)}/" in r], [])
                # What it holds is denied where it resolves, which the Read tool checks too.
                self.assertTrue(self._covering(
                    deny, Path(os.path.realpath(link / ".credentials.json"))))
                self.assertTrue(self._covering(
                    deny, Path(os.path.realpath(link / "projects" / "-tmp-old-session"
                                                / "f00d.jsonl"))))

    def test_seed_stops_at_a_link_back_to_a_directory_it_is_opening_up(self) -> None:
        # (b) The same for a link back to a directory the policy is opening up entry by entry
        # rather than to a config dir: `profiles` (it holds the active config dir) linked from
        # inside itself, a `projects` linked from inside itself, and a `$GH_CONFIG_DIR` that
        # holds `$CLAUDE_CONFIG_DIR` linked from inside itself. No rule of its own, the seed is
        # written, and what each leads to stays denied under its own name. Red on round 4's
        # code, which walked each directory again through each link.
        h = self._fake_host()
        profiles = self._profiles(h)
        (profiles / "up-0").symlink_to(".", target_is_directory=True)
        (profiles / "up-1").symlink_to(profiles, target_is_directory=True)
        projects = h["real"] / ".claude" / "projects"
        (projects / "loop-0").symlink_to(".", target_is_directory=True)
        (projects / "loop-1").symlink_to(projects, target_is_directory=True)
        gh = h["base"] / "gh"
        (gh / "claude" / "projects").mkdir(parents=True)
        (gh / "hosts.yml").write_text("not a token\n")
        (gh / "loop-0").symlink_to(".", target_is_directory=True)
        (gh / "loop-1").symlink_to(gh, target_is_directory=True)
        self._exempt("cargo xtask fdb-conformance")
        work = h["link"] / ".claude" / "profiles" / "work"
        for case, config in (("profiles", work), ("gh", gh / "claude")):
            with self.subTest(case=case), mock.patch.dict(os.environ, {
                    "CLAUDE_CONFIG_DIR": str(config), "GH_CONFIG_DIR": str(gh)}):
                deny = self._deny_rules(self._seed_in_bounded_time())
                self.assertEqual(
                    [r for r in deny if "/up-" in r or "/loop-" in r], [])
                self.assertEqual(self._covering(deny, config / self._LEAF_OUTPUT), [])
                self.assertTrue(self._covering(deny, projects / "-tmp-old-session" / "f00d.jsonl"))
                if case == "profiles":
                    for home in (h["link"], h["real"]):
                        outer = self._as_written(home / ".claude" / "profiles")
                        self.assertIn(f"Read(/{outer}/personal/**)", deny)
                        self.assertNotIn(f"Read(/{outer}/**)", deny)
                else:
                    self.assertIn(f"Read(/{self._as_written(gh)}/hosts.yml)", deny)
                    self.assertNotIn(f"Read(/{self._as_written(gh)}/**)", deny)

    def test_seed_writes_no_rule_for_a_link_in_a_config_dir_to_root_or_to_a_home(self) -> None:
        # (b) A link in a config dir to `/` (`~/.claude/r`), or from a `$CLAUDE_CONFIG_DIR`
        # outside the home to the home (`h`), gets no Read rule and is not denied where it
        # points. Claude Code applies a rule again with its path resolved, so a rule for
        # `~/.claude/r` is a rule for `/`: it would refuse the leaf every file, and a walk into
        # it would deny the whole tree, or the whole home, entry by entry. What such a link
        # leads to is denied under its own names, and the Read tool checks a path in its
        # resolved spelling too. Red on round 5's code, which read a rule for `/` as reaching
        # nothing: it kept `Read(//…/.claude/r/**)`, put `/` in `denyRead`, and walked `h`.
        h = self._fake_host()
        claude = h["real"] / ".claude"
        (claude / "r").symlink_to("/", target_is_directory=True)
        (h["config"] / "h").symlink_to(h["link"], target_is_directory=True)
        (h["real"] / ".ssh").mkdir()
        self._exempt("cargo xtask fdb-conformance")
        err = io.StringIO()
        with redirect_stderr(err):
            content = self._seed_in_bounded_time()
        deny_read, deny = self._deny_read(content), self._deny_rules(content)
        for link in (h["link"] / ".claude" / "r", claude / "r", h["config"] / "h"):
            written = self._as_written(link)
            with self.subTest(link=str(link)):
                self.assertEqual([r for r in deny if f"{written}/" in r or f"{written})" in r],
                                 [])
                self.assertIn(f"{link} links to", err.getvalue())
        for target in ("/", h["real"], h["link"]):
            with self.subTest(target=str(target)):
                self.assertNotIn(self._as_written(target), deny_read)
        for path in self._ordinary(h):
            with self.subTest(ordinary=str(path)):
                self.assertEqual(self._covering(deny, path), [])
        for config in (h["link"] / ".claude", claude, h["config"]):
            with self.subTest(config=str(config)):
                self.assertIn(self._as_written(config), deny_read)
                self.assertIn(f"Read(/{self._as_written(config)}/.credentials.json)", deny)
                self.assertEqual(self._covering(deny, config / self._LEAF_OUTPUT), [])
        # What each link leads to stays denied where it resolves, which the Read tool checks.
        through_r = claude / "r" / str(claude).lstrip("/")
        for path in (through_r / ".credentials.json",
                     through_r / "projects" / "-tmp-old-session" / "f00d.jsonl",
                     h["config"] / "h" / ".ssh" / "id_ed25519",
                     h["config"] / "h" / ".claude" / ".credentials.json"):
            with self.subTest(through=str(path)):
                self.assertTrue(self._covering(deny, Path(os.path.realpath(path))))

    def test_seed_writes_a_character_above_u_ffff_in_a_name_as_two_wildcards(self) -> None:
        # (b) Claude Code matches its Read rules as JavaScript strings (2.1.322: `?` becomes
        # `[^/]`, no `u` flag), so a `?` stands for one UTF-16 code unit, and a character above
        # U+FFFF — an emoji — is two. A name read off the disk writes such a character `??`:
        # `🔑keys` is `??keys`, which the CLI matches; `?keys` is a character short, and the
        # Read tool could open the entry. Red on round 4's code, which wrote one `?` for it.
        h = self._fake_host()
        claude = h["real"] / ".claude"
        (claude / "🔑keys").mkdir()
        (claude / "🔑keys" / "k.txt").write_text("not a key\n")
        (claude / "notes-🎉.txt").write_text("x\n")
        (claude / "projects" / "-tmp-🚀").mkdir()
        (claude / "projects" / "-tmp-🚀" / "c0de.jsonl").write_text("{}\n")
        self._exempt("cargo xtask fdb-conformance")
        deny = self._deny_rules(self._capture_sandbox_settings()["content"])
        for home in (h["link"], h["real"]):
            config = self._as_written(home / ".claude")
            with self.subTest(home=str(home)):
                self.assertIn(f"Read(/{config}/??keys/**)", deny)
                self.assertIn(f"Read(/{config}/notes-??.txt)", deny)
                self.assertIn(f"Read(/{config}/projects/-tmp-??/**)", deny)
                for path in (home / ".claude" / "🔑keys" / "k.txt",
                             home / ".claude" / "notes-🎉.txt",
                             home / ".claude" / "projects" / "-tmp-🚀" / "subagents"):
                    self.assertTrue(self._covering(deny, path), path)

    def test_seed_counts_a_character_above_u_ffff_as_the_cli_does_for_a_sibling(self) -> None:
        # (b) …and the check that keeps the active config dir's `projects` clear of a rule
        # written for a SIBLING counts the same way. Beside an active `profiles/😀a`, a sibling
        # `🔑a` is written `??a`, which Claude Code matches against `😀a` too. Denied whole,
        # the sibling would refuse the leaf its own saved outputs; it is denied entry by entry
        # instead, its login and its sessions with it. Red on round 4's code.
        h = self._fake_host()
        profiles = h["real"] / ".claude" / "profiles"
        (profiles / "😀a" / "projects").mkdir(parents=True)
        (profiles / "🔑a" / "projects" / "-tmp-old-other").mkdir(parents=True)
        (profiles / "🔑a" / ".credentials.json").write_text("{}\n")
        self._exempt("cargo xtask fdb-conformance")
        active = h["link"] / ".claude" / "profiles" / "😀a"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(active)}):
            deny = self._deny_rules(self._capture_sandbox_settings()["content"])
        for spelling in (active, profiles / "😀a"):
            with self.subTest(active=str(spelling)):
                self.assertEqual(self._covering(deny, spelling / self._LEAF_OUTPUT), [])
        for home in (h["link"], h["real"]):
            other = f"{self._as_written(home / '.claude' / 'profiles')}/??a"
            with self.subTest(sibling=other):
                self.assertNotIn(f"Read(/{other}/**)", deny)
                self.assertIn(f"Read(/{other}/.credentials.json)", deny)
                self.assertIn(f"Read(/{other}/projects/-tmp-old-other/**)", deny)
                self.assertTrue(self._covering(
                    deny, home / ".claude" / "profiles" / "🔑a" / ".credentials.json"))

    def test_seed_denies_the_password_database_home_when_home_is_unusable_or_differs(
            self) -> None:
        # (b) Home spelling: when `$HOME` is empty, `/`, relative, or another directory than
        # the password database's, that home is denied too.
        h = self._fake_host()
        self._exempt("cargo xtask fdb-conformance")
        elsewhere = h["base"] / "elsewhere"
        elsewhere.mkdir()
        for home in ("", "/", "relative/home", str(elsewhere)):
            with self.subTest(HOME=home), mock.patch.dict(os.environ, {"HOME": home}):
                content = self._capture_sandbox_settings()["content"]
                deny_read = self._deny_read(content)
                self._assert_denied(content, h["real"] / ".ssh", True)
                self._assert_denied(content, h["real"] / ".config" / "gh", True)
                self.assertIn(self._as_written(h["real"] / ".claude"), deny_read)
                self.assertTrue(all(os.path.isabs(p) for p in deny_read))
                if home == str(elsewhere):
                    self._assert_denied(content, elsewhere / ".ssh", True)

    def test_seed_unsets_the_credential_variables_and_masks_nothing(self) -> None:
        # (c) Each named variable has a `mode: "deny"` entry. (d) Nothing anywhere in the
        # file is masked, and no `tlsTerminate` is written: a masked token still leaves with
        # the leaf's traffic.
        self._fake_host(GH_TOKEN="canary-not-a-secret")
        self._exempt("cargo xtask fdb-conformance")
        self._project_settings({"sandbox": {"network": {"allowedDomains": ["github.com"]}}})
        content = self._capture_sandbox_settings()["content"]
        entries = self._env_entries(content)
        self.assertLessEqual(set(self._DENIED_VARS),
                             {e["name"] for e in entries if e.get("mode") == "deny"})
        found: list = []

        def walk(node) -> None:
            if isinstance(node, dict):
                found.extend(k for k in node if k == "tlsTerminate")
                found.extend(["mask"] if node.get("mode") == "mask" else [])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(content)
        self.assertEqual(found, [])
        self.assertNotIn("canary-not-a-secret", json.dumps(content))

    def test_network_only_seed_carries_the_same_deny(self) -> None:
        # (j) Only a network grant, no exemption: the seed is written as today and carries
        # the same deny blocks. It still turns nothing on and adds no `--setting-sources`
        # on this code (#936 changes that) — the deny rides beside, it decides nothing.
        h = self._fake_host()
        self.cfg.reviewer = LeafConfig(mode="stub", family="claude")
        self._project_settings({"sandbox": {"network": {"allowedDomains": ["github.com"]}}})
        seen = self._capture_sandbox_settings()
        content = seen["content"]
        self.assertEqual(self._grants(content),
                         {"sandbox": {"network": {"allowedDomains": ["github.com"]}}})
        self._assert_denied(content, h["link"] / ".config" / "gh", True)
        self._assert_denied(content, h["real"] / ".ssh", True)
        self.assertIn(self._as_written(h["config"]), self._deny_read(content))
        self.assertIn("Read(//proc/**)", self._deny_rules(content))
        self.assertLessEqual(set(self._DENIED_VARS),
                             {e["name"] for e in self._env_entries(content)})
        self.assertNotIn("enabled", content["sandbox"])
        self.assertNotIn("--setting-sources", seen["extra_argv"])

    def test_grants_still_merge_beside_the_deny(self) -> None:
        # (f) `excludedCommands` and the network grants land exactly as before, beside the
        # deny blocks; nothing granted still writes no file at all.
        self._fake_host()
        self._exempt("cargo xtask fdb-conformance")
        self._project_settings({"sandbox": {"network": {
            "allowLocalBinding": True, "allowedDomains": ["api.github.com"]}}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(self._grants(seen["content"]), {"sandbox": {
            "network": {"allowLocalBinding": True, "allowedDomains": ["api.github.com"]},
            "enabled": True,
            "excludedCommands": ["cargo xtask fdb-conformance"],
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True}})
        self.assertIn("filesystem", seen["content"]["sandbox"])
        self.assertEqual(seen["extra_argv"][seen["extra_argv"].index("--setting-sources") + 1],
                         "project")
        self.cfg.leaf_unsandboxed_commands = []
        (self.cfg.root / ".claude" / "settings.json").unlink()
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_never_seeds_excluded_commands_or_the_whole_sandbox_block(self) -> None:
        # PR #268 review (codex): docs 05 tells a project to use `sandbox.excludedCommands`
        # as the workaround for its GATES. Copying the whole `sandbox` object would carry
        # that into the reviewer's cwd, letting it run the excluded command OUTSIDE the
        # sandbox — a capability its `tools:` grant never gave it. The seed is an ALLOW-LIST
        # of named network keys, so `excludedCommands` / `enabled` can never ride along.
        #
        # #277 deliberately ADDED `allowedDomains` to that allow-list (the reviewer's
        # prior-art check needs api.github.com), so it is now carried — by name, not by a
        # loosened copy. The exclusions below are the part that must never change.
        self._project_settings({"sandbox": {
            "enabled": True,
            "excludedCommands": ["cargo *"],
            "network": {"allowLocalBinding": True, "allowedDomains": ["api.github.com"]},
        }})
        seen = self._capture_sandbox_settings()
        self.assertEqual(self._grants(seen["content"]), {"sandbox": {"network": {
            "allowLocalBinding": True, "allowedDomains": ["api.github.com"]}}})
        # the load-bearing exclusions
        self.assertNotIn("excludedCommands", seen["content"]["sandbox"])
        self.assertNotIn("enabled", seen["content"]["sandbox"])
        self.assertNotIn("permissions", self._grants(seen["content"]))

    def test_network_grant_is_seeded_for_the_prior_art_check(self) -> None:
        # #277: the reviewer's prior-art check needs the closed/rejected-PR corpus via
        # `gh pr list --state closed` → api.github.com. Without the grant it can never be
        # settled mechanically and is forced NEEDS-HUMAN on every bundle.
        self._project_settings({"sandbox": {"network": {
            "allowedDomains": ["github.com", "api.github.com"]}}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(seen["content"]["sandbox"]["network"]["allowedDomains"],
                         ["github.com", "api.github.com"])

    def test_an_empty_domain_list_is_off_and_seeds_nothing(self) -> None:
        # This is how the grant ships: documented in settings.json, but OFF. An empty list
        # must not create a seeded file, or it would look like a configured (empty) policy.
        self._project_settings({"sandbox": {"network": {"allowedDomains": []}}})
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_the_shipped_default_seeds_only_the_loopback_grant(self) -> None:
        # The template's own .claude/settings.json: loopback ON (#261), domains OFF (#277).
        self._project_settings({"sandbox": {"network": {
            "allowLocalBinding": True, "allowedDomains": []}}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(self._grants(seen["content"]),
                         {"sandbox": {"network": {"allowLocalBinding": True}}})

    def test_denied_domains_are_carried(self) -> None:
        self._project_settings({"sandbox": {"network": {"deniedDomains": ["evil.example"]}}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(seen["content"]["sandbox"]["network"]["deniedDomains"],
                         ["evil.example"])

    def test_a_non_list_domain_grant_seeds_nothing(self) -> None:
        self._project_settings({"sandbox": {"network": {"allowedDomains": "github.com"}}})
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_named_conformance_command_is_exempted_from_the_sandbox(self) -> None:
        # #276: a Docker-backed conformance gate is denied the docker socket inside the leaf
        # sandbox even on a Docker-capable host, so its evidence always defers to a human.
        # Naming the command exempts THAT COMMAND — not the socket, not the whole leaf.
        self._exempt("cargo xtask fdb-conformance")
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": True}}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(seen["content"]["sandbox"]["excludedCommands"],
                         ["cargo xtask fdb-conformance"])

    def test_the_exemption_list_comes_from_pdca_toml_not_the_project_settings(self) -> None:
        # THE load-bearing distinction (#268 doctrine, preserved). The project's own
        # `sandbox.excludedCommands` is its GATE workaround — inheriting it would let the leaf
        # run whatever the operator exempted for CI. A leaf's exemption is declared in
        # pdca.toml, deliberately, and nowhere else.
        self.cfg.leaf_unsandboxed_commands = []                       # harness grants nothing
        self._project_settings({"sandbox": {
            "excludedCommands": ["docker *", "rm -rf /"],             # the operator's own list
            "network": {"allowLocalBinding": True},
        }})
        seen = self._capture_sandbox_settings()
        self.assertNotIn("excludedCommands", seen["content"]["sandbox"])

    def test_the_harness_list_does_not_merge_with_the_project_list(self) -> None:
        # Even when BOTH exist, only the harness-declared commands reach the leaf.
        self._exempt("cargo xtask fdb-conformance")
        self._project_settings({"sandbox": {"excludedCommands": ["docker *"]}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(seen["content"]["sandbox"]["excludedCommands"],
                         ["cargo xtask fdb-conformance"])

    def test_no_exemptions_by_default(self) -> None:
        # Off unless declared: an instance that names nothing gets a fully sandboxed leaf.
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": True}}})
        seen = self._capture_sandbox_settings()
        self.assertNotIn("excludedCommands", seen["content"]["sandbox"])

    def test_the_exemption_is_seeded_without_any_project_settings(self) -> None:
        # PR #288 review (codex). The exemption list is HARNESS-owned (pdca.toml), so it must
        # not depend on the project having a `.claude/settings.json` at all. Gating it on that
        # file made the documented Docker exemption silently do nothing for an instance
        # without one — the leaf stayed sandboxed and still deferred to a human confirmer.
        self._exempt("cargo xtask fdb-conformance")
        self.assertFalse((self.cfg.root / ".claude" / "settings.json").is_file())
        seen = self._capture_sandbox_settings()
        self.assertEqual(self._grants(seen["content"]), {"sandbox": {
            "enabled": True,
            "excludedCommands": ["cargo xtask fdb-conformance"],
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True}})

    def test_an_unparseable_settings_file_still_seeds_the_exemption(self) -> None:
        # A corrupt settings.json costs the NETWORK grant and nothing else.
        cdir = self.cfg.root / ".claude"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "settings.json").write_text("{ not json", encoding="utf-8")
        self._exempt("cargo xtask fdb-conformance")
        buf = io.StringIO()
        with redirect_stderr(buf):
            seen = self._capture_sandbox_settings()      # must NOT raise
        self.assertEqual(seen["content"]["sandbox"]["excludedCommands"],
                         ["cargo xtask fdb-conformance"])
        self.assertNotIn("network", seen["content"]["sandbox"])   # …but no network grant
        self.assertIn("no network grant", buf.getvalue())

    def test_nothing_granted_still_seeds_nothing(self) -> None:
        # The independence must not become "always write a file": no settings, no exemption.
        self.cfg.leaf_unsandboxed_commands = []
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_an_exemption_alone_is_enough_to_seed(self) -> None:
        # No network grant at all, but a named command ⇒ the settings file is still written.
        self._exempt("cargo xtask tikv-conformance")
        self._project_settings({"permissions": {"allow": ["Read"]}})
        seen = self._capture_sandbox_settings()
        self.assertEqual(self._grants(seen["content"]), {"sandbox": {
            "enabled": True,
            "excludedCommands": ["cargo xtask tikv-conformance"],
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True}})

    def test_the_exemption_list_is_a_ceiling_not_a_floor(self) -> None:
        """PR #288 review (codex). An exemption LIST does not bound what escapes the sandbox.
        Claude Code's `allowUnsandboxedCommands` defaults to TRUE (settings schema, v2.1.207:
        `sandbox?.allowUnsandboxedCommands ?? true`), and while true the model may retry ANY
        sandbox-denied command with `dangerouslyDisableSandbox` and have it run unconfined. So
        seeding only `excludedCommands` left the named list a floor: "only these commands run
        outside the sandbox" — which pdca.toml, docs 05 and the seed's own docstring all
        promise — was simply not true. Setting it false makes `dangerouslyDisableSandbox`
        "completely ignored" (the schema's own words), so the list becomes the only way out."""
        self._exempt("cargo xtask fdb-conformance")
        seen = self._capture_sandbox_settings()
        self.assertIs(seen["content"]["sandbox"]["allowUnsandboxedCommands"], False)

    def test_an_exempted_leaf_is_confined_to_the_harnesss_own_settings(self) -> None:
        """PR #288 review (codex), the second hole — and the one no seeded file can close.

        Array-valued settings CONCATENATE across scopes (user → project → local → managed):
        the CLI folds each scope through a merge customizer that unions any two arrays, and the
        union is MONOTONIC — no scope can remove what a lower one added. So a maintainer whose
        own `~/.claude/settings.json` exempts `docker *` for their INTERACTIVE use has that
        merged straight into the leaf, and the harness's list is a floor, not the promised
        ceiling. Since nothing we WRITE can subtract, the only fix is to stop the lower scope
        loading at all: `--setting-sources project`.
        """
        self._exempt("cargo xtask fdb-conformance")
        seen = self._capture_sandbox_settings()
        argv = seen["extra_argv"]
        self.assertIn("--setting-sources", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")

    def test_a_failed_seed_never_leaves_the_leaf_unsandboxed(self) -> None:
        """PR #290 review (codex). The seed's write is best-effort — but the confinement flag
        was NOT, so a failed write produced the worst possible outcome.

        `_seed_sandbox_settings` caught the OSError, warned "the leaf runs under the ambient
        sandbox policy", and carried on. The caller then still passed `--setting-sources
        project`, so the leaf loaded ONLY project scope — which is the file that had just failed
        to be written. No `sandbox.enabled` (it defaults FALSE), and the operator's own
        user-scope sandbox dropped along with it: the leaf ran COMPLETELY unconfined, under a
        message asserting the exact opposite.

        Now the flag is withheld, so the leaf keeps the operator's ambient sandbox and the
        exemption simply does not happen. Degrade the FEATURE, never the BOUNDARY.
        """
        self._exempt("cargo xtask fdb-conformance")
        real_write = Path.write_text

        def enospc(self, *a, **k):          # only the SEED's write fails — the fixture's don't
            if self.name == "settings.json":
                raise OSError("ENOSPC")
            return real_write(self, *a, **k)

        buf = io.StringIO()
        with mock.patch.object(Path, "write_text", enospc), redirect_stderr(buf):
            seen = self._capture_sandbox_settings()
        self.assertFalse(seen["exists"])                            # nothing on disk…
        self.assertNotIn("--setting-sources", seen["extra_argv"])   # …so don't drop the ambient
        self.assertIn("did NOT take effect", buf.getvalue())

    def test_a_seed_that_fails_while_building_the_deny_takes_the_same_fallback(self) -> None:
        # (f) Building the credential deny (#935) walks the operator's home and config dirs,
        # so more than the write can fail, and not only with an OSError: a RecursionError
        # from a deep walk, say. That is a failed seed like a failed write — nothing on disk,
        # `--setting-sources` withheld, the operator's ambient sandbox kept, and the Check goes
        # on. Red on round 5's code, which caught only OSError, so the error ended the Check.
        self._fake_host()
        self._exempt("cargo xtask fdb-conformance")
        buf = io.StringIO()
        # `create=True`: the red leg runs this against code with no deny builder at all.
        with mock.patch.object(leaves, "_entries", create=True,
                               side_effect=RecursionError("maximum recursion depth exceeded")), \
                redirect_stderr(buf):
            try:
                seen = self._capture_sandbox_settings()
            except RecursionError:
                self.fail("a failure while building the deny escaped the seed: the Check ended")
        self.assertFalse(seen["exists"])
        self.assertNotIn("--setting-sources", seen["extra_argv"])
        self.assertIn("RecursionError", buf.getvalue())
        self.assertIn("did NOT take effect", buf.getvalue())

    def test_an_exempted_leaf_actually_has_a_sandbox_to_be_bounded_by(self) -> None:
        """PR #290 review (codex). Without this key the whole feature was worse than useless.

        `sandbox.enabled` DEFAULTS TO FALSE (`sandbox?.enabled ?? false`), and
        `failIfUnavailable` is gated on it (`enabled && … && failIfUnavailable`). Worse:
        `--setting-sources project` (#288) drops the user/local scope — exactly where an
        operator's `sandbox.enabled: true` lives. So BOUNDING the exemption was REMOVING the
        sandbox it claims to bound: the leaf ran fully unconfined, every command escaped, and
        the fail-closed guard never fired — while pdca.toml and docs 05 promised a boundary.

        Verified end-to-end: with these keys but no `enabled`, a leaf starts silently on a
        socat-less host; with it, it refuses — "sandbox required but unavailable … refusing to
        start without a working sandbox".
        """
        self._exempt("cargo xtask fdb-conformance")
        seen = self._capture_sandbox_settings()
        self.assertIs(seen["content"]["sandbox"]["enabled"], True)

    def test_a_leaf_with_no_exemption_gets_no_sandbox_it_never_asked_for(self) -> None:
        # `enabled` rides WITH the exemption, like the rest: an instance that grants none keeps
        # whatever ambient sandbox policy it already had.
        self.cfg.leaf_unsandboxed_commands = []
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": True}}})
        seen = self._capture_sandbox_settings()
        self.assertNotIn("enabled", seen["content"]["sandbox"])

    def test_an_exempted_leaf_refuses_to_run_unsandboxed(self) -> None:
        """Issue #289 — the hole that swallows the other two whole.

        Claude Code's sandbox does NOT fail closed. With `sandbox.enabled` true and a
        dependency missing (observed: `socat`), it DISABLES the sandbox, warns, and runs every
        command unconfined. A bounded exemption on top of no sandbox at all is not bounded — it
        is nothing, while pdca.toml and docs 05 both promise the leaf is confined to the named
        commands. `failIfUnavailable` makes the leaf REFUSE to start instead ("Exit with an
        error at startup if sandbox.enabled is true but the sandbox cannot start").
        """
        self._exempt("cargo xtask fdb-conformance")
        seen = self._capture_sandbox_settings()
        self.assertIs(seen["content"]["sandbox"]["failIfUnavailable"], True)

    def test_a_leaf_with_no_exemption_is_not_forced_to_fail(self) -> None:
        # Rides WITH the exemption: an instance granting none keeps the ambient behaviour and
        # is not made to hard-fail on a sandbox it never asked for.
        self.cfg.leaf_unsandboxed_commands = []
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": True}}})
        seen = self._capture_sandbox_settings()
        self.assertNotIn("failIfUnavailable", seen["content"]["sandbox"])
    def test_a_codex_leaf_gets_the_network_grant_that_reaches_docker(self) -> None:
        """Issue #291. #276's whole goal — a Docker-gated conformance leg earning its green at
        Check — was UNMET for codex, the harness's DEFAULT reviewer and the family whose
        NEEDS-HUMAN notes #276 quotes. It got a refusal and a docs line saying it was "out of
        harness scope". Both were wrong: it is one config flag.

        Verified on codex-cli 0.142.3, on a Docker-capable host: `--sandbox workspace-write`
        alone denies `docker ps`; adding `sandbox_workspace_write.network_access=true` yields
        `server=29.6.1`. The denial is codex's seccomp/network layer, NOT the filesystem — a
        relayed socket in a granted writable dir is refused too — so no path grant can fix it.
        The filesystem stays confined either way (a write outside the workspace is denied).
        """
        self.cfg.reviewer = LeafConfig(mode="stub", family="codex")
        self.cfg.leaf_network_access = True
        argv = self._capture_sandbox_settings()["extra_argv"]
        self.assertIn("-c", argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)

    def test_no_network_grant_by_default(self) -> None:
        # Opt-in: it opens the socket/network layer for EVERY command in the leaf, so it never
        # rides along with anything else.
        self.cfg.reviewer = LeafConfig(mode="stub", family="codex")
        argv = self._capture_sandbox_settings()["extra_argv"]
        self.assertNotIn("sandbox_workspace_write.network_access=true", argv)

    def test_the_network_grant_does_not_ride_on_the_command_exemption(self) -> None:
        # THE distinction (#291). `unsandboxed_commands` promises "only these commands leave the
        # sandbox". codex's grant frees the network for EVERY command, so honouring that key
        # with this flag would silently break the promise. Separate keys, separate opt-ins.
        self.cfg.reviewer = LeafConfig(mode="stub", family="codex")
        self.cfg.leaf_unsandboxed_commands = ["cargo xtask fdb-conformance"]  # NOT network
        buf = io.StringIO()
        with redirect_stderr(buf):
            argv = self._capture_sandbox_settings()["extra_argv"]
        self.assertNotIn("sandbox_workspace_write.network_access=true", argv)
        self.assertIn("network_access", buf.getvalue())   # …but the refusal names the fix

    def test_the_refusal_warning_states_the_posture_the_leaf_actually_gets(self) -> None:
        """PR #292 review (local codex pass). With BOTH keys set on codex, the per-command
        exemption is correctly refused — but the same run then appends the network grant. The
        warning still said "The leaf stays fully sandboxed", which was simply false; a warning
        that misstates the active security posture is worse than no warning at all."""
        self.cfg.reviewer = LeafConfig(mode="stub", family="codex")
        self.cfg.leaf_unsandboxed_commands = ["cargo xtask fdb-conformance"]   # refused…
        self.cfg.leaf_network_access = True                                    # …but this IS on
        buf = io.StringIO()
        with redirect_stderr(buf):
            argv = self._capture_sandbox_settings()["extra_argv"]
        self.assertIn("sandbox_workspace_write.network_access=true", argv)   # the grant is live
        warning = buf.getvalue()
        self.assertNotIn("stays fully sandboxed", warning)     # …so it must not claim otherwise
        self.assertIn("socket/network layer IS open", warning)

    def test_claude_does_not_take_the_blanket_network_grant(self) -> None:
        # claude scopes network by DOMAIN (`allowedDomains`, #277), which is strictly better
        # where it exists — so it must not also get codex's blanket opener.
        self.cfg.reviewer = LeafConfig(mode="stub", family="claude")
        self.cfg.leaf_network_access = True
        argv = self._capture_sandbox_settings()["extra_argv"]
        self.assertNotIn("sandbox_workspace_write.network_access=true", argv)

    def test_a_leaf_with_no_exemption_is_not_confined(self) -> None:
        # The confinement rides WITH the exemption. An instance that grants none keeps today's
        # behaviour — its leaves still see the operator's settings, exactly as before.
        self.cfg.leaf_unsandboxed_commands = []
        seen = self._capture_sandbox_settings()
        self.assertNotIn("--setting-sources", seen["extra_argv"])

    def test_a_family_that_cannot_be_bounded_is_refused_the_exemption(self) -> None:
        # Fail closed. codex has no way to be confined to the harness's settings (it does not
        # read them at all), so an exemption there could only ever be unbounded. Refuse it and
        # say why, rather than grant a boundary that does not hold.
        self.cfg.reviewer = LeafConfig(mode="stub", family="codex")
        self.cfg.leaf_unsandboxed_commands = ["cargo xtask fdb-conformance"]
        buf = io.StringIO()
        with redirect_stderr(buf):
            seen = self._capture_sandbox_settings()
        self.assertFalse(seen["exists"])                     # nothing granted at all
        self.assertIn("NOT granted", buf.getvalue())

    def test_a_leaf_with_no_exemption_is_not_silently_hardened(self) -> None:
        # The escape hatch is closed only ALONGSIDE a list. An instance that grants no
        # exemption keeps the ambient default rather than having policy imposed on it.
        self.cfg.leaf_unsandboxed_commands = []
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": True}}})
        seen = self._capture_sandbox_settings()
        self.assertNotIn("allowUnsandboxedCommands", seen["content"]["sandbox"])

    def test_the_socket_wide_grant_is_never_seeded(self) -> None:
        # We deliberately do NOT ship `allowAllUnixSockets`: it would let ANY command the leaf
        # runs reach the docker socket, and a root-owned daemon (the common setup) is
        # root-adjacent. It is not in the allow-list, so configuring it cannot reach a leaf.
        self._project_settings({"sandbox": {
            "allowAllUnixSockets": True,
            "network": {"allowLocalBinding": True},
        }})
        seen = self._capture_sandbox_settings()
        self.assertNotIn("allowAllUnixSockets", seen["content"]["sandbox"])

    def test_a_false_grant_is_carried_faithfully(self) -> None:
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": False}}})
        seen = self._capture_sandbox_settings()
        self.assertIs(seen["content"]["sandbox"]["network"]["allowLocalBinding"], False)

    def test_no_sandbox_key_seeds_nothing(self) -> None:
        # An instance that configures no sandbox is unaffected: no settings file is written.
        self._project_settings({"permissions": {"allow": ["Read"]}})
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_sandbox_block_without_the_grant_seeds_nothing(self) -> None:
        # A project that configures a sandbox but not loopback binding gets no seeded file:
        # there is nothing this fix needs to carry, so the leaf keeps the ambient policy.
        self._project_settings({"sandbox": {"enabled": True, "excludedCommands": ["cargo *"]}})
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_non_boolean_grant_seeds_nothing(self) -> None:
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": "yes"}}})
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_absent_project_settings_seeds_nothing(self) -> None:
        self.assertFalse(self._capture_sandbox_settings()["exists"])

    def test_malformed_project_settings_does_not_abort_check(self) -> None:
        # Best-effort, like the agent seeding: a corrupt settings.json degrades to a no-op.
        # (With no pdca.toml exemption declared there is nothing else to grant, so no file is
        # written at all. The exemption's independence from this file is pinned separately.)
        cdir = self.cfg.root / ".claude"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "settings.json").write_text("{ not json", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stderr(buf):
            seen = self._capture_sandbox_settings()          # must NOT raise
        self.assertFalse(seen["exists"])
        self.assertIn("could not read sandbox settings", buf.getvalue())

    def test_advisory_sandbox_seeds_settings_too(self) -> None:
        # The advisory leaves hit the same bind wall as the main reviewer (#261 names
        # advisory explicitly), so they get the same policy.
        self._fake_host()            # the seed now lists credential locations (#935)
        self._project_settings({"sandbox": {"network": {"allowLocalBinding": True}}})
        (self.d / "patch.diff").write_text("x\n", encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}\n", encoding="utf-8")
        seen: dict = {}
        orig = leaves._invoke

        def capture(leaf, workdir, prompt, **k):
            f = Path(workdir) / ".claude" / "settings.json"
            seen["content"] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
            (Path(workdir) / "check-advisory-lens.md").write_text("ok\n", encoding="utf-8")

        leaves._invoke = capture
        try:
            leaves._run_advisory_sandboxed(
                self.d, self.cfg, LeafConfig(mode="command", family="claude"),
                {"id": "lens", "role": "a lens"}, "lens")
        finally:
            leaves._invoke = orig
        self.assertIs(seen["content"]["sandbox"]["network"]["allowLocalBinding"], True)
        # …and the advisory leaf's seed carries the same credential deny (#935).
        self._assert_denied(seen["content"], self._host["real"] / ".ssh", True)
        self.assertIn("Read(//proc/**)", self._deny_rules(seen["content"]))


class AdvisoryReviewers(unittest.TestCase):
    """Optional advisory reviewer leaves (issue #64): an open, role-distinct set that
    write check-advisory-<id>.md and route NEEDS-HUMAN into §6; conditioned by `when`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("ADV")
        self.d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_advisory_leaf_runs_and_routes_to_section6(self) -> None:
        self.cfg.advisory_leaves = [{"id": "code-review", "role": "bugs+cleanups", "mode": "stub"}]
        driver.run_issue(self.d, self.cfg)
        self.assertTrue((self.d / "check-advisory-code-review.md").exists())
        summary = self.d / "SUMMARY.md"
        self.assertIn("Advisory — code-review", summary.read_text(encoding="utf-8"))  # §5
        items = signoff.open_needs_human(summary)  # the advisory NEEDS-HUMAN → §6
        self.assertTrue(any("advisory" in it.lower() for it in items))

    def test_when_condition_skips_non_matching(self) -> None:
        self.cfg.advisory_leaves = [{"id": "deep", "role": "x", "mode": "stub",
                                     "when": {"field": "review depth", "substring": "deep"}}]
        driver.run_issue(self.d, self.cfg)  # toy brief has no "Review depth" → skipped
        self.assertFalse((self.d / "check-advisory-deep.md").exists())

    def test_when_condition_runs_on_match(self) -> None:
        bp = self.d / "brief.md"
        bp.write_text(bp.read_text() + "\n- **Review depth:** deep\n", encoding="utf-8")
        self.cfg.advisory_leaves = [{"id": "deep", "role": "x", "mode": "stub",
                                     "when": {"field": "review depth", "substring": "deep"}}]
        driver.run_issue(self.d, self.cfg)
        self.assertTrue((self.d / "check-advisory-deep.md").exists())

    def test_iterate_archives_advisory_artifact(self) -> None:
        self.cfg.advisory_leaves = [{"id": "code-review", "role": "x", "mode": "stub"}]
        driver.run_issue(self.d, self.cfg)
        signoff.record(self.d / "SUMMARY.md", action="iterate-do", by="t", date="2026-01-01")
        driver.advance(self.d, self.cfg)  # archive the attempt
        self.assertFalse((self.d / "check-advisory-code-review.md").exists())
        self.assertTrue((self.d / "iteration-v1" / "check-advisory-code-review.md").exists())


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
