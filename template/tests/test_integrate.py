"""Slice for integration-branch stacking (`integrate.fold`) — the default wave
sequencing that folds each wave's accepted patches onto a run-scoped branch the next
wave builds on, without merging (#wave-model).

Two halves: pure/dry-run cases (no git — naming, nothing-to-fold, dry-run shells
nothing, different-target exclusion) and real-git folds against a bare ``origin`` +
a primary checkout (a clean fold pushes the branch; an undeclared overlap raises
``IntegrationError``). Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import integrate
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, repo_spec: str, primary: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={repo_spec: str(primary)})


class FoldDryAndUnit(unittest.TestCase):
    """No git — naming, the nothing-to-fold short-circuits, dry-run shells nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, "org/repo", self.tmp / "repo")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, target: str = "org/repo @ main",
                patch: str | None = "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-x\n+y\n") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** {target}\n",
            encoding="utf-8")
        if patch is not None:
            (d / "patch.diff").write_text(patch, encoding="utf-8")
        return d

    def test_integration_branch_name_flattens_base(self) -> None:
        self.assertEqual(integrate.integration_branch(self.cfg, "main"),
                         "pdca-integration/main")
        self.assertEqual(integrate.integration_branch(self.cfg, "release/2.0"),
                         "pdca-integration/release-2.0")

    def test_nothing_to_fold(self) -> None:
        self.assertEqual(integrate.fold(self.cfg, []), (None, None))
        no_patch = self._bundle("NP", patch=None)            # close/no-fix: nothing to ship
        self.assertEqual(integrate.fold(self.cfg, [no_patch]), (None, None))

    def test_dry_run_shells_nothing(self) -> None:
        b = self._bundle("D1")
        with mock.patch("pdca_harness.integrate.subprocess.run") as m, \
                redirect_stdout(io.StringIO()) as out:
            branch, wt = integrate.fold(self.cfg, [b], dry_run=True)
        self.assertEqual(branch, "pdca-integration/main")
        self.assertIsNone(wt)
        m.assert_not_called()                                 # no git in a dry-run
        self.assertIn("pdca-integration/main", out.getvalue())

    def test_different_target_excluded_from_fold(self) -> None:
        same = self._bundle("S1", target="org/repo @ main")
        other = self._bundle("O1", target="other/repo @ main")
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            integrate.fold(self.cfg, [same, other], dry_run=True)
        self.assertIn("excluded from the fold", err.getvalue())
        self.assertIn("issue_O1", err.getvalue())
        self.assertIn("fold 1 patch(es)", out.getvalue())     # only the same-target one


class FoldGit(unittest.TestCase):
    """Real git: fold patches onto the integration branch off a bare origin."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.primary = self.tmp / "repo"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.primary)], check=True)
        self._cfg_git(self.primary)
        (self.primary / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(self.primary, "add", "-A")
        self._git(self.primary, "commit", "-q", "-m", "base")
        self._git(self.primary, "remote", "add", "origin", str(self.origin))
        self._git(self.primary, "push", "-q", "origin", "main")
        self.cfg = _cfg(self.tmp, "org/repo", self.primary)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)

    def _cfg_git(self, repo: Path) -> None:
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "Tester")
        self._git(repo, "config", "commit.gpgsign", "false")

    def _modify_patch(self, new_content: str) -> str:
        """A valid patch that rewrites base.txt to ``new_content`` (generated by git)."""
        (self.primary / "base.txt").write_text(new_content, encoding="utf-8")
        diff = subprocess.run(["git", "-C", str(self.primary), "diff"],
                              capture_output=True, text=True).stdout
        self._git(self.primary, "checkout", "--", "base.txt")
        return diff

    def _add_patch(self, name: str, content: str) -> str:
        """A valid patch that adds a new file (generated by git)."""
        (self.primary / name).write_text(content, encoding="utf-8")
        self._git(self.primary, "add", name)
        diff = subprocess.run(["git", "-C", str(self.primary), "diff", "--cached"],
                              capture_output=True, text=True).stdout
        self._git(self.primary, "reset", "-q", "HEAD", name)
        (self.primary / name).unlink()
        return diff

    def _bundle(self, iid: str, patch: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** org/repo @ main\n",
            encoding="utf-8")
        (d / "patch.diff").write_text(patch, encoding="utf-8")
        return d

    def _pushed(self, branch: str) -> bool:
        out = subprocess.run(
            ["git", "-C", str(self.primary), "ls-remote", "--heads", "origin", branch],
            capture_output=True, text=True).stdout
        return branch in out

    def test_single_fold_pushes_branch(self) -> None:
        b = self._bundle("F1", self._modify_patch("one\n"))
        branch, wt = integrate.fold(self.cfg, [b])
        self.assertEqual(branch, "pdca-integration/main")
        self.assertIsNotNone(wt)
        self.assertEqual((wt / "base.txt").read_text(encoding="utf-8"), "one\n")
        self.assertTrue(self._pushed("pdca-integration/main"))

    def test_multi_disjoint_fold_carries_all(self) -> None:
        # A modify + an add (disjoint) both land on the branch — the multi-parent fold
        # the old _stack_base_branch parents[0] could not express.
        b1 = self._bundle("M1", self._modify_patch("one\n"))
        b2 = self._bundle("M2", self._add_patch("feature.txt", "hi\n"))
        branch, wt = integrate.fold(self.cfg, [b1, b2])
        self.assertEqual((wt / "base.txt").read_text(encoding="utf-8"), "one\n")
        self.assertTrue((wt / "feature.txt").is_file())
        self.assertTrue(self._pushed(branch))

    def test_overlap_raises_integration_error(self) -> None:
        # Two patches that each rewrite base.txt's only line — the second can't apply onto
        # the first, an undeclared cross-wave overlap → a loud STOP.
        b1 = self._bundle("C1", self._modify_patch("one\n"))
        b2 = self._bundle("C2", self._modify_patch("two\n"))
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError):
                integrate.fold(self.cfg, [b1, b2])


if __name__ == "__main__":
    unittest.main()
