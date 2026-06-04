"""End-to-end vertical slice for the PDCA driver (stdlib unittest — no deps).

Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
Exercises the full control flow on the toy brief with stub leaves/gates:
init → Do → gates → reviewer → assembled SUMMARY → human sign-off → COMPLETE,
plus the C6 accept-gate, the independence contract, and an iterate transition.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import driver, leaves, signoff, state
from pdca_harness.config import Config, LeafConfig

TOY_BRIEF = Path(__file__).resolve().parents[1] / "examples" / "toy" / "brief.md"


def _stub_config(root: Path) -> Config:
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
    )


class VerticalSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.d = self.cfg.bundle("TOY")
        self.d.mkdir(parents=True)
        shutil.copyfile(TOY_BRIEF, self.d / "brief.md")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_to_awaiting_signoff(self) -> None:
        self.assertEqual(state.state(self.d), state.PLANNED)
        final = driver.run_issue(self.d, self.cfg)
        self.assertEqual(final, state.AWAITING_SIGNOFF)
        for name in ("patch.diff", "build-notes.md", "check-gates.json", "check-review.md", "SUMMARY.md", "test_toy.py"):
            self.assertTrue((self.d / name).exists(), f"missing {name}")

    def test_independence_contract(self) -> None:
        # The reviewer's input list must never contain build-notes.md.
        inputs = leaves.reviewer_input_paths(self.d)
        self.assertNotIn(self.d / "build-notes.md", inputs)

    def test_accept_blocked_until_needs_human_cleared(self) -> None:
        driver.run_issue(self.d, self.cfg)
        summary = self.d / "SUMMARY.md"
        # Stub reviewer flags the always-human validation item → §6 is non-empty.
        self.assertTrue(signoff.open_needs_human(summary))
        # Simulate the human clearing §6 (check the box), then accept.
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertFalse(signoff.open_needs_human(summary))
        signoff.record(summary, action="accept", by="tester", date="2026-01-01")
        self.assertEqual(state.state(self.d), state.COMPLETE)

    def test_iterate_to_do_clears_downstream(self) -> None:
        driver.run_issue(self.d, self.cfg)
        summary = self.d / "SUMMARY.md"
        signoff.record(summary, action="iterate-do", by="tester", date="2026-01-01")
        self.assertEqual(state.state(self.d), state.ITERATE_DO)
        driver.advance(self.d, self.cfg)  # performs the clear
        self.assertEqual(state.state(self.d), state.PLANNED)
        self.assertFalse((self.d / "patch.diff").exists())
        self.assertFalse((self.d / "test_toy.py").exists())
        self.assertTrue((self.d / "brief.md").exists())

    def test_iterate_to_plan_versions_brief(self) -> None:
        driver.run_issue(self.d, self.cfg)
        summary = self.d / "SUMMARY.md"
        signoff.record(summary, action="iterate-plan", by="tester", date="2026-01-01")
        driver.advance(self.d, self.cfg)
        self.assertEqual(state.state(self.d), state.UNPLANNED)
        self.assertTrue((self.d / "brief.v1.md").exists())
        self.assertFalse((self.d / "brief.md").exists())


if __name__ == "__main__":
    unittest.main()
