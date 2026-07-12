"""Offline slice for opt-in auto-merge mode (`merge.merge_wave`, #wave-model).

Proves the fail-closed contract: a published, COMPLETE bundle's PR is `gh pr merge`d and
the base re-fetched; a close/no-fix bundle and an already-merged PR are skipped; a
COMPLETE bundle with no recorded PR, or a `gh pr merge` failure, returns non-zero so the
caller STOPs. Dry-run shells nothing. `gh` and state are mocked — no network. Run from the
project root:
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

from pdca_harness import merge, state
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={"org/repo": str(root / "repo")})


class MergeWave(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, pr_url: str | None = "https://gh/pr/1",
                patch: str | None = "diff\n", repo: str = "org/repo") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        if patch is not None:
            (d / "patch.diff").write_text(patch, encoding="utf-8")
        if pr_url is not None:
            (d / "publish.json").write_text(
                json.dumps({"pr_url": pr_url, "repo": repo}), encoding="utf-8")
        return d

    def test_dry_run_shells_nothing(self) -> None:
        b = self._bundle("M1")
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                redirect_stdout(io.StringIO()) as out:
            rc = merge.merge_wave(self.cfg, [b], dry_run=True, method="merge")
        self.assertEqual(rc, 0)
        run.assert_not_called()                       # no gh in a dry-run
        self.assertIn("gh pr merge", out.getvalue())

    def test_merges_then_fetches_base(self) -> None:
        b = self._bundle("M2")
        runs: list[list[str]] = []

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [b], method="squash")
        self.assertEqual(rc, 0)
        self.assertIn(["gh", "pr", "merge", "https://gh/pr/1", "--squash"], runs)
        self.assertTrue(any("fetch" in c for c in runs))   # base refreshed after merge

    def test_close_no_fix_skipped(self) -> None:
        b = self._bundle("M3", patch=None)             # no patch — nothing to merge
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE):
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_no_pr_url_fails_closed(self) -> None:
        b = self._bundle("M4", pr_url=None)            # COMPLETE + patch but never published
        with mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertIn("no recorded PR", err.getvalue())

    def test_merge_failure_stops(self) -> None:
        b = self._bundle("M5")

        def fail_merge(cmd, **kw):  # ready succeeds; the merge itself fails
            rc = 1 if cmd[:3] == ["gh", "pr", "merge"] else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="not mergeable")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fail_merge), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertIn("did not merge", err.getvalue())

    def test_readies_before_merging(self) -> None:
        # #279: the publisher opens every PR --draft, but `gh pr merge` refuses a draft, so a
        # non-final wave's PR must be readied first. `gh pr ready` must precede `gh pr merge`.
        b = self._bundle("M7")
        runs: list[list[str]] = []

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [b], method="merge")
        self.assertEqual(rc, 0)
        gh = [c for c in runs if c[:2] == ["gh", "pr"]]
        self.assertEqual(gh[0], ["gh", "pr", "ready", "https://gh/pr/1"])
        self.assertEqual(gh[1], ["gh", "pr", "merge", "https://gh/pr/1", "--merge"])

    def test_ready_failure_stops_before_merge(self) -> None:
        # If a PR can't be readied it can't be merged — fail-closed, and never attempt merge.
        b = self._bundle("M8")
        runs: list[list[str]] = []

        def fail_ready(cmd, **kw):
            runs.append(cmd)
            rc = 1 if cmd[:3] == ["gh", "pr", "ready"] else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="cannot ready")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fail_ready), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertIn("could not be marked ready", err.getvalue())
        self.assertNotIn(["gh", "pr", "merge", "https://gh/pr/1", "--merge"], runs)

    def test_dry_run_readies_nothing(self) -> None:
        # A dry-run must shell nothing — not even the new ready step.
        b = self._bundle("M9")
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                redirect_stdout(io.StringIO()):
            merge.merge_wave(self.cfg, [b], dry_run=True)
        run.assert_not_called()

    def test_already_merged_skipped(self) -> None:
        b = self._bundle("M6")
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=True):
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 0)
        run.assert_not_called()                        # idempotent — no second merge

    def test_first_failure_stops_the_wave(self) -> None:
        # The second bundle has no PR → the wave STOPs there; order is name-sorted by caller.
        ok = self._bundle("MA")
        bad = self._bundle("MB", pr_url=None)

        def fake_run(cmd, **kw):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stderr(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [ok, bad])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
