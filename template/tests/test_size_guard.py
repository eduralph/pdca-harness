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

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import driver, plan_policy, sizing, state
from pdca_harness.config import Config, LeafConfig

# Deliberately declares NO external dependency: an unregistered one is #333's blocking
# check, and mixing it in here would test that instead of the size advisory.
_OVERSIZED = ("- **Slug:** wide\n"
              "- **Difficulty:** high\n"
              "- **Conflicts with:** 12\n"
              "- **Scope:** " + ("pad " * 4000) + "\n")
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


class ThirdReviewFixes(unittest.TestCase):
    """Round three on #351."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_model_verdict_survives_an_unchanged_band(self) -> None:
        """A structurally `oversized` brief plus a sizer saying `oversized` used to drop
        the model's evidence entirely, because combine returned early when the band did
        not move — losing the one signal that can see decomposability."""
        base = sizing.SizeEstimate(9, sizing.OVERSIZED, ["structural"],
                                   churn_band=sizing.WATCH, patch_band=sizing.OVERSIZED)
        out = sizing.combine(base, {"band": "oversized",
                                    "independent_outcomes": ["a", "b"]})
        self.assertEqual(out.model_band, sizing.OVERSIZED)
        self.assertIn("2 independently shippable outcome(s)", "; ".join(out.reasons))

    def test_a_model_split_verdict_overrides_the_coherent_patch_advice(self) -> None:
        """patch=oversized with churn=watch normally reads "large but coherent". If the
        SIZER says the slice decomposes, advising against a split contradicts it."""
        from unittest import mock
        (self.d / "brief.md").write_text(_OVERSIZED, encoding="utf-8")
        cfg = _cfg(self.tmp, "warn")
        with mock.patch("pdca_harness.leaves.run_sizer",
                        return_value={"band": "oversized",
                                      "independent_outcomes": ["a", "b"]}):
            reasons = plan_policy.evaluate(self.d, cfg)
        self.assertTrue(reasons)
        self.assertIn("pdca split", reasons[0].detail)
        self.assertNotIn("COHERENT", reasons[0].detail)

    def test_a_blocking_check_runs_before_the_paid_advisory(self) -> None:
        """No sense buying a model advisory for a bundle about to be held on set
        membership — the human pays again on the retry after registering the row."""
        from unittest import mock
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        (self.d / "brief.md").write_text(
            _OVERSIZED + "- **External dependencies:** `protoc`\n", encoding="utf-8")
        cfg = _cfg(self.tmp, "warn")
        cfg.dependency_guard = "hold"
        with mock.patch("pdca_harness.leaves.run_sizer") as sizer:
            reasons = plan_policy.evaluate(self.d, cfg)
        sizer.assert_not_called()
        self.assertEqual([r.code for r in reasons], ["unregistered-dependency"])


class TheVerdictHasAConsumer(unittest.TestCase):
    """The paid verdict was written and then read by nothing (#351 review).

    `sizing.json` was consulted only by its own cache: `pdca size` recomputed the
    STRUCTURAL estimate, SUMMARY never saw it, and the operator's only glimpse was a
    stderr line at the moment Plan exited. An instance paid a model to answer "how many
    independently shippable outcomes?" and the answer went nowhere.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / "issue_9"
        self.d.mkdir(parents=True)
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        (self.d / "brief.md").write_text(
            "- **Slug:** s\n- **Difficulty:** high\n- **Conflicts with:** 1\n",
            encoding="utf-8")
        # Stamped with the brief's digest: a FREE read now honours it, so an unstamped
        # verdict is treated as belonging to some other brief and correctly ignored.
        from pdca_harness import leaves
        from pdca_harness.config import Config
        key = leaves._sizer_key(self.d, Config.load(self.tmp), self.d / "brief.md")
        (self.d / "sizing.json").write_text(json.dumps({
            "band": "oversized",
            "independent_outcomes": ["parser", "renderer"],
            "proposed_seams": ["split at the parser/renderer boundary"],
            "confidence": "high", "brief_sha": key}), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pdca_size_shows_the_stored_verdict_and_its_seams(self) -> None:
        from pdca_harness import cli
        from pdca_harness.config import Config
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli._size(Config.load(self.tmp), []), 0)
        out = buf.getvalue()
        self.assertIn("sizer=oversized", out)
        self.assertIn("2 independently shippable outcome(s)", out)
        self.assertIn("seam: split at the parser/renderer boundary", out,
                      "the seams the sizer proposed were not shown")

    def test_pdca_size_never_invokes_the_paid_leaf(self) -> None:
        """It is documented read-only and must stay safe to run against a live queue."""
        from unittest import mock
        from pdca_harness import cli, leaves
        from pdca_harness.config import Config
        with mock.patch.object(leaves, "run_sizer") as sizer, \
                contextlib.redirect_stdout(io.StringIO()):
            cli._size(Config.load(self.tmp), [])
        sizer.assert_not_called()

    def test_the_paid_leaf_is_not_invoked_before_check(self) -> None:
        """At BUILT the patch already exists, the advisory does not block, and nothing
        persists it — so a second call buys a log line about work already paid for. A
        verdict Plan already produced is read for free."""
        from unittest import mock
        from pdca_harness import leaves
        cfg = _cfg(self.tmp, "warn")
        with mock.patch.object(leaves, "run_sizer") as sizer:
            reasons = plan_policy.evaluate(self.d, cfg, before_do=False)
        sizer.assert_not_called()
        self.assertTrue(any("sizer says oversized" in r.detail for r in reasons),
                        "the stored verdict was not folded into the BUILT advisory")

    def test_the_paid_leaf_is_invoked_before_do(self) -> None:
        from unittest import mock
        from pdca_harness import leaves
        cfg = _cfg(self.tmp, "warn")
        with mock.patch.object(leaves, "run_sizer", return_value=None) as sizer:
            plan_policy.evaluate(self.d, cfg, before_do=True)
        sizer.assert_called_once()


class TheRemedyFollowsTheBeat(unittest.TestCase):
    """A split authors BRIEFS, so it belongs to Plan — and the advice has to say so.

    Before Do, the answer is "split now". After Do, telling the human to run `pdca split`
    would decompose a bundle that already has a patch, producing children that inherit
    none of it. The route back to Plan is `iterate-plan`, which archives the brief and
    returns the bundle to the beat that authors them.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        (self.d / "brief.md").write_text(_OVERSIZED, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_before_do_it_says_split_now(self) -> None:
        r = plan_policy.evaluate(self.d, _cfg(self.tmp, "warn"), before_do=True)
        self.assertIn("pdca split", r[0].detail)
        self.assertNotIn("iterate-plan", r[0].detail)

    def test_after_do_it_routes_through_iterate_plan(self) -> None:
        r = plan_policy.evaluate(self.d, _cfg(self.tmp, "warn"), before_do=False)
        self.assertIn("iterate-plan", r[0].detail)
        self.assertNotIn("pdca split` first", r[0].detail,
                         "advised splitting a bundle that already has a patch")


class FinalReviewFixes(unittest.TestCase):
    """Pre-merge review of #351."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / "issue_9"
        self.d.mkdir(parents=True)
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        (self.d / "brief.md").write_text(_OVERSIZED, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self, cfg, **over):
        from pdca_harness import leaves
        key = leaves._sizer_key(self.d, cfg, self.d / "brief.md")
        (self.d / "sizing.json").write_text(json.dumps({
            "band": "oversized", "independent_outcomes": ["a", "b"],
            "proposed_seams": ["old seam"], "brief_sha": key, **over}), encoding="utf-8")

    def test_size_guard_is_read_from_the_driver_table(self) -> None:
        """It sat AFTER the next `[table]` header, so TOML parsed it into that table and
        `driver.size_guard` was absent — setting it to "warn" did nothing at all."""
        (self.tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n\n[driver]\nsize_guard = "warn"\n',
            encoding="utf-8")
        from pdca_harness.config import Config
        self.assertEqual(Config.load(self.tmp).size_guard, "warn")

    def test_a_stale_verdict_is_not_shown_by_pdca_size(self) -> None:
        """`sizing.json` is not archived by an iterate, so a bundle re-planned from
        oversized to a small brief still carries the old verdict on disk."""
        from pdca_harness import cli
        from pdca_harness.config import Config
        cfg = Config.load(self.tmp)
        self._store(cfg)
        (self.d / "brief.md").write_text("- **Slug:** small\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._size(cfg, [])
        out = buf.getvalue()
        self.assertNotIn("old seam", out, "seams from a replaced brief were shown")
        self.assertNotIn("sizer=", out, "a stale band was folded into a fresh estimate")

    def test_a_matching_verdict_is_still_shown(self) -> None:
        from pdca_harness import cli
        from pdca_harness.config import Config
        cfg = Config.load(self.tmp)
        self._store(cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._size(cfg, [])
        self.assertIn("old seam", buf.getvalue())

    def test_the_built_advisory_ignores_a_stale_verdict_too(self) -> None:
        from pdca_harness.config import Config
        cfg = Config.load(self.tmp)
        cfg.size_guard = "warn"
        self._store(cfg)
        (self.d / "brief.md").write_text("- **Slug:** small\n", encoding="utf-8")
        self.assertEqual(plan_policy.evaluate(self.d, cfg, before_do=False), [])

    def test_a_malformed_seams_field_is_ignored_not_iterated(self) -> None:
        """The verdict is model output and the contract is deliberately tolerant of an
        untidy schema — but tolerant must mean IGNORED, not iterated. `proposed_seams: 1`
        crashed the command; a string printed one "seam" per character."""
        from pdca_harness import cli
        from pdca_harness.config import Config
        cfg = Config.load(self.tmp)
        for bad in (1, "a string", None, {"x": 1}):
            with self.subTest(proposed_seams=bad):
                self._store(cfg, proposed_seams=bad)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli._size(cfg, []), 0)
                self.assertNotIn("seam:", buf.getvalue())

    def test_reordering_escalations_changes_the_cache_key(self) -> None:
        """`run_sizer` returns on the FIRST matching escalation, so order decides which
        stronger model runs. A flattened, sorted key gave both orders the same digest, so
        promoting a rule returned the cached verdict from the one it replaced."""
        from pdca_harness import leaves
        from pdca_harness.config import Config, LeafConfig
        cfg = Config.load(self.tmp)
        cfg.sizer = LeafConfig(mode="command", family="generic", argv=["true"])
        rules = [{"on_band": ["watch"], "argv": ["model-a"]},
                 {"on_confidence": ["low"], "argv": ["model-b"]}]
        cfg.sizer_escalation = rules
        first = leaves._sizer_key(self.d, cfg, self.d / "brief.md")
        cfg.sizer_escalation = list(reversed(rules))
        self.assertNotEqual(first, leaves._sizer_key(self.d, cfg, self.d / "brief.md"))

    def test_status_and_size_agree_on_a_model_only_oversize(self) -> None:
        """The case the sizer exists for: structure says `ok`, the model finds the brief
        decomposable. `_status` read only the structural estimate, so the marker was
        missing at the sign-off queue — which is where a human is actually looking —
        while `pdca size` reported oversized."""
        from pdca_harness import cli, leaves
        from pdca_harness.config import Config
        (self.d / "brief.md").write_text("- **Slug:** small\n", encoding="utf-8")
        (self.d / "patch.diff").write_text("x", encoding="utf-8")
        (self.d / "check-gates.json").write_text("[]", encoding="utf-8")
        (self.d / "SUMMARY.md").write_text("## 6.\n", encoding="utf-8")
        cfg = Config.load(self.tmp)
        key = leaves._sizer_key(self.d, cfg, self.d / "brief.md")
        (self.d / "sizing.json").write_text(json.dumps({
            "band": "oversized", "independent_outcomes": ["a", "b"],
            "brief_sha": key}), encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._status(cfg, None)
        self.assertIn("[oversized]", buf.getvalue(),
                      "status omitted the marker for a model-only oversize")


if __name__ == "__main__":
    unittest.main()
