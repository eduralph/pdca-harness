"""Issue #650: merge mode reads a PR's head from the remote publish pushed it to.

An ``Onto branch`` bundle is published by ``publish._publish_stacked`` onto
``<remote>/<branch>`` and recorded as ``{"mode": "stacked", "base": "<remote>/<branch>",
"branch": "<branch>", …}``. When ``<remote>`` is neither the base remote nor ``origin``,
the PR's head commit exists only there. The behind-base read (#531, ``merge._base_read``)
used to fetch only the base remote and ``origin``, so ``git merge-base --is-ancestor``
exited 128 on the head and the run refused with a bare "git failed". It now fetches the
remote the record names too (read via ``integrate._published_ref``, as the fold reads it),
and a head still missing after the fetches is refused by name.

That refusal says what was checked and nothing more: the commit, the checkout, the remotes
fetched, and where the record puts the branch (that remote was fetched and did not bring
the commit in) or that the record names no remote. It does not guess a cause, so a remote
that WAS fetched is never blamed as left out: a fetch refspec that skips the branch, or a
``new-pr`` head that is not on ``origin``, is reported as exactly that.

Issue #658: ``merge._merge_one`` now refuses a ``"stacked"`` record before it reads the
head at all — merge mode never readies or merges a PR the run only added a commit to — so
a ``"stacked"`` record no longer reaches that read through ``merge_wave``. The cases about
readying, waiting on or merging such a PR assert that refusal instead. The cases about the
fetch / missing-head logic for a ``"stacked"`` record (``_remotes``, ``_fetch``, ``_have``)
call ``merge._base_read`` directly with the ``pub`` that ``integrate._published_ref`` gives
for the record, which is the code #650 added and #658 keeps. ``new-pr`` records are still
driven through ``merge._merge_one``.

Real git throughout: three bare repos as the remotes of one primary checkout. Only ``gh``
is scripted; every ``git`` call goes to the real ``subprocess.run``. In the
``_merge_one`` cases ``merge._wait_for_green`` is patched to record the head the rollup
wait was reached with, and to stop there with a non-green verdict. Run from the project
root:
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

from pdca_harness import integrate, merge, state
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

    def _wait(self, pr_url, wait_secs, *, spent=0, expected=()):
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

    def _read(self, d: Path) -> tuple[str, str, bool | None, str]:
        """``merge._base_read`` for bundle ``d``'s PR, with the ``pub`` the fold reads from
        its publish record (``integrate._published_ref``) — the path a ``"stacked"`` record
        no longer reaches through ``_merge_one`` (issue #658)."""
        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=self._run):
            return merge._base_read(self.cfg, self.repo, integrate._published_ref(d), PR)

    def _assert_refused_up_front(self, rc: int, err: str) -> None:
        """Issue #658: a ``"stacked"`` record is refused before any ``gh`` or ``git`` call."""
        self.assertEqual(rc, 1)
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.waited, [])
        self.assertIn("`Onto branch` commit added to an existing PR", err)

    def _missing(self, *fetched: str) -> str:
        """How the refusal opens for a head the fetches of ``fetched`` did not bring in."""
        return (f"its head commit {self.feat} is not in the checkout {self.repo} after "
                f"fetching {', '.join(fetched)};")

    # (1) issue #658: a "stacked" record no longer reaches the ready-mark, the rollup wait
    # or the merge, whether its head is up to date, behind, or would read green
    def test_stacked_record_is_refused_before_the_ready_mark(self) -> None:
        _g(self.seed, "push", "-q", "other", "feat")
        for verdict in (("failing", "recorded by the test; stop here"), ("green", "1 check")):
            with self.subTest(verdict=verdict[0]):
                self.verdict = verdict
                self.gh_calls.clear()
                self.fetches.clear()
                rc, out, err = self._merge(self._bundle(STACKED))
                self._assert_refused_up_front(rc, err)

    # The read #650 added still finds a head that lives only on the third remote.
    def test_stacked_head_on_third_remote_is_read_there(self) -> None:
        _g(self.seed, "push", "-q", "other", "feat")
        head, tip, behind, why = self._read(self._bundle(STACKED))
        self.assertEqual(why, "")
        self.assertEqual(head, self.feat)
        self.assertIs(behind, False)
        self.assertEqual(self.fetches, self._fetched("upstream", "origin", "other"))

    # (2) a head no fetched remote has is refused by name
    def test_head_in_no_fetched_remote_is_named(self) -> None:
        # `feat` is pushed nowhere: no remote the run fetches has it.
        head, tip, behind, err = self._read(self._bundle(STACKED))
        self.assertIsNone(behind)
        self.assertNotIn("git failed", err)
        self.assertIn(self._missing("upstream", "origin", "other"), err)
        self.assertIn("its publish record puts its branch at other/feat, and fetching "
                      "other did not bring that commit in", err)

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
        head, tip, behind, err = self._read(self._bundle(STACKED))
        self.assertIsNone(behind)
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
        head, tip, behind, err = self._read(self._bundle(STACKED))
        self.assertIsNone(behind)
        self.assertIn("git failed: `git cat-file -e not-a-commit` in "
                      f"{self.repo} exited 128", err)
        self.assertNotIn("not in the checkout", err)

    # (2b) a stacked record that names no remote: the read fetches as for any other record,
    # and the (2) refusal if the head is still missing. Through `_merge_one` it is refused
    # up front like every "stacked" record (issue #658).
    NAMES_NO_REMOTE = ({"mode": "stacked", "base": "other/feat"},
                       {"mode": "stacked", "branch": "feat", "base": "other/elsewhere"})

    def test_stacked_record_naming_no_remote_fetches_as_today_then_names_head(self) -> None:
        _g(self.seed, "push", "-q", "other", "feat")
        for record in self.NAMES_NO_REMOTE:
            with self.subTest(record=record):
                self.fetches.clear()
                head, tip, behind, err = self._read(self._bundle(record))
                self.assertIsNone(behind)
                self.assertEqual(self.fetches, self._fetched("upstream", "origin"))
                self.assertNotIn("git failed", err)
                self.assertIn(self._missing("upstream", "origin"), err)
                self.assertIn("its publish record does not say which remote its branch is "
                              "on, so no other remote was fetched", err)
                self.assertNotIn("puts its branch at", err)

    def test_stacked_record_naming_no_remote_is_refused_up_front(self) -> None:
        _g(self.seed, "push", "-q", "origin", "feat")
        for record in self.NAMES_NO_REMOTE:
            with self.subTest(record=record):
                self.fetches.clear()
                self.gh_calls.clear()
                rc, out, err = self._merge(self._bundle(record))
                self._assert_refused_up_front(rc, err)

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
