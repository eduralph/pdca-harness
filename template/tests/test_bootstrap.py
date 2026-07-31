"""Install bootstrap (issue #207) — scripts/bootstrap-tools.sh --check + the [install] config.

Deterministic subset: a REQUIRED leaf backend that isn't installed makes `--check` exit
non-zero and flag that binary; a stubs-only render never references a backend it doesn't
configure; the three tiers are reported. Plus the [install].extra_bootstrap config parse
and the ~/.local/bin PATH-link step (issue #376).
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
# The default `leaves_mode = "stub"` render: stub leaves that STILL carry a `family` (and a
# commented example). A grep would wrongly demand those CLIs; honouring `mode` must not.
_TOML_STUBS = (
    '[project]\ndefault_branch = "main"\n'
    '[leaves.builder]\nmode = "stub"\nfamily = "claude"\nargv = ["claude", "-p"]\n'
    '[leaves.reviewer]\nmode = "stub"\nfamily = "codex"\n'
    '# [[leaves.advisory]]\n# family = "gemini"\n'
)


def _run_check(toml: str, hook: str | None = None) -> subprocess.CompletedProcess:
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "pdca.toml").write_text(toml, encoding="utf-8")
        (tmp / "scripts").mkdir()
        shutil.copy2(SCRIPT, tmp / "scripts" / "bootstrap-tools.sh")
        if hook is not None:
            hd = tmp / "scripts" / "bootstrap-tools.d"
            hd.mkdir()
            (hd / "10-project.sh").write_text(hook, encoding="utf-8")
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

    def test_stub_leaves_with_family_need_no_backend(self) -> None:
        # A stub render carries `family = "claude"`/`codex` (and a commented `gemini`), but
        # `mode = "stub"` means no command leaf runs — honouring `mode` must not demand those
        # CLIs (issue #207 review). Parsing TOML (not grep) makes this hold.
        r = _run_check(_TOML_STUBS)
        self.assertNotIn("claude", r.stdout)
        self.assertNotIn("codex", r.stdout)
        self.assertNotIn("gemini", r.stdout)
        self.assertIn("all leaves are stubs", r.stdout)

    def test_reports_the_three_tiers(self) -> None:
        r = _run_check(_TOML_STUBS)
        for tier in ("tier 1", "tier 2", "tier 3"):
            self.assertIn(tier, r.stdout)

    def test_check_runs_project_hooks_and_a_failing_one_fails(self) -> None:
        # A tier-3 drop-in hook that reports a missing required tool must fail install-check,
        # not be suppressed (issue #207 review). The hook runs in --check mode (CHECK_ONLY=1).
        r = _run_check(_TOML_STUBS, hook='echo "checked: CHECK_ONLY=$CHECK_ONLY"; exit 1\n')
        self.assertIn("checked: CHECK_ONLY=1", r.stdout)  # hook actually ran under --check
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REQUIRED tools missing", r.stdout)

    def test_check_passing_hook_is_reported(self) -> None:
        r = _run_check(_TOML_STUBS, hook='echo ok; exit 0\n')
        self.assertIn("10-project.sh", r.stdout)


# A synthetic pyproject whose [project.scripts] carries one console script — the
# authoritative source the PATH-link step parses (issue #376). The name is unique so
# stdout assertions are unambiguous.
_PYPROJECT = (
    '[project]\nname = "acme-pdca-harness"\nversion = "0.1.0"\n'
    '[project.scripts]\nacmecli = "pdca_harness.cli:main"\n'
)


class PathLink(unittest.TestCase):
    """The ~/.local/bin PATH-link step (issue #376): `make install` exposes the console
    script on PATH via an idempotent symlink; `--check` reports without creating;
    a foreign ~/.local/bin/<cli> is WARNed about, never clobbered.

    Sandbox: temp root + synthetic pdca.toml/pyproject.toml, a pre-seeded fake .venv
    (skips venv creation — bootstrap-tools.sh checks `[ ! -d .venv ]`), and HOME/PATH
    injected via the subprocess env. git/gh/sudo are shadowed by stubs so install mode
    never prompts (sudo) or touches the network (gh auth). Assertions are on stdout
    rows and the filesystem, never the exit code (a host may lack unrelated tools).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "proj"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "scripts" / "bootstrap-tools.sh")
        (self.root / "pdca.toml").write_text(_TOML_STUBS, encoding="utf-8")
        (self.root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        venv_bin = self.root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        self._stub(venv_bin / "pip")      # `pip install -q -e .` becomes a no-op
        self._stub(venv_bin / "acmecli")  # the console script the link targets
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub_bin = self.tmp / "stub-bin"
        self.stub_bin.mkdir()
        for tool in ("git", "gh"):
            self._stub(self.stub_bin / tool)
        self._stub(self.stub_bin / "sudo", "exit 1\n")
        self.local_bin = self.home / ".local" / "bin"
        self.link = self.local_bin / "acmecli"
        self.venv_cli = venv_bin / "acmecli"

    def _stub(self, path: Path, body: str = "exit 0\n") -> None:
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, *args: str, local_bin_on_path: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        path = f"{self.stub_bin}:{env.get('PATH', '/usr/bin:/bin')}"
        if local_bin_on_path:
            path = f"{self.local_bin}:{path}"
        env["PATH"] = path
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "bootstrap-tools.sh"), *args],
            cwd=self.root, capture_output=True, text=True, env=env,
            stdin=subprocess.DEVNULL)

    def _row(self, r: subprocess.CompletedProcess) -> str:
        rows = [ln for ln in r.stdout.splitlines() if "(PATH link)" in ln]
        self.assertEqual(len(rows), 1, r.stdout + r.stderr)
        return rows[0]

    def test_install_creates_the_symlink(self) -> None:
        # (a) ~/.local/bin exists and is on PATH → install creates the symlink + row.
        self.local_bin.mkdir(parents=True)
        r = self._run()
        row = self._row(r)
        self.assertIn("INSTALLED", row)
        self.assertIn("acmecli", row)
        self.assertTrue(self.link.is_symlink(), r.stdout)
        self.assertEqual(os.readlink(self.link), str(self.venv_cli))

    def test_check_reports_and_creates_nothing(self) -> None:
        # (b) --check in the same setup reports the row but installs nothing.
        self.local_bin.mkdir(parents=True)
        r = self._run("--check")
        row = self._row(r)
        self.assertIn("MISSING", row)
        self.assertIn(f'ln -s "{self.venv_cli}" "{self.link}"', row)
        self.assertFalse(self.link.is_symlink())
        self.assertFalse(self.link.exists())

    def test_no_local_bin_warns_with_exact_command(self) -> None:
        # (c) HOME without .local/bin → WARN with the literal ln -s command, no mkdir
        # and no symlink behind the operator's back (never mutate shell profiles).
        r = self._run()
        row = self._row(r)
        self.assertIn("WARN", row)
        self.assertIn(f'ln -s "{self.venv_cli}" "{self.link}"', row)
        self.assertFalse(self.local_bin.exists())

    def test_local_bin_off_path_warns_and_creates_nothing(self) -> None:
        # (c) variant: .local/bin exists but is NOT on the injected PATH → same WARN.
        self.local_bin.mkdir(parents=True)
        r = self._run(local_bin_on_path=False)
        row = self._row(r)
        self.assertIn("WARN", row)
        self.assertIn(f'ln -s "{self.venv_cli}" "{self.link}"', row)
        self.assertFalse(self.link.is_symlink())

    def test_rerun_is_idempotent(self) -> None:
        # (d) a second install run reports OK and touches nothing (same inode/mtime).
        self.local_bin.mkdir(parents=True)
        self._run()
        before = os.lstat(self.link)
        r = self._run()
        self.assertIn("OK", self._row(r))
        after = os.lstat(self.link)
        self.assertEqual(os.readlink(self.link), str(self.venv_cli))
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_foreign_link_never_clobbered(self) -> None:
        # (e) an existing ~/.local/bin/<cli> pointing somewhere OTHER than this venv
        # (another instance's default-name CLI) is left untouched and WARNed about.
        self.local_bin.mkdir(parents=True)
        foreign = self.tmp / "elsewhere" / "acmecli"
        foreign.parent.mkdir()
        self._stub(foreign)
        self.link.symlink_to(foreign)
        r = self._run()
        row = self._row(r)
        self.assertIn("WARN", row)
        self.assertIn(str(foreign), row)         # names the existing target
        self.assertIn(str(self.venv_cli), row)   # and this venv's path
        self.assertEqual(os.readlink(self.link), str(foreign))


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
