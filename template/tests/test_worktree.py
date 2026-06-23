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

    def test_creates_worktree_off_base_primary_untouched(self) -> None:
        wt = worktree.ensure(self.d, self.cfg)
        self.assertIsNotNone(wt)
        self.assertEqual(wt, self.tmp / "checkout.pdca-wt")
        self.assertTrue((wt / ".git").exists())                 # a real worktree
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"), "base\n")  # off base
        self.assertEqual(worktree.path(self.d, self.cfg), wt)   # path() sees it
        # The host's primary checkout was not touched.
        self.assertEqual(self._porcelain(self.primary), "")

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
