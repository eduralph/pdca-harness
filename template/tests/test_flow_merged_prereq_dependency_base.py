"""A merged prerequisite is judged on the base its dependent is built on (#729).

In stack mode a dependent ``D`` of an out-of-batch prerequisite ``P`` — a ``Depends on
(merged): P``, or a plain ``Depends on: P`` the run was not asked for (#647) — waits until
``P``'s PR merges into ``D``'s own target base (``merged.merged_into``). Since #727 a ``D``
no seam keeps on the run's line is not cut from that line: it is built on a base of its own
(``integrate.build_bundle_base``, started from the base as fetched when ``D``'s wave
starts), or, when none of the bundles it reaches has work on its target, on the real base.
Either has ``P``'s merge once ``P`` is judged merged. So a merge after the run's first
fold, which the line (started before it) lacks — #655's case — does not hold ``D``: ``D``
is built, on a base holding ``P``'s merge commit. A ``P`` not merged into the base still
holds ``D``. A ``D`` a seam keeps on the line is still judged on the line:
tests/test_flow_merged_prereq_on_line.py.

These cases drive ``flow._drive_and_act`` against real git through the helpers of
tests/test_flow_merged_prereq_on_line.py (its ``StackFoldGit`` fixture, its ``gh`` stub and
its stubbed build and publish leaves), loaded by path and used as a helper object, as that
suite uses ``StackFoldGit``. Only modules are imported (never a symbol the fix adds), so
with the production change reverted these cases still load and run — and fail on their
assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_merged_prereq_dependency_base
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from pdca_harness import integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "merged_prereq_on_line_helpers",
    Path(__file__).with_name("test_flow_merged_prereq_on_line.py"))
ol = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ol)


class MergedPrereqOffTheLine(unittest.TestCase):

    def setUp(self) -> None:
        self.h = ol.MergedPrereqOnTheLine()   # a helper object: its fixture, stubs and runs
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    # -- helpers ---------------------------------------------------------------------------

    def _merged_after_the_first_fold(self, drive: list[Path], batch: list[str]) -> None:
        """One run driving ``drive``, asked for ``batch``: ``P`` finished in an earlier run,
        and its PR merges into main after the run's first fold. W is COMPLETE, and the run's
        line — started by W's fold, before the merge — has neither P's merge commit nor P's
        head (#655's case)."""
        h = self.h
        h._prereq()
        h.after_first_fold = h._merge
        h._run(drive, batch)
        self.assertEqual(h.results["W"], state.COMPLETE, h.err)
        self.assertTrue(h.fx._has(h.line), h.err)
        self.assertTrue(h.fx._ancestor(h.fx._tip("fix/W"), h.line))
        self.assertFalse(h.fx._ancestor(h.merge_oid["P"], h.line))
        self.assertFalse(h.fx._ancestor(h.fx._tip("fix/P"), h.line))

    def _not_held(self) -> None:
        """No stderr line holds D or skips it."""
        self.assertNotIn("issue_D held", self.h.err)
        self.assertNotIn("issue_D skipped", self.h.err)

    def _closes_with_no_patch(self, iid: str) -> None:
        """When built, ``iid`` closes with no patch and is accepted, and publish pushes no
        branch of it, as #727's suite builds such a bundle: no fold carries it, and the base
        of a bundle that depends on it takes nothing of it."""
        h, name = self.h, f"issue_{iid}"
        drive_wave, publish_bundle = h._drive_wave, h._publish_bundle

        def drive(cfg, wave: list[Path], **kw) -> int:
            for d in wave:
                if d.name == name:
                    h.seen[name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d))
                    (d / state.CLOSE_MARKER).write_text("likely-close\n", encoding="utf-8")
                    ol.sb._accept(d)
            return drive_wave(cfg, [d for d in wave if d.name != name], **kw)

        def publish_(cfg, d: Path, **kw) -> bool:
            return False if d.name == name else publish_bundle(cfg, d, **kw)

        h._drive_wave, h._publish_bundle = drive, publish_   # what `h._run` patches in

    # -- (1) merged after the first fold: D is built on a base that has the merge ----------

    def _built_on_its_own_base_with_the_merge(self, edge: str) -> None:
        h = self.h
        self._merged_after_the_first_fold(h._wd(edge), ["W", "D"])
        self._not_held()
        # D is built on a base of its own — the branch `bundle_base_branch` names for it,
        # not the line — at the tip origin has for it, which holds P's merge commit and W.
        self.assertIn("issue_D", h.seen, h.err)
        base, tip = h.seen["issue_D"]
        self.assertEqual(base, integrate.bundle_base_branch(h.cfg, "main", ["W", "D"],
                                                            "issue_D"), h.err)
        self.assertEqual(h.fx._tip(base), tip, h.err)
        self.assertTrue(h.fx._ancestor(h.merge_oid["P"], tip))
        self.assertTrue(h.fx._ancestor(h.fx._tip("fix/W"), tip))
        self.assertEqual(h.results["D"], state.COMPLETE, h.err)

    def test_a_merge_after_the_first_fold_builds_a_depends_on_merged_dependent(self):
        self._built_on_its_own_base_with_the_merge(ol.MERGED_EDGE)

    def test_a_merge_after_the_first_fold_builds_a_plain_depends_on_dependent(self):
        # The #647 edge: a plain `Depends on: P`, P not in the batch.
        self._built_on_its_own_base_with_the_merge(ol.PLAIN_EDGE)

    def test_a_merge_after_the_first_fold_builds_a_dependent_on_the_real_base(self):
        # D's only prerequisite this run built, N, closes with no patch, so D's own base
        # would take nothing: D is built on the real base (#727), which has P's merge. W,
        # unrelated, gives D's target a line, and that line lacks the merge.
        h = self.h
        self._closes_with_no_patch("N")
        d = h._planned("D", "- **Depends on:** N\n- **Depends on (merged):** P\n")
        self._merged_after_the_first_fold([h._planned("W"), h._planned("N"), d],
                                          ["W", "N", "D"])
        self.assertEqual(h.results["N"], state.COMPLETE, h.err)
        self._not_held()
        self.assertIn("issue_D", h.seen, h.err)
        self.assertEqual(h.seen["issue_D"], ("", ""), h.err)   # no stack base: the real base
        self.assertEqual(publish.read_stack_base(d), "")
        self.assertTrue(h.fx._ancestor(h.merge_oid["P"], "main"))
        self.assertEqual(h.results["D"], state.COMPLETE, h.err)

    # -- (2) not merged: still held (#647) -------------------------------------------------

    def _held_unmerged(self, edge: str) -> None:
        h = self.h
        h._prereq()                            # P's PR stays OPEN
        h._run(h._wd(edge), ["W", "D"])
        self.assertEqual(h.results["W"], state.COMPLETE, h.err)
        self.assertNotIn("issue_D", h.seen, h.err)
        self.assertEqual(h.results["D"], state.PLANNED)
        lines = [x for x in h.err.splitlines() if "issue_D held" in x and "issue_P" in x]
        self.assertEqual(len(lines), 1, h.err)
        self.assertIn("not merged into main", lines[0])

    def test_an_unmerged_depends_on_merged_prerequisite_still_holds_the_dependent(self):
        self._held_unmerged(ol.MERGED_EDGE)

    def test_an_unmerged_plain_depends_on_prerequisite_still_holds_the_dependent(self):
        self._held_unmerged(ol.PLAIN_EDGE)


if __name__ == "__main__":
    unittest.main()
