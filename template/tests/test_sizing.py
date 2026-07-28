"""The structural size estimate and the escalate-only combine (issue #320).

Calibrated against 86 settled bundles of a real instance. The figures the weights and
cutoffs were derived from are asserted here as *properties* — that the noise features
carry no weight, that the model can only escalate — because the numbers themselves live
in a corpus this repo does not contain, and a test that cannot see the corpus can still
lock the shape of the decision.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import sizing

_CFG = SimpleNamespace(sizing={})


def _brief(body: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "brief.md"
    p.write_text(body, encoding="utf-8")
    return p


class Structural(unittest.TestCase):
    def test_each_weighted_feature_moves_the_score(self) -> None:
        base = _brief("- **Slug:** s\n")
        self.assertEqual(sizing.estimate(base, _CFG).score, 0)

        cases = {
            "- **Difficulty:** high\n": 3,
            "- **Conflicts with:** 12\n": 3,
            "- **External dependencies:** `protoc`\n": 3,
        }
        for extra, expected in cases.items():
            with self.subTest(feature=extra.strip()):
                est = sizing.estimate(_brief("- **Slug:** s\n" + extra), _CFG)
                self.assertEqual(est.score, expected)

    def test_plan_pointer_de_escalates(self) -> None:
        """The one negative term (ρ −0.24): a brief pointing at a host planning artifact
        converges better, and without it the score has no de-escalating term at all."""
        with_ptr = _brief("- **Slug:** s\n- **Difficulty:** high\n"
                          "- **Planning artifact:** docs/adr-7.md\n")
        without = _brief("- **Slug:** s\n- **Difficulty:** high\n")
        self.assertLess(sizing.estimate(with_ptr, _CFG).score,
                        sizing.estimate(without, _CFG).score)

    def test_noise_features_carry_no_weight(self) -> None:
        """Guards the calibration result directly.

        `scope_words` is the trap: ρ 0.47 against patch bytes but 0.07 against rounds, so
        it LOOKS like a size signal while carrying nothing about churn. #320 originally
        proposed weighting it. If a later change starts scoring any of these, this fails.
        """
        base = "- **Slug:** s\n"
        noise = [
            "- **Scope:** " + ("word " * 400) + "\n",
            "- **Test file:** a.py, b.py, c.py\n",
            "- **Out of scope:** everything else\n",
            "- **Success criterion:** " + ("clause; " * 40) + "\n",
        ]
        for extra in noise:
            with self.subTest(feature=extra[:28]):
                self.assertEqual(sizing.estimate(_brief(base + extra), _CFG).score, 0)

    def test_difficulty_is_substring_matched(self) -> None:
        """The field is prose in practice — "high — the widest-surface slice: …" — so an
        equality test scores nearly every real brief as unset. Mirrors how
        `leaves._when_matches` routes on the same field."""
        est = sizing.estimate(
            _brief("- **Slug:** s\n- **Difficulty:** high — widest-surface slice\n"), _CFG)
        self.assertEqual(est.score, 3)

    def test_brief_bytes_are_measured_above_the_carry_forward(self) -> None:
        """An iterate APPENDS the sign-off rationale to the brief, so measuring the file
        as it sits leaks the outcome into the predictor — a brief is larger *because* it
        churned. #319 measured the leak moving ρ from 0.21 to 0.64."""
        small = "- **Slug:** s\n- **Difficulty:** high\n"
        padded = small + "\n## Iteration 1 — carry-forward\n" + ("x" * 40_000) + "\n"
        self.assertEqual(sizing.estimate(_brief(padded), _CFG).score,
                         sizing.estimate(_brief(small), _CFG).score,
                         "carry-forward text leaked into the a-priori size")

    def test_bands_follow_the_cutoffs(self) -> None:
        low = _brief("- **Slug:** s\n")
        self.assertEqual(sizing.estimate(low, _CFG).churn_band, sizing.OK)
        mid = _brief("- **Slug:** s\n- **Difficulty:** high\n")           # score 3
        self.assertEqual(sizing.estimate(mid, _CFG).churn_band, sizing.OK)
        high = _brief("- **Slug:** s\n- **Difficulty:** high\n"
                      "- **Conflicts with:** 1\n"
                      "- **External dependencies:** `protoc`\n")           # score 9
        self.assertEqual(sizing.estimate(high, _CFG).churn_band, sizing.OVERSIZED)

    def test_patch_and_churn_bands_are_reported_separately(self) -> None:
        """They answer different questions and neither is a proxy for the other: of 14
        bundles with a ≥100 KB patch 10 churned, of 16 churners 10 were big. A brief that
        is high-difficulty and large predicts a big patch without necessarily churning."""
        est = sizing.estimate(
            _brief("- **Slug:** s\n- **Difficulty:** high\n" + "- x\n" * 4000), _CFG)
        self.assertEqual(est.patch_band, sizing.OVERSIZED)
        self.assertEqual(est.churn_band, sizing.WATCH)
        self.assertEqual(est.band, sizing.OVERSIZED, "combined band is the higher")

    def test_reasons_name_the_signals_that_fired(self) -> None:
        est = sizing.estimate(_brief("- **Slug:** s\n- **Difficulty:** high\n"
                                     "- **Conflicts with:** 7\n"), _CFG)
        joined = "; ".join(est.reasons)
        self.assertIn("difficulty=high", joined)
        self.assertIn("conflict", joined)

    def test_config_retunes_weights_and_cutoffs(self) -> None:
        """`[driver.sizing]` exists so an instance calibrates against its own corpus."""
        cfg = SimpleNamespace(sizing={"difficulty_high": 99, "oversized": 50})
        est = sizing.estimate(_brief("- **Slug:** s\n- **Difficulty:** high\n"), cfg)
        self.assertEqual(est.score, 99)
        self.assertEqual(est.churn_band, sizing.OVERSIZED)

    def test_missing_or_unreadable_brief_abstains(self) -> None:
        """A detector that crashes the Plan beat is worse than one that abstains."""
        est = sizing.estimate(Path(tempfile.mkdtemp()) / "nope.md", _CFG)
        self.assertEqual(est.band, sizing.OK)
        self.assertEqual(est.score, 0)


class Combine(unittest.TestCase):
    """The property this issue is named for: combined so the model can only escalate."""

    def _structural(self, band: str) -> sizing.SizeEstimate:
        return sizing.SizeEstimate(5, band, ["structural"], churn_band=band, patch_band=band)

    def test_model_escalates(self) -> None:
        out = sizing.combine(self._structural(sizing.OK),
                             {"band": "oversized",
                              "independent_outcomes": ["a", "b", "c"],
                              "confidence": "high"})
        self.assertEqual(out.band, sizing.OVERSIZED)
        self.assertIn("3 independently shippable outcome(s)", "; ".join(out.reasons))

    def test_model_can_never_downgrade(self) -> None:
        """A model that could lower a band becomes a single point of failure over a signal
        that at least fails predictably. Asserted for every downward pair."""
        for structural, claim in ((sizing.OVERSIZED, "ok"), (sizing.OVERSIZED, "watch"),
                                  (sizing.WATCH, "ok")):
            with self.subTest(structural=structural, model=claim):
                out = sizing.combine(self._structural(structural), {"band": claim})
                self.assertEqual(out.band, structural)

    def test_structural_readouts_survive_escalation(self) -> None:
        """Escalating the combined band must not rewrite what structure actually found."""
        out = sizing.combine(self._structural(sizing.OK), {"band": "oversized"})
        self.assertEqual(out.churn_band, sizing.OK)
        self.assertEqual(out.patch_band, sizing.OK)
        self.assertEqual(out.score, 5)

    def test_absent_or_malformed_verdict_changes_nothing(self) -> None:
        """The leaf is optional; an offline run must be byte-identical."""
        base = self._structural(sizing.WATCH)
        for model in (None, {}, {"band": ""}, {"band": "enormous"}, "not-a-dict", []):
            with self.subTest(model=model):
                self.assertEqual(sizing.combine(base, model), base)




class SizerLeaf(unittest.TestCase):
    """The model half (1b): the leaf, its escalation, and its optionality."""

    def setUp(self) -> None:
        from pdca_harness.config import Config, LeafConfig
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )

    def test_stub_mode_writes_a_deterministic_verdict(self) -> None:
        """Offline runs must exercise the same combine() path as a real leaf."""
        from pdca_harness import leaves
        verdict = leaves.run_sizer(self.d, self.cfg)
        self.assertEqual(verdict["band"], sizing.OK)
        self.assertTrue((self.d / leaves.SIZING_FILE).exists())

    def test_no_brief_means_no_verdict(self) -> None:
        from pdca_harness import leaves
        (self.d / "brief.md").unlink()
        self.assertIsNone(leaves.run_sizer(self.d, self.cfg))

    def test_malformed_verdict_reads_as_absent(self) -> None:
        from pdca_harness import leaves
        (self.d / leaves.SIZING_FILE).write_text("{not json", encoding="utf-8")
        self.assertIsNone(leaves._read_sizing(self.d))
        # …and therefore leaves the structural estimate untouched.
        base = sizing.SizeEstimate(3, sizing.WATCH, [])
        self.assertEqual(sizing.combine(base, leaves._read_sizing(self.d)), base)

    def test_escalation_fires_on_band_or_confidence(self) -> None:
        from pdca_harness import leaves
        cases = [
            ({"band": "watch", "confidence": "high"}, {"on_band": ["watch"]}, True),
            ({"band": "ok", "confidence": "low"}, {"on_confidence": ["low"]}, True),
            ({"band": "ok", "confidence": "high"}, {"on_band": ["watch"]}, False),
            ({"band": "watch"}, {"on_band": ["watch"], "on_confidence": ["low"]}, True),
        ]
        for verdict, spec, expected in cases:
            with self.subTest(verdict=verdict, spec=spec):
                self.assertEqual(leaves._sizer_escalates(verdict, spec), expected)

    def test_an_empty_escalation_spec_never_fires(self) -> None:
        """A spec declaring neither condition must not escalate every bundle — the failure
        a plain truthiness test would produce."""
        from pdca_harness import leaves
        self.assertFalse(leaves._sizer_escalates({"band": "oversized"}, {}))

    def test_absent_verdict_never_escalates(self) -> None:
        """A leaf that failed to answer is not evidence a stronger one would succeed."""
        from pdca_harness import leaves
        self.assertFalse(leaves._sizer_escalates(None, {"on_band": ["watch"]}))


class DoctorCoversTheSizer(unittest.TestCase):
    """`pdca doctor --strict` must know about the sizer (#320 review).

    `_command_leaves` enumerated the named leaves plus builder variants/escalations. A
    sizer configured with its own binary — or a sizer escalation naming a stronger one —
    was invisible, so `--strict` could pass while the Plan advisory later died on a CLI
    that was never installed.
    """

    def _cfg(self, **kw):
        from pdca_harness.config import Config, LeafConfig
        cfg = Config(
            root=Path("."), bundle_root=Path("results"), process_dir=Path("process"),
            templates_dir=Path("templates"), default_branch="main",
            tracker_system="github", tracker_url="", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def test_a_command_sizer_is_enumerated(self) -> None:
        from pdca_harness import doctor
        from pdca_harness.config import LeafConfig
        cfg = self._cfg(sizer=LeafConfig(mode="command", family="claude",
                                         argv=["sizer-cli", "-p"]))
        self.assertIn("sizer", doctor._command_leaves(cfg))

    def test_a_stub_sizer_is_not_enumerated(self) -> None:
        from pdca_harness import doctor
        from pdca_harness.config import LeafConfig
        self.assertNotIn("sizer", doctor._command_leaves(self._cfg(sizer=LeafConfig())))

    def test_a_sizer_escalation_naming_another_binary_is_enumerated(self) -> None:
        """The escalation is where a DIFFERENT CLI usually appears — a stronger model."""
        from pdca_harness import doctor
        from pdca_harness.config import LeafConfig
        cfg = self._cfg(
            sizer=LeafConfig(mode="command", family="claude", argv=["sizer-cli"]),
            sizer_escalation=[{"on_band": ["watch"], "argv": ["stronger-cli", "-p"]}])
        found = doctor._command_leaves(cfg)
        self.assertTrue(any("stronger-cli" in (leaf.argv or [])
                            for leaf in found.values()),
                        f"the escalation binary was not preflighted: {list(found)}")


class SecondReviewFixes(unittest.TestCase):
    """Round two on #349."""

    def _brief(self, body: str, *, raw: bytes | None = None) -> Path:
        f = Path(tempfile.mkdtemp()) / "brief.md"
        if raw is not None:
            f.write_bytes(raw)
        else:
            f.write_text(body, encoding="utf-8")
        return f

    def test_invalid_utf8_abstains_instead_of_raising(self) -> None:
        """`_apriori_bytes` reads with errors="replace" and survives, but the field helpers
        decode strictly — so one stray byte aborted the Plan beat, which is exactly what
        "a detector that crashes Plan is worse than one that abstains" forbids."""
        f = self._brief("", raw=b"- **Slug:** s\n- **Difficulty:** high\n\xff\xfe\n")
        est = sizing.estimate(f, _CFG)
        self.assertEqual(est.band, sizing.OK)
        self.assertEqual(est.score, 0)

    def test_only_the_drivers_carry_forward_heading_truncates(self) -> None:
        """A loose "starts with Iteration" test discarded everything under a legitimate
        `## Iteration strategy` heading, scoring a large slice as small."""
        big = "x" * 14000
        legit = self._brief(f"- **Slug:** s\n- **Difficulty:** high\n\n"
                            f"## Iteration strategy\n\n{big}\n")
        real = self._brief(f"- **Slug:** s\n- **Difficulty:** high\n\n"
                           f"## Iteration 1 — carry-forward\n\n{big}\n")
        self.assertGreater(sizing.estimate(legit, _CFG).score,
                           sizing.estimate(real, _CFG).score,
                           "a legitimate Iteration heading was treated as carry-forward")

    def test_difficulty_is_word_matched_not_substring_matched(self) -> None:
        """Bare substring fired on "hardening": `medium — certificate hardening is
        localized` scored as high."""
        cases = {"medium — certificate hardening is localized": 0,
                 "low — hard-won but small": 0,
                 "high — widest surface": 3,
                 "hard problem": 3}
        for value, expected in cases.items():
            with self.subTest(difficulty=value):
                f = self._brief(f"- **Slug:** s\n- **Difficulty:** {value}\n")
                self.assertEqual(sizing.estimate(f, _CFG).score, expected)

    def test_a_pointer_brief_tells_the_sizer_to_read_the_artifact(self) -> None:
        """For a pointer brief THAT document is the plan; sizing the pointer alone scores a
        three-migration project as one small slice."""
        from pdca_harness import leaves
        d = Path(tempfile.mkdtemp())
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        self.assertNotIn("planning artifact", leaves._sizer_prompt(d).lower())
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Planning artifact:** docs/migration.md\n", encoding="utf-8")
        self.assertIn("docs/migration.md", leaves._sizer_prompt(d))


class ThirdReviewFixes(unittest.TestCase):
    """Round three on #349."""

    def _brief(self, body: str) -> Path:
        f = Path(tempfile.mkdtemp()) / "brief.md"
        f.write_text(body, encoding="utf-8")
        return f

    def test_markdown_around_the_difficulty_token_is_stripped(self) -> None:
        """Briefs write `low`, **low**, _low_ as readily as a bare word. An unstripped
        token falls through to the prose scan, where "`low` — hard-won but small" reads as
        HIGH — inverting the author's own answer."""
        for value, expected in {"`low` — hard-won but small": 0,
                                "**low** — hard-won": 0,
                                "_medium_ — certificate hardening": 0,
                                "`high` — widest surface": 3,
                                "**hard** problem": 3}.items():
            with self.subTest(difficulty=value):
                f = self._brief(f"- **Slug:** s\n- **Difficulty:** {value}\n")
                self.assertEqual(sizing.estimate(f, _CFG).score, expected)

    def test_a_valid_band_escalates_even_with_an_untidy_schema(self) -> None:
        """Deliberate, and the contract now says so: the band IS the answer this leaf was
        asked for, and the other fields explain it. Discarding a real escalation because
        its explanation was untidy throws away the one signal worth paying a model for —
        and escalate-only means a wrong escalation costs a warning, never a block."""
        base = sizing.SizeEstimate(0, sizing.OK, [], churn_band=sizing.OK,
                                   patch_band=sizing.OK)
        out = sizing.combine(base, {"band": "oversized",
                                    "independent_outcomes": "a,b",   # a string, not a list
                                    "proposed_seams": None,
                                    "confidence": "certain"})        # not low/medium/high
        self.assertEqual(out.band, sizing.OVERSIZED)
        joined = "; ".join(out.reasons)
        self.assertNotIn("outcome(s)", joined,
                         "a malformed field was quoted back into the reasons")
        self.assertNotIn("confidence", joined,
                         "an unrecognised confidence was presented as if it were an answer")

    def test_only_a_recognised_confidence_is_quoted(self) -> None:
        """`null` rendered as "(confidence none)" and "certain" as "(confidence certain)" —
        both read to a human as an answer on the scale the model was asked for, when it
        gave none."""
        base = sizing.SizeEstimate(0, sizing.OK, [], churn_band=sizing.OK,
                                   patch_band=sizing.OK)
        for value in ("certain", None, "", {"level": "high"}):
            with self.subTest(confidence=value):
                out = sizing.combine(base, {"band": "oversized", "confidence": value})
                self.assertNotIn("confidence", "; ".join(out.reasons))
        out = sizing.combine(base, {"band": "oversized", "confidence": "high"})
        self.assertIn("confidence high", "; ".join(out.reasons))

    def test_an_unusable_band_still_changes_nothing(self) -> None:
        """The guarantee that DOES hold, asserted beside the tolerance above so the two
        cannot be confused."""
        base = sizing.SizeEstimate(4, sizing.WATCH, ["structural"],
                                   churn_band=sizing.WATCH, patch_band=sizing.OK)
        for model in (None, {}, {"band": ""}, {"band": "enormous"}, "nope", []):
            with self.subTest(model=model):
                self.assertEqual(sizing.combine(base, model), base)

    def test_a_failed_escalation_restores_the_first_verdict_to_disk(self) -> None:
        """The escalation pass unlinks the artifact before running, so returning the first
        verdict only in memory left the bundle without the sizing record it had earned."""
        from unittest import mock
        from pdca_harness import leaves
        from pdca_harness.config import Config, LeafConfig
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        cfg = Config(root=tmp, bundle_root=tmp / "results", process_dir=tmp / "process",
                     templates_dir=tmp / "templates", default_branch="main",
                     tracker_system="github", tracker_url="", issue_id_example="#1",
                     builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        cfg.sizer = LeafConfig(mode="command", family="generic", argv=["true"])
        cfg.sizer_escalation = [{"on_band": ["watch"], "argv": ["also-true"]}]

        calls = {"n": 0}

        def _fake(leaf, workdir, prompt, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                (Path(workdir) / leaves.SIZING_FILE).write_text(
                    '{"band": "watch", "confidence": "low"}', encoding="utf-8")
            else:
                raise OSError("escalation binary missing")

        with mock.patch.object(leaves, "_invoke", side_effect=_fake):
            verdict = leaves.run_sizer(d, cfg)
        self.assertEqual(verdict["band"], "watch")
        self.assertTrue((d / leaves.SIZING_FILE).exists(),
                        "the first verdict's artifact was destroyed by a failed escalation")

    def _sizer_cfg(self, tmp: Path):
        from pdca_harness.config import Config, LeafConfig
        cfg = Config(root=tmp, bundle_root=tmp / "results", process_dir=tmp / "process",
                     templates_dir=tmp / "templates", default_branch="main",
                     tracker_system="github", tracker_url="", issue_id_example="#1",
                     builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        cfg.sizer = LeafConfig(mode="command", family="generic", argv=["true"])
        return cfg

    def test_one_paid_verdict_per_brief_not_per_beat(self) -> None:
        """The policy is evaluated before Do AND before Check, so a naive re-invoke doubles
        the cost of every cycle — four calls with an escalation — and lets the second
        nondeterministic answer overwrite the first."""
        from unittest import mock
        from pdca_harness import leaves
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        cfg = self._sizer_cfg(tmp)
        calls = {"n": 0}

        def _fake(leaf, workdir, prompt, **kw):
            calls["n"] += 1
            (Path(workdir) / leaves.SIZING_FILE).write_text(
                '{"band": "watch", "confidence": "high"}', encoding="utf-8")

        with mock.patch.object(leaves, "_invoke", side_effect=_fake):
            leaves.run_sizer(d, cfg)          # PLANNED
            leaves.run_sizer(d, cfg)          # BUILT
            self.assertEqual(calls["n"], 1, "the sizer was paid for twice on one brief")
            # An iterate rewrites the brief, which must earn a fresh pass.
            (d / "brief.md").write_text("- **Slug:** s\n- **Difficulty:** high\n",
                                        encoding="utf-8")
            leaves.run_sizer(d, cfg)
            self.assertEqual(calls["n"], 2, "a rewritten brief reused a stale verdict")

    def test_a_verdict_from_another_brief_is_never_reused(self) -> None:
        """The digest subsumes the stale-artifact problem the unconditional unlink guarded:
        a verdict left by a different brief simply does not match."""
        from unittest import mock
        from pdca_harness import leaves
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (d / leaves.SIZING_FILE).write_text(
            '{"band": "oversized", "brief_sha": "deadbeefdeadbeef"}', encoding="utf-8")
        cfg = self._sizer_cfg(tmp)
        with mock.patch.object(leaves, "_invoke") as inv:
            leaves.run_sizer(d, cfg)
        inv.assert_called_once()

    def test_a_pointer_briefs_artifact_is_part_of_the_cache_key(self) -> None:
        """For a pointer brief the artifact IS the plan, so hashing brief.md alone reused
        an `ok` verdict after the artifact grew from one outcome to three — suppressing
        exactly the advisory the pointer case exists to produce."""
        from unittest import mock
        from pdca_harness import leaves
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "plan.md").write_text("one outcome\n", encoding="utf-8")
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Planning artifact:** plan.md\n", encoding="utf-8")
        cfg = self._sizer_cfg(tmp)
        calls = {"n": 0}

        def _fake(leaf, workdir, prompt, **kw):
            calls["n"] += 1
            (Path(workdir) / leaves.SIZING_FILE).write_text('{"band": "ok"}',
                                                            encoding="utf-8")

        with mock.patch.object(leaves, "_invoke", side_effect=_fake):
            leaves.run_sizer(d, cfg)
            leaves.run_sizer(d, cfg)
            self.assertEqual(calls["n"], 1, "an unchanged pointer brief re-paid")
            (d / "plan.md").write_text("one\ntwo\nthree outcomes\n", encoding="utf-8")
            leaves.run_sizer(d, cfg)
            self.assertEqual(calls["n"], 2, "the artifact changed but the verdict was reused")

    def test_an_unfetchable_artifact_is_not_cached(self) -> None:
        """A URL cannot be fingerprinted. Paying for a re-run is the safe direction when
        the alternative is trusting a verdict whose input may have moved."""
        from unittest import mock
        from pdca_harness import leaves
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Planning artifact:** https://example/plan\n",
            encoding="utf-8")
        cfg = self._sizer_cfg(tmp)
        calls = {"n": 0}

        def _fake(leaf, workdir, prompt, **kw):
            calls["n"] += 1
            (Path(workdir) / leaves.SIZING_FILE).write_text('{"band": "ok"}',
                                                            encoding="utf-8")

        with mock.patch.object(leaves, "_invoke", side_effect=_fake):
            leaves.run_sizer(d, cfg)
            leaves.run_sizer(d, cfg)
        self.assertEqual(calls["n"], 2, "an unfingerprintable input was cached anyway")


if __name__ == "__main__":
    unittest.main()
