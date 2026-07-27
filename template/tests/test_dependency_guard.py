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
_ROW = {"id": "protoc", "cmd": "protoc --version", "hint": "apt install protobuf-compiler"}


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
            "cmd = \"protoc --version\"\nhint = \"apt install protobuf-compiler\"\n",
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
        driver.advance(d, _cfg(self.tmp))
        self.assertFalse((d / "patch.diff").exists(), "Do ran despite a blocking hold")
        self.assertEqual(state.state(d), state.PLANNED, "the bundle stays in-flight")

    def test_the_hold_clears_without_replanning(self) -> None:
        """Registering the row and re-running is all it takes — the policy is recomputed
        every beat and `registered_ids` reads pdca.toml as it stands NOW, so a row added
        mid-cycle counts (PR #269 review)."""
        d = self._bundle()
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


if __name__ == "__main__":
    unittest.main()
