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


if __name__ == "__main__":
    unittest.main()
