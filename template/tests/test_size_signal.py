"""The empirical Check-time size backstop (issue #324).

The load-bearing assertion in this file is `test_an_oversize_item_disqualifies_autoiterate`.
Everything else is plumbing around it: the backstop's entire mechanism is the HUMAN tag,
and tagged IMPL it would become an *accelerator* for the failure it exists to stop — more
rounds burned re-implementing a slice that needs splitting.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import assemble, autoiterate, driver, gates, size_signal
from pdca_harness.assemble import HUMAN, IMPL, STANDING, NeedsHumanItem
from pdca_harness.config import Config, LeafConfig

_CFG = SimpleNamespace(size_signal={})


def _bundle(*, patch: str | None = None, rounds: int = 0, auto: int = 0) -> Path:
    d = Path(tempfile.mkdtemp())
    if patch is not None:
        (d / "patch.diff").write_text(patch, encoding="utf-8")
    for n in range(1, rounds + 1):
        (d / f"iteration-v{n}").mkdir()
    if auto:
        (d / autoiterate.BUDGET_FILE).write_text(json.dumps({"count": auto}),
                                                 encoding="utf-8")
    return d


def _diff(files: int, kb: int = 0) -> str:
    out = []
    for i in range(files):
        out.append(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n"
                   f"@@ -1 +1 @@\n-old\n+new\n")
    text = "".join(out)
    if kb:
        text += "".join(f"+{'x' * 78}\n" for _ in range((kb * 1024) // 80 + 1))
    return text


class Measure(unittest.TestCase):
    def test_measures_the_four_signals(self) -> None:
        d = _bundle(patch=_diff(3), rounds=2, auto=1)
        sig = size_signal.measure(d)
        self.assertGreater(sig["patch_bytes"], 0)
        self.assertEqual(sig["patch_files"], 3)
        self.assertEqual(sig["rounds"], 2)
        self.assertEqual(sig["auto_iters"], 1)

    def test_a_bundle_with_no_patch_measures_zero_rather_than_raising(self) -> None:
        sig = size_signal.measure(_bundle())
        self.assertEqual(sig["patch_bytes"], 0)
        self.assertEqual(sig["patch_files"], 0)

    def test_an_unparseable_patch_does_not_abort_check(self) -> None:
        """A diff this bundle produced can still be unreadable. An unmeasurable file count
        is a missing signal, not a reason to lose the cycle."""
        d = _bundle(patch="this is not a diff at all\n\x00\x01")
        sig = size_signal.measure(d)
        self.assertGreater(sig["patch_bytes"], 0)
        self.assertEqual(sig["patch_files"], 0)

    def test_rounds_counts_archives_not_files(self) -> None:
        """`iteration-v*` is the same evidence `driver._next_iteration_no` counts, so the
        two cannot disagree — and a stray FILE of that name must not inflate it."""
        d = _bundle(rounds=2)
        (d / "iteration-v9").write_text("not a directory", encoding="utf-8")
        self.assertEqual(size_signal.measure(d)["rounds"], 2)


class RecordAndRead(unittest.TestCase):
    def test_record_writes_and_read_round_trips(self) -> None:
        d = _bundle(patch=_diff(2), rounds=1)
        written = size_signal.record(d, _CFG)
        self.assertTrue((d / size_signal.SIGNAL_FILE).is_file())
        self.assertEqual(size_signal.read(d), written)

    def test_read_of_a_missing_or_garbled_file_is_none_not_empty(self) -> None:
        """None means "not measured", which is NOT "measured and small" — a caller must
        never read an absent file as evidence the bundle is fine."""
        d = _bundle()
        self.assertIsNone(size_signal.read(d))
        (d / size_signal.SIGNAL_FILE).write_text("{not json", encoding="utf-8")
        self.assertIsNone(size_signal.read(d))
        (d / size_signal.SIGNAL_FILE).write_text('["a list"]', encoding="utf-8")
        self.assertIsNone(size_signal.read(d))

    def test_record_returns_the_signal_even_when_the_write_fails(self) -> None:
        """A read-only bundle degrades to "no record", never to "no backstop"."""
        d = _bundle(patch=_diff(30))
        (d / size_signal.SIGNAL_FILE).mkdir()   # write_text will raise OSError
        sig = size_signal.record(d, _CFG)
        self.assertEqual(sig["patch_files"], 30)
        self.assertTrue(size_signal.oversize_reasons(sig, _CFG))


class Thresholds(unittest.TestCase):
    def test_each_threshold_fires_independently(self) -> None:
        cases = [
            ("patch is", size_signal.measure(_bundle(patch=_diff(1, kb=120))), _CFG),
            ("touches", {"patch_files": 25}, _CFG),
            # The rounds rule ships DISABLED, so it needs an explicit threshold to fire.
            ("round(s) already spent", {"rounds": 3},
             SimpleNamespace(size_signal={"rounds": 2})),
        ]
        for needle, sig, cfg in cases:
            with self.subTest(rule=needle):
                joined = "; ".join(size_signal.oversize_reasons(sig, cfg))
                self.assertIn(needle, joined)

    def test_a_small_bundle_fires_nothing(self) -> None:
        sig = size_signal.measure(_bundle(patch=_diff(2), rounds=1))
        self.assertEqual(size_signal.oversize_reasons(sig, _CFG), [])

    def test_an_unmeasured_signal_fires_nothing(self) -> None:
        self.assertEqual(size_signal.oversize_reasons(None, _CFG), [])
        self.assertEqual(size_signal.oversize_reasons({}, _CFG), [])

    def test_every_crossed_rule_is_named_not_just_the_first(self) -> None:
        """"253 KB across 26 files after 2 rounds" is a different conversation from
        "110 KB", and the human is being asked to decide whether to split."""
        reasons = size_signal.oversize_reasons(
            {"patch_bytes": 260 * 1024, "patch_files": 26, "rounds": 2},
            SimpleNamespace(size_signal={"rounds": 2}))
        self.assertEqual(len(reasons), 3)

    def test_config_retunes_the_thresholds(self) -> None:
        cfg = SimpleNamespace(size_signal={"patch_files": 2})
        sig = {"patch_bytes": 0, "patch_files": 3, "rounds": 0}
        self.assertTrue(size_signal.oversize_reasons(sig, cfg))
        self.assertFalse(size_signal.oversize_reasons(sig, _CFG))

    def test_a_malformed_threshold_falls_back_instead_of_raising(self) -> None:
        """This runs inside the Check beat; a typo in an optional tuning table must not
        cost the cycle."""
        cfg = SimpleNamespace(size_signal={"patch_files": "twenty", "nonsense": 1})
        sig = {"patch_bytes": 0, "patch_files": 25, "rounds": 0}
        self.assertTrue(size_signal.oversize_reasons(sig, cfg))

    def test_a_malformed_signal_value_does_not_raise(self) -> None:
        sig = {"patch_bytes": None, "patch_files": "lots", "rounds": []}
        self.assertEqual(size_signal.oversize_reasons(sig, _CFG), [])

    def test_an_absurd_recorded_number_does_not_abort_assembly(self) -> None:
        """JSON parses `1e309` as `inf`, and `int(inf)` raises OverflowError — from a file
        this module wrote itself. Same class as the config fix, a different function."""
        self.assertEqual(size_signal.oversize_reasons(
            {"patch_bytes": 1e309, "patch_files": -1e309, "rounds": 0}, _CFG), [])


class RoundsRuleCutsTheBudgetShort(unittest.TestCase):
    """It fires at 2 while `[driver].max_auto_iters` defaults to 3 — deliberately.

    The budget is a CEILING on how many rebuilds are worth attempting; this is evidence
    that further rebuilds are the wrong move. 76% of bundles sitting at two rounds go on
    to a third, and a third round of implementation findings on a slice that needs
    splitting is the spiral the backstop exists to break. A ceiling and a stop signal are
    different things, and the stop signal wins.

    What must not happen is it winning QUIETLY — see `TheOverrideAnnouncesItself`.
    """

    def test_two_rounds_raises_the_item(self) -> None:
        reasons = size_signal.oversize_reasons(
            {"rounds": 2, "patch_bytes": 0, "patch_files": 0}, _CFG)
        self.assertEqual(len(reasons), 1)
        self.assertIn("2 round(s) already spent", reasons[0])

    def test_it_fires_below_the_default_auto_iterate_ceiling(self) -> None:
        """Pinned against `max_auto_iters`' own default so the two cannot drift apart: if
        that default ever drops to 2, this rule stops adding anything and the interaction
        needs rethinking rather than silently becoming a no-op."""
        from pdca_harness.config import Config as _C
        ceiling = _C.__dataclass_fields__["max_auto_iters"].default
        threshold = size_signal.DEFAULT_THRESHOLDS["rounds"]
        self.assertLess(threshold, ceiling,
                        "the rounds rule no longer stops the loop before the budget does")

    def test_one_round_is_still_below_it(self) -> None:
        """A bundle on its first rebuild must not trip it — that is an ordinary iterate."""
        self.assertEqual(size_signal.oversize_reasons(
            {"rounds": 1, "patch_bytes": 0, "patch_files": 0}, _CFG), [])

    def test_an_instance_can_turn_it_off(self) -> None:
        cfg = SimpleNamespace(size_signal={"rounds": 0})
        self.assertEqual(size_signal.oversize_reasons(
            {"rounds": 9, "patch_bytes": 0, "patch_files": 0}, cfg), [])

    def test_zero_disables_any_rule(self) -> None:
        cfg = SimpleNamespace(size_signal={"patch_kb": 0, "patch_files": 0, "rounds": 0})
        self.assertEqual(size_signal.oversize_reasons(
            {"patch_bytes": 999 * 1024, "patch_files": 99, "rounds": 9}, cfg), [])


class TheOverrideAnnouncesItself(unittest.TestCase):
    """An operator who set `max_auto_iters = 3` and sees the loop halt at 2 has to be able
    to read why, or the number they configured simply appears not to work."""

    def test_the_size_item_is_recognisable_to_the_flow(self) -> None:
        text = size_signal.needs_human_text(["2 round(s) already spent (threshold 2)"])
        self.assertTrue(size_signal.is_size_item(text))
        self.assertFalse(size_signal.is_size_item("C5 Causal adequacy — a real concern"))
        self.assertFalse(size_signal.is_size_item(""))

    def test_the_flow_names_the_rule_when_it_declines(self) -> None:
        """The whole point of re-enabling this rule rather than leaving it off: the
        override is fine, the SILENCE was not."""
        from pdca_harness import flow
        items = [NeedsHumanItem("a real defect", IMPL),
                 NeedsHumanItem(size_signal.needs_human_text(
                     ["2 round(s) already spent (threshold 2)"]), HUMAN)]
        err = io.StringIO()
        with mock.patch.object(flow.assemble, "collect_needs_human",
                               lambda d, cfg: items), \
             mock.patch.object(flow.state, "state",
                               lambda d: flow.state.AWAITING_SIGNOFF), \
             redirect_stderr(err):
            fired = flow._maybe_auto_iterate(
                SimpleNamespace(auto_iterate=True, max_auto_iters=3),
                Path("/tmp/issue_1"), by="t", today="2026-07-28", apply_now=False)
        self.assertFalse(fired)
        out = err.getvalue()
        self.assertIn("not auto-iterating", out)
        self.assertIn("2 round(s) already spent", out)
        self.assertIn("iterate-plan", out)

    def test_an_ordinary_human_finding_declines_without_the_extra_line(self) -> None:
        """Every other decline is a §6 item the human is about to read anyway; narrating
        those too would bury the one message that carries new information."""
        from pdca_harness import flow
        items = [NeedsHumanItem("a real defect", IMPL),
                 NeedsHumanItem("C5 Causal adequacy — a real concern", HUMAN)]
        err = io.StringIO()
        with mock.patch.object(flow.assemble, "collect_needs_human",
                               lambda d, cfg: items), \
             mock.patch.object(flow.state, "state",
                               lambda d: flow.state.AWAITING_SIGNOFF), \
             redirect_stderr(err):
            flow._maybe_auto_iterate(
                SimpleNamespace(auto_iterate=True, max_auto_iters=3),
                Path("/tmp/issue_1"), by="t", today="2026-07-28", apply_now=False)
        self.assertEqual(err.getvalue(), "")


class Wording(unittest.TestCase):
    def test_it_recommends_iterate_plan_and_names_the_wrong_answer(self) -> None:
        """The wrong answer is the plausible one: findings on an oversized slice look
        implementation-shaped every round, so `iterate-do` reads as correct."""
        text = size_signal.needs_human_text(["patch is 253 KB (threshold 100 KB)"])
        self.assertIn("iterate-plan", text)
        self.assertIn("iterate-do", text)
        self.assertIn("pdca split", text)
        self.assertIn("253 KB", text)


class DisqualifiesAutoIterate(unittest.TestCase):
    """The assertion #324 is named for."""

    def test_an_oversize_item_disqualifies_autoiterate(self) -> None:
        impl_only = [NeedsHumanItem("a real defect", IMPL),
                     NeedsHumanItem("Validation — fitness-to-purpose", STANDING)]
        self.assertTrue(autoiterate.eligible(impl_only),
                        "precondition: this set is otherwise eligible")

        backstop = NeedsHumanItem(
            size_signal.needs_human_text(["patch is 253 KB (threshold 100 KB)"]), HUMAN)
        self.assertFalse(autoiterate.eligible(impl_only + [backstop]),
                         "the backstop must STOP the rebuild loop, not feed it")

    def test_the_tag_is_the_mechanism(self) -> None:
        """Tagged IMPL the identical text becomes a reason to rebuild — the backstop
        inverted into an accelerator for the failure it exists to stop. Asserted directly
        so the tag can never be 'simplified'."""
        text = size_signal.needs_human_text(["patch is 253 KB (threshold 100 KB)"])
        self.assertFalse(autoiterate.eligible([NeedsHumanItem("defect", IMPL),
                                               NeedsHumanItem(text, HUMAN)]))
        self.assertTrue(autoiterate.eligible([NeedsHumanItem("defect", IMPL),
                                              NeedsHumanItem(text, IMPL)]),
                        "if this ever fails, IMPL no longer feeds auto-iterate and the "
                        "comment explaining why the tag matters is stale")


class ReachesSectionSix(unittest.TestCase):
    """The unit assertions above prove the tag disqualifies auto-iterate. This proves the
    item actually ARRIVES there — `collect_needs_human` is the single source for both the
    rendered §6 and the C6 accept-guard, so an item that never reaches it is a backstop
    that fires into nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp,
            bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process",
            templates_dir=self.tmp / "templates",
            default_branch="main",
            tracker_system="github",
            tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub", family="claude"),
            reviewer=LeafConfig(mode="stub", family="codex"),
        )
        self.cfg.gates_checks = [{"id": "t", "element": "unit tests",
                                  "cmd": "true", "gating": True}]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _checked_bundle(self, patch: str) -> Path:
        d = self.cfg.bundle("issue_1")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** x\n", encoding="utf-8")
        (d / "patch.diff").write_text(patch, encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            gates.run_gates(d, self.cfg)
        return d

    def test_an_oversize_bundle_raises_a_human_item_in_section_six(self) -> None:
        d = self._checked_bundle(_diff(30))
        size_signal.record(d, self.cfg)
        items = [i for i in assemble.collect_needs_human(d, self.cfg)
                 if "size backstop" in i.text]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, HUMAN)
        self.assertIn("iterate-plan", items[0].text)
        self.assertFalse(autoiterate.eligible(assemble.collect_needs_human(d, self.cfg)))

    def test_a_small_bundle_raises_nothing(self) -> None:
        d = self._checked_bundle(_diff(1))
        size_signal.record(d, self.cfg)
        self.assertFalse([i for i in assemble.collect_needs_human(d, self.cfg)
                          if "size backstop" in i.text])

    def test_a_bundle_with_no_recorded_file_is_MEASURED_not_skipped(self) -> None:
        """The file is a RECORD, not the source of truth. Reading it as the source of
        truth meant an unwritable `size-signal.json` deleted the backstop: `_size_backstop`
        warned from the in-memory signal at Check while `collect_needs_human` found
        nothing, so an oversized bundle with an IMPL finding auto-iterated anyway."""
        d = self._checked_bundle(_diff(30))
        self.assertFalse((d / size_signal.SIGNAL_FILE).exists())
        items = [i for i in assemble.collect_needs_human(d, self.cfg)
                 if "size backstop" in i.text]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, HUMAN)

    def test_a_bundle_whose_write_FAILED_still_raises_the_item(self) -> None:
        """The failure codex found, pinned end to end."""
        d = self._checked_bundle(_diff(30))
        (d / size_signal.SIGNAL_FILE).mkdir()          # write_text will raise OSError
        size_signal.record(d, self.cfg)
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertTrue([i for i in items if "size backstop" in i.text])
        self.assertFalse(autoiterate.eligible(items),
                         "an unwritable record deleted the backstop and let the rebuild "
                         "loop continue")

    def test_a_small_bundle_with_no_record_still_raises_nothing(self) -> None:
        """The complement — falling back to measuring must not invent a finding."""
        d = self._checked_bundle(_diff(1))
        self.assertFalse([i for i in assemble.collect_needs_human(d, self.cfg)
                          if "size backstop" in i.text])

    def test_the_driver_records_the_signal_at_check(self) -> None:
        """Wiring assertion: the file is written by the Check beat, not by assembly."""
        d = self._checked_bundle(_diff(30))
        with redirect_stderr(io.StringIO()) as err:
            driver._size_backstop(d, self.cfg)
        self.assertTrue((d / size_signal.SIGNAL_FILE).is_file())
        self.assertIn("size backstop", err.getvalue())


class CodexReviewFixes(unittest.TestCase):
    """Findings from the codex review of this PR."""

    def test_an_infinite_threshold_does_not_abort_check(self) -> None:
        """`int(float("inf"))` raises OverflowError, which is NOT a TypeError or a
        ValueError — and TOML writes `inf` as a bare literal, so `patch_kb = inf` (a
        plausible way to try to switch a rule off) aborted the Check beat. The docstring
        promised a typo in an optional tuning table would not cost the cycle."""
        for value in (float("inf"), float("-inf"), "twenty", None, [1]):
            with self.subTest(value=value):
                cfg = SimpleNamespace(size_signal={"patch_kb": value})
                self.assertEqual(
                    size_signal.oversize_reasons({"patch_bytes": 0, "patch_files": 0,
                                                  "rounds": 0}, cfg), [])

    def test_the_same_hole_is_closed_in_the_a_priori_estimator(self) -> None:
        """`sizing._cfg_int` and `sizing._weights` carried the identical defect, so an
        `inf` in [driver.sizing] aborted the PLAN beat the same way."""
        from pdca_harness import sizing
        cfg = SimpleNamespace(sizing={"watch": float("inf"),
                                      "difficulty_high": float("-inf")})
        self.assertEqual(sizing._cfg_int(cfg, "watch", 4), 4)
        self.assertEqual(sizing._weights(cfg)["difficulty_high"],
                         sizing.DEFAULT_WEIGHTS["difficulty_high"])

    def test_the_signal_is_archived_with_the_attempt_it_measured(self) -> None:
        """It is measured FROM patch.diff, which an iterate archives. Left behind it would
        describe an attempt that is no longer there, and the archive of a rejected attempt
        would lack the numbers that justified rejecting it."""
        from pdca_harness import driver, state
        self.assertIn(size_signal.SIGNAL_FILE, state.DOWNSTREAM_OF_BRIEF)
        d = _bundle(patch=_diff(30))
        size_signal.record(d, _CFG)
        driver._archive_iteration(d, 1, include_brief=False)
        self.assertFalse((d / size_signal.SIGNAL_FILE).exists())
        self.assertTrue((d / "iteration-v1" / size_signal.SIGNAL_FILE).is_file())

    def test_a_bundle_holding_one_counts_as_having_run_a_cycle(self) -> None:
        """The other half of DOWNSTREAM_OF_BRIEF membership: it is written at Check, so a
        bundle holding one has demonstrably run Do and Check, and a tracker `resolved`
        marker must not silently abandon it."""
        from pdca_harness import state
        d = _bundle()
        self.assertFalse(state.has_cycle_evidence(d))
        size_signal.record(d, _CFG)
        self.assertTrue(state.has_cycle_evidence(d))


class TheShippedExampleMatchesTheDefaults(unittest.TestCase):
    """The commented `[driver.size_signal]` block in pdca.toml is presented as "the
    defaults, uncomment to retune". If it drifts, uncommenting it CHANGES behaviour while
    looking like it preserves it — and it had already drifted: the block still said
    `rounds = 0` after the default became 2, so anyone uncommenting it to adjust
    `patch_kb` would have silently switched the rounds rule off."""

    def _template_config(self) -> str:
        root = Path(__file__).resolve().parents[1]
        for name in ("pdca.toml.jinja", "pdca.toml"):
            p = root / name
            if p.is_file():
                return p.read_text(encoding="utf-8")
        raise AssertionError("no pdca.toml template found")

    def test_every_commented_threshold_equals_its_default(self) -> None:
        import re as _re
        text = self._template_config()
        block = text.split("[driver.size_signal]", 1)
        self.assertEqual(len(block), 2, "the example block is gone from pdca.toml")
        found = dict(_re.findall(r"^#\s*(\w+)\s*=\s*(-?\d+)\s*$",
                                 block[1], _re.MULTILINE))
        self.assertTrue(found, "the example block declares no thresholds")
        for key, value in found.items():
            with self.subTest(key=key):
                self.assertIn(key, size_signal.DEFAULT_THRESHOLDS,
                              f"{key} is not a real threshold")
                self.assertEqual(int(value), size_signal.DEFAULT_THRESHOLDS[key],
                                 f"the shipped example sets {key}={value} but the default "
                                 f"is {size_signal.DEFAULT_THRESHOLDS[key]}")

    def test_every_default_appears_in_the_example(self) -> None:
        """The other direction: a threshold added in code and not documented is one an
        instance cannot discover."""
        import re as _re
        block = self._template_config().split("[driver.size_signal]", 1)[1]
        found = dict(_re.findall(r"^#\s*(\w+)\s*=\s*(-?\d+)\s*$", block, _re.MULTILINE))
        self.assertEqual(set(found), set(size_signal.DEFAULT_THRESHOLDS))


class RoundsAreAttributedToTheCURRENTBrief(unittest.TestCase):
    """An iterate-to-PLAN archives brief.md and the bundle is re-specified from scratch.

    Counting every `iteration-v*` charged a predecessor's churn to a brief that never
    caused it — and the failure is circular: a bundle re-planned BECAUSE this backstop
    recommended it started its new spec already over the threshold, so its very first
    Check raised "2 rounds already spent" and recommended the re-plan that had just
    happened.
    """

    def _replanned(self) -> Path:
        from pdca_harness import driver
        d = _bundle(patch="x")
        (d / "brief.md").write_text("- **Slug:** first\n", encoding="utf-8")
        driver._archive_iteration(d, 1, include_brief=False)     # iterate-do
        (d / "patch.diff").write_text("y", encoding="utf-8")
        driver._archive_iteration(d, 2, include_brief=True)      # iterate-PLAN
        (d / "brief.md").write_text("- **Slug:** respecified\n", encoding="utf-8")
        (d / "patch.diff").write_text("z", encoding="utf-8")
        return d

    def test_a_replan_resets_the_count(self) -> None:
        sig = size_signal.measure(self._replanned())
        self.assertEqual(sig["rounds"], 0)
        self.assertEqual(sig["replans"], 1)

    def test_the_new_spec_does_not_fire_on_its_first_attempt(self) -> None:
        sig = size_signal.measure(self._replanned())
        self.assertEqual(size_signal.oversize_reasons(sig, _CFG), [],
                         "the bundle was told to split again on the spec that split "
                         "produced")

    def test_rounds_since_the_replan_still_count(self) -> None:
        """The complement: resetting must not blind the backstop to real churn on the new
        brief."""
        from pdca_harness import driver
        d = self._replanned()
        driver._archive_iteration(d, 3, include_brief=False)
        (d / "patch.diff").write_text("q", encoding="utf-8")
        driver._archive_iteration(d, 4, include_brief=False)
        sig = size_signal.measure(d)
        self.assertEqual(sig["rounds"], 2)
        self.assertTrue(size_signal.oversize_reasons(sig, _CFG))

    def test_an_iterate_do_history_is_unaffected(self) -> None:
        from pdca_harness import driver
        d = _bundle(patch="x")
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        for n in (1, 2):
            driver._archive_iteration(d, n, include_brief=False)
            (d / "patch.diff").write_text("x", encoding="utf-8")
        self.assertEqual(size_signal.measure(d)["rounds"], 2)

    def test_the_calibrator_uses_THIS_definition(self) -> None:
        """The thresholds were derived with the calibrator's counter. A runtime counting
        anything else measures a different quantity from the one the numbers describe —
        the #355 defect, one milestone later. Asserted by import, not by comparison."""
        script = (Path(__file__).resolve().parents[1] / "scripts" / "size-calibrate")
        text = script.read_text(encoding="utf-8")
        self.assertIn("from pdca_harness.size_signal import iteration_rounds", text)
        self.assertNotIn("def iteration_rounds", text,
                         "the calibrator has its own copy again")

    def test_a_stray_file_named_like_an_archive_is_ignored(self) -> None:
        d = _bundle()
        (d / "iteration-v1").write_text("not a directory", encoding="utf-8")
        self.assertEqual(size_signal.iteration_rounds(d), (0, 0))


if __name__ == "__main__":
    unittest.main()
