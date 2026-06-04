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

from pdca_harness import act, driver, gates, queue, leaves, signoff, state
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

    def test_iterate_to_do_clears_downstream(self) -> None:
        driver.run_issue(self.d, self.cfg)
        summary = self.d / "SUMMARY.md"
        signoff.record(summary, action="iterate-do", by="tester", date="2026-01-01")
        self.assertEqual(state.state(self.d), state.ITERATE_DO)
        driver.advance(self.d, self.cfg)  # performs the clear
        self.assertEqual(state.state(self.d), state.PLANNED)
        self.assertFalse((self.d / "patch.diff").exists())
        self.assertFalse((self.d / "test_toy.py").exists())
        self.assertTrue((self.d / "brief.md").exists())

    def test_iterate_to_plan_versions_brief(self) -> None:
        driver.run_issue(self.d, self.cfg)
        summary = self.d / "SUMMARY.md"
        signoff.record(summary, action="iterate-plan", by="tester", date="2026-01-01")
        driver.advance(self.d, self.cfg)
        self.assertEqual(state.state(self.d), state.UNPLANNED)
        self.assertTrue((self.d / "brief.v1.md").exists())
        self.assertFalse((self.d / "brief.md").exists())


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
        driver.run_issue(live, self.cfg)  # parks at AWAITING_SIGNOFF
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
