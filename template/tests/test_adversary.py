"""Slice for the reference adversarial reviewer (issue #151) — a refutation advisory leaf.

No new orchestration code: the adversary composes from the advisory-leaf mechanism (#64).
This locks in the *shipped config's* behavior — gated to `Difficulty: high`, it runs and
writes `check-advisory-adversary.md` (an advisory artifact whose NEEDS-HUMAN findings fold
into §6); on a low-difficulty bundle it is skipped. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import Config, LeafConfig

# The config shipped (commented) in pdca.toml.jinja for the adversary leaf.
_ADVERSARY = {
    "id": "adversary",
    "mode": "stub",
    "role": "refute the red→green evidence and the reviewer's verdict",
    "when": {"field": "difficulty", "substring": "high"},
}


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        advisory_leaves=[_ADVERSARY])


class Adversary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str, difficulty: str | None) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        body = f"- **Slug:** {iid.lower()}\n- **Defect:** x.\n"
        if difficulty:
            body += f"- **Difficulty:** {difficulty}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_runs_advisory_on_high_difficulty(self) -> None:
        d = self._brief("H", "high")
        leaves.run_advisory_leaves(d, self.cfg)
        art = leaves.advisory_artifact(d, "adversary")
        self.assertTrue(art.exists())
        # Advisory: a NEEDS-HUMAN finding (the harness folds it into §6), never a gate.
        self.assertIn("NEEDS-HUMAN", art.read_text(encoding="utf-8"))

    def test_skipped_on_low_difficulty(self) -> None:
        d = self._brief("L", "low")
        leaves.run_advisory_leaves(d, self.cfg)
        self.assertFalse(leaves.advisory_artifact(d, "adversary").exists())

    def test_skipped_when_difficulty_absent(self) -> None:
        # Default-safe: an unset Difficulty doesn't trip the high-only gate.
        d = self._brief("U", None)
        leaves.run_advisory_leaves(d, self.cfg)
        self.assertFalse(leaves.advisory_artifact(d, "adversary").exists())


if __name__ == "__main__":
    unittest.main()
