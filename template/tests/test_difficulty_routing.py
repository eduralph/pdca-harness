"""Difficulty-routed builder backend (issue #134, stdlib unittest).

select_builder routes the Do backend per bundle from the brief's Difficulty field
([[leaves.builder_variant]] with when={field,substring}, mirroring the advisory leaf
gate), so a cheap local model handles easy bundles while a stronger model is reserved
for the hard ones. Routing is default-open (a missing/unknown tag keeps the default,
never silently reducing capability), and the #135 escalation ladder overrides the
difficulty pick so a looping bundle still escalates.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import Config, LeafConfig

HARD = {"family": "frontier", "argv": ["frontier-build"],
        "when": {"field": "difficulty", "substring": "high"}}
NOOP = [sys.executable, "-c", "pass"]


def _cfg(root: Path, **kw) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=kw.pop("builder", LeafConfig(mode="command", family="local", argv=["local-build"])),
        reviewer=LeafConfig(mode="stub"),
        **kw,
    )


class DifficultyRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, difficulty: str | None) -> Path:
        d = self.tmp / f"issue_{difficulty}"
        d.mkdir(parents=True)
        body = "- **Slug:** s\n"
        if difficulty is not None:
            body += f"- **Difficulty:** {difficulty}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_high_difficulty_routes_to_the_variant(self) -> None:
        cfg = _cfg(self.tmp, builder_variants=[HARD])
        self.assertEqual(leaves.select_builder(self._bundle("high"), cfg, 1).family, "frontier")

    def test_default_open_on_low_unknown_or_missing(self) -> None:
        cfg = _cfg(self.tmp, builder_variants=[HARD])
        self.assertEqual(leaves.select_builder(self._bundle("low"), cfg, 1).family, "local")
        self.assertEqual(leaves.select_builder(self._bundle("weird"), cfg, 1).family, "local")
        self.assertEqual(leaves.select_builder(self._bundle(None), cfg, 1).family, "local")

    def test_first_matching_variant_wins(self) -> None:
        first = {"family": "mid", "argv": ["mid"],
                 "when": {"field": "difficulty", "substring": "high"}}
        cfg = _cfg(self.tmp, builder_variants=[first, HARD])
        self.assertEqual(leaves.select_builder(self._bundle("high"), cfg, 1).family, "mid")

    def test_escalation_overrides_the_difficulty_pick(self) -> None:
        # A bundle self-rated "low" routes to the default, but once it iterates the
        # escalation ladder takes over regardless of the difficulty tag (#135 caps the
        # downside of a mis-rated "low").
        cfg = _cfg(
            self.tmp,
            builder_variants=[HARD],
            builder_escalation=[{"min_iteration": 2, "family": "escalated", "argv": ["esc"]}])
        d = self._bundle("low")
        self.assertEqual(leaves.select_builder(d, cfg, 1).family, "local")      # attempt 1
        self.assertEqual(leaves.select_builder(d, cfg, 2).family, "escalated")  # iterated

    def test_escalation_overrides_even_a_matched_variant(self) -> None:
        cfg = _cfg(
            self.tmp,
            builder_variants=[HARD],
            builder_escalation=[{"min_iteration": 3, "family": "escalated", "argv": ["esc"]}])
        d = self._bundle("high")
        self.assertEqual(leaves.select_builder(d, cfg, 1).family, "frontier")   # variant
        self.assertEqual(leaves.select_builder(d, cfg, 3).family, "escalated")  # escalation wins

    def test_do_build_dispatches_on_the_routed_builder_mode(self) -> None:
        # Default is stub, but a high-difficulty variant is a command builder. do_build
        # must run the routed COMMAND backend (it writes loop-telemetry.json), not fall
        # through to the stub because cfg.builder.mode is stub (Codex review, PR #146).
        cfg = _cfg(
            self.tmp,
            builder=LeafConfig(mode="stub", family="local", argv=[]),
            builder_variants=[{"mode": "command", "family": "frontier", "argv": NOOP,
                               "when": {"field": "difficulty", "substring": "high"}}])
        d = self._bundle("high")
        leaves.do_build(d, cfg)
        self.assertTrue((d / "loop-telemetry.json").exists())  # the command path ran

    def test_shared_when_predicate(self) -> None:
        # #152: ONE when={field,substring} matcher, two empty-when defaults. The substring
        # match itself is identical regardless of the caller.
        d = self._bundle("high")
        match = {"field": "difficulty", "substring": "high"}
        miss = {"field": "difficulty", "substring": "low"}
        self.assertTrue(leaves._when_matches(match, d, default=False))
        self.assertFalse(leaves._when_matches(miss, d, default=False))
        # An empty / absent condition yields the caller's default.
        self.assertFalse(leaves._when_matches({}, d, default=False))
        self.assertTrue(leaves._when_matches({}, d, default=True))
        self.assertTrue(leaves._when_matches(None, d, default=True))

    def test_both_gates_delegate_to_the_shared_predicate(self) -> None:
        # _variant_applies and _advisory_applies are now thin wrappers over _when_matches
        # (no second implementation of when-matching, #152): same match, different default
        # for an empty `when` (variant opts out; advisory runs).
        d = self._bundle("high")
        match = {"when": {"field": "difficulty", "substring": "high"}}
        self.assertTrue(leaves._variant_applies(match, d))
        self.assertTrue(leaves._advisory_applies(match, d))
        self.assertFalse(leaves._variant_applies({}, d))   # variant is opt-in
        self.assertTrue(leaves._advisory_applies({}, d))    # advisory is default-on

    # --- #167: explicit per-bundle Do model, matched by a variant's `model` key ---

    def _model_bundle(self, model: str | None, *, difficulty: str | None = None) -> Path:
        d = self.tmp / f"issue_m_{model}_{difficulty}"
        d.mkdir(parents=True)
        body = "- **Slug:** s\n"
        if difficulty is not None:
            body += f"- **Difficulty:** {difficulty}\n"
        if model is not None:
            body += f"- **Do model:** {model}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def test_explicit_do_model_selects_named_variant(self) -> None:
        # #167: `Do model: <name>` picks the variant whose `model` matches, no `when` gate.
        variants = [{"family": "frontier", "model": "frontier", "argv": ["f"]},
                    {"family": "local-big", "model": "local", "argv": ["l"]}]
        cfg = _cfg(self.tmp, builder_variants=variants)
        self.assertEqual(leaves.select_builder(self._model_bundle("frontier"), cfg, 1).family, "frontier")
        self.assertEqual(leaves.select_builder(self._model_bundle("local"), cfg, 1).family, "local-big")

    def test_explicit_do_model_overrides_when_routing(self) -> None:
        # difficulty=high would route to HARD/frontier via `when`, but the brief pins
        # `Do model: local` — the explicit choice wins over `when`.
        variants = [HARD, {"family": "local-big", "model": "local", "argv": ["l"]}]
        cfg = _cfg(self.tmp, builder_variants=variants)
        d = self._model_bundle("local", difficulty="high")
        self.assertEqual(leaves.select_builder(d, cfg, 1).family, "local-big")

    def test_unknown_do_model_falls_back_to_when_routing(self) -> None:
        # A `Do model` naming no variant `model` is a no-op → fall back to `when` routing.
        cfg = _cfg(self.tmp, builder_variants=[HARD])
        d = self._model_bundle("nonexistent", difficulty="high")
        self.assertEqual(leaves.select_builder(d, cfg, 1).family, "frontier")

    def test_escalation_overrides_explicit_do_model(self) -> None:
        # Even an explicit Do model is overridden by the escalation ladder on iterate.
        cfg = _cfg(
            self.tmp,
            builder_variants=[{"family": "frontier", "model": "frontier", "argv": ["f"]}],
            builder_escalation=[{"min_iteration": 2, "family": "escalated", "argv": ["e"]}])
        d = self._model_bundle("frontier")
        self.assertEqual(leaves.select_builder(d, cfg, 1).family, "frontier")    # explicit pick
        self.assertEqual(leaves.select_builder(d, cfg, 2).family, "escalated")   # escalation wins


if __name__ == "__main__":
    unittest.main()
