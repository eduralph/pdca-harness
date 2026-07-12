"""`pdca doctor` (stdlib unittest, offline): config-derived checks, the
[[doctor.checks]] table, and the exit-code contract (0 iff required OK;
--strict escalates every non-OK row)."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import doctor
from pdca_harness.config import Config


def _load(tmp: Path, extra: str = "") -> Config:
    (tmp / "pdca.toml").write_text(
        '[project]\ndefault_branch = "main"\n'
        '[leaves.builder]\nmode = "stub"\n'
        '[leaves.reviewer]\nmode = "stub"\n' + extra,
        encoding="utf-8",
    )
    # The suite may run under PDCA_LEAVES_MODE=stub, which would force every leaf
    # to stub at load time — the doctor must see the config as WRITTEN here.
    saved = os.environ.pop("PDCA_LEAVES_MODE", None)
    try:
        return Config.load(tmp)
    finally:
        if saved is not None:
            os.environ["PDCA_LEAVES_MODE"] = saved


class Doctor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, cfg: Config, **kw) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = doctor.run(cfg, **kw)
        return rc, out.getvalue()

    def test_all_stub_leaves_is_ok_and_exit_zero(self) -> None:
        rc, out = self._run(_load(self.tmp))
        self.assertEqual(rc, 0)
        self.assertIn("all leaves are stubs", out)

    def test_missing_leaf_cli_is_reported_not_fatal(self) -> None:
        cfg = _load(self.tmp,
                    '[leaves.planner]\nmode = "command"\n'
                    'argv = ["no-such-vendor-cli-xyz"]\n')
        rc, out = self._run(cfg)
        self.assertEqual(rc, 0)  # a missing model CLI is MISSING, not required-fatal
        self.assertIn("MISSING", out)
        self.assertIn("no-such-vendor-cli-xyz", out)
        self.assertEqual(self._run(cfg, strict=True)[0], 1)  # --strict escalates

    def test_project_checks_run_and_required_fails(self) -> None:
        cfg = _load(self.tmp,
                    '[[doctor.checks]]\nid = "always-ok"\ncmd = "true"\n'
                    '[[doctor.checks]]\nid = "broken"\ncmd = "false"\n'
                    'hint = "fix me"\nrequired = true\n')
        rc, out = self._run(cfg)
        self.assertEqual(rc, 1)  # the required row failed
        self.assertIn("always-ok", out)
        self.assertIn("fix me", out)

    def test_optional_level_and_group_header(self) -> None:
        cfg = _load(self.tmp,
                    '[[doctor.checks]]\ngroup = "engine"\nid = "opt"\n'
                    'cmd = "false"\nhint = "later"\nlevel = "WARN"\n')
        rc, out = self._run(cfg)
        self.assertEqual(rc, 0)  # WARN, not required → exit 0
        self.assertIn("== engine ==", out)
        self.assertIn("WARN", out)
        self.assertEqual(self._run(cfg, strict=True)[0], 1)  # --strict escalates the WARN

    def test_per_lane_expands_over_driver_lanes(self) -> None:
        cfg = _load(self.tmp,
                    '[driver]\nlanes = 3\n'
                    '[[doctor.checks]]\nid = "lane{lane}"\n'
                    'cmd = "test {lane} -lt 2"\n'  # lane 0,1 pass; lane 2 fails
                    'hint = "make worktrees LANES={lanes}"\nper_lane = true\nlevel = "WARN"\n')
        rc, out = self._run(cfg)
        self.assertEqual(rc, 0)  # WARN level
        for lane in ("lane0", "lane1", "lane2"):
            self.assertIn(lane, out)              # one row per lane
        self.assertIn("make worktrees LANES=3", out)  # {lanes} substituted

    def test_inherited_command_variant_binary_is_checked(self) -> None:
        # A [[leaves.builder_variant]] that omits `mode` inherits the command
        # builder's mode but sets its OWN argv (a different binary). The doctor must
        # check that binary — else --strict passes and the routed Do attempt dies.
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "command"\nfamily = "claude"\nargv = ["claude", "-p"]\n'
            '[leaves.reviewer]\nmode = "stub"\n'
            '[[leaves.builder_variant]]\nmodel = "frontier"\n'
            'argv = ["no-such-variant-cli-xyz"]\n'
            'when = { field = "difficulty", substring = "high" }\n',
            encoding="utf-8")
        saved = os.environ.pop("PDCA_LEAVES_MODE", None)
        try:
            cfg = Config.load(self.tmp)
        finally:
            if saved is not None:
                os.environ["PDCA_LEAVES_MODE"] = saved
        _, out = self._run(cfg)
        self.assertIn("no-such-variant-cli-xyz", out)  # inherited-command variant checked

    def test_per_lane_yields_nothing_when_serial(self) -> None:
        cfg = _load(self.tmp,
                    '[driver]\nlanes = 1\n'
                    '[[doctor.checks]]\nid = "lane{lane}"\ncmd = "false"\nper_lane = true\n')
        _, out = self._run(cfg)
        self.assertNotIn("lane0", out)  # serial mode uses base worktrees, not lanes


class SandboxDeps(unittest.TestCase):
    """Issue #289. Claude Code's sandbox does NOT fail closed: with `sandbox.enabled` true and
    a dependency missing it DISABLES the sandbox, warns, and runs every command unconfined
    ("dependencies are missing: socat not installed · Commands will run WITHOUT sandboxing").

    A leaf would then run *everything* outside a sandbox that pdca.toml and docs 05 both say
    bounds it to the named commands. The harness also *mandates* (brief.md.tpl) that every
    human-installable dependency have a detecting doctor row — and shipped one of its own with
    none. These rows are REQUIRED because a miss is not a degraded feature, it is a false
    security claim.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, cfg: Config, **kw) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = doctor.run(cfg, **kw)
        return rc, out.getvalue()

    _EXEMPTION = ('[leaves.sandbox]\nunsandboxed_commands = ["cargo xtask fdb-conformance"]\n')

    def _settings(self, payload: str) -> None:
        cdir = self.tmp / ".claude"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "settings.json").write_text(payload, encoding="utf-8")

    @staticmethod
    def _deps(present: bool):
        """Control ONLY the sandbox probes; every other tool answers truthfully.

        A blanket `_have -> True` would tell the doctor that `gh` exists on a host without it,
        and the `gh` row then shells out to `gh auth status` → FileNotFoundError, so the test
        died before reaching its own assertions (PR #290 review — hit in a clean container).
        Patch what the test is about, and nothing else.
        """
        real = doctor._have
        return mock.patch.object(
            doctor, "_have",
            side_effect=lambda cmd: present if cmd in doctor._SANDBOX_DEPS else real(cmd))

    def test_an_exemption_makes_the_sandbox_deps_required(self) -> None:
        cfg = _load(self.tmp, self._EXEMPTION)
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertIn("leaf sandbox", out)
        self.assertIn("socat", out)
        self.assertIn("bwrap", out)
        self.assertEqual(rc, 1, "a sandbox that cannot start is a REQUIRED failure")

    def test_enabling_the_sandbox_alone_is_enough_to_check(self) -> None:
        # No exemption — but the project turned the sandbox on, so it believes it is confined.
        self._settings('{"sandbox": {"enabled": true}}')
        cfg = _load(self.tmp)
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertIn("socat", out)
        self.assertEqual(rc, 1)

    def test_present_deps_pass(self) -> None:
        cfg = _load(self.tmp, self._EXEMPTION)
        with self._deps(True):
            rc, out = self._run(cfg)
        self.assertIn("leaf sandbox", out)
        self.assertEqual(rc, 0)

    def test_an_instance_with_no_sandbox_is_not_nagged(self) -> None:
        # The rows ride WITH the belief in a sandbox. An instance that never asked for one is
        # not told to install bubblewrap.
        cfg = _load(self.tmp)
        with self._deps(False):
            _, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)
        self.assertNotIn("socat", out)

    def test_a_disabled_sandbox_setting_is_not_a_belief(self) -> None:
        self._settings('{"sandbox": {"enabled": false}}')
        cfg = _load(self.tmp)
        with self._deps(False):
            _, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)

    def test_unreadable_settings_never_crash_the_doctor(self) -> None:
        self._settings("{ not json")
        cfg = _load(self.tmp)
        with self._deps(False):
            _, out = self._run(cfg)          # must not raise
        self.assertNotIn("leaf sandbox", out)


if __name__ == "__main__":
    unittest.main()
