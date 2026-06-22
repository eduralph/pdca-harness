"""Offline slice for `merged.is_merged` — the wait-for-merged gate's merge check (#107).

Proves the fail-closed contract: only a COMPLETE prereq whose recorded PR reports
`state == MERGED` counts as merged; a close/no-fix prereq is vacuously merged; anything
unconfirmable (not COMPLETE, unpublished, or a `gh` failure) stays not-merged so the
dependent waits. `gh` is mocked — no network.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import merged, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
    )


def _complete_bundle(cfg: Config, iid: str, *, patch: str = "diff --git a/x b/x\n",
                     pr_url: str | None = None) -> Path:
    d = cfg.bundle(iid)
    d.mkdir(parents=True)
    (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
    (d / "patch.diff").write_text(patch, encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-06-05")
    if pr_url is not None:
        (d / "publish.json").write_text(json.dumps({"pr_url": pr_url}), encoding="utf-8")
    return d


class IsMerged(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gh(self, state_value: str, rc: int = 0):
        return SimpleNamespace(returncode=rc,
                               stdout=json.dumps({"state": state_value}), stderr="")

    def test_not_complete_is_not_merged(self) -> None:
        d = self.cfg.bundle("WIP"); d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        self.assertNotEqual(state.state(d), state.COMPLETE)
        self.assertFalse(merged.is_merged(self.cfg, "WIP"))

    def test_close_no_fix_is_vacuously_merged(self) -> None:
        # COMPLETE with an empty patch = a close disposition — nothing to wait on.
        _complete_bundle(self.cfg, "CLOSE", patch="   \n")
        self.assertEqual(state.state(self.cfg.bundle("CLOSE")), state.COMPLETE)
        self.assertTrue(merged.is_merged(self.cfg, "CLOSE"))

    def test_published_but_pr_open_is_not_merged(self) -> None:
        _complete_bundle(self.cfg, "OPEN", pr_url="https://x/pr/1")
        with mock.patch("pdca_harness.merged.subprocess.run", return_value=self._gh("OPEN")):
            self.assertFalse(merged.is_merged(self.cfg, "OPEN"))

    def test_published_and_pr_merged_is_merged(self) -> None:
        _complete_bundle(self.cfg, "DONE", pr_url="https://x/pr/2")
        with mock.patch("pdca_harness.merged.subprocess.run", return_value=self._gh("MERGED")):
            self.assertTrue(merged.is_merged(self.cfg, "DONE"))

    def test_accepted_but_unpublished_is_not_merged(self) -> None:
        # COMPLETE with a real patch but no publish.json → no PR to check → wait.
        _complete_bundle(self.cfg, "NOPR")
        with mock.patch("pdca_harness.merged.subprocess.run") as run:
            self.assertFalse(merged.is_merged(self.cfg, "NOPR"))
            run.assert_not_called()  # short-circuits before gh

    def test_gh_failure_is_fail_closed(self) -> None:
        _complete_bundle(self.cfg, "ERR", pr_url="https://x/pr/3")
        with mock.patch("pdca_harness.merged.subprocess.run",
                        return_value=self._gh("", rc=1)):
            self.assertFalse(merged.is_merged(self.cfg, "ERR"))


if __name__ == "__main__":
    unittest.main()
