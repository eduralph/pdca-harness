"""Do builder worktree-confinement (issue #136, stdlib unittest).

Worktree isolation (#94) kept a cycle off the *target's* primary checkout, but
do_build ran the builder with cwd = the harness root and only *exposed*
$PDCA_WORKTREE — nothing confined a non-claude command builder's edits to the
worktree. #136 runs every command builder *in* the worktree (cwd), a contract
independent of family, so the produced patch.diff is confined to the worktree and
the host checkout is provably untouched. The real-git test uses a bare origin +
clone; no Claude, no network.
"""

from __future__ import annotations

import shutil
import subprocess as sp
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, builder: LeafConfig) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=builder,
        reviewer=LeafConfig(mode="stub"),
        base_remote="origin",
    )


class DoBuildConfinement(unittest.TestCase):
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

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *a: str) -> None:
        sp.run(["git", "-C", str(self.primary), *a], check=True, capture_output=True)

    def _bundle(self, cfg: Config, iid: str = "WT") -> Path:
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n", encoding="utf-8")
        return d

    def test_command_builder_runs_in_the_worktree(self) -> None:
        # A non-claude command builder that writes a RELATIVE-path file lands it in the
        # worktree (its cwd), NOT the harness root — proving cwd-confinement (#136).
        builder = LeafConfig(
            mode="command", family="",
            argv=[sys.executable, "-c",
                  "import os; open('cwd-probe.txt', 'w').write(os.getcwd())"])
        cfg = _cfg(self.tmp, builder)
        cfg.repo_checkouts = {"org/repo": str(self.primary)}
        d = self._bundle(cfg)
        leaves.do_build(d, cfg)
        wt = self.tmp / "checkout.pdca-wt"
        probe = wt / "cwd-probe.txt"
        self.assertTrue(probe.exists(), "builder did not run in the worktree")
        self.assertEqual(probe.read_text(encoding="utf-8"), str(wt))  # cwd WAS the worktree
        self.assertFalse((cfg.root / "cwd-probe.txt").exists())       # not the harness root

    def test_claude_family_keeps_root_cwd_for_agent_discovery(self) -> None:
        # The claude builder must run from the harness root (cwd), not the worktree, so it
        # can discover its `builder` subagent + builder_guard hook under .claude/; it is
        # grounded in the worktree via --add-dir instead (Codex review, PR #143).
        captured: dict = {}

        def fake_invoke(leaf, workdir, prompt, **kw):
            captured["workdir"] = workdir
            captured["extra_argv"] = kw.get("extra_argv")

        builder = LeafConfig(mode="command", family="claude", argv=["claude", "-p"])
        cfg = _cfg(self.tmp, builder)
        cfg.repo_checkouts = {"org/repo": str(self.primary)}
        d = self._bundle(cfg)
        orig = leaves._invoke
        leaves._invoke = fake_invoke
        try:
            leaves.do_build(d, cfg)
        finally:
            leaves._invoke = orig
        wt = self.tmp / "checkout.pdca-wt"
        self.assertEqual(captured["workdir"], cfg.root)                  # agent/hook discovery
        self.assertEqual(captured["extra_argv"], ["--add-dir", str(wt)])  # grounded in the wt


if __name__ == "__main__":
    unittest.main()
