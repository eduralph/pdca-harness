"""Slice for the gate-promotion lifecycle (issue #156) — `gates.promotion_candidates`.

An advisory check carrying `promote_after = N` earns promotion to gating once it has PASSED
in its N most-recent frozen cycles (a hint; the human flips `gating`). Frozen
`check-gates.json` records drive it; `state` is mocked COMPLETE so the test needn't assemble
full SUMMARY sign-offs. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import gates, state
from pdca_harness.config import Config, LeafConfig

_CHECK = {"id": "C5-prod", "label": "test exercises production", "scope": "bundle",
          "gating": False, "promote_after": 3}


def _cfg(root: Path, checks: list[dict]) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        gates_checks=checks)


class Promotion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, cfg: Config, name: str, date: str, result: str) -> None:
        d = cfg.bundle(name)
        d.mkdir(parents=True)
        (d / "check-gates.json").write_text(
            json.dumps({"rows": [{"rule_id": "C5-prod", "result": result}]}),
            encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            f"## 9. Check sign-off\n- By / date: t / {date}\n", encoding="utf-8")

    def _candidates(self, cfg: Config) -> list[dict]:
        with mock.patch.object(gates.state, "state", return_value=state.COMPLETE):
            return gates.promotion_candidates(cfg)

    def _three(self, cfg: Config, results: tuple[str, str, str]) -> None:
        for (name, date), res in zip(
                [("A", "2026-06-01"), ("B", "2026-06-02"), ("C", "2026-06-03")], results):
            self._bundle(cfg, name, date, res)

    def test_ready_when_clean_for_threshold(self) -> None:
        cfg = _cfg(self.tmp, [_CHECK])
        self._three(cfg, ("pass", "pass", "pass"))
        self.assertEqual([c["id"] for c in self._candidates(cfg)], ["C5-prod"])

    def test_not_ready_when_most_recent_failed(self) -> None:
        cfg = _cfg(self.tmp, [_CHECK])
        self._three(cfg, ("pass", "pass", "fail"))   # newest (C, 06-03) failed
        self.assertEqual(self._candidates(cfg), [])

    def test_unverifiable_breaks_the_streak(self) -> None:
        cfg = _cfg(self.tmp, [_CHECK])
        self._three(cfg, ("pass", "pass", "unverifiable"))
        self.assertEqual(self._candidates(cfg), [])

    def test_not_ready_below_threshold(self) -> None:
        cfg = _cfg(self.tmp, [_CHECK])
        self._bundle(cfg, "A", "2026-06-01", "pass")
        self._bundle(cfg, "B", "2026-06-02", "pass")   # only 2 runs, threshold is 3
        self.assertEqual(self._candidates(cfg), [])

    def test_gating_check_not_a_candidate(self) -> None:
        cfg = _cfg(self.tmp, [{**_CHECK, "gating": True}])
        self._three(cfg, ("pass", "pass", "pass"))
        self.assertEqual(self._candidates(cfg), [])

    def test_without_promote_after_not_a_candidate(self) -> None:
        cfg = _cfg(self.tmp, [{k: v for k, v in _CHECK.items() if k != "promote_after"}])
        self._three(cfg, ("pass", "pass", "pass"))
        self.assertEqual(self._candidates(cfg), [])

    def test_recency_uses_signoff_date_not_an_earlier_section(self) -> None:
        # Three older PASSING cycles, then the NEWEST cycle FAILS — but its §9 sign-off date
        # (2026-06-09) sits after an OLD date in §1 (2026-05-01). A "first date anywhere"
        # heuristic would sort the failing cycle as oldest and wrongly report the check
        # ready; scoping to §9 keeps it among the most-recent N, so it's NOT ready.
        cfg = _cfg(self.tmp, [_CHECK])
        self._bundle(cfg, "A", "2026-06-01", "pass")
        self._bundle(cfg, "B", "2026-06-02", "pass")
        self._bundle(cfg, "D", "2026-06-03", "pass")
        d = cfg.bundle("C")
        d.mkdir(parents=True)
        (d / "check-gates.json").write_text(
            json.dumps({"rows": [{"rule_id": "C5-prod", "result": "fail"}]}), encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            "## 1. Spec\n- cited 2026-05-01 in the brief\n\n"
            "## 9. Check sign-off\n- By / date: t / 2026-06-09\n", encoding="utf-8")
        self.assertEqual(self._candidates(cfg), [])


if __name__ == "__main__":
    unittest.main()
