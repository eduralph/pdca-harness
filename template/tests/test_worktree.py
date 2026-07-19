"""Offline slice for per-cycle git worktree isolation (issue #94, stdlib unittest).

Proves the harness runs Do/Check in a worktree off the target base so the host's
primary checkout is never mutated, and that it's best-effort (disabled / no target /
non-git checkout fall back to in-place → None). The real-git test uses a bare origin
+ clone; no Claude, no network.
"""

from __future__ import annotations

import shutil
import subprocess as sp
import tempfile
import unittest
from pathlib import Path

from pdca_harness import worktree
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        base_remote="origin",  # own-repo: branch the worktree off origin/<base>
    )


def _bundle(cfg: Config, iid: str, *, target: str) -> Path:
    d = cfg.bundle(iid)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(f"- **Slug:** s\n- **Repo + branch target:** {target}\n",
                                encoding="utf-8")
    return d


class WorktreeFallback(unittest.TestCase):
    """Best-effort: isolation that can't apply returns None (cycle runs in place)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disabled_returns_none(self) -> None:
        self.cfg.worktree = False
        d = _bundle(self.cfg, "OFF", target="org/repo @ main")
        self.assertIsNone(worktree.ensure(d, self.cfg))
        self.assertIsNone(worktree.path(d, self.cfg))

    def test_no_target_returns_none(self) -> None:
        d = self.cfg.bundle("NOTGT")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")  # no target
        self.assertIsNone(worktree.ensure(d, self.cfg))

    def test_non_git_checkout_returns_none(self) -> None:
        # Target resolves to a real dir that is NOT a git checkout → fall back.
        plain = self.tmp / "plain"
        plain.mkdir()
        self.cfg.repo_checkouts = {"org/repo": str(plain)}
        d = _bundle(self.cfg, "PLAIN", target="org/repo @ main")
        self.assertIsNone(worktree.ensure(d, self.cfg))

    def test_gate_rebuild_none_when_isolation_cannot_apply(self) -> None:
        # The gate read keeps the best-effort contract ONLY where isolation legitimately
        # doesn't apply (disabled / no target) — same cases as ensure/path (#296).
        self.cfg.worktree = False
        d = _bundle(self.cfg, "OFF2", target="org/repo @ main")
        self.assertIsNone(worktree.rebuild_for_gate(d, self.cfg))
        self.cfg.worktree = True
        d2 = self.cfg.bundle("NOTGT2")
        d2.mkdir(parents=True)
        (d2 / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")  # no target
        self.assertIsNone(worktree.rebuild_for_gate(d2, self.cfg))


class WorktreeRealGit(unittest.TestCase):
    """The host's primary checkout is never mutated; the worktree is off the base and
    reset-and-reused per cycle."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.primary = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(self.primary)], check=True)
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.primary / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "-A"); self._git("commit", "-q", "-m", "base")
        self._git("branch", "-M", "main"); self._git("push", "-q", "-u", "origin", "main")
        self.cfg.repo_checkouts = {"org/repo": str(self.primary)}
        self.d = _bundle(self.cfg, "WT", target="org/repo @ main")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *a: str) -> None:
        sp.run(["git", "-C", str(self.primary), *a], check=True, capture_output=True)

    def _porcelain(self, repo: Path) -> str:
        return sp.run(["git", "-C", str(repo), "status", "--porcelain"],
                      capture_output=True, text=True).stdout.strip()

    def _ignore_build_dir(self) -> None:
        """Commit a `.gitignore` (ignoring `build/`) to the base so a `build/` artifact in a
        worktree is genuinely IGNORED — the condition under which `git clean -fd` leaves it."""
        (self.primary / ".gitignore").write_text("build/\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "ignore build/")
        self._git("push", "-q", "origin", "main")

    def test_creates_worktree_off_base_primary_untouched(self) -> None:
        wt = worktree.ensure(self.d, self.cfg)
        self.assertIsNotNone(wt)
        self.assertEqual(wt, self.tmp / "checkout.pdca-wt")
        self.assertTrue((wt / ".git").exists())                 # a real worktree
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\n")  # off base
        self.assertEqual(worktree.path(self.d, self.cfg), wt)   # path() sees it
        # The host's primary checkout was not touched.
        self.assertEqual(self._porcelain(self.primary), "")

    def test_ensure_fails_closed_on_unresolvable_base(self) -> None:
        # #235: the target IS a git checkout but the specified base doesn't resolve → ABORT
        # (WorktreeError), never fall back to running Do/Check in the operator's primary
        # checkout. Nothing is created or mutated.
        bad = _bundle(self.cfg, "BAD", target="org/repo @ no-such-base")
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure(bad, self.cfg)
        self.assertEqual(self._porcelain(self.primary), "")             # primary untouched
        self.assertFalse((self.tmp / "checkout.pdca-wt").exists())      # no worktree created

    def test_reused_worktree_is_reset_each_cycle(self) -> None:
        wt = worktree.ensure(self.d, self.cfg)
        # A prior cycle's edits in the worktree…
        (wt / "file.txt").write_text("dirty edit\n", encoding="utf-8")
        (wt / "stray.txt").write_text("x\n", encoding="utf-8")
        # …are wiped by the next ensure (reset to base + clean), not accumulated.
        wt2 = worktree.ensure(self.d, self.cfg)
        self.assertEqual(wt2, wt)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((wt / "stray.txt").exists())
        self.assertEqual(self._porcelain(wt), "")  # clean

    def test_ensure_stamps_owner_and_reuse_reassigns(self) -> None:
        # ensure() records which bundle's Do owns the tree; a second bundle reusing the same
        # per-lane worktree reassigns ownership (so `pdca try <old>` can detect the swap).
        wt = worktree.ensure(self.d, self.cfg)
        self.assertEqual(worktree.owner_of(wt), "issue_WT")
        other = _bundle(self.cfg, "OTHER", target="org/repo @ main")
        wt2 = worktree.ensure(other, self.cfg)
        self.assertEqual(wt2, wt)                          # same reused tree…
        self.assertEqual(worktree.owner_of(wt), "issue_OTHER")  # …now owned by the later bundle

    _PATCH = (
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1,2 @@\n base\n+patched\n"
    )

    def test_rebuild_heals_a_foreign_owned_worktree(self) -> None:
        # #224: a shared lane worktree still holds a DIFFERENT bundle's net-new orphan when
        # this bundle's gate reads it. The rebuild must reset+clean the orphan, re-apply THIS
        # bundle's patch, and take ownership — so the gate sees only this bundle's change.
        other = _bundle(self.cfg, "OTHER", target="org/repo @ main")
        wt = worktree.ensure(other, self.cfg)                       # lane tree owned by OTHER
        (wt / "orphan.rs").write_text("stray net-new from OTHER\n", encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        healed = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertEqual(healed, wt)
        self.assertFalse((wt / "orphan.rs").exists())               # foreign orphan swept
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")  # our patch
        self.assertEqual(worktree.owner_of(wt), "issue_WT")         # ownership taken

    def test_rebuild_reconstructs_even_its_own_worktree(self) -> None:
        # #296: the incident tree carried a correct-looking owner stamp (iterate-do reuses
        # the bundle dir) around STALE content — a standby-interrupted re-populate. The gate
        # read must therefore never trust the stamp: even this bundle's own lane is rebuilt
        # to base + patch.diff, so a gating green can only ever attest the patch under review.
        wt = worktree.ensure(self.d, self.cfg)                      # owned by WT
        (wt / "file.txt").write_text("previous iteration's code\n", encoding="utf-8")
        (wt / "stale.rs").write_text("leftover from iteration N-1\n", encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        rebuilt = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertEqual(rebuilt, wt)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")
        self.assertFalse((wt / "stale.rs").exists())                # stale content swept
        self.assertEqual(worktree.owner_of(wt), "issue_WT")

    def test_rebuild_sweeps_ignored_artifacts_from_a_foreign_build(self) -> None:
        # #237: `git clean -fd` leaves IGNORED files, so a foreign bundle's ignored build
        # outputs (dist/, caches, generated assets) would survive the heal and contaminate
        # THIS bundle's gate. -x must remove them so the gate sees only this bundle's change.
        self._ignore_build_dir()
        other = _bundle(self.cfg, "OTHER", target="org/repo @ main")
        wt = worktree.ensure(other, self.cfg)                       # foreign-owned lane tree
        (wt / "build").mkdir()
        (wt / "build" / "leftover.o").write_text("OTHER's compiled output\n", encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        healed = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertEqual(healed, wt)
        self.assertFalse((wt / "build" / "leftover.o").exists())    # ignored artifact swept (-x)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")

    def test_rebuild_creates_worktree_when_absent(self) -> None:
        # No lane on disk (crash / standby wiped it, or Do never ran here): the gate read
        # creates it off the base and applies the patch — it never runs in the primary.
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        wt = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertIsNotNone(wt)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")
        self.assertEqual(self._porcelain(self.primary), "")         # primary untouched

    def test_rebuild_fails_closed_on_a_submodule_gitlink_patch(self) -> None:
        # #296 review: plain `git apply` exits 0 for a gitlink (mode 160000) hunk while
        # leaving the submodule checkout untouched — the "reconstructed" tree would carry
        # the wrong submodule revision under a valid stamp. Fail CLOSED instead.
        worktree.ensure(self.d, self.cfg)
        (self.d / "patch.diff").write_text(
            "diff --git a/vendor/lib b/vendor/lib\nindex 1111111..2222222 160000\n"
            "--- a/vendor/lib\n+++ b/vendor/lib\n@@ -1 +1 @@\n"
            "-Subproject commit 1111111111111111111111111111111111111111\n"
            "+Subproject commit 2222222222222222222222222222222222222222\n",
            encoding="utf-8")
        with self.assertRaises(worktree.WorktreeError) as ctx:
            worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertIn("gitlink", str(ctx.exception))
        wt = self.tmp / "checkout.pdca-wt"
        self.assertIsNone(worktree.owner_of(wt))            # never stamped as valid

    def test_text_mention_of_subproject_commit_is_not_a_gitlink(self) -> None:
        # #296 review round 2: detection keys on the 160000 mode headers, never hunk
        # text — a docs/test-fixture line reading "Subproject commit …" must still
        # reconstruct normally, not fail closed.
        worktree.ensure(self.d, self.cfg)
        (self.d / "patch.diff").write_text(
            "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
            "@@ -1 +1,2 @@\n base\n+Subproject commit deadbeef documented here\n",
            encoding="utf-8")
        wt = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertIsNotNone(wt)
        self.assertIn("Subproject commit deadbeef",
                      (wt / "file.txt").read_text(encoding="utf-8"))

    def test_rebuild_removes_a_nested_repository(self) -> None:
        # #296 review round 3: `git clean` preserves an untracked NESTED REPOSITORY
        # under a single -f, so a stale lane carrying one would survive reconstruction
        # and the tree would not be base + patch.diff. The double-force sweep removes it.
        wt = worktree.ensure(self.d, self.cfg)
        nested = wt / "vendor-scratch"
        sp.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "junk.txt").write_text("stale nested repo\n", encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        rebuilt = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertEqual(rebuilt, wt)
        self.assertFalse(nested.exists())                   # nested repo swept (-ff)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")

    def test_unopenable_lane_lock_fails_closed_not_raw_oserror(self) -> None:
        # #296 review round 3: the lock sidecar's open() can itself fail; that must
        # surface as WorktreeError (the fail-closed gating red), never a raw OSError
        # that aborts the whole run. A directory squatting on the lock path forces it.
        (self.tmp / "checkout.pdca-wt.lock").mkdir()
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        with self.assertRaises(worktree.WorktreeError) as ctx:
            worktree.for_gate(self.d, self.cfg)
        self.assertIn("lane lock", str(ctx.exception))

    def test_busy_lane_fails_the_gate_read_closed(self) -> None:
        # #296 review: an owner stamp cannot say whether that Do is STILL RUNNING —
        # while the lane lock is held (an in-flight Do / another gate run), a gate read
        # must fail closed ("lane busy") instead of reconstructing under the live run.
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        with worktree.lane_lock(self.d, self.cfg, wait=True):   # simulate the live Do
            with self.assertRaises(worktree.WorktreeError) as ctx:
                worktree.for_gate(self.d, self.cfg)
            self.assertIn("busy", str(ctx.exception))
        wt, ovf = worktree.for_gate(self.d, self.cfg)           # released → proceeds
        self.assertIsNotNone(wt)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")

    def test_lane_lock_is_a_noop_where_isolation_cannot_apply(self) -> None:
        self.cfg.worktree = False
        with worktree.lane_lock(self.d, self.cfg, wait=False):
            pass                                            # yields without a lock file
        self.cfg.worktree = True

    def test_rebuild_fails_closed_when_patch_does_not_apply(self) -> None:
        # #296 (supersedes the #225 best-effort fallback): if this bundle's patch no longer
        # applies to the base, the tree CANNOT be made to match patch.diff. The gate read
        # must fail CLOSED (WorktreeError → synthetic gating red), never fall back to the
        # primary checkout or present a clean base as the build, and must not claim
        # ownership (a later read would match the stamp and silently green a clean base).
        other = _bundle(self.cfg, "OTHER", target="org/repo @ main")
        wt = worktree.ensure(other, self.cfg)                       # foreign-owned lane tree
        (self.d / "patch.diff").write_text(                          # context that isn't on base
            "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
            "@@ -1 +1 @@\n-not the base line\n+changed\n", encoding="utf-8")
        with self.assertRaises(worktree.WorktreeError):
            worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertIsNone(worktree.owner_of(wt))                    # stamp cleared → re-attempted

    def test_stage_creates_tree_from_patch_when_absent(self) -> None:
        # `pdca try` on a batch: no per-cycle worktree exists yet (all Do already done, tree
        # reset-reused). stage() CREATES it off the base and applies THIS bundle's patch.diff,
        # so any parked bundle is launchable — not only the last one Do populated.
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        self.assertIsNone(worktree.path(self.d, self.cfg))          # nothing on disk yet
        wt = worktree.stage(self.d, self.cfg)
        self.assertEqual(wt, self.tmp / "checkout.pdca-wt")
        self.assertTrue((wt / ".git").exists())
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")
        self.assertEqual(worktree.owner_of(wt), "issue_WT")
        self.assertEqual(self._porcelain(self.primary), "")         # primary untouched

    def test_stage_reconstructs_over_a_foreign_owned_tree(self) -> None:
        # The batch-then-review fix: the shared tree holds a LATER bundle's build. stage()
        # resets it and rebuilds THIS bundle from patch.diff (replacing the old owner-mismatch
        # refusal), so `pdca try <earlier-id>` works instead of erroring.
        other = _bundle(self.cfg, "OTHER", target="org/repo @ main")
        wt = worktree.ensure(other, self.cfg)                       # tree owned by OTHER
        (wt / "orphan.txt").write_text("from OTHER\n", encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        staged = worktree.stage(self.d, self.cfg)
        self.assertEqual(staged, wt)
        self.assertFalse((wt / "orphan.txt").exists())              # OTHER's build swept
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")
        self.assertEqual(worktree.owner_of(wt), "issue_WT")         # now ours

    def test_stage_sweeps_ignored_artifacts_from_a_foreign_build(self) -> None:
        # #237 (Codex P1): a later bundle's IGNORED build outputs survive `git clean -fd`, so
        # without -x `pdca try <earlier-id>` would launch this bundle's source patch on top of
        # another bundle's ignored artifacts — a wrong build a reviewer could sign off. -x sweeps
        # them so the staged tree is a pristine reconstruction of THIS bundle's build.
        self._ignore_build_dir()
        other = _bundle(self.cfg, "OTHER", target="org/repo @ main")
        wt = worktree.ensure(other, self.cfg)                       # tree owned by OTHER
        (wt / "build").mkdir()
        (wt / "build" / "leftover.o").write_text("OTHER's compiled output\n", encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        staged = worktree.stage(self.d, self.cfg)
        self.assertEqual(staged, wt)
        self.assertFalse((wt / "build" / "leftover.o").exists())    # ignored artifact swept (-x)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\npatched\n")
        self.assertEqual(worktree.owner_of(wt), "issue_WT")

    def test_stage_none_when_patch_does_not_apply(self) -> None:
        (self.d / "patch.diff").write_text(
            "diff --git a/nope.txt b/nope.txt\n--- a/nope.txt\n+++ b/nope.txt\n"
            "@@ -1 +1 @@\n-absent\n+x\n", encoding="utf-8")
        self.assertIsNone(worktree.stage(self.d, self.cfg))

    def test_stage_none_when_isolation_off(self) -> None:
        self.cfg.worktree = False
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        self.assertIsNone(worktree.stage(self.d, self.cfg))

    def test_stacked_bundle_bases_off_parent_branch(self) -> None:
        # #123: a `Stacks on:` dependent's worktree bases off the parent's PUBLISHED branch
        # (on origin), not origin/main — so Do builds + verifies on top of the parent's diff.
        self._git("checkout", "-qb", "fix/PARENT-x")
        (self.primary / "parent.txt").write_text("from parent\n", encoding="utf-8")
        self._git("add", "-A"); self._git("commit", "-q", "-m", "parent change")
        self._git("push", "-q", "-u", "origin", "fix/PARENT-x")
        self._git("checkout", "-q", "main")
        parent = self.cfg.bundle("PARENT")
        parent.mkdir(parents=True)
        (parent / "publish.json").write_text('{"branch": "fix/PARENT-x"}', encoding="utf-8")
        dep = self.cfg.bundle("DEP")
        dep.mkdir(parents=True)
        (dep / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n"
            "- **Stacks on:** PARENT\n", encoding="utf-8")
        wt = worktree.ensure(dep, self.cfg)
        self.assertIsNotNone(wt)
        # the worktree carries the parent's change → it's off fix/PARENT-x, not main
        self.assertEqual((wt / "parent.txt").read_text(encoding="utf-8"), "from parent\n")


if __name__ == "__main__":
    unittest.main()
