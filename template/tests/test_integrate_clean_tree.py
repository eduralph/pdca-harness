"""A fold starts from a clean integration tree (#631). One integration worktree per target
is reused by every fold of a run, by the #646 pre-wave carry, and by the between-waves
re-gate. Before: a fold reused the tree as it found it, so a re-gate's build output (an
untracked file a later branch adds, a rewritten tracked file a later branch changes) or a
merge left in progress made the next fold's ``checkout -B`` / ``git merge`` refuse, and the
run stopped with "did not integrate"; and a failed ``git merge --abort`` went unseen, so the
carry's per-bundle ``skipped`` mode merged the next bundle into a mid-merge tree.

These cases pin the fix: every fold resets the tree to exactly its start ref (untracked,
non-ignored files removed, a nested git repository among them; ignored files kept), and a
failed merge puts the tree back to the line tip it had before that merge — raising
``IntegrationTreeError`` only if that reset fails. Real git against a
bare ``origin`` + a primary checkout (the ``StackFoldGit`` shape of
``tests/test_integrate_stack_bases.py``, its small fixture copied here); the re-gate is the
real ``gates.run_integration``.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail.
    PYTHONPATH=src python -m unittest tests.test_integrate_clean_tree
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import gates, integrate
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TARGET = ("org/repo", "main")
LINE = "pdca-integration/main"


def _cfg(root: Path, primary: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        publisher=LeafConfig(mode="stub", interactive=True), gates_checks=[],
        base_remote="origin", repo_checkouts={"org/repo": str(primary)})


def _gate(cmd: str) -> dict:
    """A repo-scoped gate — the kind the between-waves re-gate runs in the tree."""
    return {"id": "BUILD", "tier": "T3", "label": "build", "cmd": cmd, "scope": "repo",
            "gating": True}


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _git(repo: Path, *args: str) -> str:
    r = _run(repo, *args)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {r.stderr}")
    return r.stdout


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "fetch.prune", "false")


class FoldStartsClean(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.primary = self.tmp / "repo"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.primary)], check=True)
        _identity(self.primary)
        (self.primary / "base.txt").write_text("base\n", encoding="utf-8")
        _git(self.primary, "add", "-A")
        _git(self.primary, "commit", "-q", "-m", "base")
        _git(self.primary, "remote", "add", "origin", str(self.origin))
        _git(self.primary, "push", "-q", "origin", "main")
        self.cfg = _cfg(self.tmp, self.primary)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------------------

    def _publish(self, iid: str, files: dict[str, str], *, cut_from: str = "origin/main") -> Path:
        """What `publish` does for an accepted bundle: cut ``fix/<iid>`` off ``cut_from``,
        commit ``files`` signed off, push it to origin, record it in publish.json."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** org/repo @ main\n",
            encoding="utf-8")
        branch = f"fix/{iid}"
        _git(self.primary, "fetch", "-q", "origin")
        _git(self.primary, "checkout", "-q", "-B", branch, cut_from)
        for name, text in files.items():
            path = self.primary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(self.primary, "add", "--all")
        _git(self.primary, "commit", "-q", "-s", "-m", f"fix {iid}")
        (d / "patch.diff").write_text(_git(self.primary, "diff", "HEAD~1", "HEAD"),
                                      encoding="utf-8")
        _git(self.primary, "push", "-q", "origin", branch)
        _git(self.primary, "checkout", "-q", "main")
        mode = "new-pr" if cut_from == "origin/main" else "stacked-pr"
        (d / "publish.json").write_text(json.dumps(
            {"mode": mode, "branch": branch, "base": "main", "repo": "org/repo",
             "pr_url": f"https://example.test/pr/{iid}"}), encoding="utf-8")
        return d

    def _tip(self, branch: str) -> str:
        return _git(self.origin, "rev-parse", f"refs/heads/{branch}").strip()

    def _show(self, rev: str, path: str) -> str:
        return _git(self.origin, "show", f"{rev}:{path}")

    def _branch_tip(self, iid: str) -> str:
        return self._tip(f"fix/{iid}")

    def _in_line(self, commit: str) -> bool:
        return _run(self.origin, "merge-base", "--is-ancestor", commit,
                    f"refs/heads/{LINE}").returncode == 0

    def _regate(self, wt: Path, cmd: str) -> None:
        """The between-waves re-gate: the real repo-scoped gate run, from the fold's tree."""
        cfg = dataclasses.replace(self.cfg, gates_checks=[_gate(cmd)])
        self.assertEqual(gates.run_integration(cfg, wt)["overall"], "pass")

    def _fold_a(self, files: dict[str, str] | None = None) -> tuple[Path, Path, str]:
        a = self._publish("A", files or {"a.txt": "a\n"})
        res = integrate.fold(self.cfg, [a], folded_this_run={})
        return a, res[TARGET][1], self._tip(LINE)

    # -- (1) a re-gate's untracked build output -------------------------------------------

    def test_untracked_regate_output_does_not_block_the_next_fold(self) -> None:
        a, wt, t1 = self._fold_a()
        self._regate(wt, "mkdir -p out && echo gate > out/build.txt")
        self.assertEqual((wt / "out/build.txt").read_text(encoding="utf-8"), "gate\n")
        b = self._publish("B", {"out/build.txt": "b\n"})
        integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        tip = self._tip(LINE)
        self.assertTrue(self._in_line(self._branch_tip("B")))
        self.assertEqual(self._show(tip, "out/build.txt"), "b\n")
        self.assertEqual((wt / "out/build.txt").read_text(encoding="utf-8"), "b\n")

    # -- (2) a re-gate's rewrite of a tracked file the next branch changes ----------------

    def test_modified_tracked_file_does_not_block_the_next_fold(self) -> None:
        a, wt, t1 = self._fold_a()
        self._regate(wt, "echo gate > a.txt")
        self.assertEqual((wt / "a.txt").read_text(encoding="utf-8"), "gate\n")
        b = self._publish("B", {"a.txt": "b\n"}, cut_from=f"origin/{LINE}")
        integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        tip = self._tip(LINE)
        self.assertTrue(self._in_line(self._branch_tip("B")))
        self.assertEqual(self._show(tip, "a.txt"), "b\n")

    # -- (1), a nested repository: a gate that clones or `git init`s into the tree ---------

    def test_untracked_nested_repo_does_not_block_the_next_fold(self) -> None:
        a, wt, t1 = self._fold_a()
        self._regate(wt, "git init -q out && echo gate > out/build.txt")
        self.assertTrue((wt / "out/.git").is_dir())        # really a repository of its own
        b = self._publish("B", {"out/build.txt": "b\n"})
        integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        self.assertTrue(self._in_line(self._branch_tip("B")))
        self.assertEqual(self._show(self._tip(LINE), "out/build.txt"), "b\n")

    # -- (3) a failed `merge --abort` in the carry's per-bundle `skipped` mode -------------

    def _abort_fails(self, *, reset_fails_after_merge: bool = False):
        """Patch ``integrate._git`` so ``git merge --abort`` fails — a fold that relies on it
        really leaves the tree mid-merge — and, optionally, every ``git reset`` after the
        fold's first merge attempt fails too. Every other git call is real."""
        real = integrate._git
        merged: list[bool] = []

        def spy(repo: Path, *args: str) -> int:
            if args[:2] == ("merge", "--abort"):
                return 128
            if args[:1] == ("merge",):
                merged.append(True)
            if reset_fails_after_merge and merged and args[:1] == ("reset",):
                return 128
            return real(repo, *args)

        return mock.patch.object(integrate, "_git", spy)

    def test_failed_abort_resets_the_tree_and_the_carry_goes_on(self) -> None:
        a, wt, t1 = self._fold_a()
        x = self._publish("X", {"a.txt": "x\n"})          # add/add conflict with the line
        b = self._publish("B", {"b.txt": "b\n"})
        skipped: dict[str, str] = {}
        with self._abort_fails():
            integrate.fold(self.cfg, [x, b], folded_this_run={TARGET: t1}, skipped=skipped)
        self.assertIn("issue_X", skipped)
        self.assertNotIn("issue_B", skipped, skipped.get("issue_B"))
        self.assertTrue(self._in_line(self._branch_tip("B")))
        self.assertFalse(self._in_line(self._branch_tip("X")))
        self.assertTrue(self._in_line(t1))
        self.assertNotEqual(_run(wt, "rev-parse", "--verify", "--quiet",
                                 "MERGE_HEAD").returncode, 0)   # no merge left in progress

    def test_failed_abort_and_failed_reset_stops_the_fold(self) -> None:
        a, wt, t1 = self._fold_a()
        x = self._publish("X", {"a.txt": "x\n"})
        b = self._publish("B", {"b.txt": "b\n"})
        skipped: dict[str, str] = {}
        with self._abort_fails(reset_fails_after_merge=True), \
                self.assertRaises(integrate.IntegrationError) as caught:
            integrate.fold(self.cfg, [x, b], folded_this_run={TARGET: t1}, skipped=skipped)
        # The tree error itself, not an ordinary per-bundle failure: the carry's `skipped`
        # mode must not absorb it — X is not recorded as a skip, and B is never tried.
        self.assertIsInstance(caught.exception, integrate.IntegrationTreeError)
        self.assertEqual(skipped, {})                      # neither issue_X nor issue_B
        self.assertEqual(self._tip(LINE), t1)              # nothing pushed

    # -- (4) a tree left mid-merge by an earlier user -------------------------------------

    def test_fold_starts_clean_from_a_tree_left_mid_merge(self) -> None:
        a, wt, t1 = self._fold_a()
        self._publish("X", {"a.txt": "x\n"})
        self.assertNotEqual(_run(wt, "merge", "--no-edit", "fix/X").returncode, 0)
        self.assertEqual(_run(wt, "rev-parse", "--verify", "--quiet",
                              "MERGE_HEAD").returncode, 0)  # really mid-merge
        b = self._publish("B", {"b.txt": "b\n"})
        integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        self.assertTrue(self._in_line(self._branch_tip("B")))
        self.assertFalse(self._in_line(self._branch_tip("X")))
        self.assertEqual(self._show(self._tip(LINE), "a.txt"), "a\n")

    # -- ignored files are outside the reset (no `clean -x`) ------------------------------

    def test_ignored_build_cache_is_kept(self) -> None:
        a, wt, t1 = self._fold_a({"a.txt": "a\n", ".gitignore": "cache/\n"})
        self._regate(wt, "mkdir -p cache && echo warm > cache/obj")
        b = self._publish("B", {"b.txt": "b\n"})
        integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        self.assertTrue(self._in_line(self._branch_tip("B")))
        self.assertEqual((wt / "cache/obj").read_text(encoding="utf-8"), "warm\n")


if __name__ == "__main__":
    unittest.main()
