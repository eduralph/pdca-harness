"""Vendor-neutral STOP-discipline guard (pdca_harness.guard + builder_guard
--command): the `gh` PATH shim blocks ready/merge for non-claude builders with the
SAME single-sourced rules as the claude PreToolUse hook. Stdlib unittest; a fake
`gh` binary stands in for the real CLI (no network, no GitHub).
"""

from __future__ import annotations

import os
import shutil
import subprocess as sp
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import guard
from pdca_harness.config import Config, LeafConfig

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "builder_guard.py"


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
    )


class CommandMode(unittest.TestCase):
    """builder_guard.py --command mirrors the stdin-JSON verdicts."""

    def _rc(self, command: str) -> int:
        return sp.run([sys.executable, str(HOOK), "--command", command],
                      capture_output=True).returncode

    def test_blocks_ready_merge_and_substitution(self) -> None:
        for cmd in ("gh pr ready 5",
                    "gh pr merge 5",
                    "git push && gh pr ready 5",
                    "timeout 60 gh pr merge 5",
                    "gh pr $(echo ready) 5"):
            self.assertEqual(self._rc(cmd), 2, cmd)

    def test_allows_draft_and_reads(self) -> None:
        for cmd in ("gh pr create --draft",
                    "gh pr view 5",
                    "gh issue list",
                    "git push origin HEAD"):
            self.assertEqual(self._rc(cmd), 0, cmd)


class ShimEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The harness root must carry the hook the shim single-sources from.
        hooks = self.tmp / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy2(HOOK, hooks / "builder_guard.py")
        self.cfg = _cfg(self.tmp)
        # A fake "real gh" on PATH: records its argv so the test can tell whether
        # the shim let a call through.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.marker = self.tmp / "gh-ran.txt"
        fake = self.bin / "gh"
        fake.write_text(f'#!/bin/sh\necho "$@" >> "{self.marker}"\n', encoding="utf-8")
        fake.chmod(0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)

    def _run(self, env: dict, command: str) -> sp.CompletedProcess:
        full = {**os.environ, **env}
        return sp.run(["sh", "-c", command], env=full, capture_output=True, text=True)

    def test_shim_blocks_ready_and_passes_draft_through(self) -> None:
        env = guard.shim_env(self.cfg, {"PDCA_WORKTREE": "x"})
        self.assertIn("PATH", env)
        self.assertTrue(env["PATH"].split(os.pathsep)[0].startswith(tempfile.gettempdir()))

        blocked = self._run(env, "gh pr ready 5")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("STOP discipline", blocked.stderr)
        self.assertFalse(self.marker.exists(), "blocked call reached the real gh")

        allowed = self._run(env, "gh pr create --draft --title t")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("pr create --draft", self.marker.read_text(encoding="utf-8"))

    def test_input_env_is_copied_not_mutated(self) -> None:
        base = {"PDCA_WORKTREE": "x"}
        env = guard.shim_env(self.cfg, base)
        self.assertNotIn("PATH", base)
        self.assertEqual(env["PDCA_WORKTREE"], "x")

    def test_missing_hook_or_gh_degrades_to_unchanged_env(self) -> None:
        # No hook file at the root → nothing to guard with.
        bare = _cfg(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, bare.root, ignore_errors=True)
        self.assertEqual(guard.shim_env(bare, {"A": "1"}), {"A": "1"})
        # No gh anywhere on PATH → nothing to guard.
        os.environ["PATH"] = str(self.tmp / "empty-nonexistent")
        try:
            self.assertEqual(guard.shim_env(self.cfg, None), {})
        finally:
            os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._old_path}"


class DoBuildWiring(unittest.TestCase):
    """do_build wraps a non-native-guard command builder with the shim: a fake
    vendor builder that calls `gh pr ready` sees it blocked, while `gh pr create
    --draft` still works — end to end through the real do_build path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        hooks = self.tmp / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy2(HOOK, hooks / "builder_guard.py")
        self.marker = self.tmp / "gh-ran.txt"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(f'#!/bin/sh\necho "$@" >> "{self.marker}"\n', encoding="utf-8")
        fake.chmod(0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)

    def test_generic_builder_gets_the_shim(self) -> None:
        builder_sh = self.tmp / "builder.sh"
        builder_sh.write_text(
            "#!/bin/sh\n"
            "gh pr ready 5; echo \"ready-rc=$?\" > guard-probe.txt\n"
            "gh pr create --draft; echo \"draft-rc=$?\" >> guard-probe.txt\n",
            encoding="utf-8")
        builder_sh.chmod(0o755)
        cfg = _cfg(self.tmp)
        cfg.builder = LeafConfig(mode="command", family="generic", argv=[str(builder_sh)])
        cfg.worktree = False  # no target checkout in this test; run in cfg.root
        d = cfg.bundle("GUARD")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        from pdca_harness import leaves
        leaves.do_build(d, cfg)
        probe = (self.tmp / "guard-probe.txt").read_text(encoding="utf-8")
        self.assertIn("ready-rc=2", probe)   # STOP discipline enforced
        self.assertIn("draft-rc=0", probe)   # legitimate draft-PR path intact
        gh_log = self.marker.read_text(encoding="utf-8") if self.marker.exists() else ""
        self.assertNotIn("pr ready", gh_log)
        self.assertIn("pr create --draft", gh_log)


if __name__ == "__main__":
    unittest.main()
