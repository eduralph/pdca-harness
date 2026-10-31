"""Offline slice for issue #780 — a standalone `pdca signoff` consumes the bundle's
`signoff-decision` once it records a decision, as `flow._apply_decision` already does
(stdlib unittest).

`cli._signoff` recorded §9 and drove the transition but never deleted the decision file.
A decision left on disk by an earlier flow session — most often an `accept` C6 refused —
therefore outlived the attempt it was made about: after `pdca signoff --iterate-plan` the
bundle was archived, re-planned and rebuilt, and the next `pdca flow` pre-apply read the
old `accept` and finished the rebuilt bundle with no sign-off session at all.

Post-fix the file is deleted after `signoff.record` succeeds and before
`driver.run_issue`, so neither a held nor a failed transition can leave it behind. A
standalone accept C6 refuses, or a `record` that raises, records nothing and so consumes
nothing.

Run from the project root:
    PYTHONPATH=src python3 -m unittest tests.test_signoff_consumes_decision
"""

from __future__ import annotations

import argparse
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import cli, driver, flow, leaves, signoff, state
from pdca_harness.config import Config, LeafConfig

ACTIONS = ("accept", "iterate-do", "iterate-plan", "discontinue")


def _stub_config(root: Path, *, auto_iterate: bool = False) -> Config:
    """All six leaves stubbed — the offline shape of ``tests.test_signoff_orphan``."""
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
        planner=LeafConfig(mode="stub", family="claude", interactive=True),
        signoff=LeafConfig(mode="stub", family="claude", interactive=True),
        publisher=LeafConfig(mode="stub", family="claude", interactive=True),
        act=LeafConfig(mode="stub", family="claude", interactive=True),
        auto_iterate=auto_iterate,
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


def _clear_needs_human(d: Path) -> None:
    """Tick every §6 box, so C6 permits an accept."""
    summary = d / "SUMMARY.md"
    summary.write_text(summary.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                       encoding="utf-8")


def _args(issue_id: str, action: str) -> argparse.Namespace:
    """The namespace `pdca signoff <id> --<action>` parses to."""
    return argparse.Namespace(
        issue_id=issue_id,
        accept=action == "accept",
        iterate_do=action == "iterate-do",
        iterate_plan=action == "iterate-plan",
        discontinue=action == "discontinue",
        by="t", delta=None,
        no_publish=True,   # publish is not under test; keep the accept leg offline
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp, auto_iterate=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _halted_bundle(self, issue_id: str, decision: str = "accept\n") -> Path:
        """A bundle driven (stub Plan→Do→Check) to AWAITING_SIGNOFF, carrying a stale
        ``decision`` on disk with §9 unrecorded — what a C6-refused flow accept leaves."""
        d = self.cfg.bundle(issue_id)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertTrue(flow._plan_if_unplanned(self.cfg, d, None))
            self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)
        (d / leaves.SIGNOFF_DECISION).write_text(decision, encoding="utf-8")
        return d

    def _signoff(self, issue_id: str, action: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli._signoff(self.cfg, _args(issue_id, action))


class ConsumesOnRecord(_Base):
    def test_iterate_plan_consumes_stale_accept(self) -> None:
        # (a)
        d = self._halted_bundle("CONSUME_A")
        self._signoff("CONSUME_A", "iterate-plan")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists(),
                         "a standalone sign-off recorded a decision but left the stale "
                         "`signoff-decision` on disk")

    def test_every_recording_action_consumes_the_file(self) -> None:
        # (d)
        for i, action in enumerate(ACTIONS):
            with self.subTest(action=action):
                issue_id = f"CONSUME_D{i}"
                d = self._halted_bundle(issue_id)
                if action == "accept":
                    _clear_needs_human(d)   # else C6 refuses and nothing is recorded
                with mock.patch.object(signoff, "record", wraps=signoff.record) as rec:
                    self._signoff(issue_id, action)
                self.assertEqual(rec.call_count, 1,
                                 "fixture: the decision must actually be recorded")
                self.assertFalse((d / leaves.SIGNOFF_DECISION).exists(),
                                 f"`pdca signoff --{action}` left `signoff-decision`")

    def test_stale_accept_does_not_finish_the_rebuilt_bundle(self) -> None:
        # (b) end to end: iterate-plan, rebuild, §6 clear — the next flow wave must open a
        # fresh session rather than apply the old `accept`.
        d = self._halted_bundle("CONSUME_B")
        self._signoff("CONSUME_B", "iterate-plan")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            flow._plan_if_unplanned(self.cfg, d, None)
            self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)
        _clear_needs_human(d)
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [],
                         "precondition: §6 must be clear, so C6 would permit a stale accept")

        offered: list[list[str]] = []

        def spying_batch(cfg: Config, bundles: list[Path]) -> None:
            offered.append([b.name for b in bundles])

        with mock.patch.object(leaves, "run_signoff_batch", spying_batch), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-01-01", max_passes=1)

        self.assertNotEqual(state.state(d), state.COMPLETE,
                            "the stale `accept` finished a rebuilt bundle nobody reviewed")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertEqual(offered, [[d.name]], "the rebuilt bundle must get a fresh session")


class LeavesUnrecordedDecision(_Base):
    def test_c6_refused_accept_leaves_the_file(self) -> None:
        # (c) — §6 left open, so C6 refuses and nothing is recorded.
        d = self._halted_bundle("KEEP_C6")
        self.assertTrue(signoff.open_needs_human(d / "SUMMARY.md"),
                        "fixture: §6 must be open")
        self.assertEqual(self._signoff("KEEP_C6", "accept"), 1)
        self.assertEqual(leaves.signoff_decision(d), "accept")

    def test_failed_record_leaves_the_file(self) -> None:
        # (c) — `signoff.record` raising ValueError records nothing.
        d = self._halted_bundle("KEEP_VE")
        with mock.patch.object(signoff, "record", side_effect=ValueError("no §9")):
            self.assertEqual(self._signoff("KEEP_VE", "iterate-do"), 1)
        self.assertEqual(leaves.signoff_decision(d), "accept")


class ConsumedBeforeTransition(_Base):
    def test_consumed_before_a_raising_transition(self) -> None:
        # (e) — the delete precedes `driver.run_issue`, so a failed transition can't keep it.
        d = self._halted_bundle("ORDER_RAISE")
        with mock.patch.object(driver, "run_issue", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._signoff("ORDER_RAISE", "iterate-do")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())

    def test_consumed_before_a_held_transition(self) -> None:
        # (e) — a held transition makes `_signoff` return 1; the decision is still consumed.
        d = self._halted_bundle("ORDER_HELD")
        with mock.patch.object(driver, "run_issue", return_value=state.PLANNED), \
                mock.patch.object(driver, "held", return_value=True):
            self.assertEqual(self._signoff("ORDER_HELD", "iterate-do"), 1)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
