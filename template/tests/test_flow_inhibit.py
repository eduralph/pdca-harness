"""Unit tests for the `pdca flow` suspend-inhibitor decision (#244).

`_suspend_inhibitor_argv` is the pure decision behind the re-exec: given argv + env it
returns the keep-awake wrapper command, or None to run unwrapped. Tests exercise the
decision without actually re-exec'ing the process.
"""

from __future__ import annotations

import unittest
from unittest import mock

from pdca_harness import cli


BASE_ARGV = ["pdca", "flow", "123"]


class SuspendInhibitorArgvTest(unittest.TestCase):
    def test_wraps_with_systemd_inhibit_on_linux(self):
        with mock.patch.object(cli.shutil, "which",
                               side_effect=lambda name: "/usr/bin/systemd-inhibit"
                               if name == "systemd-inhibit" else None):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {})
        self.assertEqual(
            got,
            ["systemd-inhibit", "--what=idle:sleep", "--why=pdca flow", *BASE_ARGV],
        )

    def test_wraps_with_caffeinate_when_only_caffeinate_present(self):
        with mock.patch.object(cli.shutil, "which",
                               side_effect=lambda name: "/usr/bin/caffeinate"
                               if name == "caffeinate" else None):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {})
        # -i (idle assertion, valid on battery) must be present; -s alone no-ops on battery.
        self.assertEqual(got, ["caffeinate", "-i", "-s", *BASE_ARGV])

    def test_module_invocation_reexecs_via_interpreter(self):
        # `python -m pdca_harness.cli flow …` → argv[0] is the cli.py file path, which the
        # inhibitor can't exec directly; rebuild via the interpreter + -m so it re-invokes.
        argv = ["/opt/venv/lib/pdca_harness/cli.py", "flow", "123"]
        with mock.patch.object(cli.shutil, "which",
                               side_effect=lambda name: "/usr/bin/systemd-inhibit"
                               if name == "systemd-inhibit" else None):
            got = cli._suspend_inhibitor_argv(argv, {})
        self.assertEqual(
            got,
            ["systemd-inhibit", "--what=idle:sleep", "--why=pdca flow",
             cli.sys.executable, "-m", "pdca_harness.cli", "flow", "123"],
        )

    def test_console_script_argv_forwarded_as_is(self):
        # An installed console script (argv[0] has no .py) is directly execable → forward it.
        argv = ["/opt/venv/bin/pdca-gramps", "flow", "123"]
        with mock.patch.object(cli.shutil, "which",
                               side_effect=lambda name: "/usr/bin/systemd-inhibit"
                               if name == "systemd-inhibit" else None):
            got = cli._suspend_inhibitor_argv(argv, {})
        self.assertEqual(got, ["systemd-inhibit", "--what=idle:sleep", "--why=pdca flow", *argv])

    def test_none_when_already_inhibited(self):
        # Guard against double-wrap: the child re-exec sets PDCA_FLOW_INHIBITED=1.
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {"PDCA_FLOW_INHIBITED": "1"})
        self.assertIsNone(got)

    def test_none_when_opted_out_via_env(self):
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {"PDCA_NO_INHIBIT": "1"})
        self.assertIsNone(got)

    def test_none_when_opted_out_via_flag(self):
        argv = [*BASE_ARGV, "--no-inhibit"]
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/systemd-inhibit"):
            got = cli._suspend_inhibitor_argv(argv, {})
        self.assertIsNone(got)

    def test_none_when_no_inhibitor_available(self):
        with mock.patch.object(cli.shutil, "which", return_value=None):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {})
        self.assertIsNone(got)

    def test_prefers_systemd_inhibit_over_caffeinate(self):
        # Both present (unusual, but deterministic): Linux inhibitor wins.
        with mock.patch.object(cli.shutil, "which",
                               side_effect=lambda name: f"/usr/bin/{name}"):
            got = cli._suspend_inhibitor_argv(BASE_ARGV, {})
        self.assertEqual(got[0], "systemd-inhibit")


if __name__ == "__main__":
    unittest.main()
