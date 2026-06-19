"""Offline slice for `pdca revalidate` — re-gate a frozen bundle (stdlib unittest).

Proves the issue-#11 contract: revalidate re-runs the single-sourced gates against the
current engine, writes an additive dated stamp, NEVER mutates the frozen
check-gates.json / check-gates.md / §9, refuses a non-COMPLETE bundle, reports a changed
row in either direction, and surfaces deltas where Act looks. Deterministic real gates
(`true` / `false`) flip a gate's result between freeze and revalidate with no Claude /
Docker. Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import act, cli, gates, revalidate, state
from pdca_harness.config import Config, LeafConfig

# A real bundle-scoped gate keyed on a stable (element, rule_id, check) so a frozen row
# and a fresh row line up; only the cmd's exit code (true/false) decides pass/fail.
_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle", "gating": True}
_PASS = {**_GATE, "cmd": "true"}
_FAIL = {**_GATE, "cmd": "false"}


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
        planner=LeafConfig(mode="stub", family="claude", interactive=True),
        signoff=LeafConfig(mode="stub", family="claude", interactive=True),
        publisher=LeafConfig(mode="stub", family="claude", interactive=True),
        act=LeafConfig(mode="stub", family="claude", interactive=True),
    )


class Revalidate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _complete_bundle(self, iid: str, *, frozen_gate: dict) -> Path:
        """A COMPLETE (frozen) bundle whose check-gates.json was written by `frozen_gate`."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** reval\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.cfg.gates_checks = [frozen_gate]
        gates.run_gates(d, self.cfg)  # writes the frozen check-gates.json / .md
        (d / "SUMMARY.md").write_text(
            "## 9. Check sign-off\n- Outcome: accepted\n- By / date: t / 2026-06-04\n",
            encoding="utf-8")
        self.assertEqual(state.state(d), state.COMPLETE)
        return d

    def test_stamp_written_and_frozen_files_untouched(self) -> None:
        # Frozen FAIL (old engine); the current engine PASSes. Revalidate records the
        # delta but leaves the frozen record byte-for-byte intact.
        d = self._complete_bundle("REVAL", frozen_gate=_FAIL)
        before = {name: (d / name).read_bytes()
                  for name in ("check-gates.json", "check-gates.md", "SUMMARY.md")}
        self.cfg.gates_checks = [_PASS]  # engine since fixed
        result = revalidate.revalidate(self.cfg, d, "2026-06-12")

        self.assertTrue((d / "revalidation-2026-06-12.json").exists())
        for name, blob in before.items():
            self.assertEqual((d / name).read_bytes(), blob,
                             f"revalidate must not touch the frozen {name}")
        self.assertTrue(result["changed"])
        self.assertFalse(result["regression"])  # FAIL→PASS is a stale artifact, not a regression
        c4 = next(r for r in result["rows"] if r["element"] == "C4")
        self.assertEqual((c4["old"], c4["new"]), ("fail", "pass"))

    def test_regression_when_frozen_pass_now_fails(self) -> None:
        # Frozen PASS; the current engine FAILs the same gate — the load-bearing signal.
        d = self._complete_bundle("REG", frozen_gate=_PASS)
        self.cfg.gates_checks = [_FAIL]
        result = revalidate.revalidate(self.cfg, d, "2026-06-12")
        self.assertTrue(result["changed"])
        self.assertTrue(result["regression"])
        c4 = next(r for r in result["rows"] if r["element"] == "C4")
        self.assertEqual((c4["old"], c4["new"]), ("pass", "fail"))

    def test_unchanged_is_a_quiet_confirmation(self) -> None:
        # Same gate result at freeze and now → no delta; the CLI exits 0.
        d = self._complete_bundle("SAME", frozen_gate=_PASS)
        self.cfg.gates_checks = [_PASS]
        rc = cli._revalidate(self.cfg, SimpleNamespace(issue_id="SAME", date="2026-06-12"))
        self.assertEqual(rc, 0)
        stamp = json.loads((d / "revalidation-2026-06-12.json").read_text(encoding="utf-8"))
        self.assertFalse(stamp["changed"])

    def test_cli_exit_nonzero_on_delta(self) -> None:
        d = self._complete_bundle("DELTA", frozen_gate=_FAIL)
        self.cfg.gates_checks = [_PASS]
        rc = cli._revalidate(self.cfg, SimpleNamespace(issue_id="DELTA", date="2026-06-12"))
        self.assertEqual(rc, 1)  # a changed row is surfaced to the caller
        self.assertTrue((d / "revalidation-2026-06-12.json").exists())

    def test_refuses_non_complete_bundle(self) -> None:
        # A bundle that is only PLANNED (brief, no patch) must be refused, no stamp.
        d = self.cfg.bundle("PARTIAL")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** x\n", encoding="utf-8")
        self.assertNotEqual(state.state(d), state.COMPLETE)
        rc = cli._revalidate(self.cfg, SimpleNamespace(issue_id="PARTIAL", date="2026-06-12"))
        self.assertEqual(rc, 2)
        self.assertEqual(list(d.glob("revalidation-*.json")), [])

    def test_missing_bundle_returns_one(self) -> None:
        rc = cli._revalidate(self.cfg, SimpleNamespace(issue_id="GHOST", date=None))
        self.assertEqual(rc, 1)

    def test_close_bundle_revalidates_with_close_matrix(self) -> None:
        # A frozen close-disposition bundle (no patch, N/A matrix) must re-gate with the
        # SAME close matrix — running the real gates would apply a missing patch and drift.
        d = self.cfg.bundle("CLOSE")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** dup\n", encoding="utf-8")
        (d / state.CLOSE_MARKER).write_text("duplicate\n", encoding="utf-8")
        gates.run_close_gates(d, self.cfg)  # frozen N/A matrix
        (d / "SUMMARY.md").write_text(
            "## 9. Check sign-off\n- Outcome: accepted\n- By / date: t / 2026-06-04\n",
            encoding="utf-8")
        self.assertEqual(state.state(d), state.COMPLETE)
        self.cfg.gates_checks = [_FAIL]  # a real gate that WOULD fail on a missing patch
        result = revalidate.revalidate(self.cfg, d, "2026-06-12")
        self.assertFalse(result["changed"])  # confirmed, not drifted
        self.assertFalse(result["regression"])

    def test_act_index_surfaces_revalidation_delta(self) -> None:
        # A COMPLETE bundle carrying a revalidation delta shows it in the Act index,
        # so Act can tell a stale frozen FAIL from a real accepted failure.
        d = self._complete_bundle("ACTREVAL", frozen_gate=_FAIL)
        self.cfg.gates_checks = [_PASS]
        revalidate.revalidate(self.cfg, d, "2026-06-12")
        entries = act.index(self.cfg)
        entry = next(e for e in entries if e.bundle.name == "issue_ACTREVAL")
        self.assertTrue(entry.reval_deltas)
        rendered = act.render_index(entries, act.patterns(entries))
        self.assertIn("revalidation deltas", rendered)
        self.assertIn("fail→pass", rendered)


if __name__ == "__main__":
    unittest.main()
