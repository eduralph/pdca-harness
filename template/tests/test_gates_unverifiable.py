"""Offline slice for the `unverifiable` gate-result class (issue #46, stdlib unittest).

A gating gate that genuinely cannot RUN its mechanical check declares `unverifiable`
(exit 77, or a line it STARTS with the `PDCA-UNVERIFIABLE:` marker while exiting 0 or 77 —
a non-zero exit is a fail whatever it printed, #329, and a marker quoted mid-line is text the
gate relayed rather than a verdict it declared, #428) instead of a bogus pass or
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
# A gate that genuinely FAILED while its output happens to carry the marker — a suite in which
# one check deferred and a DIFFERENT test failed. The marker must not launder this (#329).
_FAIL_WITH_MARKER = {**_GATE, "cmd": (
    "echo 'PDCA-UNVERIFIABLE: PDCA_PROD_PACKAGE is unset'; "
    "echo 'AssertionError: expected 3, got 7'; exit 1")}

# --- #428: output a gate RELAYED is not a declaration ------------------------------------
# The frozen shape: a green gate whose captured output quotes the contract sentence of
# `engine/scripts/run-verify.sh` (its comment block) — nothing declared it. Composed from the
# production constant on purpose, so this module never emits the literal at a *declaring*
# position in its own (test-runner) output and cannot flip the C4 row that classifies it.
_M = gates.UNVERIFIABLE_MARKER
_QUOTE = f"# ... Emit `{_M} <reason>` and exit 77 (-> SUMMARY 6 NEEDS-HUMAN, non-gating)"
_RELAYED = {**_GATE, "cmd": f"echo '{_QUOTE}'; echo 'suite OK'; exit 0"}
_RELAYED_ONLY_LINE = {**_GATE, "cmd": f"echo 'see the docs: {_QUOTE}'; exit 0"}
_RELAYED_THEN_DECLARED = {**_GATE, "cmd": (
    f"echo '{_QUOTE}'; echo '  {_M} no prod file to revert'; exit 0")}


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

    def test_the_marker_does_not_launder_a_non_zero_exit(self) -> None:
        """#329: the marker used to win over ANY exit code, so a hard failure whose output
        merely CONTAINED the marker became `unverifiable` — and that is not a gating failure,
        so `overall` read "pass". A gate with no possible verdict has its own channel (exit
        77); it must not piggy-back on a failure."""
        result = gates.run_gates(self._gated_bundle("FAILMARK", _FAIL_WITH_MARKER), self.cfg)
        self.assertEqual(self._c4_row(result)["result"], "fail")
        self.assertEqual(result["overall"], "fail")

    def test_the_marker_is_still_honoured_alongside_exit_77(self) -> None:
        """Exit 77 IS the unverifiable channel, so a marker there is a better reason string —
        the fix narrows to non-failing exits, it does not require rc == 0."""
        gate = {**_GATE, "cmd": "echo 'PDCA-UNVERIFIABLE: no prod path declared'; exit 77"}
        result = gates.run_gates(self._gated_bundle("RCMARK", gate), self.cfg)
        row = self._c4_row(result)
        self.assertEqual(row["result"], "unverifiable")
        self.assertIn("no prod path declared", row["path_line"])  # the marker's reason, not rc

    def test_the_shipped_production_path_check_still_defers(self) -> None:
        """The contract the fix relies on: `scripts/checks/test_exercises_production.py`
        returns 0 on every marker-emitting path, so narrowing to non-failing exits leaves the
        real deferral route working."""
        gate = {**_GATE, "cmd": "echo 'PDCA-UNVERIFIABLE: PDCA_PROD_PACKAGE is unset'; exit 0"}
        self.assertEqual(self._c4_row(gates.run_gates(
            self._gated_bundle("SHIPPED", gate), self.cfg))["result"], "unverifiable")

    def test_a_relayed_marker_does_not_override_a_green_gate(self) -> None:
        """#428 — the exit-0 half of the substring hole #329 closed for non-zero exits. The
        verdict is the GATE's to declare; a line it merely relayed from what it ran (a child's
        log, an assertion diff, a source comment a test read back) declares nothing. Recording
        it `unverifiable` drops a genuine green out of `overall` — it does not count toward it
        — and would launder a genuine red into "defer to the human" just as readily."""
        result = gates.run_gates(self._gated_bundle("RELAY", _RELAYED), self.cfg)
        row = self._c4_row(result)
        self.assertEqual(row["result"], "pass")
        self.assertEqual(row["path_line"], "suite OK")  # the gate's real evidence line
        self.assertEqual(result["overall"], "pass")

    def test_a_relayed_marker_on_the_only_output_line_still_passes(self) -> None:
        """The narrowing is by POSITION IN THE LINE, not by which line: a green gate whose
        single output line quotes the contract mid-sentence is still a pass."""
        result = gates.run_gates(self._gated_bundle("RELAY1", _RELAYED_ONLY_LINE), self.cfg)
        self.assertEqual(self._c4_row(result)["result"], "pass")
        self.assertEqual(result["overall"], "pass")

    def test_a_declaration_after_relayed_text_is_still_honoured(self) -> None:
        """Symmetry check: relayed noise before the gate's own declaration must not hide it.
        The declaring line here is also indented — leading whitespace is ignored, the marker
        just has to be the first text the gate put on the line."""
        result = gates.run_gates(
            self._gated_bundle("RELAYDECL", _RELAYED_THEN_DECLARED), self.cfg)
        row = self._c4_row(result)
        self.assertEqual(row["result"], "unverifiable")
        self.assertIn("no prod file to revert", row["path_line"])
        self.assertEqual(result["overall"], "pass")

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
