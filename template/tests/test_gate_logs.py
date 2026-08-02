"""Gate evidence logs (issue #370, stdlib unittest).

A gate's full output used to be discarded: `_run_one` captured combined stdout+stderr,
`_classify` kept only the last line, the row truncated it to 120 characters, and nothing
wrote the rest anywhere — so the entire record of a *gating* red that parked a bundle was
one truncated line. This suite proves the evidence-sufficiency invariant (state-is-files:
a verdict's full basis is reconstructable from bundle files alone, per round):

  (a) a bundle-scoped run writes `gate-logs/<rule_id>.log` — header (cmd, cwd,
      $PDCA_WORKTREE, start, duration, exit/outcome) + the combined output VERBATIM,
      one file per rule id, overwritten per Check run;
  (b) the row gains `log` + `duration_secs` additively (existing keys unchanged);
  (c) the iterate archive moves `gate-logs/` with the round's other artifacts;
  (d) a timed-out gate (#368) attaches its partial capture — where it hung, not nothing;
  (e) a repo-scoped run with no bundle keeps today's behaviour (no logs, no new keys);
  a dry re-gate (`run_gates_dry`, revalidate) never overwrites frozen evidence;
  and (iteration 2) a log-write FAILURE is never silent: the row carries `log_error`
  with the reason and a stderr line names it — while the verdict stays unchanged.

And (issue #402) the other half of the same invariant: the one line the row DOES keep must
be the gate's own verdict, not a line it relayed. The capture is one merged stdout+stderr
stream, so "the last line" is whatever a child process flushed last — a green C4 wrapper
was recorded with a scratch `/tmp` path from a since-deleted sandbox as its whole evidence,
which is neither the verdict's *basis* nor *reconstructable* from bundle files. A gate now
declares its summary with `PDCA-EVIDENCE:` (the #428 declaration form — first text on the
line) and that is what the row files, whatever follows it.

Deterministic real gate commands — no Claude / Docker. Run from the project root:
PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import driver, gates, state
from pdca_harness.config import Config, LeafConfig

# A real bundle-scoped gating gate; only the cmd varies per test.
_GATE = {"id": "C4-log", "tier": "C4", "label": "verify", "scope": "bundle", "gating": True}
# Multi-line output: the 120-char evidence line keeps only the LAST line, so the earlier
# lines prove the log carries what the row summary drops.
_MULTI = {**_GATE, "cmd": "echo first-line; echo middle-line; echo last-line"}


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


class GateEvidenceLogs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** gl\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    def _gate_row(self, result: dict) -> dict:
        return next(r for r in result["rows"] if r["rule_id"] == "C4-log")

    # -- (a) the log file: header + verbatim body, bundle-scoped ---------------------

    def test_bundle_run_writes_header_and_verbatim_output(self) -> None:
        d = self._bundle("LOG")
        self.cfg.gates_checks = [_MULTI]
        result = gates.run_gates(d, self.cfg)

        log = d / "gate-logs" / "C4-log.log"
        self.assertTrue(log.exists(), "bundle-scoped gate run left no gate-logs/<id>.log")
        text = log.read_text(encoding="utf-8")
        # The header: command, cwd, $PDCA_WORKTREE, start time, duration, exit/outcome.
        self.assertIn(f"# cmd: {_MULTI['cmd']}", text)
        self.assertIn(f"# cwd: {self.cfg.root}", text)
        self.assertIn("# PDCA_WORKTREE:", text)
        self.assertIn("# start: ", text)
        self.assertIn("# duration_secs: ", text)
        self.assertIn("# exit: 0", text)
        self.assertIn("# outcome: pass", text)
        # The body: the combined output VERBATIM — every line, in order, not just the
        # last one the 120-char evidence summary keeps.
        self.assertIn("first-line\nmiddle-line\nlast-line\n", text)
        # `_MULTI` declares nothing, so the documented FALLBACK applies (#402): the last
        # output line. The declared case is pinned in GateEvidenceIsTheGatesOwnVerdict.
        self.assertEqual(self._gate_row(result)["path_line"], "last-line")

    # -- (b) the row keys, additive ---------------------------------------------------

    def test_row_gains_log_and_duration_additively(self) -> None:
        d = self._bundle("ROW")
        self.cfg.gates_checks = [_MULTI]
        row = self._gate_row(gates.run_gates(d, self.cfg))
        self.assertEqual(row["log"], "gate-logs/C4-log.log")  # bundle-relative
        self.assertIsInstance(row["duration_secs"], (int, float))
        self.assertGreaterEqual(row["duration_secs"], 0)
        self.assertNotIn("log_error", row)  # the failure key only appears on failure
        # Additive: every pre-existing key is still there for existing consumers.
        self.assertLessEqual(
            {"check", "result", "oracle", "rule_id", "path_line", "gating", "element"},
            set(row))

    # -- (a) one file per rule id, overwritten per Check run ---------------------------

    def test_rerun_clears_stale_logs_and_overwrites(self) -> None:
        d = self._bundle("OVR")
        stale = d / "gate-logs" / "removed-gate.log"
        stale.parent.mkdir(parents=True)
        stale.write_text("evidence of a check no longer configured\n", encoding="utf-8")
        self.cfg.gates_checks = [_MULTI]
        gates.run_gates(d, self.cfg)
        self.assertFalse(stale.exists(),
                         "a stale log survived the re-run masquerading as current evidence")
        self.assertTrue((d / "gate-logs" / "C4-log.log").exists())

    # -- (iteration 2) a write failure is surfaced, never silent -----------------------

    def test_write_failure_surfaces_log_error_and_stderr(self) -> None:
        d = self._bundle("ERR")
        # The reproduced collision: a FILE squatting on the gate-logs path. The pre-run
        # clear (rmtree) cannot remove it and mkdir cannot replace it, so the evidence
        # write fails — which must be visible, not a silently absent log.
        (d / state.GATE_LOGS_DIR).write_text("squatter\n", encoding="utf-8")
        self.cfg.gates_checks = [_MULTI]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = gates.run_gates(d, self.cfg)
        row = self._gate_row(result)
        # The verdict is untouched — persistence failure never alters a gate result …
        self.assertEqual(row["result"], "pass")
        self.assertEqual(result["overall"], "pass")
        self.assertEqual(row["path_line"], "last-line")  # undeclared ⇒ fallback (#402)
        # … but the failure is recorded IN the row (additive), with the reason …
        self.assertNotIn("log", row)
        self.assertIn("gate-logs/C4-log.log", row["log_error"])
        self.assertIn("duration_secs", row)  # the measurement itself did happen
        # … and named on stderr, so a live run shows it too.
        self.assertIn("C4-log", err.getvalue())
        self.assertIn("not written", err.getvalue())
        # The recorded row (check-gates.json) carries the same signal — state is files.
        self.assertIn("log_error", (d / "check-gates.json").read_text(encoding="utf-8"))

    # -- (d) timeout attaches the partial capture (#368 × #370) ------------------------

    def test_timeout_attaches_partial_capture(self) -> None:
        d = self._bundle("TMO")
        self.cfg.gates_checks = [
            {**_GATE, "cmd": "echo before-the-hang; sleep 30", "timeout_secs": 1}]
        row = self._gate_row(gates.run_gates(d, self.cfg))
        self.assertEqual(row["result"], "unverifiable")  # the #368 outcome, unchanged
        text = (d / "gate-logs" / "C4-log.log").read_text(encoding="utf-8")
        self.assertIn("timeout", text)  # the header names the outcome …
        self.assertIn("before-the-hang", text)  # … and the log shows WHERE it hung

    # -- (e) repo-scoped, no bundle: today's behaviour ---------------------------------

    def test_working_tree_run_keeps_todays_behaviour(self) -> None:
        self.cfg.gates_checks = [
            {"id": "T3-x", "tier": "T3", "label": "suite", "scope": "repo",
             "gating": False, "cmd": "echo repo-run"}]
        result = gates.run_working_tree(self.cfg)
        row = next(r for r in result["rows"] if r["rule_id"] == "T3-x")
        self.assertNotIn("log", row)
        self.assertNotIn("log_error", row)
        self.assertNotIn("duration_secs", row)
        self.assertFalse((self.cfg.root / "gate-logs").exists())

    # -- a dry re-gate (revalidate) never rewrites frozen evidence ---------------------

    def test_dry_regate_leaves_frozen_evidence_untouched(self) -> None:
        d = self._bundle("DRY")
        self.cfg.gates_checks = [{**_GATE, "cmd": "echo attempt-evidence"}]
        gates.run_gates(d, self.cfg)
        log = d / "gate-logs" / "C4-log.log"
        frozen = log.read_text(encoding="utf-8")
        self.cfg.gates_checks = [{**_GATE, "cmd": "echo later-re-gate"}]
        gates.run_gates_dry(d, self.cfg)
        self.assertEqual(log.read_text(encoding="utf-8"), frozen,
                         "run_gates_dry mutated the frozen gate evidence")

    # -- (c) the iterate archive keeps each round's evidence ---------------------------

    def test_gate_logs_dir_is_downstream_of_brief(self) -> None:
        self.assertIn(state.GATE_LOGS_DIR, state.DOWNSTREAM_OF_BRIEF)

    def test_iterate_archives_gate_logs_with_the_round(self) -> None:
        d = self._bundle("ARCH")
        log = d / state.GATE_LOGS_DIR / "C4-log.log"
        log.parent.mkdir(parents=True)
        log.write_text("round-one evidence\n", encoding="utf-8")
        driver._archive_iteration(d, 1, include_brief=False)
        moved = d / "iteration-v1" / state.GATE_LOGS_DIR / "C4-log.log"
        self.assertTrue(moved.exists(), "gate-logs/ was not archived with its round")
        self.assertEqual(moved.read_text(encoding="utf-8"), "round-one evidence\n")
        self.assertFalse((d / state.GATE_LOGS_DIR).exists(),
                         "the next round would inherit the previous round's evidence")


class GateEvidenceIsTheGatesOwnVerdict(unittest.TestCase):
    """(issue #402) The recorded evidence is what the GATE declared — never a line it
    merely relayed from a child process.

    Reproduced verbatim from the shipped defect: `run-verify.sh` ends with
    `echo "C4 PASS: red without the fix, green with it"`, exits 0, and the merged
    stdout+stderr capture then carries block-buffered stdout the offline suite leaked from
    the code it drove. `_classify` took the last line, so a GREEN gate was frozen with
    `/tmp/…/results/issue_500/split-proposal.md` — a path in a sandbox that no longer
    exists — as its whole evidence, and readers escalated it to §6 NEEDS-HUMAN.
    """

    # The gate's own summary, then trailing child stdout — the shipped shape.
    _SUMMARY = "C4 PASS: red without the fix, green with it"
    _RELAYED = "/tmp/tmpy_ulekwf/results/issue_500/split-proposal.md"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.n = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, cmd: str) -> dict:
        """Run one real gate command through the production path and return its row."""
        self.n += 1
        d = self.cfg.bundle(f"EV{self.n}")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** ev\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.cfg.gates_checks = [{**_GATE, "cmd": cmd}]
        self.result = gates.run_gates(d, self.cfg)
        self.bundle = d
        return next(r for r in self.result["rows"] if r["rule_id"] == "C4-log")

    def test_declared_summary_survives_trailing_child_output(self) -> None:
        row = self._row(f"echo '{gates.EVIDENCE_MARKER} {self._SUMMARY}'; "
                        f"echo {self._RELAYED}")
        self.assertEqual(row["result"], "pass")
        self.assertEqual(row["path_line"], self._SUMMARY,
                         "the row filed a line the gate relayed instead of its own verdict")
        self.assertNotIn("/tmp/", row["path_line"])
        # The full basis is untouched — the relayed line is still in the evidence log.
        log = (self.bundle / row["log"]).read_text(encoding="utf-8")
        self.assertIn(self._RELAYED, log)

    def test_the_last_declaration_wins(self) -> None:
        """A wrapper declares per leg; its final word summarises the run."""
        row = self._row(f"echo '{gates.EVIDENCE_MARKER} leg 1 of 2 green'; "
                        f"echo relayed-noise; "
                        f"echo '{gates.EVIDENCE_MARKER} both legs green'; "
                        f"echo {self._RELAYED}")
        self.assertEqual(row["path_line"], "both legs green")

    def test_a_declaring_gate_that_exits_non_zero_still_fails(self) -> None:
        """The evidence marker declares EVIDENCE, never a verdict — the #329 hazard
        (a marker laundering a red into something that does not gate) must not reappear."""
        row = self._row(f"echo '{gates.EVIDENCE_MARKER} C4 FAIL: green without the fix'; "
                        f"echo {self._RELAYED}; exit 1")
        self.assertEqual(row["result"], "fail")
        self.assertEqual(row["path_line"], "C4 FAIL: green without the fix")
        self.assertEqual(self.result["overall"], "fail")  # a gating red still gates

    def test_a_mid_line_marker_is_relayed_text_not_a_declaration(self) -> None:
        """Same rule as #428: a gate that quotes the marker (a child's log, this test file
        read back) has not declared anything — the undeclared fallback applies."""
        row = self._row(f"echo 'suite log: write \"{gates.EVIDENCE_MARKER} x\" to declare'; "
                        f"echo real-last-line")
        self.assertEqual(row["path_line"], "real-last-line")

    def test_an_undeclared_gate_keeps_the_documented_fallback(self) -> None:
        row = self._row("echo first; echo second-and-last")
        self.assertEqual(row["path_line"], "second-and-last")
        # … and a bare marker with no summary is no declaration, so it falls back too.
        row = self._row(f"echo {gates.EVIDENCE_MARKER}; echo second-and-last")
        self.assertEqual(row["path_line"], "second-and-last")

    def test_unverifiable_declaration_still_wins_the_result(self) -> None:
        """#428's marker decides the RESULT and its own reason; #402's decides the evidence
        of a pass/fail row. One notion of "the gate said this", two channels."""
        row = self._row(f"echo '{gates.UNVERIFIABLE_MARKER} test-only patch'; "
                        f"echo '{gates.EVIDENCE_MARKER} summary'; echo {self._RELAYED}")
        self.assertEqual(row["result"], "unverifiable")
        self.assertEqual(row["path_line"], "test-only patch")


if __name__ == "__main__":
    unittest.main()
