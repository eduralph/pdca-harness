"""A wave>0 bundle-scoped gate is told the folded base via $PDCA_VERIFY_BASE (#273).

Under the wave model, a dependent bundle's Do worktree is cut off the run-scoped integration
branch (prior waves' folded patches). A per-fix verifier that resets to a base must reset to
THAT branch, not the brief's origin base — else the dependent false-fails "patch does not
apply" or measures red→green against a tree lacking its prereq. The driver exports the folded
base as `PDCA_VERIFY_BASE=origin/<integration-branch>` to bundle-scoped gate commands, read
from the per-bundle `stack-base` marker the wave driver stamped before Check. A wave-0 bundle
has no marker, so the var is absent and behaviour is unchanged.

Real gate commands, no model/network. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import gates, publish
from pdca_harness.config import Config, LeafConfig

# A bundle-scoped gate whose cmd records the exported verify base into the bundle dir, so the
# test can read back exactly what the driver set (or `UNSET` when the var is absent).
_ECHO_VERIFY_BASE = {
    "id": "C4", "tier": "C4", "label": "record verify base", "scope": "bundle", "gating": True,
    "cmd": 'printf "%s" "${PDCA_VERIFY_BASE-UNSET}" > "$PDCA_BUNDLE/verify-base.txt"',
}


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
        base_remote="origin",
    )


class VerifyBaseExport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.cfg.gates_checks = [_ECHO_VERIFY_BASE]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** v\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    def _recorded_base(self, d: Path) -> str:
        gates.run_gates(d, self.cfg)
        return (d / "verify-base.txt").read_text(encoding="utf-8")

    def test_wave_dependent_gets_the_folded_base(self) -> None:
        d = self._bundle("DEP")
        publish.write_stack_base(d, "pdca-integration/main")   # the wave driver stamps this
        self.assertEqual(self._recorded_base(d), "origin/pdca-integration/main")

    def test_flattened_base_is_carried_verbatim(self) -> None:
        # The marker already holds the flattened branch name; the gate export just prefixes it.
        d = self._bundle("DEP2")
        publish.write_stack_base(d, "pdca-integration/maintenance-sgramps60")
        self.assertEqual(self._recorded_base(d),
                         "origin/pdca-integration/maintenance-sgramps60")

    def test_wave0_bundle_has_no_verify_base(self) -> None:
        # No stack-base marker → the var is unset → today's behaviour, unchanged.
        d = self._bundle("W0")
        self.assertFalse((d / publish.STACK_BASE_FILE).exists())
        self.assertEqual(self._recorded_base(d), "UNSET")

    def test_cleared_marker_reverts_to_no_verify_base(self) -> None:
        # A stale marker cleared by the driver (#187) → back to unset.
        d = self._bundle("CLR")
        publish.write_stack_base(d, "pdca-integration/main")
        publish.clear_stack_base(d)
        self.assertEqual(self._recorded_base(d), "UNSET")

    def test_public_accessor_matches_the_marker(self) -> None:
        d = self._bundle("ACC")
        self.assertEqual(publish.read_stack_base(d), "")
        publish.write_stack_base(d, "pdca-integration/main")
        self.assertEqual(publish.read_stack_base(d), "pdca-integration/main")


if __name__ == "__main__":
    unittest.main()
