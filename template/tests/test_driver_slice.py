"""End-to-end vertical slice for the PDCA driver (stdlib unittest — no deps).

Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
Exercises the full control flow on the toy brief with stub leaves/gates:
init → Do → gates → reviewer → assembled SUMMARY → human sign-off → COMPLETE,
plus the C6 accept-gate, the independence contract, and an iterate transition.
"""

from __future__ import annotations

import io
import json
import shutil
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
        """Run the sandboxed reviewer, returning what landed at <sandbox>/.claude/settings.json."""
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
        self.assertNotIn("permissions", seen["content"])

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
        self.assertEqual(seen["content"], {"sandbox": {"network": {
            "allowLocalBinding": True, "allowedDomains": ["api.github.com"]}}})
        # the load-bearing exclusions
        self.assertNotIn("excludedCommands", seen["content"]["sandbox"])
        self.assertNotIn("enabled", seen["content"]["sandbox"])
        self.assertNotIn("permissions", seen["content"])

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
        self.assertEqual(seen["content"], {"sandbox": {"network": {"allowLocalBinding": True}}})

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
        self.assertEqual(seen["content"], {"sandbox": {
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
        self.assertEqual(seen["content"], {"sandbox": {
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
