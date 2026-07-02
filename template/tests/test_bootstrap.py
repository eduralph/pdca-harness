"""Install bootstrap (issue #207) — scripts/bootstrap-tools.sh --check + the [install] config.

Deterministic subset: a REQUIRED leaf backend that isn't installed makes `--check` exit
non-zero and flag that binary; a stubs-only render never references a backend it doesn't
configure; the three tiers are reported. Plus the [install].extra_bootstrap config parse.
Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pdca_harness.config import Config

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-tools.sh"

# A minimal pdca.toml whose builder leaf uses a backend guaranteed absent from any host
# (so the REQUIRED-leaf path is deterministic — the real `claude`/`codex` may be installed).
_TOML_MISSING = (
    '[project]\ndefault_branch = "main"\n'
    '[leaves.builder]\nmode = "command"\nfamily = "acme-llm"\n'
    'argv = ["acme-llm", "-p"]\n'
)
_TOML_STUBS = (
    '[project]\ndefault_branch = "main"\n'
    '[leaves.builder]\nmode = "stub"\n[leaves.reviewer]\nmode = "stub"\n'
)


def _run_check(toml: str) -> subprocess.CompletedProcess:
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "pdca.toml").write_text(toml, encoding="utf-8")
        (tmp / "scripts").mkdir()
        shutil.copy2(SCRIPT, tmp / "scripts" / "bootstrap-tools.sh")
        return subprocess.run(
            ["bash", str(tmp / "scripts" / "bootstrap-tools.sh"), "--check"],
            cwd=tmp, capture_output=True, text=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class BootstrapCheck(unittest.TestCase):
    def test_required_leaf_missing_exits_nonzero(self) -> None:
        # The builder family's binary is absent → a REQUIRED miss → non-zero exit.
        r = _run_check(_TOML_MISSING)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("acme-llm", r.stdout)
        self.assertIn("MISSING", r.stdout)
        self.assertIn("REQUIRED tools missing", r.stdout)

    def test_stubs_only_render_references_no_backend(self) -> None:
        # "Only the configured leaf backends are installed" — a claude-only concept must not
        # leak into a stubs-only render (regardless of git/gh/venv presence in the env).
        r = _run_check(_TOML_STUBS)
        self.assertNotIn("claude", r.stdout)
        self.assertNotIn("codex", r.stdout)
        self.assertIn("all leaves are stubs", r.stdout)

    def test_reports_the_three_tiers(self) -> None:
        r = _run_check(_TOML_STUBS)
        for tier in ("tier 1", "tier 2", "tier 3"):
            self.assertIn(tier, r.stdout)


class InstallConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, extra: str) -> Config:
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\n[leaves.reviewer]\nmode = "stub"\n' + extra,
            encoding="utf-8")
        saved = os.environ.pop("PDCA_LEAVES_MODE", None)
        try:
            return Config.load(self.tmp)
        finally:
            if saved is not None:
                os.environ["PDCA_LEAVES_MODE"] = saved

    def test_extra_bootstrap_parsed(self) -> None:
        cfg = self._load('[install]\nextra_bootstrap = "rustup show"\n')
        self.assertEqual(cfg.install_extra_bootstrap, "rustup show")

    def test_extra_bootstrap_defaults_empty(self) -> None:
        self.assertEqual(self._load("").install_extra_bootstrap, "")


if __name__ == "__main__":
    unittest.main()
