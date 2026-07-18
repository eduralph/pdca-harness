"""Overflow worktrees (issue #226, stdlib unittest, real git — no Claude, no network).

Proves the lane vs ephemeral-overflow decision for a gate read:
- the bundle's OWN lane is RECONSTRUCTED to base + patch.diff before the gate reads it
  (#296 — the lane is a warm checkout cache, never a trusted content cache);
- a lane owned by a DIFFERENT bundle spills to a throwaway overflow tree (built off the
  base + this bundle's patch) when `[driver].overflow` > 0 — the lane is NOT mutated —
  and falls back to the in-lane `rebuild_for_gate` reconstruction when overflow is 0 or
  at the cap;
- overflow trees are torn down after use and reclaimable by a sweep.
"""

from __future__ import annotations

import shutil
import subprocess as sp
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import gates, worktree
from pdca_harness.config import Config, LeafConfig

_PATCH = (
    "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
    "@@ -1 +1,2 @@\n base\n+patched\n"
)


class Overflow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.primary = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(self.primary)], check=True)
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.primary / "file.txt").write_text("base\n", encoding="utf-8")
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

    def _bundle(self, iid: str, *, patch: bool = False) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n", encoding="utf-8")
        if patch:
            (d / "patch.diff").write_text(_PATCH, encoding="utf-8")
        return d

    # --- the decision --------------------------------------------------------

    def test_own_lane_is_reconstructed_before_a_gate(self) -> None:
        # #296 (inverts the former warm-and-untouched contract): even the bundle's OWN lane
        # is rebuilt to base + patch.diff — an owner stamp attests what Do intended, not
        # what the tree is, so a gating green must never trust leftover tree content.
        d = self._bundle("WT", patch=True)
        worktree.ensure(d, self.cfg)                       # lane owned by WT
        (self.lane / "built.txt").write_text("do output\n", encoding="utf-8")  # untracked leftover
        self.cfg.overflow = 2
        wt, ovf_primary = worktree.for_gate(d, self.cfg)
        self.assertEqual(wt, self.lane)                    # still the lane…
        self.assertIsNone(ovf_primary)                     # …not an overflow
        self.assertFalse((self.lane / "built.txt").exists())  # reconstructed, not trusted
        self.assertEqual((self.lane / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")

    def test_foreign_lane_spills_to_overflow_and_leaves_the_lane(self) -> None:
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)                   # lane owned by OTHER
        (self.lane / "orphan.txt").write_text("from OTHER\n", encoding="utf-8")
        d = self._bundle("WT", patch=True)
        self.cfg.overflow = 2
        wt, ovf_primary = worktree.for_gate(d, self.cfg)
        self.assertIsNotNone(wt)
        self.assertNotEqual(wt, self.lane)                 # a DISTINCT throwaway tree
        self.assertIn(".pdca-wt-ovf-", wt.name)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")  # WT's patch
        self.assertEqual(ovf_primary, self.primary)
        # the lane was NOT mutated — still OTHER's build, orphan intact
        self.assertEqual(worktree.owner_of(self.lane), "issue_OTHER")
        self.assertTrue((self.lane / "orphan.txt").exists())
        # teardown removes the overflow tree
        worktree.overflow_remove(ovf_primary, wt)
        self.assertFalse(wt.exists())

    def test_foreign_lane_heals_in_place_when_overflow_disabled(self) -> None:
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)
        (self.lane / "orphan.txt").write_text("from OTHER\n", encoding="utf-8")
        d = self._bundle("WT", patch=True)
        self.cfg.overflow = 0                              # disabled → rebuild in the lane
        wt, ovf_primary = worktree.for_gate(d, self.cfg)
        self.assertEqual(wt, self.lane)                    # the healed lane
        self.assertIsNone(ovf_primary)
        self.assertEqual(worktree.owner_of(self.lane), "issue_WT")   # lane taken over
        self.assertFalse((self.lane / "orphan.txt").exists())        # foreign orphan swept
        self.assertEqual(worktree._overflow_dirs(self.primary), [])  # no overflow created

    def test_overflow_cap_falls_back_to_in_place_heal(self) -> None:
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)
        d = self._bundle("WT", patch=True)
        self.cfg.overflow = 1
        (self.primary.parent / "checkout.pdca-wt-ovf-occupied").mkdir()  # the single slot is taken
        wt, ovf_primary = worktree.for_gate(d, self.cfg)
        self.assertEqual(wt, self.lane)                    # at cap → healed lane, no new overflow
        self.assertIsNone(ovf_primary)
        self.assertEqual(worktree.owner_of(self.lane), "issue_WT")

    def test_sweep_reclaims_overflow_trees(self) -> None:
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)
        d = self._bundle("WT", patch=True)
        self.cfg.overflow = 2
        wt, _ = worktree.for_gate(d, self.cfg)
        self.assertTrue(wt.exists() and worktree._overflow_dirs(self.primary))
        worktree.sweep_overflow(self.primary)
        self.assertFalse(wt.exists())
        self.assertEqual(worktree._overflow_dirs(self.primary), [])
        self.assertEqual(self._porcelain(self.primary), "")  # primary untouched throughout

    def test_cap_is_respected_under_concurrent_reads(self) -> None:
        # #241: the count-check and create must be atomic — concurrent Check threads hitting a
        # foreign-owned lane must not BOTH slip past the cap. Widen the create window so a
        # naive (unlocked) check-then-create would over-spill, and confirm the cap holds.
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)                   # lane owned by OTHER
        self.cfg.overflow = 2
        bundles = [self._bundle(f"B{i}", patch=True) for i in range(6)]
        real_create = worktree._overflow_create

        def slow_create(d, primary, base_ref):
            time.sleep(0.03)                               # widen the check→create race window
            return real_create(d, primary, base_ref)

        results: list = []
        rlock = threading.Lock()

        def run(d):
            # Over-cap readers race for the shared lane; the #296-review lane lock makes
            # the losers fail closed ("lane busy") instead of clobbering each other —
            # count those as no-overflow results, the property under test is the cap.
            try:
                r = worktree.for_gate(d, self.cfg)
            except worktree.WorktreeError:
                r = (None, None)
            with rlock:
                results.append(r)

        # rebuild_for_gate (the non-overflow fallback) mutates the shared lane concurrently —
        # stub it to isolate the cap logic under test from that unrelated contention.
        with mock.patch.object(worktree, "_overflow_create", slow_create), \
                mock.patch.object(worktree, "rebuild_for_gate", return_value=self.lane):
            ts = [threading.Thread(target=run, args=(d,)) for d in bundles]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

        overflows = [(wt, ovf) for (wt, ovf) in results if ovf is not None]
        self.assertLessEqual(len(overflows), 2)                              # cap held
        self.assertLessEqual(len(worktree._overflow_dirs(self.primary)), 2)  # on disk too
        for wt, ovf in overflows:
            worktree.overflow_remove(ovf, wt)

    # --- gates end-to-end ----------------------------------------------------

    def test_gate_reads_the_overflow_tree_then_tears_it_down(self) -> None:
        # A foreign-owned lane + overflow on: the gate runs against a fresh overflow tree
        # carrying THIS bundle's patch, the lane is left alone, and the overflow is removed.
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)
        (self.lane / "orphan.txt").write_text("from OTHER\n", encoding="utf-8")
        d = self._bundle("WT", patch=True)
        self.cfg.overflow = 2
        probe = self.tmp / "probe.txt"
        self.cfg.gates_checks = [{
            "id": "probe", "tier": "T3", "label": "reads worktree",
            "cmd": f'cat "$PDCA_WORKTREE/file.txt" > "{probe}"',
            "gating": False, "scope": "repo"}]
        gates._run_checks(self.cfg, cwd=self.cfg.root, bundle=d, scopes=("repo",))
        self.assertEqual(probe.read_text(encoding="utf-8"), "base\npatched\n")  # WT's build
        self.assertEqual(worktree.owner_of(self.lane), "issue_OTHER")           # lane untouched
        self.assertTrue((self.lane / "orphan.txt").exists())
        self.assertEqual(worktree._overflow_dirs(self.primary), [])             # torn down

    def test_overflow_declines_gitlink_patches_and_fails_closed(self) -> None:
        # #296 review round 2: the overflow reconstruction uses the same plain
        # `git apply`, so it must carry the same gitlink fail-closed — the spill
        # declines (tree torn down) and the read falls through to rebuild_for_gate,
        # which raises the loud WorktreeError instead of certifying the wrong
        # submodule revision.
        other = self._bundle("OTHER")
        worktree.ensure(other, self.cfg)                   # lane owned by OTHER
        d = self._bundle("WT")
        (d / "patch.diff").write_text(
            "diff --git a/vendor/lib b/vendor/lib\nindex 1111111..2222222 160000\n"
            "--- a/vendor/lib\n+++ b/vendor/lib\n@@ -1 +1 @@\n"
            "-Subproject commit 1111111111111111111111111111111111111111\n"
            "+Subproject commit 2222222222222222222222222222222222222222\n",
            encoding="utf-8")
        self.cfg.overflow = 2
        with self.assertRaises(worktree.WorktreeError) as ctx:
            worktree.for_gate(d, self.cfg)
        self.assertIn("gitlink", str(ctx.exception))
        self.assertEqual(worktree._overflow_dirs(self.primary), [])  # spill torn down

    def test_gate_fails_closed_when_tree_cannot_match_patch(self) -> None:
        # #296: a patch.diff that no longer applies means NO tree can be shown to match the
        # patch under review. The run must fail CLOSED: no gate command executes, the matrix
        # carries one synthetic gating red (rule_id "worktree-mismatch"), overall is "fail" —
        # a green for a mismatched tree is the defect class this guards against.
        d = self._bundle("WT")
        (d / "patch.diff").write_text(
            "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
            "@@ -1 +1 @@\n-not the base line\n+changed\n", encoding="utf-8")
        probe = self.tmp / "probe.txt"
        self.cfg.gates_checks = [{
            "id": "probe", "tier": "T3", "label": "would touch probe",
            "cmd": f'touch "{probe}"', "gating": True, "scope": "repo"}]
        result = gates.run_gates(d, self.cfg)
        self.assertEqual(result["overall"], "fail")
        self.assertTrue(any(r["rule_id"] == "worktree-mismatch" and r["gating"]
                            and r["result"] == "fail" for r in result["rows"]))
        self.assertFalse(probe.exists())                   # no gate command was run

    def _porcelain(self, repo: Path) -> str:
        return sp.run(["git", "-C", str(repo), "status", "--porcelain"],
                      capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
