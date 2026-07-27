"""`publish._t4_passes` announces its gates, and resolves delegated ones (#338).

Two defects in one function.

**Silence.** Every T4 gate ran through a bare captured subprocess: nothing printed
before, nothing during, and `capture_output=True` withheld the child's output until it
exited. On a bundle whose `commit-msg.txt` / `pr-description.md` already exist the
publisher-leaf step is skipped, so this is the FIRST thing publish does after its guards —
the terminal goes quiet immediately and stays that way. Measured: 6m25s of silence for
three parallel model review passes over a 300 KB `patch.diff`; an operator killed a
working run on that basis. `progress.run_with_heartbeat` already owns this pattern and
`gates.py` uses it for Check-time gates; publish was the one runner never migrated.

**Delegation.** Check resolves a bare `subcmd` against `[gates] runner` via
`gates._delegated_cmd`; publish read the raw `cmd`. For a delegated row that is the empty
string — and `subprocess.run("")` exits 0 — so a gate the instance believed it had
registered passed *vacuously* at publish while working correctly at Check.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import progress, publish
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, checks: list[dict], runner: str = "") -> Config:
    cfg = Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main",
        tracker_system="github", tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
    )
    cfg.gates_checks = checks
    cfg.gates_runner = runner
    return cfg


class T4PublishGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bundle = self.tmp / "results" / "issue_1"
        self.bundle.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- delegation ------------------------------------------------------------------

    def test_delegated_subcmd_is_resolved_against_the_runner(self) -> None:
        """Before this fix the empty `cmd` ran, `subprocess.run("")` exited 0, and the gate
        passed without executing anything."""
        marker = self.tmp / "ran"
        cfg = _cfg(self.tmp, [{"id": "T4-d", "tier": "T4", "scope": "bundle", "subcmd": "contribcheck"}],
                   runner=f"sh -c 'touch {marker}' #")
        self.assertTrue(publish._t4_passes(cfg, self.bundle))
        self.assertTrue(marker.exists(),
                        "delegated T4 gate never executed — it passed vacuously")

    def test_delegated_row_with_no_runner_fails_loudly(self) -> None:
        """A misconfiguration must block the push, not sail through as a pass."""
        cfg = _cfg(self.tmp, [{"id": "T4-d", "tier": "T4", "scope": "bundle", "subcmd": "contribcheck"}])
        self.assertFalse(publish._t4_passes(cfg, self.bundle))

    def test_plain_cmd_rows_are_unaffected(self) -> None:
        cfg = _cfg(self.tmp, [{"id": "T4-p", "tier": "T4", "scope": "bundle", "cmd": "true"}])
        self.assertTrue(publish._t4_passes(cfg, self.bundle))
        cfg = _cfg(self.tmp, [{"id": "T4-p", "tier": "T4", "scope": "bundle", "cmd": "false"}])
        self.assertFalse(publish._t4_passes(cfg, self.bundle))

    def test_no_t4_rows_is_still_vacuously_true(self) -> None:
        cfg = _cfg(self.tmp, [{"id": "C4", "tier": "C4", "cmd": "false"}])
        self.assertTrue(publish._t4_passes(cfg, self.bundle))

    # -- reporting -------------------------------------------------------------------

    def test_the_gate_is_announced_before_it_runs(self) -> None:
        """The whole point: something reaches the terminal before the silence starts."""
        cfg = _cfg(self.tmp, [{"id": "T4-x", "tier": "T4", "scope": "bundle",
                               "label": "contribution lint", "cmd": "true"}])
        with mock.patch.object(progress, "run_with_heartbeat",
                               wraps=progress.run_with_heartbeat) as spy:
            self.assertTrue(publish._t4_passes(cfg, self.bundle))
        self.assertEqual(spy.call_count, 1, "T4 gate did not go through the heartbeat")
        self.assertIn("contribution lint", spy.call_args.kwargs.get("label", ""))

    def test_bundle_activity_is_not_used_as_the_status_probe(self) -> None:
        """`gates.py` passes `status=progress.bundle_activity`; publish must NOT.

        A T4 gate reads patch.diff and writes its report once at the end, so the bundle's
        newest write is whatever Check left hours earlier — every tick would render
        "no writes 180m", a stall warning on the run proving it is not stalled.
        """
        cfg = _cfg(self.tmp, [{"id": "T4-x", "tier": "T4", "scope": "bundle", "cmd": "true"}])
        with mock.patch.object(progress, "run_with_heartbeat",
                               wraps=progress.run_with_heartbeat) as spy:
            publish._t4_passes(cfg, self.bundle)
        self.assertIsNone(spy.call_args.kwargs.get("status"),
                          "a T4 gate must not be probed for bundle writes")

    def test_failure_output_reaches_stderr(self) -> None:
        """`run_with_heartbeat` merges the child's stderr into stdout, so the report now
        carries BOTH streams — strictly more than the old `stdout or stderr`, which
        discarded stderr whenever stdout was non-empty."""
        cfg = _cfg(self.tmp, [{"id": "T4-x", "tier": "T4", "scope": "bundle",
                               "cmd": "echo to-stdout; echo to-stderr 1>&2; exit 1"}])
        with mock.patch("sys.stderr") as err:
            self.assertFalse(publish._t4_passes(cfg, self.bundle))
        written = "".join(c.args[0] for c in err.write.call_args_list if c.args)
        self.assertIn("to-stdout", written)
        self.assertIn("to-stderr", written,
                      "stderr was dropped — the old `stdout or stderr` behaviour")

    def test_bundle_is_exported_to_the_gate(self) -> None:
        cfg = _cfg(self.tmp, [{"id": "T4-x", "tier": "T4", "scope": "bundle",
                               "cmd": 'test "$PDCA_BUNDLE" = "' + str(self.bundle) + '"'}])
        self.assertTrue(publish._t4_passes(cfg, self.bundle))

    def test_unlaunchable_gate_blocks_instead_of_crashing(self) -> None:
        cfg = _cfg(self.tmp, [{"id": "T4-x", "tier": "T4", "scope": "bundle", "cmd": "false"}])
        with mock.patch.object(progress, "run_with_heartbeat",
                               side_effect=OSError("boom")):
            self.assertFalse(publish._t4_passes(cfg, self.bundle))


if __name__ == "__main__":
    unittest.main()
