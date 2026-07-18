"""Footprint sweep (issue #297; stdlib unittest, real git — no Claude, no network).

Proves `sweep.sweep` reclaims exactly the harness-named siblings of a target checkout —
lane worktrees (cleaned or removed by mode), integration worktrees and orphaned overflow
trees (always removed) — while never touching the primary checkout or bundle artifacts;
plus the flow wiring (the end-of-run call) and the dry-run/off contracts.
"""

from __future__ import annotations

import shutil
import subprocess as sp
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import flow, sweep, worktree
from pdca_harness.config import Config, LeafConfig


class SweepRealGit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.primary = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(self.primary)], check=True)
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.primary / "file.txt").write_text("base\n", encoding="utf-8")
        (self.primary / ".gitignore").write_text("build/\n", encoding="utf-8")
        self._git("add", "-A"); self._git("commit", "-q", "-m", "base")
        self._git("branch", "-M", "main"); self._git("push", "-q", "-u", "origin", "main")
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results", process_dir=self.tmp / "process",
            templates_dir=self.tmp / "templates", default_branch="main",
            tracker_system="github", tracker_url="", issue_id_example="1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
            base_remote="origin", repo_checkouts={"org/repo": str(self.primary)})
        self.lane = self.tmp / "checkout.pdca-wt"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *a: str) -> None:
        sp.run(["git", "-C", str(self.primary), *a], check=True, capture_output=True)

    def _porcelain(self, repo: Path) -> str:
        return sp.run(["git", "-C", str(repo), "status", "--porcelain"],
                      capture_output=True, text=True).stdout.strip()

    def _seed_footprint(self) -> Path:
        """A populated lane (with ignored build output + an untracked stray), an
        integration tree, and a fake orphaned overflow dir."""
        d = self.cfg.bundle("WT")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n", encoding="utf-8")
        wt = worktree.ensure(d, self.cfg)
        (wt / "build").mkdir()
        (wt / "build" / "leftover.o").write_text("compiled\n", encoding="utf-8")
        (wt / "stray.txt").write_text("untracked\n", encoding="utf-8")
        integ = self.tmp / "checkout.pdca-integ-main"
        sp.run(["git", "-C", str(self.primary), "worktree", "add", "--force",
                str(integ), "origin/main"], check=True, capture_output=True)
        (self.tmp / "checkout.pdca-wt-ovf-9").mkdir()
        return d

    def test_clean_mode_strips_build_state_keeps_lane_warm(self) -> None:
        d = self._seed_footprint()
        lines = sweep.sweep(self.cfg, [d])                    # default mode: clean
        self.assertTrue(lines)
        self.assertTrue((self.lane / ".git").exists())        # lane kept, still a worktree
        self.assertFalse((self.lane / "build" / "leftover.o").exists())  # ignored output gone
        self.assertFalse((self.lane / "stray.txt").exists())  # untracked gone
        self.assertEqual(self._porcelain(self.lane), "")      # clean tree
        self.assertFalse((self.tmp / "checkout.pdca-integ-main").exists())  # integ removed
        self.assertFalse((self.tmp / "checkout.pdca-wt-ovf-9").exists())    # orphan removed
        self.assertEqual(self._porcelain(self.primary), "")   # primary never touched
        self.assertTrue((d / "brief.md").exists())            # bundles never touched

    def test_remove_mode_drops_lane_and_owner_sidecar(self) -> None:
        d = self._seed_footprint()
        self.assertTrue(worktree._owner_file(self.lane).exists())
        sweep.sweep(self.cfg, [d], mode="remove")
        self.assertFalse(self.lane.exists())
        self.assertFalse(worktree._owner_file(self.lane).exists())
        wtl = sp.run(["git", "-C", str(self.primary), "worktree", "list", "--porcelain"],
                     capture_output=True, text=True).stdout
        self.assertNotIn(".pdca-", wtl)                       # git admin state pruned too

    def test_off_mode_touches_nothing(self) -> None:
        d = self._seed_footprint()
        self.cfg.sweep_worktrees = "off"
        self.assertEqual(sweep.sweep(self.cfg, [d]), [])      # flow path: no-op
        self.assertTrue((self.lane / "stray.txt").exists())
        self.assertTrue((self.tmp / "checkout.pdca-wt-ovf-9").exists())
        # …but an explicit CLI mode still reclaims under "off".
        self.assertTrue(sweep.sweep(self.cfg, [d], mode="clean"))
        self.assertFalse((self.lane / "stray.txt").exists())

    def test_dry_run_reports_without_touching(self) -> None:
        d = self._seed_footprint()
        lines = sweep.sweep(self.cfg, [d], dry_run=True)
        self.assertTrue(any("would clean" in ln for ln in lines))
        self.assertTrue(any("would remove integration tree" in ln for ln in lines))
        self.assertTrue((self.lane / "stray.txt").exists())   # nothing touched
        self.assertTrue((self.tmp / "checkout.pdca-integ-main").exists())
        self.assertTrue((self.tmp / "checkout.pdca-wt-ovf-9").exists())

    def test_second_sweep_is_a_quiet_noop_for_removed_trees(self) -> None:
        d = self._seed_footprint()
        sweep.sweep(self.cfg, [d])
        again = sweep.sweep(self.cfg, [d])
        self.assertFalse(any("integration tree" in ln for ln in again))  # already gone
        self.assertFalse(any("overflow" in ln for ln in again))
        self.assertTrue((self.lane / ".git").exists())        # lane still valid


class FlowWiring(unittest.TestCase):
    """The flow sweeps once at its publish/freeze boundary — after the drive, never per
    beat — and a sweep failure never fails the run (best-effort teardown)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results", process_dir=self.tmp / "process",
            templates_dir=self.tmp / "templates", default_branch="main",
            tracker_system="github", tracker_url="", issue_id_example="1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
            planner=LeafConfig(mode="stub", interactive=True),
            signoff=LeafConfig(mode="stub", interactive=True),
            publisher=LeafConfig(mode="stub", interactive=True),
            act=LeafConfig(mode="stub", interactive=True))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str) -> None:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")

    def test_single_issue_flow_sweeps_once(self) -> None:
        self._brief("S1")
        with mock.patch.object(sweep, "sweep", return_value=[]) as m:
            flow.flow(self.cfg, "S1", do_publish=False, do_act=False, today="2026-07-18")
        self.assertEqual(m.call_count, 1)

    def test_batch_flow_sweeps_once_after_all_waves(self) -> None:
        for iid in ("B1", "B2"):
            self._brief(iid)
        with mock.patch.object(sweep, "sweep", return_value=[]) as m:
            flow.flow_ids(self.cfg, ["B1", "B2"], do_publish=False, do_act=False,
                          today="2026-07-18")
        self.assertEqual(m.call_count, 1)

    def test_sweep_failure_never_fails_the_run(self) -> None:
        self._brief("S2")
        with mock.patch.object(sweep, "sweep", side_effect=OSError("disk went away")):
            final = flow.flow(self.cfg, "S2", do_publish=False, do_act=False,
                              today="2026-07-18")
        self.assertTrue(final)  # the run still returned its result


if __name__ == "__main__":
    unittest.main()
