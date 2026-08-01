"""The Act-index sizing column + configurable model weight (issue #359).

#359 closes the calibration loop #320/#324 opened: the a-priori estimate and the measured
outcome are JOINED per frozen bundle in the Act index — the review that already looks
across cycles — so estimator drift is seen there instead of waiting for someone to
re-derive the numbers by hand (the 0.56 67%→62% episode). Proven here:

* ``act.index`` carries the estimate (brief-derived, a priori) beside the outcome read
  from the bundle's RECORDED ``size-signal.json``, and ``act.render_index`` prints them;
* a bundle predating the signal renders a graceful blank — never an outcome fabricated
  by measuring whatever is on disk at review time;
* ``model_weight`` is read from ``[driver.sizing]`` (``cfg.sizing``), defaults to 0 —
  today's band-only behaviour — and stays escalate-only;
* the retuning walk from ``size-calibrate`` output back into ``[driver.sizing]`` is
  documented in the config template's comment block;
* (iteration 2) one garbled recorded value — an int too large for float — blanks its
  own outcome cell instead of aborting the whole index, and the retuning walk names
  the model-verdict blind spot so a review cannot mistake it for evidence.

Offline, stdlib only. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import act, signoff, sizing
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _config_template() -> str:
    """The shipped config, template-repo or rendered instance — same fallback as
    test_size_signal's example-block guard, because this suite runs in BOTH (the render
    test re-runs it inside a rendered instance, where the file is `pdca.toml`)."""
    root = Path(__file__).resolve().parents[1]
    for name in ("pdca.toml.jinja", "pdca.toml"):
        p = root / name
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise AssertionError("no pdca.toml template found")


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
    )


#: A brief with a real a-priori footprint (difficulty + a declared conflict), so the
#: estimate joined into the index is visibly non-trivial rather than a degenerate 0/ok.
_BRIEF = (
    "- **Slug:** act-sizing\n"
    "- **Difficulty:** high — wide surface\n"
    "- **Conflicts with:** #311\n"
)


def _freeze(cfg: Config, iid: str, *, date: str = "2026-07-01",
            patch: str = "diff --git a/x b/x\n") -> Path:
    """A COMPLETE (frozen) bundle with an accepted §9 — the only material Act reads."""
    d = cfg.bundle(iid)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(_BRIEF, encoding="utf-8")
    (d / "patch.diff").write_text(patch, encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    signoff.record(d / "SUMMARY.md", action="accept", by="T", date=date)
    return d


class IndexSizingColumn(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_joins_estimate_and_recorded_outcome(self) -> None:
        """Criterion (a): the a-priori estimate beside the measured outcome, per bundle."""
        d = _freeze(self.cfg, "42")
        (d / "size-signal.json").write_text(json.dumps(
            {"patch_bytes": 34816, "patch_files": 3, "rounds": 1,
             "replans": 0, "auto_iters": 0}), encoding="utf-8")

        [entry] = act.index(self.cfg)
        # The estimate side is the brief-derived a-priori estimate — the same one the
        # driver computes — asserted via the estimator itself so this binds the JOIN,
        # not the estimator's internal numerology.
        est = sizing.estimate(d / "brief.md", self.cfg)
        expected_est = f"{est.band} (score {est.score})"
        self.assertNotEqual(est.band, sizing.OK)  # the fixture brief really scores
        self.assertEqual(entry.size_estimate, expected_est)
        self.assertEqual(entry.size_outcome, "34 KB / 3 file(s) / 1 round(s)")

        out = act.render_index([entry], act.patterns([entry]))
        self.assertIn(
            f"- sizing: estimate {expected_est} → outcome 34 KB / 3 file(s) / 1 round(s)",
            out)

    def test_bundle_predating_signal_renders_blank_never_a_measurement(self) -> None:
        """Criterion (a), the blank half: no recorded signal ⇒ '—', and NEVER an outcome
        measured at review time — the bundle carries a 2 KB patch that a fallback
        measurement would happily report, and reporting it would fake a record #324
        never made."""
        _freeze(self.cfg, "7", patch="diff --git a/x b/x\n" + "+" * 2048 + "\n")

        [entry] = act.index(self.cfg)
        self.assertEqual(entry.size_outcome, "")
        out = act.render_index([entry], act.patterns([entry]))
        self.assertIn("- sizing: estimate ", out)
        self.assertIn("→ outcome —", out)
        self.assertNotIn("2 KB", out)

    def test_overflowing_recorded_value_blanks_outcome_never_aborts_index(self) -> None:
        """Iteration 2 (T3): a recorded value can parse as a Python int yet exceed float
        range, so it sails past the int() guard and the `/ 1024` division raises
        OverflowError. One garbled size-signal.json must cost its own outcome cell —
        the criterion's graceful blank, same reading as "predates the signal" — never
        abort the whole index. The healthy sibling bundle is the point: it proves the
        index SURVIVES the garbled record, not merely that one entry reads blank."""
        bad = _freeze(self.cfg, "13")
        (bad / "size-signal.json").write_text(json.dumps(
            {"patch_bytes": 10 ** 400, "patch_files": 3, "rounds": 1}), encoding="utf-8")
        good = _freeze(self.cfg, "42")
        (good / "size-signal.json").write_text(json.dumps(
            {"patch_bytes": 34816, "patch_files": 3, "rounds": 1}), encoding="utf-8")

        entries = act.index(self.cfg)  # pre-fix this raises OverflowError
        by_name = {e.bundle.name: e for e in entries}
        self.assertEqual(by_name["issue_13"].size_outcome, "")
        self.assertNotEqual(by_name["issue_13"].size_estimate, "")  # estimate survives
        self.assertEqual(by_name["issue_42"].size_outcome, "34 KB / 3 file(s) / 1 round(s)")

        out = act.render_index(entries, act.patterns(entries))
        self.assertIn("→ outcome —", out)  # the garbled record reads "not measured"
        self.assertIn("→ outcome 34 KB / 3 file(s) / 1 round(s)", out)


class ModelWeight(unittest.TestCase):
    """Criterion (c): `model_weight` is `[driver.sizing]` config, defaulting to today's
    behaviour, and escalate-only like the rest of `combine`'s contract."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.structural = sizing.SizeEstimate(
            3, sizing.WATCH, ["difficulty=high"],
            churn_band=sizing.WATCH, patch_band=sizing.OK)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_current_behaviour_band_only(self) -> None:
        # No config, and config without the key: the model escalates the BAND and the
        # score stays purely structural — byte-for-byte today's behaviour.
        for cfg in (None, self.cfg):
            est = sizing.combine(self.structural, {"band": "oversized"}, cfg)
            self.assertEqual(est.band, sizing.OVERSIZED)
            self.assertEqual(est.score, 3)
        self.assertEqual(sizing.DEFAULT_MODEL_WEIGHT, 0)

    def test_configured_weight_joins_score_on_escalation(self) -> None:
        self.cfg.sizing = {"model_weight": 5}
        est = sizing.combine(self.structural, {"band": "watch"}, self.cfg)
        self.assertEqual(est.score, 8)
        self.assertEqual(est.band, sizing.WATCH)

    def test_ok_verdict_carries_no_weight(self) -> None:
        # The weight is "how much a model ESCALATION is worth" — an `ok` verdict flags
        # nothing, so it adds nothing regardless of configuration.
        self.cfg.sizing = {"model_weight": 5}
        est = sizing.combine(self.structural, {"band": "ok"}, self.cfg)
        self.assertEqual(est.score, 3)

    def test_weight_is_escalate_only_and_typo_tolerant(self) -> None:
        # A negative weight would let the model LOWER a structural score — the exact
        # single-point-of-failure `combine` exists to forbid — so it clamps to 0. A
        # malformed value falls back to the default rather than aborting the beat,
        # the same tolerance as every other `[driver.sizing]` key.
        for bad in (-3, "lots"):
            self.cfg.sizing = {"model_weight": bad}
            est = sizing.combine(self.structural, {"band": "oversized"}, self.cfg)
            self.assertEqual(est.score, 3, f"model_weight={bad!r} moved the score")

    def test_missing_verdict_untouched_with_weight_configured(self) -> None:
        self.cfg.sizing = {"model_weight": 5}
        self.assertIs(sizing.combine(self.structural, None, self.cfg), self.structural)


class RetuningDocs(unittest.TestCase):
    def _sizing_block(self) -> str:
        """The `[driver.sizing]` example block, scoped to end where the NEXT sub-table's
        section begins — the `[driver.size_signal]` guard in test_size_signal.py scans
        from ITS header to EOF, so the two blocks must not read each other's keys."""
        parts = _config_template().split("# [driver.sizing]", 1)
        self.assertEqual(len(parts), 2, "the [driver.sizing] example block is missing")
        return parts[1].split("[driver.size_signal]", 1)[0]

    def test_config_template_documents_the_retuning_walk(self) -> None:
        """Criterion (b): the `[driver.sizing]` comment block every rendered instance
        gets walks `size-calibrate` output back into the table, and defines
        `model_weight` with its Act-cadence review noted."""
        text = _config_template()
        self.assertIn("# [driver.sizing]", text)
        self.assertIn("size-calibrate", text)
        self.assertIn("model_weight", text)
        self.assertIn("Act cadence", text)

    def test_retuning_walk_names_the_model_verdict_blind_spot(self) -> None:
        """Iteration 2 (C5): the index's sizing line is structural-only and the
        calibrator mines no model-verdict feature, so escalation-vs-outcome correlation
        is unobservable today. The walk must SAY so where `model_weight` is documented —
        otherwise an Act-cadence review reads the loop as licensing a weight change
        that no shipped artifact can evidence."""
        text = _config_template()
        self.assertIn("model-verdict", text)
        self.assertIn("escalation-vs-outcome", text)

    def test_shipped_example_matches_the_defaults_both_ways(self) -> None:
        """Same guard the `[driver.size_signal]` example carries: the block is presented
        as 'the defaults, uncomment to retune', so a drifted value CHANGES behaviour
        while looking like it preserves it — and a default absent from the example is
        one an instance cannot discover."""
        import re as _re
        defaults = dict(sizing.DEFAULT_WEIGHTS)
        defaults.update(brief_bytes_kb=sizing.DEFAULT_BRIEF_KB,
                        watch=sizing.DEFAULT_WATCH,
                        oversized=sizing.DEFAULT_OVERSIZED,
                        model_weight=sizing.DEFAULT_MODEL_WEIGHT)
        found = dict(_re.findall(r"^#\s*(\w+)\s*=\s*(-?\d+)\s*$",
                                 self._sizing_block(), _re.MULTILINE))
        self.assertEqual(set(found), set(defaults))
        for key, value in found.items():
            with self.subTest(key=key):
                self.assertEqual(int(value), defaults[key])


if __name__ == "__main__":
    unittest.main()
