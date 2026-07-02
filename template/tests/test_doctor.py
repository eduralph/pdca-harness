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


if __name__ == "__main__":
    unittest.main()
