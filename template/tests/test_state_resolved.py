"""RESOLVED terminal state for notes-only trackers (issue #302).

A briefless bundle whose notes.json carries a top-level ``resolved`` object was settled
in the tracker outside a cycle — terminal, not pending. Proves the defensive contract
(malformed / non-object input never crashes and never resolves) and that RESOLVED is
threaded through every terminal set (driver HALTED, flow terminals, status ordering).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import cli, driver, flow, state
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


_RESOLVED = {"resolved": {"github_state": "closed", "state_reason": "completed",
                          "closed_at": "2026-07-01T00:00:00Z", "note": "settled in-issue"}}


class StateResolved(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, notes: str | None) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        if notes is not None:
            (d / "notes.json").write_text(notes, encoding="utf-8")
        return d

    def test_briefless_with_resolved_object_is_resolved(self) -> None:
        d = self._bundle("1", json.dumps(_RESOLVED))
        self.assertEqual(state.state(d), state.RESOLVED)

    def test_briefless_without_resolved_stays_unplanned(self) -> None:
        d = self._bundle("2", json.dumps({"title": "open question"}))
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_malformed_notes_is_unplanned_not_a_crash(self) -> None:
        d = self._bundle("3", "{not json")
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_non_object_resolved_is_unplanned(self) -> None:
        for iid, value in (("4", json.dumps({"resolved": "closed"})),
                           ("5", json.dumps({"resolved": True})),
                           ("6", json.dumps(["resolved"]))):
            d = self._bundle(iid, value)
            self.assertEqual(state.state(d), state.UNPLANNED, msg=value)

    def test_no_notes_at_all_is_unplanned(self) -> None:
        d = self._bundle("7", None)
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_briefed_bundle_with_stray_resolved_key_is_not_reclassified(self) -> None:
        d = self._bundle("8", json.dumps(_RESOLVED))
        (d / "brief.md").write_text("- **Slug:** real-work\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.PLANNED)

    def test_placeholder_brief_does_not_unresolve_a_resolved_tracker(self) -> None:
        # #302 review: a stray unfilled template copy (e.g. the stub/batch planner
        # copying brief.md.tpl) is "never authored" — the same standing as no brief —
        # so the tracker's terminal resolution still wins and the bundle must not
        # reappear as pending. Without the resolved marker it stays UNPLANNED (#113).
        d = self._bundle("9", json.dumps(_RESOLVED))
        (d / "brief.md").write_text("- **Slug:** <fill-me>\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.RESOLVED)
        (d / "notes.json").unlink()
        self.assertEqual(state.state(d), state.UNPLANNED)


class ResolvedIsTerminal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolved_in_every_terminal_set(self) -> None:
        self.assertIn(state.RESOLVED, state.HALTED)
        self.assertIn(state.RESOLVED, flow._TERMINAL)
        self.assertIn(state.RESOLVED, cli._STATE_ORDER)
        # Status ordering groups RESOLVED with the terminals, at the very bottom.
        self.assertGreater(cli._STATE_ORDER.index(state.RESOLVED),
                           cli._STATE_ORDER.index(state.DISCONTINUED))

    def test_driver_halts_immediately_on_resolved(self) -> None:
        d = self.cfg.bundle("9")
        d.mkdir(parents=True)
        (d / "notes.json").write_text(json.dumps(_RESOLVED), encoding="utf-8")
        self.assertEqual(driver.run_issue(d, self.cfg), state.RESOLVED)

    def test_flow_ids_skips_resolved_as_terminal(self) -> None:
        d = self.cfg.bundle("10")
        d.mkdir(parents=True)
        (d / "notes.json").write_text(json.dumps(_RESOLVED), encoding="utf-8")
        self.assertEqual(flow.flow_ids(self.cfg, ["10"], plan_missing=False), {})


if __name__ == "__main__":
    unittest.main()
