"""Slice for the Act process-delta ledger (issue #149) — make Act self-auditing.

A recurring signal is registered (`open`); the human marks it `applied`; a later review
flags it if the same signal recurs in a cycle frozen AFTER the applied date (a likely
ineffective delta). The ledger logic is exercised over constructed `ActEntry` objects —
no SUMMARY parsing, no model. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import act, leaves
from pdca_harness.config import Config, LeafConfig

_SIG = "add a po/POTFILES gate for new core .py files"


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))


def _entry(name: str, date: str, *, candidates=(), needs=()) -> act.ActEntry:
    return act.ActEntry(bundle=Path(f"issue_{name}"), date=date,
                        act_candidates=list(candidates), needs_human=list(needs))


class Ledger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recurring_signal_registered_idempotently(self) -> None:
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        added = act.register_signals(self.cfg, entries, "2026-06-03")
        self.assertEqual(len(added), 1)
        ledger = act.load_ledger(self.cfg)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["status"], "open")
        # Re-registering the same recurring signal adds nothing.
        self.assertEqual(act.register_signals(self.cfg, entries, "2026-06-04"), [])
        self.assertEqual(len(act.load_ledger(self.cfg)), 1)

    def test_single_cycle_signal_not_tracked(self) -> None:
        # A signal in only ONE cycle is not a process defect yet — not registered.
        entries = [_entry("A", "2026-06-01", candidates=["a one-off observation"])]
        self.assertEqual(act.register_signals(self.cfg, entries, "2026-06-03"), [])
        self.assertEqual(act.load_ledger(self.cfg), [])

    def test_resolve_marks_applied(self) -> None:
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        act.register_signals(self.cfg, entries, "2026-06-03")
        raw = act.resolve(self.cfg, "POTFILES", "t2_shape.py:99", "2026-06-05")
        self.assertIsNotNone(raw)
        led = act.load_ledger(self.cfg)[0]
        self.assertEqual(led["status"], "applied")
        self.assertEqual(led["applied_date"], "2026-06-05")
        self.assertEqual(led["location"], "t2_shape.py:99")
        # No open entry matches → None (and an already-applied one isn't re-matched).
        self.assertIsNone(act.resolve(self.cfg, "POTFILES", "", "2026-06-06"))
        self.assertIsNone(act.resolve(self.cfg, "nonexistent", "", "2026-06-06"))

    def test_recurrence_after_applied_is_flagged(self) -> None:
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        act.register_signals(self.cfg, entries, "2026-06-03")
        act.resolve(self.cfg, "POTFILES", "loc", "2026-06-05")
        # A NEW cycle frozen AFTER the applied date still shows the signal → recurrence.
        later = entries + [_entry("C", "2026-06-10", needs=[_SIG])]
        recs = act.recurrences(self.cfg, later)
        self.assertEqual(len(recs), 1)
        self.assertIn("C", recs[0]["recurred_in"])
        self.assertEqual(recs[0]["applied"], "2026-06-05")

    def test_no_recurrence_when_signal_stops(self) -> None:
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        act.register_signals(self.cfg, entries, "2026-06-03")
        act.resolve(self.cfg, "POTFILES", "loc", "2026-06-05")
        # Pre-applied appearances don't count; a later clean cycle isn't a recurrence.
        later = entries + [_entry("C", "2026-06-10", candidates=["something unrelated"])]
        self.assertEqual(act.recurrences(self.cfg, later), [])

    def test_open_signal_never_counts_as_recurrence(self) -> None:
        # An un-resolved (open) signal is just "still recurring", not an INEFFECTIVE delta.
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        act.register_signals(self.cfg, entries, "2026-06-03")
        self.assertEqual(act.recurrences(self.cfg, entries), [])

    def test_render_index_shows_ledger_and_recurrence(self) -> None:
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG]),
                   _entry("C", "2026-06-10", needs=[_SIG])]
        act.register_signals(self.cfg, entries, "2026-06-03")
        act.resolve(self.cfg, "POTFILES", "t2_shape.py:99", "2026-06-05")
        out = act.render_index(entries, act.patterns(entries), act.load_ledger(self.cfg),
                               act.recurrences(self.cfg, entries))
        self.assertIn("Process-delta ledger", out)
        self.assertIn("applied 2026-06-05", out)
        self.assertIn("Ineffective deltas", out)

    def test_scaffold_includes_ineffective_section(self) -> None:
        recs = [{"signal": _SIG, "applied": "2026-06-05", "recurred_in": ["C"]}]
        text = act.scaffold_entry([_entry("C", "2026-06-10")], {}, "2026-06-12", recs=recs)
        self.assertIn("Ineffective deltas", text)
        self.assertIn("revisit", text)

    def test_recurrences_scoped_to_passed_entries(self) -> None:
        # Passing a filtered entries list scopes recurrence detection to it (the --since
        # case): a recurrence outside the passed set is not reported.
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        act.register_signals(self.cfg, entries, "2026-06-03")
        act.resolve(self.cfg, "POTFILES", "loc", "2026-06-05")
        full = entries + [_entry("C", "2026-06-10", needs=[_SIG])]
        self.assertEqual(len(act.recurrences(self.cfg, full)), 1)   # C in scope → flagged
        self.assertEqual(act.recurrences(self.cfg, entries), [])    # C out of scope → not

    def test_automatic_stub_act_registers_ledger(self) -> None:
        # The cadence-driven Act beat (leaves._stub_act) registers recurring signals too,
        # not only the manual `pdca act log` path (#149 / Codex review).
        entries = [_entry("A", "2026-06-01", candidates=[_SIG]),
                   _entry("B", "2026-06-02", candidates=[_SIG])]
        with mock.patch.object(leaves.act_mod, "index", return_value=entries):
            leaves._stub_act(self.cfg, "2026-06-03")
        ledger = act.load_ledger(self.cfg)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["status"], "open")
        self.assertTrue((self.cfg.process_dir / "act-log.md").exists())


if __name__ == "__main__":
    unittest.main()
