"""Offline slice: dependency resolution must find a prereq archived in ``completed/`` (#171).

A finished prerequisite moved to ``results/completed/`` must still satisfy an active
dependent's ``Depends on:`` — otherwise the dep resolver (``waves.check_dep_graph``) aborts
the whole batch with "neither in this batch nor an existing COMPLETE bundle". Proves
``Config.find_bundle`` resolves ``completed/`` (falling back to the active path for a missing
id, so a genuinely-missing dep still blocks), and that ``waves.check_dep_graph`` and
``merged.is_merged`` treat a completed/ prereq as satisfied. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import merged, signoff, state, waves
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))


def _complete_bundle(d: Path) -> None:
    """A COMPLETE bundle (brief + patch + gates + accepted SUMMARY), as state.state reads it."""
    d.mkdir(parents=True)
    (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-06-28")


class DepsInCompleted(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str, *, depends_on: str = "", stacks_on: str = "") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        body = "- **Slug:** s\n- **Repo + branch target:** o/r @ main\n"
        if depends_on:
            body += f"- **Depends on:** {depends_on}\n"
        if stacks_on:
            body += f"- **Stacks on:** {stacks_on}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def _completed(self, iid: str) -> Path:
        d = self.cfg.bundle_root / "completed" / f"issue_{iid}"
        _complete_bundle(d)
        return d

    def test_find_bundle_active_then_completed_then_active(self) -> None:
        active = self.cfg.bundle("A")
        active.mkdir(parents=True)
        self.assertEqual(self.cfg.find_bundle("A"), active)            # active wins

        arch = self.cfg.bundle_root / "completed" / "issue_B"
        arch.mkdir(parents=True)
        self.assertEqual(self.cfg.find_bundle("B"), arch)             # archived

        self.assertEqual(self.cfg.find_bundle("Z"), self.cfg.bundle("Z"))  # missing → active path
        self.assertEqual(state.state(self.cfg.find_bundle("Z")), state.UNPLANNED)

    def test_check_dep_graph_accepts_archived_completed_prereq(self) -> None:
        self.assertEqual(state.state(self._completed("PREREQ")), state.COMPLETE)
        dep = self._brief("DEP", depends_on="PREREQ")
        # Before #171 this raised "neither in this batch nor an existing COMPLETE bundle".
        waves.check_dep_graph(self.cfg, [dep])  # must NOT raise
        self.assertEqual(
            [[p.name for p in w] for w in waves.compute_waves(self.cfg, [dep])],
            [["issue_DEP"]])  # out-of-batch landed prereq imposes no in-wave ordering

    def test_genuinely_missing_prereq_still_aborts(self) -> None:
        dep = self._brief("DEP", depends_on="GHOST")
        with self.assertRaises(ValueError):
            waves.check_dep_graph(self.cfg, [dep])

    def test_archived_stacks_on_parent_is_rejected(self) -> None:
        # A `Stacks on` parent must be ACTIVE — the dependent stacks its worktree + PR on the
        # parent's LIVE published branch (read from the active bundle). An archived-only stack
        # parent stays rejected, even though an archived Depends on parent is accepted (above).
        self.assertEqual(state.state(self._completed("SP")), state.COMPLETE)
        dep = self._brief("SDEP", stacks_on="SP")
        with self.assertRaises(ValueError):
            waves.check_dep_graph(self.cfg, [dep])

    def test_is_merged_resolves_completed_prereq(self) -> None:
        arch = self._completed("PREREQ")
        (arch / "publish.json").write_text('{"pr_url": "https://gh/pr/9"}', encoding="utf-8")
        with mock.patch.object(merged.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout='{"state":"MERGED"}', stderr="")):
            self.assertTrue(merged.is_merged(self.cfg, "PREREQ"))  # archived prereq resolved
        self.assertFalse(merged.is_merged(self.cfg, "GHOST"))      # missing → not merged


if __name__ == "__main__":
    unittest.main()
