"""Slice for driver-driven vendor-complement advisory selection (issue #200).

The `reviewer`/advisory decorrelation ideal (INTEGRATION §4) is that the reviewer be a
DIFFERENT vendor than the builder — but the builder that actually runs varies per bundle
(`Do model` #167, difficulty routing #134, escalation #135). With
`[leaves.advisory_selection] mode = "vendor-complement"` the driver treats the
[[leaves.advisory]] list as a vendor pool and runs the single leaf whose `family` differs
from the builder recorded in loop-telemetry.json — a Codex build gets a Claude review and
vice-versa. No different-vendor leaf (or an unknown builder) ⇒ same-vendor fallback + a §6
NEEDS-HUMAN note. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import Config, LeafConfig

# A two-vendor pool sharing one role — the shape #200 is built for.
_CLAUDE = {"id": "review-claude", "mode": "stub", "family": "claude", "role": "cleanups"}
_CODEX = {"id": "review-codex", "mode": "stub", "family": "codex", "role": "cleanups"}


def _cfg(root: Path, *, pool: list[dict], selection: dict) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        advisory_leaves=pool, advisory_selection=selection)


class VendorComplement(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, builder_family: str | None) -> Path:
        d = self.tmp / "results" / f"issue_{iid}"
        d.mkdir(parents=True)
        (d / "brief.md").write_text(f"- **Slug:** {iid}\n- **Defect:** x.\n", encoding="utf-8")
        if builder_family is not None:  # the fact Do would have recorded
            (d / "loop-telemetry.json").write_text(
                json.dumps({"attempts": [{"n": 1, "builder": builder_family,
                                          "family": builder_family}]}),
                encoding="utf-8")
        return d

    def _ran(self, d: Path, leaf_id: str) -> bool:
        return leaves.advisory_artifact(d, leaf_id).exists()

    def test_codex_builder_runs_claude_advisory(self) -> None:
        cfg = _cfg(self.tmp, pool=[_CLAUDE, _CODEX], selection={"mode": "vendor-complement"})
        d = self._bundle("C", builder_family="codex")
        leaves.run_advisory_leaves(d, cfg)
        self.assertTrue(self._ran(d, "review-claude"))   # complement of the codex builder
        self.assertFalse(self._ran(d, "review-codex"))   # same-vendor leaf is not run
        self.assertFalse(self._ran(d, "decorrelation"))  # decorrelation held → no §6 note

    def test_claude_builder_runs_codex_advisory(self) -> None:
        cfg = _cfg(self.tmp, pool=[_CLAUDE, _CODEX], selection={"mode": "vendor-complement"})
        d = self._bundle("D", builder_family="claude")
        leaves.run_advisory_leaves(d, cfg)
        self.assertTrue(self._ran(d, "review-codex"))
        self.assertFalse(self._ran(d, "review-claude"))
        self.assertFalse(self._ran(d, "decorrelation"))

    def test_no_complement_falls_back_same_vendor_with_section6_note(self) -> None:
        # Only a claude leaf configured, but the builder was claude too — can't decorrelate.
        cfg = _cfg(self.tmp, pool=[_CLAUDE], selection={"mode": "vendor-complement"})
        d = self._bundle("S", builder_family="claude")
        leaves.run_advisory_leaves(d, cfg)
        self.assertTrue(self._ran(d, "review-claude"))            # review still runs
        note = leaves.advisory_artifact(d, "decorrelation")
        self.assertTrue(note.exists())                            # lapse recorded
        self.assertIn("NEEDS-HUMAN", note.read_text(encoding="utf-8"))

    def test_blank_family_leaf_is_not_a_complement(self) -> None:
        # A #64 leaf that omits `family` is an unknown vendor, not a guaranteed complement:
        # it must not be silently reported as decorrelated (bot review on PR #204).
        no_family = {"id": "review-legacy", "mode": "stub", "role": "cleanups"}
        cfg = _cfg(self.tmp, pool=[no_family], selection={"mode": "vendor-complement"})
        d = self._bundle("B", builder_family="codex")
        leaves.run_advisory_leaves(d, cfg)
        self.assertTrue(self._ran(d, "review-legacy"))   # still runs (fallback, not skipped)
        self.assertTrue(self._ran(d, "decorrelation"))   # but the lapse IS recorded

    def test_unknown_builder_family_falls_back_with_note(self) -> None:
        # No loop-telemetry.json ⇒ the builder vendor is unknown; run one, flag the lapse.
        cfg = _cfg(self.tmp, pool=[_CLAUDE, _CODEX], selection={"mode": "vendor-complement"})
        d = self._bundle("U", builder_family=None)
        leaves.run_advisory_leaves(d, cfg)
        self.assertTrue(self._ran(d, "review-claude"))            # first applicable leaf
        self.assertTrue(self._ran(d, "decorrelation"))

    def test_stale_note_cleared_when_decorrelation_holds(self) -> None:
        cfg = _cfg(self.tmp, pool=[_CLAUDE, _CODEX], selection={"mode": "vendor-complement"})
        d = self._bundle("R", builder_family="codex")
        leaves.advisory_artifact(d, "decorrelation").write_text("stale", encoding="utf-8")
        leaves.run_advisory_leaves(d, cfg)
        self.assertFalse(self._ran(d, "decorrelation"))           # prior attempt's note gone

    def test_default_selection_runs_all_leaves(self) -> None:
        # No advisory_selection ⇒ #64 behaviour: every applicable leaf runs, no note.
        cfg = _cfg(self.tmp, pool=[_CLAUDE, _CODEX], selection={})
        d = self._bundle("A", builder_family="codex")
        leaves.run_advisory_leaves(d, cfg)
        self.assertTrue(self._ran(d, "review-claude"))
        self.assertTrue(self._ran(d, "review-codex"))
        self.assertFalse(self._ran(d, "decorrelation"))


if __name__ == "__main__":
    unittest.main()
