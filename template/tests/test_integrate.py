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

    def test_integration_branch_name_is_injective_per_base(self) -> None:
        self.assertEqual(integrate.integration_branch(self.cfg, "main"),
                         "pdca-integration/main")
        # `/` → `-`, but existing `-` is doubled first, so two bases that differ only by
        # `/` vs `-` never collide onto one branch / worktree (#187).
        self.assertEqual(integrate.integration_branch(self.cfg, "release/2.0"),
                         "pdca-integration/release-2.0")
        self.assertEqual(integrate.integration_branch(self.cfg, "release-2.0"),
                         "pdca-integration/release--2.0")
        self.assertNotEqual(integrate.integration_branch(self.cfg, "release/2.0"),
                            integrate.integration_branch(self.cfg, "release-2.0"))

    def test_nothing_to_fold(self) -> None:
        self.assertEqual(integrate.fold(self.cfg, []), {})
        no_patch = self._bundle("NP", patch=None)            # close/no-fix: nothing to ship
        self.assertEqual(integrate.fold(self.cfg, [no_patch]), {})

    def test_dry_run_shells_nothing(self) -> None:
        b = self._bundle("D1")
        with mock.patch("pdca_harness.integrate.subprocess.run") as m, \
                redirect_stdout(io.StringIO()) as out:
            folded = integrate.fold(self.cfg, [b], dry_run=True)
        self.assertEqual(folded, {("org/repo", "main"): ("pdca-integration/main", None)})
        m.assert_not_called()                                 # no git in a dry-run
        self.assertIn("pdca-integration/main", out.getvalue())

    def test_each_target_gets_its_own_integration_line(self) -> None:
        # A batch spanning two (repo, base) targets folds one line per target — not a single
        # global branch a sibling-target bundle would wrongly stack on (#187).
        a = self._bundle("S1", target="org/repo @ main")
        b = self._bundle("O1", target="other/repo @ develop")
        self.cfg.repo_checkouts["other/repo"] = str(self.tmp / "other")
        with redirect_stdout(io.StringIO()) as out:
            folded = integrate.fold(self.cfg, [a, b], dry_run=True)
        self.assertEqual(folded, {
            ("org/repo", "main"): ("pdca-integration/main", None),
            ("other/repo", "develop"): ("pdca-integration/develop", None)})
        self.assertNotIn("excluded", out.getvalue())          # nothing dropped anymore
        self.assertIn("fold 1 patch(es) onto pdca-integration/main", out.getvalue())
        self.assertIn("fold 1 patch(es) onto pdca-integration/develop", out.getvalue())


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
        folded = integrate.fold(self.cfg, [b])
        branch, wt = folded[("org/repo", "main")]
        self.assertEqual(branch, "pdca-integration/main")
        self.assertIsNotNone(wt)
        self.assertEqual((wt / "base.txt").read_text(encoding="utf-8"), "one\n")
        self.assertTrue(self._pushed("pdca-integration/main"))

    def test_multi_disjoint_fold_carries_all(self) -> None:
        # A modify + an add (disjoint) both land on the branch — the multi-parent fold
        # the old _stack_base_branch parents[0] could not express.
        b1 = self._bundle("M1", self._modify_patch("one\n"))
        b2 = self._bundle("M2", self._add_patch("feature.txt", "hi\n"))
        branch, wt = integrate.fold(self.cfg, [b1, b2])[("org/repo", "main")]
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


class PointAtIntegration(unittest.TestCase):
    """`flow._point_at_integration` writes each later-wave bundle's stack base from the
    integration line matching *its own* (repo, base) — the second half of the #187 fix
    (fold tracks per target; the driver routes per target)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, "org/repo", self.tmp / "repo")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, target: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** {target}\n",
            encoding="utf-8")
        return d

    def test_routes_each_bundle_to_its_own_target_branch(self) -> None:
        from pdca_harness import flow, publish
        a = self._bundle("A", "org/repo @ main")
        b = self._bundle("B", "other/repo @ develop")
        c = self._bundle("C", "third/repo @ main")          # not integrated → off its own base
        integ = {("org/repo", "main"): "pdca-integration/main",
                 ("other/repo", "develop"): "pdca-integration/develop"}
        flow._point_at_integration(integ, [a, b, c])
        self.assertEqual(publish._read_stack_base(a), "pdca-integration/main")
        self.assertEqual(publish._read_stack_base(b), "pdca-integration/develop")  # not main!
        self.assertEqual(publish._read_stack_base(c), "")   # no integ line → builds off base

    def test_clears_a_stale_stack_base_for_an_un_integrated_target(self) -> None:
        # A bundle carrying a stack base from a prior/resumed run whose target isn't integrated
        # this run must have it CLEARED, else it builds against an old integration branch (#187).
        from pdca_harness import flow, publish
        d = self._bundle("D", "third/repo @ main")
        publish.write_stack_base(d, "pdca-integration/stale")     # left by a prior run
        flow._point_at_integration({("org/repo", "main"): "pdca-integration/main"}, [d])
        self.assertEqual(publish._read_stack_base(d), "")          # cleared → off its own base


if __name__ == "__main__":
    unittest.main()
