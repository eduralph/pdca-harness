"""Slice for `pdca revert` (issue #158) — undo a published contribution.

Routing by the recorded PR state: MERGED → a draft revert PR (reverse-apply patch.diff);
OPEN → withdraw (`gh pr close --delete-branch`); CLOSED → no-op. Dry-run prints the plan
and mutates nothing. `gh` state + subprocess are mocked — no network. Run from the project
root:
    PYTHONPATH=src python -m unittest discover -s tests
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

from pdca_harness import revert
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={"org/repo": str(root / "repo")})


class Revert(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, pr_url: str | None = "https://gh/pr/1",
                patch: str | None = "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-x\n+y\n") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        if pr_url is not None:
            (d / "publish.json").write_text(json.dumps(
                {"pr_url": pr_url, "repo": "org/repo", "base": "main", "branch": f"fix/{iid}"}),
                encoding="utf-8")
        if patch is not None:
            (d / "patch.diff").write_text(patch, encoding="utf-8")
        (d / "commit-msg.txt").write_text("Fix the thing\n", encoding="utf-8")
        return d

    def test_no_publish_record_fails(self) -> None:
        d = self.cfg.bundle("NP")
        d.mkdir(parents=True)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(revert.revert(self.cfg, "NP"), 1)

    def test_merged_dry_run_plans_revert_pr(self) -> None:
        self._bundle("M")
        with mock.patch.object(revert, "_pr_state", return_value="MERGED"), \
                mock.patch.object(revert.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()) as out:
            rc = revert.revert(self.cfg, "M", dry_run=True)
        self.assertEqual(rc, 0)
        run.assert_not_called()                       # dry-run mutates nothing
        self.assertIn("apply --reverse", out.getvalue())
        self.assertIn("gh pr create", out.getvalue())

    def test_open_dry_run_plans_withdraw(self) -> None:
        self._bundle("O")
        with mock.patch.object(revert, "_pr_state", return_value="OPEN"), \
                mock.patch.object(revert.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()) as out:
            rc = revert.revert(self.cfg, "O", dry_run=True)
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertIn("gh pr close", out.getvalue())
        self.assertIn("--delete-branch", out.getvalue())

    def test_closed_pr_is_noop(self) -> None:
        self._bundle("C")
        with mock.patch.object(revert, "_pr_state", return_value="CLOSED"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(revert.revert(self.cfg, "C"), 0)

    def test_merged_without_patch_fails(self) -> None:
        self._bundle("MP", patch=None)
        with mock.patch.object(revert, "_pr_state", return_value="MERGED"), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(revert.revert(self.cfg, "MP"), 1)

    def test_withdraw_real_closes_and_records(self) -> None:
        d = self._bundle("W")
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(revert, "_pr_state", return_value="OPEN"), \
                mock.patch.object(revert.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()):
            rc = revert.revert(self.cfg, "W")
        self.assertEqual(rc, 0)
        self.assertIn(["gh", "pr", "close", "https://gh/pr/1", "--delete-branch"], calls)
        rec = json.loads((d / "revert.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["action"], "withdraw")
        self.assertEqual(rec["reverts"], "https://gh/pr/1")

    def test_open_stacked_pr_is_refused(self) -> None:
        # mode="stacked" (Onto branch #54) = a commit on a PRE-EXISTING PR the harness did
        # not create — revert must NOT close/delete it.
        d = self._bundle("S")
        pj = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        pj["mode"] = "stacked"
        (d / "publish.json").write_text(json.dumps(pj), encoding="utf-8")
        with mock.patch.object(revert, "_pr_state", return_value="OPEN"), \
                mock.patch.object(revert.subprocess, "run") as run, \
                redirect_stderr(io.StringIO()) as err:
            rc = revert.revert(self.cfg, "S")
        self.assertEqual(rc, 1)
        run.assert_not_called()                  # never touched the collaborator's PR
        self.assertIn("refusing to close", err.getvalue())


if __name__ == "__main__":
    unittest.main()
