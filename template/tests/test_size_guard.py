"""The pre-dispatch size advisory and where it is evaluated (issue #321).

Two properties, and the second is the one that took three review rounds to get right.

**Advisory, not blocking.** Calibrated over 86 settled bundles, the best structural rule
reaches 50% recall at 62% precision — nearly one wrong hold per right one. #321's own DoD
says to ship `warn` and leave `hold` unimplemented rather than train people to override a
gate, and that is what this does.

**Evaluated at `driver.advance`, not at Plan exit.** A Plan-exit hook covers two of the
four ways a bundle reaches Do. `flow.flow_ids` (explicit ids) and `pdca run` reach neither
proposed consumer, and a partially-built bundle derives as BUILT and never re-enters
PLANNED at all.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import driver, plan_policy, sizing, state
from pdca_harness.config import Config, LeafConfig

_OVERSIZED = ("- **Slug:** wide\n"
              "- **Difficulty:** high\n"
              "- **Conflicts with:** 12\n"
              "- **External dependencies:** `protoc`\n")
_SMALL = "- **Slug:** narrow\n"


def _cfg(root: Path, guard: str = "warn") -> Config:
    cfg = Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main",
        tracker_system="github", tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
    )
    cfg.size_guard = guard
    return cfg


class SizeGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, brief_body: str, *, built: bool = False) -> Path:
        d = self.tmp / "results" / "issue_1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(brief_body, encoding="utf-8")
        if built:
            (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    # -- the policy itself ------------------------------------------------------------

    def test_off_is_silent_and_does_no_work(self) -> None:
        """`off` must be byte-identical to having no guard — no output, no leaf, nothing.

        This is the default, so it is also the property that keeps `copier update` from
        changing behaviour for every existing instance.
        """
        d = self._bundle(_OVERSIZED)
        self.assertEqual(plan_policy.evaluate(d, _cfg(self.tmp, "off")), [])

    def test_warn_reports_an_oversized_slice_with_reasons(self) -> None:
        d = self._bundle(_OVERSIZED)
        reasons = plan_policy.evaluate(d, _cfg(self.tmp, "warn"))
        self.assertEqual([r.code for r in reasons], ["oversized"])
        detail = reasons[0].detail
        self.assertIn("pdca split", detail)
        self.assertIn("difficulty=high", detail, "the reason must name what fired")

    def test_a_small_slice_is_silent(self) -> None:
        d = self._bundle(_SMALL)
        self.assertEqual(plan_policy.evaluate(d, _cfg(self.tmp, "warn")), [])

    def test_hold_is_accepted_but_says_it_is_not_blocking(self) -> None:
        """Silently downgrading `hold` would let an instance believe it is protected.

        `hold` is unimplemented on evidence, not oversight: 62% precision means nearly one
        wrong block per right one, which is how a gate gets trained out of usefulness.
        """
        d = self._bundle(_OVERSIZED)
        reasons = plan_policy.evaluate(d, _cfg(self.tmp, "hold"))
        self.assertEqual(len(reasons), 1)
        self.assertIn("treated as 'warn'", reasons[0].detail)
        self.assertIn("62%", reasons[0].detail, "the evidence should travel with the note")

    # -- where it is evaluated --------------------------------------------------------

    def test_advance_evaluates_at_planned(self) -> None:
        d = self._bundle(_OVERSIZED)
        self.assertEqual(state.state(d), state.PLANNED)
        with mock.patch.object(plan_policy, "evaluate", return_value=[]) as spy:
            driver.advance(d, _cfg(self.tmp))
        spy.assert_called_once()

    def test_advance_evaluates_at_built_too(self) -> None:
        """A bundle with a brief and a patch but no gate record derives as BUILT and never
        re-enters PLANNED — a resumed bundle, or a builder that wrote a patch then exited
        non-zero. Gating PLANNED alone would let Check run unpoliced on exactly those."""
        d = self._bundle(_OVERSIZED, built=True)
        self.assertEqual(state.state(d), state.BUILT)
        with mock.patch.object(plan_policy, "evaluate", return_value=[]) as spy:
            driver.advance(d, _cfg(self.tmp))
        spy.assert_called_once()

    def test_close_disposition_bundles_are_exempt(self) -> None:
        """A close-disposition bundle skips builder and reviewer entirely, so advising a
        split on a duplicate/wontfix parent is noise about work that never enters Do."""
        d = self._bundle(_OVERSIZED + "- **Disposition hint:** duplicate\n")
        with mock.patch.object(plan_policy, "evaluate", return_value=[]) as spy:
            driver.advance(d, _cfg(self.tmp))
        spy.assert_not_called()

    def test_the_advisory_never_stops_the_beat(self) -> None:
        """Advisory means advisory: Do still dispatches. If this ever starts blocking, it
        must be a deliberate change with fresh evidence, not a drift."""
        d = self._bundle(_OVERSIZED)
        driver.advance(d, _cfg(self.tmp, "warn"))
        self.assertTrue((d / "patch.diff").exists(),
                        "the size advisory blocked Do — it must only warn")

    def test_the_verdict_is_recomputed_not_cached(self) -> None:
        """Fixing the BUNDLE must take effect immediately. A persisted marker would pin the
        verdict: once PLANNED, resuming does not re-run Plan, so the bundle would warn
        forever."""
        d = self._bundle(_OVERSIZED)
        cfg = _cfg(self.tmp, "warn")
        self.assertTrue(plan_policy.evaluate(d, cfg))
        (d / "brief.md").write_text(_SMALL, encoding="utf-8")
        self.assertEqual(plan_policy.evaluate(d, cfg), [],
                         "verdict survived the brief being fixed — it was cached")

    def test_config_default_is_off(self) -> None:
        """A rendered default of `warn` would emit output and consult a leaf for every
        instance taking a `copier update`; the opt-in has to be deliberate."""
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.assertEqual(cfg.size_guard, "off")
        self.assertEqual(plan_policy.evaluate(self._bundle(_OVERSIZED), cfg), [])

    def test_band_matches_the_estimator(self) -> None:
        """The guard must not re-derive a band of its own."""
        d = self._bundle(_OVERSIZED)
        cfg = _cfg(self.tmp, "warn")
        self.assertEqual(sizing.estimate(d / "brief.md", cfg).band, sizing.OVERSIZED)


class ConfigIsASnapshot(unittest.TestCase):
    """The recompute guarantee is about the BUNDLE, not the settings (PR #350 review).

    `Config.load()` runs once per invocation, so `[driver].size_guard` and
    `[driver.sizing]` are fixed for the whole run. Re-reading them per beat would let one
    `pdca flow` score two bundles in the same batch against two different thresholds — a
    batch has to be reproducible and explainable as one unit. The docstring used to claim
    the policy "reads config from disk", which it does not; that claim is now scoped to
    what actually is re-read.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_config_object_governs_the_whole_run(self) -> None:
        d = self.tmp / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "brief.md").write_text(_OVERSIZED, encoding="utf-8")
        off, warn = _cfg(self.tmp, "off"), _cfg(self.tmp, "warn")
        self.assertEqual(plan_policy.evaluate(d, off), [])
        self.assertTrue(plan_policy.evaluate(d, warn))

    def test_the_docstring_no_longer_claims_a_config_reload(self) -> None:
        """Locks the correction: the module must not re-acquire a claim the code does not
        deliver, which is how this was found in the first place."""
        self.assertNotIn("reads config from disk", plan_policy.__doc__ or "")
        self.assertIn("CONFIG is a snapshot", plan_policy.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
