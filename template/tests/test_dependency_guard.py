"""A declared-but-unregistered external dependency holds the bundle at Plan (#333).

`assemble._unregistered_dependency_items` is a pure function of `brief.md` + `pdca.toml` —
`doctor.registered_ids` vs `brief.external_dependency_tokens`, set membership, no patch, no
gates, no review. Every input exists the moment Plan writes the brief. It ran at **Check**,
by which point an `opus`/`max` builder, a codex reviewer at `xhigh` and the adversary have
all been spent to discover something knowable before Do was ever dispatched.

Its own docstring names the principle it was failing to deliver:

> when a change needs something a human must install or provide, the system must REGISTER
> it … rather than let it surface mid-cycle as a cryptic build failure

It still surfaced mid-cycle — one beat later than the build failure it was meant to
pre-empt.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import assemble, doctor, driver, plan_policy, state
from pdca_harness.config import Config, LeafConfig

_DECLARED = "- **Slug:** needs-protoc\n- **External dependencies:** `protoc`\n"
# The detect cmd is a stub that PASSES (`true`): since #340 the guard executes the
# registered row's cmd, and these tests are about registration semantics — the probe
# has its own class below, with `false` as the failing detect.
_ROW = {"id": "protoc", "cmd": "true", "hint": "apt install protobuf-compiler"}


def _cfg(root: Path, rows: list[dict] | None = None, guard: str = "hold") -> Config:
    cfg = Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main",
        tracker_system="github", tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
    )
    cfg.doctor_checks = list(rows or [])
    cfg.dependency_guard = guard
    return cfg


class DependencyGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "results").mkdir(parents=True)
        # A real pdca.toml, because `registered_ids` deliberately reads rows from DISK
        # rather than from the Config snapshot — that is what makes the hold self-clearing.
        (self.tmp / "pdca.toml").write_text("[paths]\nbundle_root = \"results\"\n",
                                            encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, body: str = _DECLARED) -> Path:
        d = self.tmp / "results" / "issue_1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def _register(self) -> None:
        (self.tmp / "pdca.toml").write_text(
            "[paths]\nbundle_root = \"results\"\n\n"
            "[[doctor.checks]]\nid = \"protoc\"\n"
            "cmd = \"true\"\nhint = \"apt install protobuf-compiler\"\n",
            encoding="utf-8")

    # -- the check ---------------------------------------------------------------------

    def test_unregistered_declaration_is_a_blocking_reason(self) -> None:
        reasons = plan_policy.evaluate(self._bundle(), _cfg(self.tmp))
        self.assertEqual([r.code for r in reasons], ["unregistered-dependency"])
        self.assertTrue(plan_policy.blocking(reasons),
                        "set membership must block, not merely warn")
        self.assertIn("protoc", reasons[0].detail)

    def test_registering_the_row_clears_it(self) -> None:
        """Registered means the row is IN pdca.toml — `registered_ids` reads the file, not
        the Config snapshot, so a row added mid-cycle counts (PR #269 review)."""
        d = self._bundle()
        self.assertTrue(plan_policy.evaluate(d, _cfg(self.tmp)))
        self._register()
        self.assertEqual(plan_policy.evaluate(d, _cfg(self.tmp, [_ROW])), [])

    def test_an_exempt_annotation_never_holds(self) -> None:
        """A topology nothing can detect is written in prose or annotated `(no-check: …)`,
        yields no token, and is exempt — so this can never become a reason to stop
        declaring dependencies."""
        for body in ("- **Slug:** s\n- **External dependencies:** a ≥3-replica cluster\n",
                     "- **Slug:** s\n- **External dependencies:** `fdb` (no-check: topology)\n",
                     "- **Slug:** s\n- **External dependencies:** none\n"):
            with self.subTest(body=body.splitlines()[-1]):
                self.assertEqual(plan_policy.evaluate(self._bundle(body), _cfg(self.tmp)), [])

    def test_off_disables_it(self) -> None:
        self.assertEqual(plan_policy.evaluate(self._bundle(), _cfg(self.tmp, guard="off")), [])

    def test_default_is_hold(self) -> None:
        """Unlike `size_guard`, this defaults ON: the verdict is set membership with no
        false-positive class, and it moves an EXISTING block earlier — the same condition
        already refuses `signoff --accept` through the C6 guard."""
        cfg = _cfg(self.tmp)
        del cfg.dependency_guard
        self.assertTrue(plan_policy.evaluate(self._bundle(), cfg))

    # -- where it acts -----------------------------------------------------------------

    def test_do_is_not_dispatched_while_it_holds(self) -> None:
        """The whole point: the cycle is not burned discovering this at Check."""
        d = self._bundle()
        with self.assertRaises(plan_policy.PolicyHold):
            driver.advance(d, _cfg(self.tmp))
        self.assertFalse((d / "patch.diff").exists(), "Do ran despite a blocking hold")
        self.assertEqual(state.state(d), state.PLANNED, "the bundle stays in-flight")

    def test_the_hold_clears_without_replanning(self) -> None:
        """Registering the row and re-running is all it takes — the policy is recomputed
        every beat and `registered_ids` reads pdca.toml as it stands NOW, so a row added
        mid-cycle counts (PR #269 review)."""
        d = self._bundle()
        with self.assertRaises(plan_policy.PolicyHold):
            driver.advance(d, _cfg(self.tmp))
        self.assertFalse((d / "patch.diff").exists())
        self._register()
        driver.advance(d, _cfg(self.tmp, [_ROW]))
        self.assertTrue((d / "patch.diff").exists(), "the hold survived registration")

    # -- the backstop stays ------------------------------------------------------------

    def test_check_time_reconciliation_still_exists(self) -> None:
        """Not redundant: `pdca.toml` can LOSE a row mid-cycle, which is why the
        reconciliation reads the file as it stands now rather than the run's opening
        snapshot. A row deleted after Plan passed is still caught at Check."""
        d = self._bundle()
        self.assertTrue(assemble._unregistered_dependency_items(d / "brief.md", _cfg(self.tmp)))

    def test_both_callers_share_one_implementation(self) -> None:
        """Two enumerations of "what counts as registered" would drift — the failure mode
        #334 documents for the archive/evidence sets."""
        d = self._bundle()
        cfg = _cfg(self.tmp)
        self.assertEqual(assemble._unregistered_dependency_items(d / "brief.md", cfg),
                         doctor.unregistered_dependencies(d / "brief.md", cfg))




class HoldDoesNotSpin(unittest.TestCase):
    """`run_issue`, not `advance` — the gap that let an infinite loop through review.

    Every earlier test drove `driver.advance` directly, so none of them exercised the loop
    that actually consumes a hold: `while state not in HALTED: advance(...)`. A hold leaves
    the bundle PLANNED or BUILT — neither halted — so a quiet return spins forever printing
    the same warning until the process is killed.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "results").mkdir(parents=True)
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(_DECLARED, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_issue_returns_instead_of_looping(self) -> None:
        import signal

        def _die(_sig, _frm):  # pragma: no cover - only on regression
            raise AssertionError("run_issue did not terminate on a policy hold")

        old = signal.signal(signal.SIGALRM, _die)
        signal.alarm(10)
        try:
            self.assertEqual(driver.run_issue(self.d, _cfg(self.tmp)), state.PLANNED)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_the_hold_is_raised_so_a_caller_cannot_ignore_it(self) -> None:
        with self.assertRaises(plan_policy.PolicyHold):
            driver.advance(self.d, _cfg(self.tmp))

    def test_warn_mode_reports_but_does_not_block(self) -> None:
        """The documented `warn` option must actually differ from `hold` — sharing a
        reason code made `blocking()` stop the beat for both."""
        cfg = _cfg(self.tmp, guard="warn")
        reasons = plan_policy.evaluate(self.d, cfg)
        self.assertTrue(reasons, "warn must still report the item")
        self.assertEqual(plan_policy.blocking(reasons), [])
        driver.advance(self.d, cfg)
        self.assertTrue((self.d / "patch.diff").exists(), "warn blocked Do")


class HoldReachesTheCaller(unittest.TestCase):
    """Round two on #351: a hold has to be visible to whatever invoked the driver."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "results").mkdir(parents=True)
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(_DECLARED, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_typo_in_the_guard_fails_safe(self) -> None:
        """`dependency_guard = "hld"` fell through to the warn branch and dispatched Do
        past an unregistered dependency, with nothing on screen to say the setting had not
        been understood. An unrecognised value now warns and holds."""
        cfg = _cfg(self.tmp, guard="hld")
        self.assertTrue(plan_policy.blocking(plan_policy.evaluate(self.d, cfg)))

    def test_every_recognised_value_still_behaves(self) -> None:
        for guard, blocks in (("hold", True), ("warn", False), ("off", None)):
            with self.subTest(guard=guard):
                reasons = plan_policy.evaluate(self.d, _cfg(self.tmp, guard=guard))
                if blocks is None:
                    self.assertEqual(reasons, [])
                else:
                    self.assertEqual(bool(plan_policy.blocking(reasons)), blocks)

    def test_held_distinguishes_a_hold_from_completion(self) -> None:
        """A non-halted return can only mean a policy hold — the loop has no other early
        exit — so this is the predicate a caller needs."""
        self.assertTrue(driver.held(state.PLANNED))
        self.assertTrue(driver.held(state.BUILT))
        self.assertFalse(driver.held(state.COMPLETE))
        self.assertFalse(driver.held(state.AWAITING_SIGNOFF))

    def test_pdca_run_exits_non_zero_when_held(self) -> None:
        """Otherwise automation reads a bundle blocked before Do as a completed run —
        `run_issue` returns PLANNED and the command returned 0."""
        from pdca_harness import cli
        from pdca_harness.config import Config
        self.assertEqual(cli._run(Config.load(self.tmp), "1"), 1)

    def test_pdca_run_still_exits_zero_when_it_finishes(self) -> None:
        (self.tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n\n'
            '[[doctor.checks]]\nid = "protoc"\n'
            'cmd = "true"\nhint = "apt install protobuf-compiler"\n',
            encoding="utf-8")
        from pdca_harness import cli
        from pdca_harness.config import Config
        self.assertEqual(cli._run(Config.load(self.tmp), "1"), 0)

    def test_signoff_iterate_also_exits_non_zero_when_held(self) -> None:
        """The second `run_issue` caller. An iterate archives the attempt, returns to
        PLANNED and is then held before Do — nothing was rebuilt, so exiting 0 would tell
        automation the sign-off decision had been carried out."""
        from types import SimpleNamespace
        from pdca_harness import cli, gates, assemble
        from pdca_harness.config import Config
        cfg = Config.load(self.tmp)
        cfg.gates_checks = [{"id": "C4", "tier": "C4", "label": "v", "scope": "bundle",
                             "gating": True, "cmd": "true"}]
        (self.d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (self.d / "check-review.md").write_text("All advisory items PASS.\n",
                                                encoding="utf-8")
        gates.run_gates(self.d, cfg)
        assemble.assemble_summary(self.d, cfg)
        summary = self.d / "SUMMARY.md"
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")

        args = SimpleNamespace(issue_id="1", accept=False, iterate_do=True,
                               iterate_plan=False, discontinue=False, by="t", delta="",
                               no_publish=True)
        self.assertEqual(cli._signoff(cfg, args), 1,
                         "a held rebuild reported the sign-off as carried out")


class DependencyProbe(unittest.TestCase):
    """#340: the guard RUNS the registered detect cmd — registration alone no longer
    discharges a dependency the host does not have.

    #333 forces every checkable token to name a `[[doctor.checks]]` row, but
    `registered_ids` only requires a non-empty `cmd` — nothing executed it. A planner
    could discharge every check on a machine where the dependency is absent, and Do then
    dispatched into the silently-worked-around case whose only detector was the builder's
    own self-report. The probe is scoped to exactly the rows the brief's tokens name, so
    an instance's wider doctor inventory is never a tax on every bundle.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "results").mkdir(parents=True)
        self._write_toml()
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(_DECLARED, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_toml(self, *rows: dict) -> None:
        body = '[paths]\nbundle_root = "results"\n'
        for row in rows:
            body += "\n[[doctor.checks]]\n" + "".join(
                f'{k} = "{v}"\n' for k, v in row.items())
        (self.tmp / "pdca.toml").write_text(body, encoding="utf-8")

    # -- the probe ---------------------------------------------------------------------

    def test_a_failing_detect_cmd_holds_before_do(self) -> None:
        """A registered row whose detect cmd exits non-zero holds the bundle before Do
        spends a builder, quoting that row's own hint — at the default `lanes = 1`, the
        path that had zero preflight of any kind."""
        self._write_toml({"id": "protoc", "cmd": "false",
                          "hint": "apt install protobuf-compiler"})
        cfg = _cfg(self.tmp)
        self.assertEqual(cfg.lanes, 1, "the probe must not depend on lane preflight")
        reasons = plan_policy.evaluate(self.d, cfg)
        self.assertEqual([r.code for r in reasons], ["failed-dependency"])
        self.assertTrue(plan_policy.blocking(reasons),
                        "an exit code is deterministic — it blocks, like set membership")
        self.assertIn("apt install protobuf-compiler", reasons[0].detail,
                      "the hold must quote the row's own hint")
        with self.assertRaises(plan_policy.PolicyHold):
            driver.advance(self.d, cfg)
        self.assertFalse((self.d / "patch.diff").exists(), "Do ran despite a failing probe")
        self.assertEqual(state.state(self.d), state.PLANNED, "the bundle stays in-flight")

    def test_a_passing_detect_cmd_changes_nothing(self) -> None:
        """Exit 0 ⇒ behaviour unchanged: no reasons, and Do dispatches."""
        self._write_toml({"id": "protoc", "cmd": "true", "hint": "h"})
        cfg = _cfg(self.tmp)
        self.assertEqual(plan_policy.evaluate(self.d, cfg), [])
        driver.advance(self.d, cfg)
        self.assertTrue((self.d / "patch.diff").exists())

    def test_only_the_rows_the_brief_names_are_executed(self) -> None:
        """A registered row the brief does NOT name is never run (#340's definition of
        done) — probing is scoped to the bundle's declared dependencies, not the
        instance's doctor inventory. The unnamed row both fails and leaves a marker, so
        execution is observable either way: no hold AND no marker ⇒ never spawned."""
        marker = self.tmp / "unnamed-row-ran"
        self._write_toml({"id": "protoc", "cmd": "true"},
                         {"id": "docker", "cmd": f"touch {marker}; exit 1",
                          "hint": "install docker"})
        self.assertEqual(plan_policy.evaluate(self.d, _cfg(self.tmp)), [])
        self.assertFalse(marker.exists(), "a row the brief does not name was executed")

    def test_the_probe_runs_after_the_registration_check(self) -> None:
        """An unregistered token holds as `unregistered-dependency` first — the probe
        only ever executes registered rows, and its reasons list after #333's."""
        (self.d / "brief.md").write_text(
            "- **Slug:** s\n- **External dependencies:** `missing`, `protoc`\n",
            encoding="utf-8")
        self._write_toml({"id": "protoc", "cmd": "false", "hint": "h"})
        reasons = plan_policy.evaluate(self.d, _cfg(self.tmp))
        self.assertEqual([r.code for r in reasons],
                         ["unregistered-dependency", "failed-dependency"])
        self.assertIn("missing", reasons[0].detail)

    def test_exempt_and_prose_dependencies_are_not_probed(self) -> None:
        """`(no-check: …)` and plain prose yield no token, so a matching registered row
        is not run for them — the escape hatch stays free of side effects too."""
        marker = self.tmp / "exempt-row-ran"
        self._write_toml({"id": "fdb", "cmd": f"touch {marker}; exit 1", "hint": "h"})
        for body in ("- **Slug:** s\n- **External dependencies:** `fdb` (no-check: topology)\n",
                     "- **Slug:** s\n- **External dependencies:** an fdb cluster\n"):
            with self.subTest(body=body.splitlines()[-1]):
                (self.d / "brief.md").write_text(body, encoding="utf-8")
                self.assertEqual(plan_policy.evaluate(self.d, _cfg(self.tmp)), [])
                self.assertFalse(marker.exists(), "an exempt dependency was probed")

    def test_rows_are_read_from_disk_not_the_snapshot(self) -> None:
        """The probe reads `Config.current_doctor_checks` — pdca.toml as it stands NOW —
        so a row added or edited during the Plan beat counts in the same pass. The stale
        snapshot row here would PASS; only the disk row fails."""
        self._write_toml({"id": "protoc", "cmd": "false", "hint": "h"})
        cfg = _cfg(self.tmp, [{"id": "protoc", "cmd": "true", "hint": "h"}])
        self.assertEqual([r.code for r in plan_policy.evaluate(self.d, cfg)],
                         ["failed-dependency"])

    def test_installing_the_dependency_clears_the_hold_without_replanning(self) -> None:
        """Recomputed per beat, never cached (the module docstring's own rule): fix the
        row — or install the tool — and the next beat proceeds."""
        self._write_toml({"id": "protoc", "cmd": "false", "hint": "h"})
        with self.assertRaises(plan_policy.PolicyHold):
            driver.advance(self.d, _cfg(self.tmp))
        self._write_toml({"id": "protoc", "cmd": "true", "hint": "h"})
        driver.advance(self.d, _cfg(self.tmp))
        self.assertTrue((self.d / "patch.diff").exists(), "the hold survived the fix")

    # -- modes -------------------------------------------------------------------------

    def test_off_is_byte_identical_no_probe_runs(self) -> None:
        """`off` disables the guard entirely — the detect cmd is not even spawned."""
        marker = self.tmp / "probed-under-off"
        self._write_toml({"id": "protoc", "cmd": f"touch {marker}; exit 1", "hint": "h"})
        self.assertEqual(plan_policy.evaluate(self.d, _cfg(self.tmp, guard="off")), [])
        self.assertFalse(marker.exists(), "off must not execute detect cmds")

    def test_warn_reports_the_failure_without_holding(self) -> None:
        """`warn` prints the same item under a non-blocking code and Do proceeds."""
        self._write_toml({"id": "protoc", "cmd": "false",
                          "hint": "apt install protobuf-compiler"})
        cfg = _cfg(self.tmp, guard="warn")
        reasons = plan_policy.evaluate(self.d, cfg)
        self.assertEqual([r.code for r in reasons], ["failed-dependency-warn"])
        self.assertEqual(plan_policy.blocking(reasons), [])
        driver.advance(self.d, cfg)
        self.assertTrue((self.d / "patch.diff").exists(), "warn blocked Do")


if __name__ == "__main__":
    unittest.main()
