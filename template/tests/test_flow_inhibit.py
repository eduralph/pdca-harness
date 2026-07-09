"""Unit tests for the `pdca flow` suspend-inhibitor decision (#244, #259).

`_suspend_inhibitor_argv` is the pure decision behind the re-exec: given argv + env it
returns the keep-awake wrapper command, or None to run unwrapped. Tests exercise the
decision without actually re-exec'ing the process, and inject `probe` (#259) so no test
shells out to a real inhibitor.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from pdca_harness import cli


BASE_ARGV = ["pdca", "flow", "123"]

SYSTEMD = ["systemd-inhibit", "--what=idle:sleep", "--why=pdca flow"]
CAFFEINATE = ["caffeinate", "-i", "-s"]


def _works(_prefix: list[str]) -> bool:
    return True


def _broken(_prefix: list[str]) -> bool:
    return False


class _RecordingProbe:
    """A probe that remembers which inhibitor prefixes it was asked about."""

    def __init__(self, *, works: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.works = works

    def __call__(self, prefix: list[str]) -> bool:
        self.calls.append(prefix)
        return self.works


def _which(*present: str):
    """Patch shutil.which so only `present` binaries resolve."""
    return mock.patch.object(cli.shutil, "which",
                             side_effect=lambda name: f"/usr/bin/{name}" if name in present else None)


class SuspendInhibitorArgvTest(unittest.TestCase):
    def test_wraps_with_systemd_inhibit_on_linux(self):
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=_works)
        self.assertEqual(got, [*SYSTEMD, *BASE_ARGV])

    def test_wraps_with_caffeinate_when_only_caffeinate_present(self):
        with _which("caffeinate"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=_works)
        # -i (idle assertion, valid on battery) must be present; -s alone no-ops on battery.
        self.assertEqual(got, [*CAFFEINATE, *BASE_ARGV])

    def test_module_invocation_reexecs_via_interpreter(self):
        # `python -m pdca_harness.cli flow …` → argv[0] is the cli.py file path, which the
        # inhibitor can't exec directly; rebuild via the interpreter + -m so it re-invokes.
        argv = ["/opt/venv/lib/pdca_harness/cli.py", "flow", "123"]
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(argv, {}, probe=_works)
        self.assertEqual(
            got, [*SYSTEMD, cli.sys.executable, "-m", "pdca_harness.cli", "flow", "123"])

    def test_console_script_argv_forwarded_as_is(self):
        # An installed console script (argv[0] has no .py) is directly execable → forward it.
        argv = ["/opt/venv/bin/pdca-gramps", "flow", "123"]
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(argv, {}, probe=_works)
        self.assertEqual(got, [*SYSTEMD, *argv])

    def test_none_when_already_inhibited(self):
        # Guard against double-wrap: the child re-exec sets PDCA_FLOW_INHIBITED=1.
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {"PDCA_FLOW_INHIBITED": "1"}, probe=_works)
        self.assertIsNone(got)

    def test_none_when_opted_out_via_env(self):
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {"PDCA_NO_INHIBIT": "1"}, probe=_works)
        self.assertIsNone(got)

    def test_none_when_opted_out_via_flag(self):
        argv = [*BASE_ARGV, "--no-inhibit"]
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(argv, {}, probe=_works)
        self.assertIsNone(got)

    def test_none_when_no_inhibitor_available(self):
        with _which():
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=_works)
        self.assertIsNone(got)

    def test_prefers_systemd_inhibit_over_caffeinate(self):
        # Both present (unusual, but deterministic): Linux inhibitor wins.
        with _which("systemd-inhibit", "caffeinate"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=_works)
        self.assertEqual(got[0], "systemd-inhibit")


class BrokenInhibitorTest(unittest.TestCase):
    """#259: a present-but-broken inhibitor (installed binary, no systemd bus) must not be
    re-exec'd under — it exits 1 without ever running the wrapped command, killing the run."""

    def test_present_but_broken_runs_unwrapped(self):
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=_broken)
        self.assertIsNone(got, "a broken inhibitor must fail OPEN (un-inhibited run), not closed")

    def test_falls_through_to_caffeinate_when_systemd_inhibit_is_broken(self):
        def probe(prefix):
            return prefix[0] != "systemd-inhibit"

        with _which("systemd-inhibit", "caffeinate"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=probe)
        self.assertEqual(got, [*CAFFEINATE, *BASE_ARGV])

    def test_probe_is_skipped_when_opted_out(self):
        # The opt-out short-circuit comes first, so the common CI path never pays for a probe.
        probe = _RecordingProbe()
        with _which("systemd-inhibit"):
            cli._suspend_inhibitor_argv(BASE_ARGV, {"PDCA_NO_INHIBIT": "1"}, probe=probe)
        self.assertEqual(probe.calls, [])

    def test_probe_is_skipped_when_already_inhibited(self):
        probe = _RecordingProbe()
        with _which("systemd-inhibit"):
            cli._suspend_inhibitor_argv(BASE_ARGV, {"PDCA_FLOW_INHIBITED": "1"}, probe=probe)
        self.assertEqual(probe.calls, [])

    def test_probe_is_skipped_when_binary_absent(self):
        # `which` gates the probe, so an absent binary is never shelled out to.
        probe = _RecordingProbe()
        with _which():
            cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=probe)
        self.assertEqual(probe.calls, [])

    def test_probe_asks_about_the_exact_prefix_it_would_exec(self):
        probe = _RecordingProbe(works=True)
        with _which("systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {}, probe=probe)
        self.assertEqual(probe.calls, [SYSTEMD])
        self.assertEqual(got[:len(SYSTEMD)], SYSTEMD)


class InhibitorWorksProbeTest(unittest.TestCase):
    """The real probe: wrap `true` and look at the exit code."""

    def test_exit_zero_means_working(self):
        with mock.patch.object(cli.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(cli._inhibitor_works(SYSTEMD))
        self.assertEqual(run.call_args.args[0], [*SYSTEMD, "true"])

    def test_nonzero_exit_means_broken(self):
        # `systemd-inhibit … true` with no bus: "Failed to connect to … bus", rc 1, and the
        # wrapped command never runs.
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(cli._inhibitor_works(SYSTEMD))

    def test_timeout_means_broken(self):
        with mock.patch.object(cli.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(SYSTEMD, 10)):
            self.assertFalse(cli._inhibitor_works(SYSTEMD))

    def test_oserror_means_broken(self):
        with mock.patch.object(cli.subprocess, "run", side_effect=OSError("not executable")):
            self.assertFalse(cli._inhibitor_works(SYSTEMD))


if __name__ == "__main__":
    unittest.main()
