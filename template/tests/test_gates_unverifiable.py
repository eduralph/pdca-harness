"""Offline slice for the `unverifiable` gate-result class (issue #46, stdlib unittest).

A gating gate that genuinely cannot RUN its mechanical check declares `unverifiable`
(exit 77 or a `PDCA-UNVERIFIABLE:` marker line, marker wins) instead of a bogus pass or
a hard fail. Proves: the gate runner classifies it, it does NOT fail `overall`, assemble
routes it into SUMMARY §6 NEEDS-HUMAN, and the existing C6 accept-guard then blocks
`--accept` until the human clears it. Deterministic real gate commands — no Claude /
Docker. Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import assemble, cli, gates, signoff, state
from pdca_harness.config import Config, LeafConfig

# A real bundle-scoped gating gate; only the cmd decides the result.
_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle", "gating": True}
_PASS = {**_GATE, "cmd": "true"}
_FAIL = {**_GATE, "cmd": "false"}
_UNVERIFIABLE_RC = {**_GATE, "cmd": "echo 'no prod file to revert'; exit 77"}
_UNVERIFIABLE_MARKER = {**_GATE, "cmd": "echo 'PDCA-UNVERIFIABLE: test-only change'; exit 0"}


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


class UnverifiableGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gated_bundle(self, iid: str, gate: dict) -> Path:
        """A bundle whose check-gates.json was written by running `gate`."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** uv\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.cfg.gates_checks = [gate]
        gates.run_gates(d, self.cfg)  # writes check-gates.json / .md
        return d

    def _c4_row(self, result: dict) -> dict:
        return next(r for r in result["rows"] if r["element"] == "C4")

    # --- the gate runner classifies the third result, which never fails overall ---

    def test_exit_77_is_unverifiable_not_fail(self) -> None:
        result = gates.run_gates(self._gated_bundle("RC", _UNVERIFIABLE_RC), self.cfg)
        self.assertEqual(self._c4_row(result)["result"], "unverifiable")
        self.assertEqual(result["overall"], "pass")  # gating row, but not a failure

    def test_marker_line_wins_over_exit_zero(self) -> None:
        result = gates.run_gates(self._gated_bundle("MARK", _UNVERIFIABLE_MARKER), self.cfg)
        row = self._c4_row(result)
        self.assertEqual(row["result"], "unverifiable")
        self.assertIn("test-only change", row["path_line"])  # reason after the marker
        self.assertEqual(result["overall"], "pass")

    def test_real_fail_still_fails(self) -> None:
        result = gates.run_gates(self._gated_bundle("FAIL", _FAIL), self.cfg)
        self.assertEqual(self._c4_row(result)["result"], "fail")
        self.assertEqual(result["overall"], "fail")  # unchanged: a real fail still gates

    def test_pass_still_passes(self) -> None:
        result = gates.run_gates(self._gated_bundle("PASS", _PASS), self.cfg)
        self.assertEqual(self._c4_row(result)["result"], "pass")
        self.assertEqual(result["overall"], "pass")

    # --- assemble routes it to §6, and C6 then blocks accept until cleared ---

    def test_unverifiable_routes_to_section6_and_c6_blocks_accept(self) -> None:
        d = self._gated_bundle("UV", _UNVERIFIABLE_RC)
        # A clean advisory review so §6 is fed ONLY by the unverifiable gate.
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        assemble.assemble_summary(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

        summary = d / "SUMMARY.md"
        open_items = signoff.open_needs_human(summary)
        self.assertTrue(any("unverifiable" in it for it in open_items),
                        f"unverifiable gate not routed to §6: {open_items}")

        # C6: accept is refused while the §6 item is open …
        accept = SimpleNamespace(issue_id="UV", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="t", delta="")
        self.assertEqual(cli._signoff(self.cfg, accept), 1)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)  # not accepted

        # … and allowed once the human checks it off.
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertEqual(cli._signoff(self.cfg, accept), 0)
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_gating_fail_routes_to_section6_and_c6_blocks_accept(self) -> None:
        # #166: a gating gate that hard-FAILS must become a §6 item so C6 blocks accept —
        # previously only `unverifiable` reached §6, so a red gating gate could reach COMPLETE.
        d = self._gated_bundle("GF", _FAIL)
        self.assertEqual(gates.run_gates(d, self.cfg)["overall"], "fail")  # gating fail
        # A clean advisory review so §6 is fed ONLY by the failing gate.
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        assemble.assemble_summary(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

        summary = d / "SUMMARY.md"
        open_items = signoff.open_needs_human(summary)
        self.assertTrue(any("FAILED (gating)" in it for it in open_items),
                        f"gating fail not routed to §6: {open_items}")

        # C6: accept is refused while the §6 item is open …
        accept = SimpleNamespace(issue_id="GF", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="t", delta="")
        self.assertEqual(cli._signoff(self.cfg, accept), 1)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)  # not accepted

        # … and allowed once the human clears it (an explicit override).
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertEqual(cli._signoff(self.cfg, accept), 0)
        self.assertEqual(state.state(d), state.COMPLETE)


if __name__ == "__main__":
    unittest.main()
