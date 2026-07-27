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


if __name__ == "__main__":
    unittest.main()
