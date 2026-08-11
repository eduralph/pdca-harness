"""An honest split-child advisory, with an escape hatch that actually works (#458).

`plan_policy.size_reasons` answered EVERY oversized split child with the same `consider
`pdca split` first` its parent got — advice keyed on the one readout a split inflates, so
each level of a recursion saw the same inputs and gave the same answer. The fix consumes
the count `sizing` already publishes for exactly this: ``SizeEstimate.sibling_conflicts``
(#457), the `Conflicts with` entries naming this bundle's own split siblings, excluded
from the score and reported beside it.

Three properties the fix has to hold together, because two earlier attempts each held one
and broke another:

1. **The count, not the lineage record's presence.** Every split child carries lineage
   forever, including one whose conflicts are all organic — keying on presence printed
   "driven by inherited/sibling fields" over a child's own contradicting
   `4 conflict(s) declared`.
2. **Asked BEFORE the readout fork.** With sibling conflicts no longer scored (#457), a
   sibling-carrying child routinely lands `churn=watch` / `patch=oversized`, so a
   provenance branch nested inside the `splittable` fork is unreachable for precisely the
   bundles it exists for.
3. **Reachable on the sizer this project SHIPS.** `[leaves.sizer]` ships `mode = "stub"`
   and ``leaves._stub_sizer`` returns `{"band": "ok"}` unconditionally, so a recovery
   gated on the sizer's own verdict is dead config offline. The hatch here is the brief's
   own evidence: drop the stale sibling entries and the ordinary remedy returns, with no
   paid sizer anywhere in the path.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves, plan_policy, sizing, split
from pdca_harness.config import Config, LeafConfig

_ID = "601"
_PARENT = "500"
_SIBLINGS = ["602", "603"]
_DEPTH = 1

#: The provenance sentence criterion (i) names, spelled out rather than rebuilt from the
#: production helper: an assertion that calls the code under test to compute its own
#: expectation passes whatever that code says.
_PROVENANCE = ("scores large for a split child (child 601 of a split of #500, depth 1) — "
               "driven by inherited/sibling fields; prefer building over re-splitting")
_ORDINARY = "consider `pdca split` first"


def _child_brief(conflicts: str, *, ext_deps: bool = False) -> str:
    """A materialised split child's brief: the parent's `Difficulty` and size inherited,
    plus whatever `Conflicts with` it declares. 4000 `pad ` repetitions clear the 12 KB
    brief-size cutoff, the same shape `test_size_guard._OVERSIZED` uses."""
    return (
        "- **Slug:** split-child\n"
        "- **Difficulty:** high\n"
        f"- **Conflicts with:** {conflicts}\n"
        + ("- **External dependencies:** `docker`, `protoc`\n" if ext_deps else "")
        + "- **Scope:** " + ("pad " * 4000) + "\n"
    )


class SplitChildAdvisory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / f"issue_{_ID}"
        self.d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, guard: str = "warn") -> Config:
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        cfg.size_guard = guard
        # `sizer` is left at its shipped default — LeafConfig(mode="stub") — so every call
        # below really runs `leaves._stub_sizer`. Nothing in this module mocks it out.
        return cfg

    def _brief(self, conflicts: str, *, ext_deps: bool = False) -> None:
        (self.d / "brief.md").write_text(_child_brief(conflicts, ext_deps=ext_deps),
                                         encoding="utf-8")

    def _lineage(self, **over) -> None:
        """The record `split.materialise` writes into each child (`split.py:493-499`)."""
        record = {"version": split.LINEAGE_VERSION, "id": _ID, "parent": _PARENT,
                  "siblings": list(_SIBLINGS), "depth": _DEPTH, **over}
        (self.d / split.LINEAGE).write_text(json.dumps(record, indent=2, sort_keys=True)
                                            + "\n", encoding="utf-8")

    def _estimate(self, cfg: Config, *, before_do: bool = True) -> sizing.SizeEstimate:
        """The same estimate the policy sees — through the same two production calls."""
        verdict = (leaves.run_sizer(self.d, cfg) if before_do
                   else leaves.current_sizing(self.d, cfg))
        return sizing.combine(sizing.estimate(self.d / "brief.md", cfg), verdict, cfg)

    # -- (i) an oversized score carried by sibling conflicts names its provenance --------

    def test_i_sibling_carried_score_names_the_provenance_not_pdca_split(self) -> None:
        """Child 601's `Conflicts with` names only its own siblings, and what is left of
        its size is the parent's `Difficulty`, dependency tokens and brief. The advisory
        must say so — not hand back the same `pdca split` its parent was given."""
        self._brief("602, 603", ext_deps=True)
        self._lineage()
        cfg = self._cfg()

        est = self._estimate(cfg)
        # The shape that makes this the load-bearing red: churn itself is oversized, so
        # `splittable` is True and the unfixed code takes the `pdca split` branch.
        self.assertEqual(est.sibling_conflicts, 2)
        self.assertEqual(est.churn_band, sizing.OVERSIZED)

        detail = self._one(plan_policy.size_reasons(self.d, cfg, before_do=True))

        self.assertIn(_PROVENANCE, detail)
        self.assertNotIn(_ORDINARY, detail)
        # The evidence the message rests on, in the message: `sizing` dropped these from
        # the score, so nothing else in the line accounts for them.
        self.assertIn("2 sibling conflict(s) not counted", detail)

    def test_i_a_patch_only_child_reaches_the_same_branch(self) -> None:
        """The shape #457 leaves behind, and the one a provenance test nested inside the
        `splittable` fork silently stops covering: with the sibling conflicts excluded the
        score falls to 6, so churn reads `watch` and only the patch readout is oversized.
        The child's size is no less inherited for that."""
        self._brief("602")
        self._lineage()
        cfg = self._cfg()

        est = self._estimate(cfg)
        self.assertEqual(est.sibling_conflicts, 1)
        self.assertEqual((est.churn_band, est.patch_band), (sizing.WATCH, sizing.OVERSIZED))
        self.assertEqual(est.band, sizing.OVERSIZED)

        detail = self._one(plan_policy.size_reasons(self.d, cfg, before_do=True))

        self.assertIn(_PROVENANCE, detail)
        self.assertNotIn("expect a large patch", detail)
        self.assertIn("1 sibling conflict(s) not counted", detail)

    def test_i_a_mixed_child_discloses_both_counts(self) -> None:
        """One sibling entry among four organic ones. The count is non-zero, so the split
        this child came from demonstrably separated nothing and re-splitting is still the
        wrong advice — but the message may not claim more than it can show, so BOTH
        numbers appear: the organic conflicts `sizing` scored, and the sibling one it did
        not. That pairing is the whole of the first failure this fix exists for."""
        self._brief("602, 811, 812, 813, 814")
        self._lineage()
        cfg = self._cfg()

        self.assertEqual(self._estimate(cfg).sibling_conflicts, 1)

        detail = self._one(plan_policy.size_reasons(self.d, cfg, before_do=True))

        self.assertIn(_PROVENANCE, detail)
        self.assertIn("4 conflict(s) declared", detail)
        self.assertIn("1 sibling conflict(s) not counted", detail)

    # -- (ii) zero sibling conflicts: the ordinary remedy, unchanged ---------------------

    def test_ii_organic_conflicts_keep_the_ordinary_split_remedy(self) -> None:
        """Four conflicts, none of them a sibling: this child's size is its own, and it is
        still a split candidate. Keying on the lineage record's presence told it the
        opposite — in the same string as its own `4 conflict(s) declared`."""
        self._brief("811, 812, 813, 814")
        self._lineage()
        cfg = self._cfg()

        est = self._estimate(cfg)
        self.assertEqual(est.sibling_conflicts, 0)

        detail = self._one(plan_policy.size_reasons(self.d, cfg, before_do=True))

        self.assertIn(_ORDINARY, detail)
        self.assertIn("4 conflict(s) declared", detail)
        self.assertNotIn("driven by inherited/sibling fields", detail)
        self.assertNotIn("scores large for a split child", detail)
        self.assertNotIn("sibling conflict(s) not counted", detail)

    # -- (iii) and it is reachable on the sizer this project actually ships --------------

    def test_iii_the_ordinary_remedy_returns_under_the_shipped_stub_sizer(self) -> None:
        """The escape hatch, exercised end to end through `plan_policy.evaluate` — the
        entry `driver.advance` calls — with `[leaves.sizer] mode = "stub"` untouched.

        Same bundle, same lineage record, one edit: the siblings have landed and their
        ordering entries leave the brief. The suppression must lift, on an instance that
        never bought a `mode = "command"` sizer. A recovery gated on `model_band` could
        not pass this: `leaves._stub_sizer` answers `ok` unconditionally.
        """
        cfg = self._cfg()
        self.assertEqual(cfg.sizer.mode, "stub",
                         "this test only means something against the shipped default")

        self._brief("602, 603")
        self._lineage()
        suppressed = self._one(plan_policy.evaluate(self.d, cfg, before_do=True))
        self.assertIn(_PROVENANCE, suppressed)

        # Not a mock: the real `_stub_sizer` ran and left its artifact, and its `ok` band
        # is what `combine` folded in — so no model escalation is holding either leg up.
        verdict = json.loads((self.d / "sizing.json").read_text(encoding="utf-8"))
        self.assertEqual(verdict.get("band"), "ok")
        self.assertTrue(verdict.get("stub"))
        self.assertNotEqual(self._estimate(cfg).model_band, sizing.OVERSIZED)

        # The hatch: the stale sibling entries go, the size that remains is this brief's.
        self._brief("811, 812, 813, 814")
        recovered = self._one(plan_policy.evaluate(self.d, cfg, before_do=True))

        self.assertIn(_ORDINARY, recovered)
        self.assertNotIn("driven by inherited/sibling fields", recovered)
        self.assertTrue(json.loads((self.d / "sizing.json").read_text(encoding="utf-8"))
                        .get("stub"), "the stub sizer was replaced mid-test")

    # -- (iv) before_do=False still routes through iterate-plan -------------------------

    def test_iv_a_built_bundle_still_gets_the_iterate_plan_wording(self) -> None:
        """A bundle that already has a patch is told to re-plan, provenance or not: a
        split authors briefs, so `pdca split` is not on offer at BUILT — and the re-plan
        lands back at the branch that does the naming."""
        self._brief("602, 603", ext_deps=True)
        self._lineage()
        cfg = self._cfg()

        detail = self._one(plan_policy.size_reasons(self.d, cfg, before_do=False))

        self.assertIn("answer `iterate-plan` at sign-off", detail)
        self.assertNotIn("pdca split` first", detail)
        self.assertNotIn("driven by inherited/sibling fields", detail)
        self.assertNotIn("sibling conflict(s) not counted", detail)

    # -- (v) the same one sentence reaches both prompts ---------------------------------

    def test_v_both_prompts_gain_the_same_provenance_note_and_nothing_else(self) -> None:
        """The planner and the splitter read the brief before the advisory runs again, so
        the context travels with the prompt. Built twice off ONE bundle — the only
        difference between the runs is the lineage record — so the equality below is an
        exact "nothing else was reworded"."""
        self._brief("811, 812")           # organic: the estimate is identical either way
        cfg = self._cfg()

        plan_before = leaves._plan_prompt(cfg, None, self.d)
        split_before = leaves._split_prompt(self.d, cfg)
        self._lineage()
        plan_after = leaves._plan_prompt(cfg, None, self.d)
        split_after = leaves._split_prompt(self.d, cfg)

        note = leaves._split_provenance_note(self.d)
        self.assertIn(f"split child of #{_PARENT}", note)
        self.assertIn("SIBLINGS", note)
        self.assertIn("not by itself a reason to split again", note)

        for label, before, after in (("plan", plan_before, plan_after),
                                     ("split", split_before, split_after)):
            with self.subTest(prompt=label):
                self.assertNotIn("split child of #", before)
                self.assertIn(note.strip(), after)
                self.assertEqual(after.replace(note, ""), before)

        # The instructions the note sits next to are untouched.
        self.assertIn("SPLIT IT IN THIS BEAT", plan_after)
        self.assertIn("The `Depends on:` / `Conflicts with:` fields BETWEEN children are "
                      "the point.", split_after)

    def test_v_a_bundle_that_is_not_a_split_child_gets_no_note(self) -> None:
        """`children` without `parent` is the parent half of the same record shape
        (`split.py:436-446`) — a bundle that HAS been split, not one that came from a
        split. It inherited nothing, so there is nothing to say."""
        self._brief("811, 812")
        cfg = self._cfg()
        (self.d / split.LINEAGE).write_text(
            json.dumps({"version": split.LINEAGE_VERSION, "id": _ID,
                        "children": ["701", "702"]}) + "\n", encoding="utf-8")

        self.assertEqual(leaves._split_provenance_note(self.d), "")
        self.assertNotIn("split child of #", leaves._plan_prompt(cfg, None, self.d))
        self.assertNotIn("split child of #", leaves._split_prompt(self.d, cfg))

    # -- (vi) no lineage at all: byte-identical to today --------------------------------

    def test_vi_a_bundle_with_no_lineage_is_byte_identical(self) -> None:
        """The regression guard for every bundle that was never split — the overwhelming
        majority. Full-string equality, so a stray clause fails it as loudly as a reworded
        remedy would. `602` is a sibling id in the fixtures above and organic here: with
        no record to read it against, it scores exactly as it always did."""
        self._brief("602")
        cfg = self._cfg()
        self.assertIsNone(split.read_lineage(self.d))

        detail = self._one(plan_policy.size_reasons(self.d, cfg, before_do=True))

        est = self._estimate(cfg)
        self.assertEqual(detail,
                         f"oversized — {_ORDINARY} ({'; '.join(est.reasons)})")

    # -- helper -------------------------------------------------------------------------

    def _one(self, reasons) -> str:
        """The single `oversized` advisory's detail line."""
        self.assertEqual([r.code for r in reasons], ["oversized"])
        return reasons[0].detail


if __name__ == "__main__":
    unittest.main()
