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

    @staticmethod
    def _dead_pid() -> int:
        """A pid that provably no longer exists (a reaped child) — the orphan case."""
        p = sp.Popen(["true"])
        p.wait()
        return p.pid

    def _seed_footprint(self) -> Path:
        """A populated lane (with ignored build output + an untracked stray), an
        integration tree, and an orphaned overflow dir (dead creator pid)."""
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
        # The orphan is a REAL registered worktree whose creator pid is gone — the
        # crash-leftover shape (#297 review round 4: unregistered pattern-matching
        # dirs are never removed, so a plain mkdir would no longer model an orphan).
        self.ovf = self.tmp / f"checkout.pdca-wt-ovf-{self._dead_pid()}-0"
        sp.run(["git", "-C", str(self.primary), "worktree", "add", "--force",
                str(self.ovf), "origin/main"], check=True, capture_output=True)
        return d

    def test_clean_mode_strips_build_state_keeps_lane_warm(self) -> None:
        d = self._seed_footprint()
        lines = sweep.sweep(self.cfg, [d])                    # default mode: clean
        self.assertTrue(lines)
        self.assertTrue((self.lane / ".git").exists())        # lane kept, still a worktree
        self.assertFalse((self.lane / "build" / "leftover.o").exists())  # ignored output gone
        self.assertFalse((self.lane / "stray.txt").exists())  # untracked gone
        self.assertEqual(self._porcelain(self.lane), "")      # clean tree
        # #297 review round 2: the reset stripped the bundle's patch, so the owner
        # stamp must go with it — a stale stamp would let a later gate read trust the
        # unpatched base as the bundle's build.
        self.assertIsNone(worktree.owner_of(self.lane))
        self.assertFalse((self.tmp / "checkout.pdca-integ-main").exists())  # integ removed
        self.assertFalse(self.ovf.exists())                                  # orphan removed
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
        self.assertTrue(self.ovf.exists())
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
        self.assertTrue(self.ovf.exists())
        # #297 review round 9: "reports without touching" includes the lifecycle
        # sidecars — the dry-run contention probe must not CREATE .lock files.
        self.assertEqual(list(self.tmp.glob("*.lock")), [])

    def test_dry_run_still_reports_a_busy_lane(self) -> None:
        # The non-mutating probe still detects real contention: a held lane lock
        # shows as busy in the dry-run report, and the sidecar is left as-is.
        d = self._seed_footprint()
        lock = self.lane.with_name(self.lane.name + ".lock").open("w")
        self.addCleanup(lock.close)
        worktree._lock_file(lock, wait=True)
        try:
            lines = sweep.sweep(self.cfg, [d], mode="remove", dry_run=True)
        finally:
            worktree._unlock_file(lock)
        self.assertTrue(any("busy" in ln for ln in lines))
        self.assertTrue(self.lane.exists())

    def test_live_owner_overflow_tree_is_left_alone(self) -> None:
        # #297 review: an overflow name embeds its creator pid; a LIVE pid may be
        # another process's in-flight gate read — deleting its working directory
        # mid-command would invalidate that gate. Only proven orphans are reclaimed.
        d = self._seed_footprint()
        holder = sp.Popen(["sleep", "30"])                    # a live owner process
        self.addCleanup(holder.kill)
        live = self.tmp / f"checkout.pdca-wt-ovf-{holder.pid}-0"
        live.mkdir()
        unparseable = self.tmp / "checkout.pdca-wt-ovf-occupied"
        unparseable.mkdir()
        lines = sweep.sweep(self.cfg, [d])
        self.assertFalse(self.ovf.exists())                   # dead pid → reclaimed
        self.assertTrue(live.exists())                        # live pid → left alone
        self.assertTrue(unparseable.exists())                 # unprovable → left alone
        self.assertTrue(any("owner process still alive" in ln for ln in lines))

    def test_manual_sweep_discovers_sibling_convention_targets(self) -> None:
        # #297 review: with no [publisher.checkouts] entries (the sibling-convention
        # setup) and no bundles passed (the manual `pdca sweep`), targets are derived
        # from the persisted issue_* bundles — the command must not report "nothing".
        d = self._seed_footprint()
        self.cfg.repo_checkouts = {}
        self.cfg.root = self.tmp / "proj"                     # sibling: <root>/../checkout
        self.cfg.root.mkdir()
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/checkout @ main\n",
            encoding="utf-8")
        lines = sweep.sweep(self.cfg)                         # no bundles argument
        self.assertTrue(lines)
        self.assertFalse((self.lane / "stray.txt").exists())  # lane found and cleaned
        self.assertFalse((self.tmp / "checkout.pdca-integ-main").exists())

    def test_unrelated_siblings_are_never_touched(self) -> None:
        # #297 review: the lane glob matches EXACTLY <name>.pdca-wt / -l<slot>; a
        # sibling like `<name>.pdca-wt-backup` is not ours, and an integ-named plain
        # dir with no .git entry must never hit the rmtree fallback.
        d = self._seed_footprint()
        backup = self.tmp / "checkout.pdca-wt-backup"
        backup.mkdir()
        (backup / "precious.txt").write_text("keep me\n", encoding="utf-8")
        fake_integ = self.tmp / "checkout.pdca-integ-fake"
        fake_integ.mkdir()
        (fake_integ / "precious.txt").write_text("keep me\n", encoding="utf-8")
        lines = sweep.sweep(self.cfg, [d], mode="remove")
        self.assertTrue((backup / "precious.txt").exists())   # never matched
        self.assertTrue((fake_integ / "precious.txt").exists())  # matched, refused
        self.assertTrue(any("not ours to remove" in ln for ln in lines))
        self.assertFalse(self.lane.exists())                  # the real lane still removed

    def test_unregistered_dir_matching_the_overflow_pattern_is_left(self) -> None:
        # #297 review round 4: overflow removal gets the same registration guard as
        # lanes/integs — a random dir matching `…-ovf-<dead-pid>-*` must never reach
        # overflow_remove's rmtree fallback.
        d = self._seed_footprint()
        impostor = self.tmp / f"checkout.pdca-wt-ovf-{self._dead_pid()}-7"
        impostor.mkdir()
        (impostor / "precious.txt").write_text("not a worktree\n", encoding="utf-8")
        lines = sweep.sweep(self.cfg, [d])
        self.assertTrue((impostor / "precious.txt").exists())   # left untouched
        self.assertFalse(self.ovf.exists())                     # the real orphan removed
        self.assertTrue(any("not ours to remove" in ln and "-7" in ln for ln in lines))

    def test_standalone_clone_on_the_lane_path_is_never_cleaned(self) -> None:
        # #297 review round 3: clean mode's `clean -fdxq` + `reset --hard` are as
        # destructive as removal — the registration guard must gate them too. A clone
        # squatting on the EXACT lane path keeps its untracked work.
        d = self._seed_footprint()
        shutil.rmtree(self.lane)                              # free the exact lane path…
        sp.run(["git", "-C", str(self.primary), "worktree", "prune"],
               check=True, capture_output=True)
        sp.run(["git", "init", "-q", str(self.lane)], check=True)  # …for an unrelated clone
        (self.lane / "precious.txt").write_text("someone's WIP\n", encoding="utf-8")
        lines = sweep.sweep(self.cfg, [d])                    # default clean mode
        self.assertTrue((self.lane / "precious.txt").exists())  # untouched
        self.assertTrue(any("not ours to touch" in ln for ln in lines))

    def test_standalone_clone_matching_our_naming_is_never_deleted(self) -> None:
        # #297 review round 2: a standalone git CLONE named like an integ tree has a
        # .git entry, fails `git worktree remove`, and the old fallback would rmtree an
        # unrelated repository. Registration in `git worktree list` is now required.
        d = self._seed_footprint()
        clone = self.tmp / "checkout.pdca-integ-notours"
        sp.run(["git", "init", "-q", str(clone)], check=True)
        (clone / "precious.txt").write_text("someone's repo\n", encoding="utf-8")
        lines = sweep.sweep(self.cfg, [d], mode="remove")
        self.assertTrue((clone / "precious.txt").exists())    # survived
        self.assertTrue(any("not ours to remove" in ln and "notours" in ln
                            for ln in lines))
        self.assertFalse((self.tmp / "checkout.pdca-integ-main").exists())  # real one gone

    def test_discovery_covers_archived_completed_bundles(self) -> None:
        # #297 review round 2: with everything archived to results/completed/ and no
        # [publisher.checkouts], the archived bundles are the only record of the
        # sibling-convention targets — discovery must read them too.
        d = self._seed_footprint()
        self.cfg.repo_checkouts = {}
        self.cfg.root = self.tmp / "proj"
        self.cfg.root.mkdir()
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/checkout @ main\n",
            encoding="utf-8")
        archived = self.cfg.bundle_root / "completed" / d.name
        archived.parent.mkdir(parents=True, exist_ok=True)
        d.rename(archived)                                    # the #171 archive move
        lines = sweep.sweep(self.cfg)                         # no bundles argument
        self.assertTrue(lines)
        self.assertFalse((self.lane / "stray.txt").exists())  # target still discovered

    def test_busy_lane_is_left_untouched(self) -> None:
        # #297 review round 5: an out-of-process Do/gate holds the per-lane .lock for
        # its whole critical section — the sweep must try it non-blocking and leave a
        # busy lane alone, in every mode.
        d = self._seed_footprint()
        lock = self.lane.with_name(self.lane.name + ".lock").open("w")
        self.addCleanup(lock.close)
        worktree._lock_file(lock, wait=True)               # simulate the live run
        try:
            lines = sweep.sweep(self.cfg, [d], mode="remove")
        finally:
            worktree._unlock_file(lock)
        self.assertTrue(self.lane.exists())                # not removed
        self.assertTrue((self.lane / "stray.txt").exists())  # not cleaned either
        self.assertTrue(any("busy" in ln for ln in lines))

    def test_busy_integration_tree_is_left_untouched(self) -> None:
        # #297 review round 6: another process may be mid-fold / mid-re-gate in the
        # integ tree (integrate.fold and gates.run_integration hold integ_lock for
        # their critical section) — the sweep tries the same lock non-blocking and
        # leaves a busy tree alone instead of removing it under a live run.
        d = self._seed_footprint()
        integ = self.tmp / "checkout.pdca-integ-main"
        lock = integ.with_name(integ.name + ".lock").open("w")
        self.addCleanup(lock.close)
        worktree._lock_file(lock, wait=True)               # simulate the live fold
        try:
            lines = sweep.sweep(self.cfg, [d], mode="remove")
        finally:
            worktree._unlock_file(lock)
        self.assertTrue(integ.exists())                    # not removed
        self.assertTrue(any("integration lock" in ln for ln in lines))
        # Released → the next sweep reclaims it.
        lines = sweep.sweep(self.cfg, [d], mode="remove")
        self.assertFalse(integ.exists())

    def test_symlinked_lane_path_is_never_followed(self) -> None:
        # #297 review round 5: a symlink aliasing the primary (or any registered tree)
        # would pass a resolving registration check — the destructive git commands must
        # never follow it. Symlinks on harness paths are rejected outright.
        d = self._seed_footprint()
        shutil.rmtree(self.lane)
        sp.run(["git", "-C", str(self.primary), "worktree", "prune"],
               check=True, capture_output=True)
        self.lane.symlink_to(self.primary)                 # alias the PRIMARY checkout
        (self.primary / "wip.txt").write_text("operator's WIP\n", encoding="utf-8")
        lines = sweep.sweep(self.cfg, [d])                 # default clean mode
        self.assertTrue((self.primary / "wip.txt").exists())  # primary never touched
        self.assertTrue(any("not ours to touch" in ln for ln in lines))

    def test_clean_sweeps_nested_repositories(self) -> None:
        # #297 review round 5: a single -f preserves untracked nested repositories —
        # vendor checkouts would survive every sweep and keep the disk. -ff removes them.
        d = self._seed_footprint()
        nested = self.lane / "vendor-scratch"
        sp.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "junk.txt").write_text("vendored\n", encoding="utf-8")
        sweep.sweep(self.cfg, [d])
        self.assertFalse(nested.exists())

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
