"""Slice for the wave integration re-gate (`gates.run_integration`, #wave-model).

Runs the repo-scoped gates over a folded integration tip; a red combination (red though
each fix was green alone) tells the wave driver to STOP before the next wave builds on it.
The gate command runs FROM the given worktree, and only repo-scoped gates run. Run from
the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import gates
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, checks: list[dict]) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        gates_checks=checks)


def _check(cid: str, cmd: str, scope: str = "repo") -> dict:
    return {"id": cid, "tier": "T3", "label": cid.lower(), "cmd": cmd,
            "scope": scope, "gating": True}


class RunIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_checks_stub_passes(self) -> None:
        # Offline (no configured gates) → the all-PASS stub matrix → pass.
        self.assertEqual(gates.run_integration(_cfg(self.tmp, []), self.tmp)["overall"],
                         "pass")

    def test_passing_repo_check(self) -> None:
        cfg = _cfg(self.tmp, [_check("OK", "exit 0")])
        self.assertEqual(gates.run_integration(cfg, self.tmp)["overall"], "pass")

    def test_failing_repo_check_stops(self) -> None:
        cfg = _cfg(self.tmp, [_check("BAD", "exit 1")])
        self.assertEqual(gates.run_integration(cfg, self.tmp)["overall"], "fail")

    def test_runs_in_the_given_worktree(self) -> None:
        # The gate runs FROM the integration tree, so it tests the folded files there.
        (self.tmp / "marker").write_text("x", encoding="utf-8")
        ok = _cfg(self.tmp, [_check("CWD", "test -f marker")])
        self.assertEqual(gates.run_integration(ok, self.tmp)["overall"], "pass")
        miss = _cfg(self.tmp, [_check("CWD", "test -f nope")])
        self.assertEqual(gates.run_integration(miss, self.tmp)["overall"], "fail")

    def test_only_repo_scoped_gates_run(self) -> None:
        # A bundle-scoped check is excluded from the repo-only re-gate (so its failure
        # doesn't gate the integration tip).
        cfg = _cfg(self.tmp, [_check("BND", "exit 1", scope="bundle")])
        self.assertEqual(gates.run_integration(cfg, self.tmp)["overall"], "pass")


if __name__ == "__main__":
    unittest.main()
