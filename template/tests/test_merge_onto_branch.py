"""Issue #658: merge mode never readies or merges the PR behind a ``"stacked"`` record.

An ``Onto branch`` bundle is published by ``publish._publish_stacked`` as one commit added
to an EXISTING open PR — possibly someone else's, with other work on it and any base — and
recorded in ``publish.json`` with ``"mode": "stacked"``. Merge mode (``[driver].wave_mode
= "merge"``) readies and merges only the PRs the run opened, so ``merge.merge_wave`` — the
call ``flow`` makes for a non-final merge-mode wave — must refuse such a record before any
``gh`` call for that PR (no ready, no update, no merge, no undo), stop the wave, and say
why and what the operator can do instead. It refuses a PR that already reads merged too
(``merged.merged_into`` never counts a ``"stacked"`` record merged), and a dry run prints
no merge plan for it. A ``"new-pr"`` record (or one with no ``mode``) merges as before.

``gh``, ``git`` and state are stubbed — no network. Run from the project root:
    PYTHONPATH=src python -m unittest tests.test_merge_onto_branch
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import merge, state
from pdca_harness.config import Config, LeafConfig

PR = "https://gh/pr/1"
STACKED = {"mode": "stacked", "branch": "feat", "base": "other/feat", "pr_url": PR,
           "repo": "org/repo"}
HEAD = "1" * 40
BASE_TIP = "b" * 40


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={"org/repo": str(root / "repo")})


def _ok_run(cmd, **kw):
    """Every `gh`/`git` call succeeds: an up-to-date head, a green rollup — so a record
    that is NOT refused goes all the way to `gh pr merge`."""
    if cmd[:3] == ["gh", "pr", "view"]:
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"headRefOid": HEAD, "baseRefName": "main"}))
    if cmd[:3] == ["gh", "pr", "checks"]:
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=json.dumps([{"name": "ci", "bucket": "pass"}]))
    if cmd[:1] == ["git"] and "rev-parse" in cmd:
        return SimpleNamespace(returncode=0, stderr="", stdout=BASE_TIP + "\n")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class OntoBranchRecordInMergeMode(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = _cfg(self.tmp)
        sleeper = mock.patch.object(merge, "_sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def _bundle(self, iid: str, record: dict) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "patch.diff").write_text("diff\n", encoding="utf-8")
        (d / "publish.json").write_text(json.dumps(record), encoding="utf-8")
        return d

    def _wave(self, b: Path, *, dry_run: bool = False,
              is_merged: bool = False) -> tuple[int, list[list[str]], str, str]:
        calls: list[list[str]] = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            return _ok_run(cmd, **kw)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=is_merged), \
                redirect_stdout(out), redirect_stderr(err):
            rc = merge.merge_wave(self.cfg, [b], dry_run=dry_run, method="merge")
        return rc, calls, out.getvalue(), err.getvalue()

    def _assert_no_pr_action(self, calls: list[list[str]], out: str) -> None:
        for forbidden in (["gh", "pr", "ready", PR], ["gh", "pr", "ready", PR, "--undo"],
                          ["gh", "pr", "update-branch", PR]):
            self.assertNotIn(forbidden, calls)
        self.assertFalse([c for c in calls if c[:3] == ["gh", "pr", "merge"]], calls)
        self.assertFalse([c for c in calls if c[:2] == ["gh", "pr"]], calls)
        self.assertNotIn(f"gh pr merge {PR}", out)

    def _assert_says_why(self, d: Path, err: str) -> None:
        self.assertIn(d.name, err)
        self.assertIn(PR, err)
        self.assertIn("Onto branch", err)
        self.assertIn("existing PR", err)
        self.assertIn("does not ready or merge", err)
        self.assertIn('wave_mode = "stack"', err)
        self.assertIn("take this bundle out of this merge-mode batch", err)
        # Merging the PR by hand would not unblock the run (criterion 3), so the message
        # must not offer it as a way forward; it may only say it does not help.
        self.assertIn("does not let this merge-mode run continue", err)

    # (1) + (2): no ready, no update, no merge, no undo; the wave stops and says why
    def test_stacked_record_is_refused_before_any_gh_call(self) -> None:
        b = self._bundle("658", STACKED)
        rc, calls, out, err = self._wave(b)
        self.assertNotEqual(rc, 0)
        self._assert_no_pr_action(calls, out)
        self.assertEqual(b.name, "issue_658")
        self._assert_says_why(b, err)

    # (3): a PR that already reads MERGED is refused all the same, not skipped
    def test_stacked_record_already_merged_is_still_refused(self) -> None:
        b = self._bundle("658", STACKED)
        rc, calls, out, err = self._wave(b, is_merged=True)
        self.assertNotEqual(rc, 0)
        self._assert_no_pr_action(calls, out)
        self._assert_says_why(b, err)

    # (4): a dry run prints no merge plan for it and stops too
    def test_stacked_record_dry_run_prints_no_merge_plan(self) -> None:
        b = self._bundle("658", STACKED)
        rc, calls, out, err = self._wave(b, dry_run=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertNotIn("gh pr merge", out)
        self._assert_says_why(b, err)

    # The wave stops at the stacked member: a later member is not merged either.
    def test_wave_stops_at_the_stacked_member(self) -> None:
        a = self._bundle("658", STACKED)
        b = self._bundle("659", {"mode": "new-pr", "branch": "fix", "pr_url": PR + "9",
                                 "repo": "org/repo"})
        calls: list[list[str]] = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            return _ok_run(cmd, **kw)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [a, b], method="merge")
        self.assertNotEqual(rc, 0)
        self.assertEqual([c for c in calls if c[:2] == ["gh", "pr"]], [])

    # (5): a "new-pr" record, or one with no mode, merges exactly as before
    def test_new_pr_and_modeless_records_still_merge(self) -> None:
        for iid, record in (("1", {"mode": "new-pr", "branch": "feat", "pr_url": PR,
                                   "repo": "org/repo"}),
                            ("2", {"pr_url": PR, "repo": "org/repo"})):
            with self.subTest(record=record):
                rc, calls, out, err = self._wave(self._bundle(iid, record))
                self.assertEqual(rc, 0, err)
                gh = [c for c in calls if c[:2] == ["gh", "pr"]]
                self.assertEqual(gh[0], ["gh", "pr", "ready", PR])
                self.assertEqual(gh[-1], ["gh", "pr", "merge", PR, "--merge",
                                          "--match-head-commit", HEAD])


if __name__ == "__main__":
    unittest.main()
