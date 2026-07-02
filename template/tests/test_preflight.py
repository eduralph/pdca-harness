"""Per-lane resource preflight (issue #213) — verify before a lanes>1 fan-out.

Locks in: serial (lanes<=1) never preflights; a failing REQUIRED per_lane doctor check or a
failing `[driver].lane_preflight` command makes it fail (with hints); nothing declared is a
clean no-op; and `_drive_and_act` aborts a lanes>1 batch (raising PreflightError) before
driving any bundle. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import flow, preflight
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, *, lanes: int, lane_preflight: str = "",
         doctor_checks: list[dict] | None = None) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        lanes=lanes, lane_preflight=lane_preflight,
        doctor_checks=doctor_checks or [])


class LanePreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_serial_never_preflights(self) -> None:
        # lanes<=1 → no-op even with a command that would fail.
        cfg = _cfg(self.tmp, lanes=1, lane_preflight="exit 1")
        self.assertEqual(preflight.lane_preflight(cfg), (True, []))

    def test_nothing_declared_is_a_clean_pass(self) -> None:
        cfg = _cfg(self.tmp, lanes=4)
        self.assertEqual(preflight.lane_preflight(cfg), (True, []))

    def test_failing_lane_preflight_command_fails(self) -> None:
        cfg = _cfg(self.tmp, lanes=2, lane_preflight="exit 3")
        ok, msgs = preflight.lane_preflight(cfg)
        self.assertFalse(ok)
        self.assertTrue(any("lane_preflight failed (rc 3)" in m for m in msgs))

    def test_lanes_is_interpolated(self) -> None:
        # {lanes} reaches the command; a mismatch would exit non-zero.
        cfg = _cfg(self.tmp, lanes=6, lane_preflight='test "{lanes}" = "6"')
        self.assertEqual(preflight.lane_preflight(cfg), (True, []))

    def test_required_per_lane_doctor_check_gates(self) -> None:
        checks = [{"id": "lane wt lane{lane}", "cmd": "test -e /nonesuch/lane{lane}",
                   "hint": "make worktrees LANES={lanes}", "per_lane": True, "required": True}]
        cfg = _cfg(self.tmp, lanes=2, doctor_checks=checks)
        ok, msgs = preflight.lane_preflight(cfg)
        self.assertFalse(ok)
        self.assertEqual(len(msgs), 2)                    # one per lane slot (0, 1)
        self.assertIn("make worktrees LANES=2", msgs[0])  # {lanes} interpolated in the hint

    def test_non_required_per_lane_check_does_not_gate(self) -> None:
        checks = [{"id": "opt", "cmd": "exit 1", "per_lane": True, "required": False}]
        cfg = _cfg(self.tmp, lanes=2, doctor_checks=checks)
        self.assertEqual(preflight.lane_preflight(cfg), (True, []))


class DriveAndActPreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundles(self, n: int) -> list[Path]:
        out = []
        for i in range(n):
            d = self.tmp / "results" / f"issue_{i}"
            d.mkdir(parents=True)
            out.append(d)
        return out

    def test_batch_aborts_before_driving_on_failed_preflight(self) -> None:
        cfg = _cfg(self.tmp, lanes=2, lane_preflight="exit 1")
        with self.assertRaises(flow.PreflightError):
            flow._drive_and_act(cfg, self._bundles(2), do_publish=False, do_act=False,
                                by="t", today="2026-07-02")


if __name__ == "__main__":
    unittest.main()
