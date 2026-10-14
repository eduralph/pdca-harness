"""Issue #650: merge mode reads a PR's head from the remote publish pushed it to.

An ``Onto branch`` bundle is published by ``publish._publish_stacked`` onto
``<remote>/<branch>`` and recorded as ``{"mode": "stacked", "base": "<remote>/<branch>",
"branch": "<branch>", …}``. When ``<remote>`` is neither the base remote nor ``origin``,
the PR's head commit exists only there. ``_merge_one``'s behind-base read (#531) used to
fetch only the base remote and ``origin``, so ``git merge-base --is-ancestor`` exited 128
on the head and the run refused with a bare "git failed". It now fetches the remote the
record names too (read via ``integrate._published_ref``, as the fold reads it), and a head
still missing after the fetches is refused by name.

That refusal says what was checked and nothing more: the commit, the checkout, the remotes
fetched, and where the record puts the branch (that remote was fetched and did not bring
the commit in) or that the record names no remote. It does not guess a cause, so a remote
that WAS fetched is never blamed as left out: a fetch refspec that skips the branch, or a
``new-pr`` head that is not on ``origin``, is reported as exactly that.

Real git throughout: three bare repos as the remotes of one primary checkout. Only ``gh``
is scripted; every ``git`` call goes to the real ``subprocess.run``. Everything is driven
through ``merge._merge_one``, never a helper the fix changes. ``merge._wait_for_green`` is
patched to record the head the rollup wait was reached with, and to stop there with a
non-green verdict unless a case scripts a green. Run from the project root:
    PYTHONPATH=src python -m unittest tests.test_merge_third_remote
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import merge, state
from pdca_harness.config import Config, LeafConfig

_REAL_RUN = subprocess.run
_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "commit.gpgsign=false"]
PR = "https://gh/pr/650"
REMOTES = ("origin", "upstream", "other")
STACKED = {"mode": "stacked", "branch": "feat", "base": "other/feat"}
NEW_PR = {"mode": "new-pr", "branch": "feat"}


def _g(cwd: Path, *args: str) -> str:
    """Real git for the fixture, never routed through the patched `subprocess.run`."""
    r = _REAL_RUN(["git", "-C", str(cwd), *_ID, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"fixture git {args} failed: {r.stderr}")
    return r.stdout.strip()


def _init(path: Path, *flags: str) -> None:
    _REAL_RUN(["git", "init", "-q", *flags, str(path)], check=True, capture_output=True)


class ThirdRemoteHead(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in REMOTES:
            _init(self.tmp / f"{name}.git", "--bare")
        # A scratch clone that authors the commits and pushes them where each case wants.
        self.seed = self.tmp / "seed"
        _init(self.seed, "-b", "main")
        for name in REMOTES:
            _g(self.seed, "remote", "add", name, str(self.tmp / f"{name}.git"))
        (self.seed / "f.txt").write_text("base\n", encoding="utf-8")
        _g(self.seed, "add", "f.txt")
        _g(self.seed, "commit", "-q", "-m", "base")
        _g(self.seed, "push", "-q", "upstream", "main")
        _g(self.seed, "push", "-q", "origin", "main")
        _g(self.seed, "checkout", "-q", "-b", "feat")
        (self.seed / "g.txt").write_text("feat\n", encoding="utf-8")
        _g(self.seed, "add", "g.txt")
        _g(self.seed, "commit", "-q", "-m", "feat")
        self.feat = _g(self.seed, "rev-parse", "HEAD")
        # The primary checkout merge mode reads in: all three remotes, nothing fetched yet.
        self.repo = self.tmp / "repo"
        _init(self.repo)
        for name in REMOTES:
            _g(self.repo, "remote", "add", name, str(self.tmp / f"{name}.git"))
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results", process_dir=self.tmp / "p",
            templates_dir=self.tmp / "t", default_branch="main", tracker_system="github",
            tracker_url="", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
            base_remote="upstream", repo_checkouts={"org/repo": str(self.repo)},
            merge_requires="all", merge_wait_secs=0)
        self.head = self.feat      # the head `gh pr view` reports
        self.update = None         # what `gh pr update-branch` does, if a case scripts it
        self.verdict = ("failing", "recorded by the test; stop here")
        self.fetches: list[list[str]] = []
        self.waited: list[str] = []
        self.gh_calls: list[list[str]] = []

    def _bundle(self, record: dict) -> Path:
        d = self.cfg.bundle("650")
        d.mkdir(parents=True, exist_ok=True)
        (d / "patch.diff").write_text("diff\n", encoding="utf-8")
        (d / "publish.json").write_text(
            json.dumps({"pr_url": PR, "repo": "org/repo", **record}), encoding="utf-8")
        return d

    def _fetched(self, *remotes: str) -> list[list[str]]:
        """The argv of a `git fetch` of each of ``remotes`` in the primary checkout."""
        return [["git", "-C", str(self.repo), "fetch", r] for r in remotes]

    def _run(self, cmd, *args, **kw):
        if cmd[:1] == ["git"]:
            if "fetch" in cmd:
                self.fetches.append(list(cmd))
            return _REAL_RUN(cmd, *args, **kw)
        if cmd[:2] != ["gh", "pr"]:
            raise AssertionError(f"unexpected command: {cmd}")
        self.gh_calls.append(list(cmd))
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[2] == "view" and cmd[cmd.index("--json") + 1] == "headRefOid,baseRefName":
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"headRefOid": self.head, "baseRefName": "main"}))
        if cmd[2] == "update-branch" and self.update:
            self.head = self.update()
            return ok
        if cmd[2] in ("ready", "merge"):
            return ok
        raise AssertionError(f"unexpected gh call: {cmd}")

    def _wait(self, pr_url, wait_secs, *, spent=0):
        self.waited.append(self.head)
        return self.verdict

    def _merge(self, d: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=self._run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                mock.patch.object(merge, "_wait_for_green", side_effect=self._wait), \
                mock.patch.object(merge, "_sleep"), \
                redirect_stdout(out), redirect_stderr(err):
            rc = merge._merge_one(self.cfg, d, dry_run=False, method="merge",
                                  fetched=set())
        return rc, out.getvalue(), err.getvalue()

    def _missing(self, *fetched: str) -> str:
        """How the refusal opens for a head the fetches of ``fetched`` did not bring in."""
        return (f"its head commit {self.feat} is not in the checkout {self.repo} after "
                f"fetching {', '.join(fetched)};")

    # (1) the head lives only on the third remote the record names
    def test_stacked_head_on_third_remote_reaches_rollup_wait(self) -> None:
        _g(self.seed, "push", "-q", "other", "feat")
        rc, out, err = self._merge(self._bundle(STACKED))
        self.assertNotIn("git failed", err)
        self.assertEqual(self.waited, [self.feat], err)
        self.assertIn(f"head {self.feat[:12]} contains base commit", out)
        self.assertEqual(self.fetches, self._fetched("upstream", "origin", "other"))
        self.assertNotIn(["gh", "pr", "update-branch", PR], self.gh_calls)  # up to date

    def test_stacked_head_behind_is_updated_on_third_remote_and_reaches_wait(self) -> None:
        _g(self.seed, "push", "-q", "other", "feat")
        # The base moves after the branch was cut, so the head is behind it.
        _g(self.seed, "checkout", "-q", "main")
        (self.seed / "f.txt").write_text("base moved\n", encoding="utf-8")
        _g(self.seed, "commit", "-q", "-am", "base moves")
        _g(self.seed, "push", "-q", "upstream", "main")
        _g(self.seed, "fetch", "-q", "upstream")

        def update() -> str:  # the host merges the base into other/feat, and only there
            _g(self.seed, "checkout", "-q", "feat")
            _g(self.seed, "merge", "-q", "--no-edit", "upstream/main")
            _g(self.seed, "push", "-q", "other", "feat")
            return _g(self.seed, "rev-parse", "HEAD")

        self.update = update
        rc, out, err = self._merge(self._bundle(STACKED))
        self.assertNotIn("git failed", err)
        self.assertIn(["gh", "pr", "update-branch", PR], self.gh_calls)
        updated = _g(self.seed, "rev-parse", "HEAD")
        self.assertNotEqual(updated, self.feat)
        self.assertIn(f"updated: head {updated[:12]} contains base commit", out)
        self.assertEqual(self.waited, [updated], err)

    def test_stacked_green_merges_pinned_to_the_head_on_third_remote(self) -> None:
        # The read after the green fetches the record's remote again, then the merge is
        # pinned to the head read there.
        _g(self.seed, "push", "-q", "other", "feat")
        self.verdict = ("green", "1 check")
        rc, out, err = self._merge(self._bundle(STACKED))
        self.assertEqual(rc, 0, err)
        self.assertIn(["gh", "pr", "merge", PR, "--merge", "--match-head-commit",
                       self.feat], self.gh_calls)
        self.assertEqual(self.fetches,
                         self._fetched("upstream", "origin", "other") * 2
                         + self._fetched("upstream"))   # the post-merge base refresh

    # (2) a head no fetched remote has is refused by name
    def test_head_in_no_fetched_remote_is_named(self) -> None:
        # `feat` is pushed nowhere: no remote the run fetches has it.
        rc, out, err = self._merge(self._bundle(STACKED))
        self.assertEqual(rc, 1)
        self.assertEqual(self.waited, [])
        self.assertNotIn("git failed", err)
        self.assertIn(self._missing("upstream", "origin", "other"), err)
        self.assertIn("its publish record puts its branch at other/feat, and fetching "
                      "other did not bring that commit in", err)
        self.assertIn(["gh", "pr", "ready", PR, "--undo"], self.gh_calls)

    # The refusal reports what was checked, never a cause it did not check: a remote that
    # was fetched is not blamed as left out.
    def test_record_remote_fetched_without_the_head_is_not_blamed_as_left_out(self) -> None:
        # `other` holds the head, at the branch the record names, but this checkout's fetch
        # refspec for `other` brings only `main`: the fetch runs and succeeds, and the head
        # is still not here.
        _g(self.seed, "push", "-q", "other", "main", "feat")
        _g(self.repo, "config", "remote.other.fetch",
           "+refs/heads/main:refs/remotes/other/main")
        self.assertEqual(_g(self.repo, "ls-remote", "other", "refs/heads/feat").split()[0],
                         self.feat)
        rc, out, err = self._merge(self._bundle(STACKED))
        self.assertEqual(rc, 1)
        self.assertEqual(self.waited, [])
        self.assertEqual(self.fetches, self._fetched("upstream", "origin", "other"))
        self.assertNotIn("git failed", err)
        self.assertIn(self._missing("upstream", "origin", "other"), err)
        self.assertIn("its publish record puts its branch at other/feat, and fetching "
                      "other did not bring that commit in", err)
        self.assertNotIn("does not say which remote", err)

    def test_new_pr_head_not_on_origin_says_origin_was_fetched(self) -> None:
        # A `new-pr` record's branch is on `origin`, which is always fetched. A head that is
        # not there (here it was pushed only to `other`) is reported as that.
        _g(self.seed, "push", "-q", "other", "feat")
        rc, out, err = self._merge(self._bundle(NEW_PR))
        self.assertEqual(rc, 1)
        self.assertEqual(self.waited, [])
        self.assertEqual(self.fetches, self._fetched("upstream", "origin"))
        self.assertNotIn("git failed", err)
        self.assertIn(self._missing("upstream", "origin"), err)
        self.assertIn("its publish record puts its branch at origin/feat, and fetching "
                      "origin did not bring that commit in", err)
        self.assertNotIn("does not say which remote", err)

    def test_a_head_git_cannot_read_is_a_git_failure_not_a_missing_commit(self) -> None:
        # Only `git cat-file -e` exiting 1 means "not in the checkout". Any other exit is
        # git failing, and the refusal says that instead of claiming the commit is missing.
        _g(self.seed, "push", "-q", "other", "feat")
        self.head = "not-a-commit"
        rc, out, err = self._merge(self._bundle(STACKED))
        self.assertEqual(rc, 1)
        self.assertEqual(self.waited, [])
        self.assertIn("git failed: `git cat-file -e not-a-commit` in "
                      f"{self.repo} exited 128", err)
        self.assertNotIn("not in the checkout", err)

    # (2b) a stacked record that names no remote: today's fetches, no up-front refusal, and
    # the (2) refusal if the head is still missing
    NAMES_NO_REMOTE = ({"mode": "stacked", "base": "other/feat"},
                       {"mode": "stacked", "branch": "feat", "base": "other/elsewhere"})

    def test_stacked_record_naming_no_remote_fetches_as_today_then_names_head(self) -> None:
        _g(self.seed, "push", "-q", "other", "feat")
        for record in self.NAMES_NO_REMOTE:
            with self.subTest(record=record):
                self.fetches.clear()
                rc, out, err = self._merge(self._bundle(record))
                self.assertEqual(rc, 1)
                self.assertEqual(self.waited, [])
                self.assertEqual(self.fetches, self._fetched("upstream", "origin"))
                self.assertNotIn("git failed", err)
                self.assertIn(self._missing("upstream", "origin"), err)
                self.assertIn("its publish record does not say which remote its branch is "
                              "on, so no other remote was fetched", err)
                self.assertNotIn("puts its branch at", err)

    def test_stacked_record_naming_no_remote_is_not_refused_up_front(self) -> None:
        _g(self.seed, "push", "-q", "origin", "feat")
        for record in self.NAMES_NO_REMOTE:
            with self.subTest(record=record):
                self.fetches.clear()
                self.waited.clear()
                rc, out, err = self._merge(self._bundle(record))
                self.assertEqual(self.fetches, self._fetched("upstream", "origin"))
                self.assertEqual(self.waited, [self.feat], err)

    # (3) unchanged: a new-pr record fetches the base remote, then origin, deduplicated
    def test_new_pr_record_fetches_exactly_base_remote_then_origin(self) -> None:
        _g(self.seed, "push", "-q", "origin", "feat")
        for base_remote, fetched in (("upstream", ("upstream", "origin")),
                                     ("origin", ("origin",))):
            with self.subTest(base_remote=base_remote):
                self.cfg.base_remote = base_remote
                self.fetches.clear()
                self.waited.clear()
                rc, out, err = self._merge(self._bundle(NEW_PR))
                self.assertEqual(self.fetches, self._fetched(*fetched))
                self.assertEqual(self.waited, [self.feat], err)


if __name__ == "__main__":
    unittest.main()
