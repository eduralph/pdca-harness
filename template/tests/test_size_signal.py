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


class RoundsRuleShipsDisabled(unittest.TestCase):
    """It is the most precise rule of the three and it still ships off.

    `[driver].max_auto_iters` defaults to 3. With `rounds` at 2 the backstop raises a HUMAN
    item after the second archive, auto-iterate declines, and a budget of 3 can never be
    spent — an explicit operator setting silently overridden by a heuristic, with nothing
    naming the rule that changed it.
    """

    def test_two_rounds_alone_raises_nothing_by_default(self) -> None:
        self.assertEqual(
            size_signal.oversize_reasons({"rounds": 2, "patch_bytes": 0,
                                          "patch_files": 0}, _CFG), [])

    def test_the_default_leaves_the_auto_iterate_budget_intact(self) -> None:
        """The regression this default exists to prevent, pinned directly: a bundle part-way
        through its auto-iterate budget must still look clean to the backstop."""
        from pdca_harness.config import Config as _C
        default_budget = _C.__dataclass_fields__["max_auto_iters"].default
        for spent in range(default_budget):
            with self.subTest(rounds=spent):
                sig = {"rounds": spent, "patch_bytes": 0, "patch_files": 0}
                self.assertEqual(size_signal.oversize_reasons(sig, _CFG), [])

    def test_but_it_still_gets_measured_and_recorded(self) -> None:
        """Recorded even though it raises nothing — #359 retunes against this file, and a
        signal that is never written cannot be calibrated later."""
        d = _bundle(rounds=2)
        self.assertEqual(size_signal.record(d, _CFG)["rounds"], 2)
        self.assertEqual(size_signal.read(d)["rounds"], 2)

    def test_an_instance_can_turn_it_on(self) -> None:
        cfg = SimpleNamespace(size_signal={"rounds": 2})
        self.assertTrue(size_signal.oversize_reasons(
            {"rounds": 2, "patch_bytes": 0, "patch_files": 0}, cfg))

    def test_zero_disables_any_rule(self) -> None:
        cfg = SimpleNamespace(size_signal={"patch_kb": 0, "patch_files": 0})
        self.assertEqual(size_signal.oversize_reasons(
            {"patch_bytes": 999 * 1024, "patch_files": 99, "rounds": 0}, cfg), [])


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

    def test_a_bundle_never_measured_raises_nothing(self) -> None:
        """No `size-signal.json` — an older bundle, or one whose write failed. Absence
        must not be read as either "fine" or "oversized"."""
        d = self._checked_bundle(_diff(30))
        self.assertFalse((d / size_signal.SIGNAL_FILE).exists())
        self.assertFalse([i for i in assemble.collect_needs_human(d, self.cfg)
                          if "size backstop" in i.text])

    def test_the_driver_records_the_signal_at_check(self) -> None:
        """Wiring assertion: the file is written by the Check beat, not by assembly."""
        d = self._checked_bundle(_diff(30))
        with redirect_stderr(io.StringIO()) as err:
            driver._size_backstop(d, self.cfg)
        self.assertTrue((d / size_signal.SIGNAL_FILE).is_file())
        self.assertIn("size backstop", err.getvalue())


if __name__ == "__main__":
    unittest.main()
