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

# A bundle-scoped gate whose cmd records BOTH exported bases into the bundle dir, so the test
# reads back exactly what the driver set (`UNSET` when a var is absent). Both, because the
# load-bearing property is that at most ONE of them is ever set (PR #282 review).
_ECHO_BASES = {
    "id": "C4", "tier": "C4", "label": "record bases", "scope": "bundle", "gating": True,
    "cmd": ('printf "%s\\n%s\\n" "${PDCA_BASE-UNSET}" "${PDCA_VERIFY_BASE-UNSET}" '
            '> "$PDCA_BUNDLE/bases.txt"'),
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
        self.cfg.gates_checks = [_ECHO_BASES]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** v\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    def _recorded_bases(self, d: Path) -> dict[str, str]:
        """Both bases as the gate command actually saw them."""
        gates.run_gates(d, self.cfg)
        base, verify_base = (d / "bases.txt").read_text(encoding="utf-8").splitlines()[:2]
        return {"PDCA_BASE": base, "PDCA_VERIFY_BASE": verify_base}

    def _recorded_base(self, d: Path) -> str:
        return self._recorded_bases(d)["PDCA_VERIFY_BASE"]

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

    def test_onto_branch_wins_over_the_wave_base(self) -> None:
        """PR #282 review (codex). A bundle can carry BOTH an `Onto branch` and a wave
        stack-base marker. `publish.publish` takes the Onto path and returns BEFORE it ever
        reads the stack-base, so the fix is committed to the Onto branch. Exporting the wave
        base too would send the verifier to the integration branch while publish commits
        elsewhere — the test base diverging from the deploy base, which is exactly what #54's
        PDCA_BASE exists to prevent. The two exports are mutually exclusive; Onto wins."""
        d = self._bundle("ONTO")
        (d / "brief.md").write_text(
            "- **Slug:** v\n- **Onto branch:** origin/feature/x\n", encoding="utf-8")
        publish.write_stack_base(d, "pdca-integration/main")   # the wave driver stamps it too
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "origin/feature/x")   # where publish commits
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "UNSET")       # …and where the gate tests

    def test_wave_base_still_exported_without_an_onto_branch(self) -> None:
        # The ordinary wave dependent — no Onto — is unaffected by the precedence rule.
        d = self._bundle("NOONTO")
        publish.write_stack_base(d, "pdca-integration/main")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "UNSET")
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "origin/pdca-integration/main")

    def test_onto_alone_is_unchanged(self) -> None:
        # Stack mode (#54) with no wave marker — behaviour predating #273.
        d = self._bundle("ONTOONLY")
        (d / "brief.md").write_text(
            "- **Slug:** v\n- **Onto branch:** origin/feature/x\n", encoding="utf-8")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "origin/feature/x")
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "UNSET")

    def test_the_two_bases_are_never_both_set(self) -> None:
        # The invariant, stated directly: a gate is told exactly one base, or none.
        for name, onto, marker in (("A", True, True), ("B", True, False),
                                   ("C", False, True), ("D", False, False)):
            with self.subTest(onto=onto, marker=marker):
                d = self._bundle(f"INV{name}")
                if onto:
                    (d / "brief.md").write_text(
                        "- **Slug:** v\n- **Onto branch:** origin/feature/x\n",
                        encoding="utf-8")
                if marker:
                    publish.write_stack_base(d, "pdca-integration/main")
                bases = self._recorded_bases(d)
                set_count = sum(1 for v in bases.values() if v != "UNSET")
                self.assertLessEqual(set_count, 1,
                                     f"the test base and the deploy base can diverge: {bases}")

    def test_public_accessor_matches_the_marker(self) -> None:
        d = self._bundle("ACC")
        self.assertEqual(publish.read_stack_base(d), "")
        publish.write_stack_base(d, "pdca-integration/main")
        self.assertEqual(publish.read_stack_base(d), "pdca-integration/main")


if __name__ == "__main__":
    unittest.main()
