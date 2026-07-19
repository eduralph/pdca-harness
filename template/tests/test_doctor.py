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

    def test_low_free_space_warns_and_strict_escalates(self) -> None:
        # #297: a 1 PiB threshold is always unmet, so the workspace row WARNs (preflight,
        # not required-fatal) and points at `pdca sweep`; --strict escalates as usual.
        cfg = _load(self.tmp, "[doctor]\nmin_free_gb = 1048576.0\n")
        rc, out = self._run(cfg)
        self.assertEqual(rc, 0)
        self.assertIn("== workspace ==", out)
        self.assertIn("free disk space", out)
        self.assertIn("pdca sweep", out)
        self.assertEqual(self._run(cfg, strict=True)[0], 1)

    def test_free_space_row_disabled_at_zero(self) -> None:
        cfg = _load(self.tmp, "[doctor]\nmin_free_gb = 0\n")
        _rc, out = self._run(cfg)
        self.assertNotIn("free disk space", out)

    def test_free_space_measures_each_target_filesystem(self) -> None:
        # #297 review round 7: a target checkout can sit on ANOTHER filesystem than
        # the harness root — lane worktrees and gate build output fill THAT fs, so
        # the preflight must measure it too, not only cfg.root.
        from types import SimpleNamespace
        cfg = _load(self.tmp, "[doctor]\nmin_free_gb = 10.0\n")
        other = self.tmp / "elsewhere-checkout"
        other.mkdir()
        big = SimpleNamespace(free=100 * 1024 ** 3, total=1, used=1)
        small = SimpleNamespace(free=1 * 1024 ** 3, total=1, used=1)
        with mock.patch.object(doctor, "_space_roots",
                               return_value=[cfg.root, other]), \
                mock.patch.object(doctor.shutil, "disk_usage",
                                  side_effect=lambda p: small if Path(p) == other
                                  else big):
            rc, out = self._run(cfg)
        self.assertEqual(rc, 0)                            # WARN, not fatal
        self.assertIn("free disk space (elsewhere-checkout)", out)
        self.assertIn("pdca sweep", out)

    def test_quota_headroom_bounds_the_free_space_row(self) -> None:
        # #297 review round 12: EDQUOT was the motivating incident — a shared volume
        # with hundreds of fs-level GiB free while THIS user's quota is nearly
        # exhausted. The row must report (and WARN on) the tighter bound.
        from types import SimpleNamespace
        cfg = _load(self.tmp, "[doctor]\nmin_free_gb = 10.0\n")
        big = SimpleNamespace(free=500 * 1024 ** 3, total=1, used=1)
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=big), \
                mock.patch.object(doctor, "_quota_free_gb", return_value=1.5):
            rc, out = self._run(cfg)
        self.assertEqual(rc, 0)                            # WARN, not fatal
        self.assertIn("1.5 GiB user-quota headroom", out)
        self.assertIn("pdca sweep", out)

    def test_quota_probe_parses_the_matching_mountpoint(self) -> None:
        from types import SimpleNamespace
        text = (
            "Disk quotas for user eddie (uid 1000):\n"
            "     Filesystem  blocks   quota   limit   grace   files   quota   limit"
            "   grace\n"
            "      /          1000     0       0       0       1       0       0"
            "       0\n"
            "      /home      968000*  1000000 1048576 6days   12      0       0"
            "       0\n")
        fake = SimpleNamespace(returncode=1, stdout=text, stderr="")  # over-quota rc
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/quota"):
            gb = doctor._quota_free_gb(Path("/home/eddie/project"),
                                       runner=lambda *a, **k: fake)
            self.assertIsNotNone(gb)
            self.assertAlmostEqual(gb, (1048576 - 968000) / 1024 ** 2, places=4)
            # A path outside every quota'd mountpoint (the / row has limit 0 — no
            # quota) → None: the fs-level number stands.
            self.assertIsNone(doctor._quota_free_gb(Path("/srv/elsewhere"),
                                                    runner=lambda *a, **k: fake))

    def test_space_roots_measure_the_checkout_parent_filesystem(self) -> None:
        # #297 review round 9: lane/integ/overflow siblings are created under
        # checkout.PARENT — when the checkout is itself a mount point, statting the
        # checkout measures the mounted fs while the siblings fill the parent's.
        cfg = _load(self.tmp, "[doctor]\nmin_free_gb = 10.0\n")
        mount = self.tmp / "mnt" / "checkout"
        (mount / ".git").mkdir(parents=True)
        cfg.repo_checkouts = {"org/x": str(mount)}
        devs = {cfg.root: 1, mount.parent: 2, mount: 3}  # the mount differs from both
        roots = doctor._space_roots(cfg, dev=lambda p: devs[p])
        self.assertEqual(roots, [cfg.root, mount.parent])  # the parent, never the mount

    def test_space_roots_dedupe_by_filesystem(self) -> None:
        # Same-device targets collapse to one measurement (statvfs per FILESYSTEM,
        # not per checkout); the root always leads.
        cfg = _load(self.tmp, "[doctor]\nmin_free_gb = 10.0\n")
        checkout = self.tmp / "checkout"
        (checkout / ".git").mkdir(parents=True)            # same fs as cfg.root
        cfg.repo_checkouts = {"org/x": str(checkout)}
        self.assertEqual(doctor._space_roots(cfg), [cfg.root])

    @staticmethod
    def _dead_pid() -> int:
        """A pid that provably no longer exists (a reaped child) — the orphan case."""
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def test_orphaned_overflow_trees_warn(self) -> None:
        # #297 (+review): only an overflow dir whose creator pid is provably gone is an
        # orphan — WARN with the reclaim hint; a live-pid tree may be another process's
        # in-flight gate read and must not be counted (or reclaimed).
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (self.tmp / f"repo.pdca-wt-ovf-{self._dead_pid()}-0").mkdir()
        (self.tmp / f"repo.pdca-wt-ovf-{os.getpid()}-0").mkdir()   # live: this process
        cfg = _load(self.tmp,
                    "[doctor]\nmin_free_gb = 0\n"
                    f'[publisher.checkouts]\n"org/repo" = "{repo}"\n')
        _rc, out = self._run(cfg)
        self.assertIn("harness worktree footprint", out)
        self.assertIn("1 orphaned overflow tree(s)", out)
        self.assertIn("pdca sweep", out)

    def test_footprint_counts_cover_sibling_convention_bundles(self) -> None:
        # #297 review: with no [publisher.checkouts] entries, targets must still be
        # derived from persisted bundles (the sibling convention, <root>/../checkout) —
        # the counts were permanently "0 lane / 0 integration" in those default setups.
        proj = self.tmp / "proj"
        proj.mkdir()
        repo = self.tmp / "checkout"
        (repo / ".git").mkdir(parents=True)
        (self.tmp / "checkout.pdca-wt").mkdir()
        d = proj / "results" / "issue_7"
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/checkout @ main\n",
            encoding="utf-8")
        cfg = _load(proj, "[doctor]\nmin_free_gb = 0\n")
        _rc, out = self._run(cfg)
        self.assertIn("1 lane / 0 integration worktree(s)", out)

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

    def _cfg(self, reviewer: str = 'mode = "stub"\n', extra: str = "") -> Config:
        """A config whose REVIEWER we control — it is the sandboxed leaf that decides whether a
        bounded sandbox is ever seeded, so these tests must be able to set its family/mode."""
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\n'
            '[leaves.reviewer]\n' + reviewer + extra, encoding="utf-8")
        saved = os.environ.pop("PDCA_LEAVES_MODE", None)
        try:
            return Config.load(self.tmp)
        finally:
            if saved is not None:
                os.environ["PDCA_LEAVES_MODE"] = saved

    # A CLAUDE reviewer in COMMAND mode — the only shape that actually receives a seeded,
    # bounded sandbox, and therefore the only shape whose deps the doctor may demand.
    _CLAUDE_REVIEWER = 'mode = "command"\nfamily = "claude"\nargv = ["claude", "-p"]\n'
    _CODEX_REVIEWER = 'mode = "command"\nfamily = "codex"\nargv = ["codex", "exec"]\n'

    def test_an_exemption_on_a_bounded_leaf_makes_the_deps_required(self) -> None:
        cfg = self._cfg(self._CLAUDE_REVIEWER, self._EXEMPTION)
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertIn("leaf sandbox", out)
        self.assertIn("socat", out)
        self.assertIn("bwrap", out)
        self.assertEqual(rc, 1, "a sandbox that cannot start is a REQUIRED failure")

    def test_the_install_hint_names_the_PACKAGE_not_the_binary(self) -> None:
        """PR #290 review (codex). The binary is `bwrap`; the package that provides it is
        `bubblewrap` (`dpkg -S /usr/bin/bwrap` → bubblewrap; `apt-cache show bwrap` finds
        nothing). The hint said `sudo apt install bwrap`, which simply FAILS — and on a REQUIRED
        row that is not cosmetic: an operator who follows the hint stays blocked forever."""
        cfg = self._cfg(self._CLAUDE_REVIEWER, self._EXEMPTION)
        with self._deps(False):
            _, out = self._run(cfg)
        self.assertIn("sudo apt install bubblewrap", out)
        self.assertNotIn("sudo apt install bwrap", out)
        self.assertIn("bwrap (", out)                    # …the ROW still names the binary probed
        self.assertIn("sudo apt install socat", out)     # socat: binary and package coincide

    def test_present_deps_pass(self) -> None:
        cfg = self._cfg(self._CLAUDE_REVIEWER, self._EXEMPTION)
        with self._deps(True):
            rc, out = self._run(cfg)
        self.assertIn("leaf sandbox", out)
        self.assertEqual(rc, 0)

    def test_project_sandbox_enabled_alone_says_nothing_about_a_leaf(self) -> None:
        """PR #290 review (codex). The old predicate read the PROJECT's `.claude/settings.json`
        `sandbox.enabled` — which predicts NOTHING about a leaf. The reviewer/advisory leaves run
        from a **temp cwd**, so they never load the project's settings at all; only the file the
        harness seeds there. That row told the operator to install bwrap/socat as REQUIRED, for a
        leaf sandbox that was never going to exist."""
        self._settings('{"sandbox": {"enabled": true}}')
        cfg = self._cfg(self._CLAUDE_REVIEWER)           # …but NO exemption
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)
        self.assertEqual(rc, 0)

    def test_a_family_that_cannot_be_bounded_needs_none_of_this(self) -> None:
        """PR #290 review (codex). A codex reviewer's exemption is REFUSED by
        `_seed_sandbox_settings` (it cannot be confined to the harness's settings) and its
        sandbox is its own — so demanding claude's bwrap/socat as a REQUIRED failure blocks a
        run that never uses them."""
        cfg = self._cfg(self._CODEX_REVIEWER, self._EXEMPTION)
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)
        self.assertEqual(rc, 0)

    def test_a_claude_plan_advisory_leaf_requires_the_sandbox_deps(self) -> None:
        """#301 review round 8. The plan-advisory runner seeds a MINIMAL fail-closed
        sandbox for every confinable family — with NO [leaves.sandbox] exemption
        involved — so a claude plan reviewer needs bwrap/socat even when no
        exemption is configured (the seeded failIfUnavailable makes it REFUSE
        without them)."""
        cfg = self._cfg('mode = "stub"\n',
                        '[[leaves.plan_advisory]]\nid = "pr"\nmode = "command"\n'
                        'family = "claude"\nargv = ["claude", "-p"]\n')
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertIn("leaf sandbox", out)
        self.assertEqual(rc, 1)                            # REQUIRED failure

    def test_a_codex_plan_advisory_leaf_needs_no_claude_deps(self) -> None:
        # codex's sandbox is its own (argv-configured); nothing is seeded for it.
        cfg = self._cfg('mode = "stub"\n',
                        '[[leaves.plan_advisory]]\nid = "pr"\nmode = "command"\n'
                        'family = "codex"\nargv = ["codex", "exec"]\n')
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)
        self.assertEqual(rc, 0)

    def test_a_stub_leaf_spawns_nothing_to_sandbox(self) -> None:
        # An exemption configured, but every leaf is a stub: no leaf runs, so no sandbox is
        # seeded and no dependency is used.
        cfg = self._cfg(extra=self._EXEMPTION)   # reviewer stays a stub
        with self._deps(False):
            rc, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)
        self.assertEqual(rc, 0)

    def test_an_instance_with_no_sandbox_is_not_nagged(self) -> None:
        # The rows ride WITH the exemption. An instance that never asked for one is not told to
        # install bubblewrap.
        cfg = self._cfg(self._CLAUDE_REVIEWER)
        with self._deps(False):
            _, out = self._run(cfg)
        self.assertNotIn("leaf sandbox", out)
        self.assertNotIn("socat", out)


if __name__ == "__main__":
    unittest.main()
