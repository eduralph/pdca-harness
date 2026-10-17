"""A re-issued stack-mode run's carry walks through a finished id with no patch (#728).

A finished named id with no patch (a close / no-fix disposition) is not a fold candidate,
and the re-issue carry (#646) followed only the plain ``Depends on`` edges landing directly
on one, so an edge running through a finished id with no patch was lost. A dependent naming
such an id was cut from the plain base without the work it reaches, and was not held when
that work was not clean; a finished id built on not-clean work through one was judged clean
and carried, bringing that work onto the line. Such an id is a node of the graph, as it is
for the in-run closure walk (#727): the carry follows plain ``Depends on`` edges through it
for what it carries, what is not clean, and which dependents wait.

These cases drive ``flow._drive_and_act`` through the ``ResumeStackPrereqs`` harness of
tests/test_flow_resume_stack_prereqs.py — loaded by path and used as a helper object, so its
own cases are not collected here — on the ``StackFoldGit`` real-git fixture, with ``gh``
stubbed and the publisher NOT a stub, except in the dry-run case, which prints the plan and
still asks no host and holds nothing. The drive set is the planned ids; the batch is every
id. A patchless id with no prerequisite of its own still carries nothing: that case is the
peer suite's ``test_a_prerequisite_without_a_patch_carries_nothing``.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_carried_prereq_bases
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pdca_harness import integrate, state

_SPEC = importlib.util.spec_from_file_location(
    "resume_stack_prereqs", Path(__file__).with_name("test_flow_resume_stack_prereqs.py"))
rs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rs)
sb = rs.sb                                   # the StackFoldGit fixture module


class CarryThroughPatchless(unittest.TestCase):

    def setUp(self) -> None:
        self.h = rs.ResumeStackPrereqs()     # its real-git setUp, stubs, run and checks
        self.addCleanup(self.h.doCleanups)   # what its setUp registers (the repo's removal)
        self.h.setUp()

    # -- fixture ---------------------------------------------------------------------------

    def _patchless(self, iid: str, prereq: str) -> Path:
        """``iid`` finished in an earlier run with no patch (a close disposition), its brief
        naming ``Depends on: <prereq>``: COMPLETE, and nothing of it for the fold to carry."""
        d = self.h._finished(iid, None, patch=None)
        sb._brief(d, extra=f"- **Depends on:** {prereq}\n")
        sb._accept(d)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertEqual(integrate._fold_candidates([d]), [])
        return d

    def _finished_through(self, iid: str, built_on: str, prereq: str) -> Path:
        """``iid`` finished in an earlier run, its branch cut from ``origin/fix/<built_on>``
        (so it holds that one's commit), its brief restated to ``Depends on: <prereq>``."""
        d = self.h._finished_on(iid, built_on)
        sb._brief(d, extra=f"- **Depends on:** {prereq}\n")
        sb._accept(d)
        self.assertEqual(state.state(d), state.COMPLETE)
        return d

    # -- what the run did ------------------------------------------------------------------

    def _carried(self, *iids: str) -> None:
        """ONE ``flow: carried`` line, naming each of ``iids``."""
        lines = [x for x in self.h.err.splitlines() if x.startswith("flow: carried ")]
        self.assertEqual(len(lines), 1, self.h.err)
        for iid in iids:
            self.assertIn(f"issue_{iid}", lines[0])

    def _holds(self, iid: str, *prereqs: str) -> None:
        """``iid`` was built, not on the plain base, and is COMPLETE; the tip it was built on
        and its own branch ``fix/<iid>`` both hold each of ``prereqs``' branch tips."""
        h = self.h
        base, tip, _line_tip = h._built(iid)
        self.assertNotEqual((base, tip), ("", ""), f"{iid} built on the plain base\n{h.err}")
        for p in prereqs:
            head = h.fx._tip(f"fix/{p}")
            for of in (tip, h.fx._tip(f"fix/{iid}")):
                self.assertTrue(h.fx._ancestor(head, of), f"no fix/{p} in {of}\n{h.err}")
        self.assertEqual(h.results[iid], state.COMPLETE)

    @staticmethod
    def _holding(line: str) -> list[str]:
        """The bundles an "is not carried" line says it holds this run."""
        m = re.search(r" — holding (.+?) this run; ", line)
        return m.group(1).split(", ") if m else []

    def _no_line_holds(self, *iids: str) -> None:
        """No branch under ``pdca-integration/`` on origin — the run's line, or a bundle's
        own base — holds the branch tip of any of ``iids``."""
        h = self.h
        refs = sb._git(h.fx.origin, "for-each-ref", "--format=%(refname:short)",
                       "refs/heads/pdca-integration/").split()
        for ref in refs:
            for iid in iids:
                self.assertFalse(h.fx._ancestor(h.fx._tip(f"fix/{iid}"), ref), (ref, iid))

    # -- (1): a prerequisite reached through a patchless id is carried ---------------------

    def test_a_prerequisite_reached_through_a_patchless_id_is_carried(self):
        # (1) A ← N ← D. Before: D named no fold candidate, so nothing was carried and D
        # was built on the plain base, without A.
        h = self.h
        h._finished("A", {"a.txt": "a\n"})
        self._patchless("N", "A")
        d = h._planned("D", "N")
        h._run([d], ["A", "N", "D"])
        self._carried("A")
        self._holds("D", "A")

    def test_a_chain_of_patchless_ids_is_walked_through(self):
        # (1b) A ← N1 ← N2 ← D, asked for from the far end.
        h = self.h
        h._finished("A", {"a.txt": "a\n"})
        self._patchless("N1", "A")
        self._patchless("N2", "N1")
        d = h._planned("D", "N2")
        h._run([d], ["D", "N2", "N1", "A"])
        self._carried("A")
        self._holds("D", "A")

    # -- (2): a dependent reaching held work through a patchless id waits ------------------

    def test_a_closed_prerequisite_reached_through_a_patchless_id_holds_the_dependent(self):
        # (2) As (1), A's PR closed. Before: nothing held D; it was built on the plain base.
        h = self.h
        h._finished("A", {"a.txt": "a\n"}, pr="CLOSED")
        self._patchless("N", "A")
        d, u = h._planned("D", "N"), h._planned("U")
        h._run([d, u], ["A", "N", "D", "U"])
        h._held("A", "D", "CLOSED")         # D never built, PLANNED; ONE line names both
        self.assertIn("issue_D", self._holding(h._not_carried("A")))
        self.assertEqual([x for x in h.err.splitlines() if x.startswith(
            "flow: issue_N (finished in an earlier run) is not")], [], h.err)
        h._on_base("U")
        self.assertNotIn("STOPPING", h.err)
        self.assertNotIn("flow: carried", h.err)
        self.assertFalse(h.fx._has(h.line))

    # -- (3): not clean spreads through a patchless id -------------------------------------

    def test_one_built_on_a_closed_id_through_a_patchless_id_is_not_carried(self):
        # (3) A (CLOSED) ← N ← P ← D: P's branch is cut from fix/A, so it holds A's closed
        # work. Before: P's one edge, to N, was not followed, so P was judged clean and
        # carried, and D was built on a line holding A's commit.
        h = self.h
        h._finished("A", {"a.txt": "a\n"}, pr="CLOSED")
        self._patchless("N", "A")
        self._finished_through("P", "A", "N")
        d, u = h._planned("D", "P"), h._planned("U")
        h._run([d, u], ["A", "N", "P", "D", "U"])
        line = h._not_carried("P")
        self.assertIn("issue_A", line)
        self.assertIn("built on", line)
        self.assertNotIn("get issue_P's PR open", line)         # not the `open` hint
        self.assertIn("get issue_A carried, or re-drive issue_P on a base without it", line)
        self.assertIn("CLOSED", h._not_carried("A"))
        h._held("P", "D")
        self.assertIn("issue_D", self._holding(line))
        h._on_base("U")
        self.assertNotIn("STOPPING", h.err)
        self.assertNotIn("flow: carried", h.err)
        self._no_line_holds("A", "P")

    # -- (4): guards, green before and after -----------------------------------------------

    def test_a_dependent_through_a_patchless_id_beside_one_naming_the_prerequisite(self):
        # (4a) W names A, so A is carried before #728 too; D, through N, builds on it.
        h = self.h
        h._finished("A", {"a.txt": "a\n"})
        self._patchless("N", "A")
        d, w = h._planned("D", "N"), h._planned("W", "A")
        h._run([d, w], ["A", "N", "D", "W"])
        self._carried("A")
        self._holds("D", "A")
        self._holds("W", "A")

    def test_a_clean_resume_through_a_patchless_id_carries_both_and_stops_nothing(self):
        # (4c) A ← N ← P ← D, all clean, P's branch cut from fix/A: D names P, a fold
        # candidate, so this carried before #728 too. The walk through N must not mark P
        # not clean. No line is on origin yet; the next case has one.
        h = self.h
        h._finished("A", {"a.txt": "a\n"})
        self._patchless("N", "A")
        self._finished_through("P", "A", "N")
        d = h._planned("D", "P")
        h._run([d], ["A", "N", "P", "D"])
        self._carried("A", "P")
        self._holds("D", "A", "P")
        self.assertNotIn("STOPPING", h.err)
        self.assertNotIn("is not carried", h.err)

    def test_a_clean_resume_through_a_patchless_id_continues_the_earlier_line(self):
        # (4c) with the earlier run's line on origin holding A and P, so the foreign-commit
        # check runs: A's and P's heads account for every commit on it, and it is continued.
        h = self.h
        a = h._finished("A", {"a.txt": "a\n"})
        self._patchless("N", "A")
        p = self._finished_through("P", "A", "N")
        d = h._planned("D", "P")
        batch = ["A", "N", "P", "D"]
        line = h._line(batch)
        with redirect_stdout(io.StringIO()):
            integrate.fold(h.cfg, [a, p], folded_this_run={}, batch=batch)  # the earlier run
        old = h.fx._tip(line)
        h._run([d], batch)
        self._carried("A", "P")
        self._holds("D", "A", "P")
        self.assertTrue(h.fx._ancestor(old, line))              # continued, not replaced
        self.assertNotIn("STOPPING", h.err)
        self.assertNotIn("is not carried", h.err)

    def test_a_dry_run_through_a_patchless_id_prints_the_plan_and_holds_nothing(self):
        # A ← N ← D in a dry-run (stub publisher): the carry plan names A, as a run would
        # carry it, but no host is asked and nothing is held, though A's PR is closed.
        h = self.h
        h._finished("A", {"a.txt": "a\n"}, pr="CLOSED")
        self._patchless("N", "A")
        d = h._planned("D", "N")
        h.cfg.publisher = dataclasses.replace(h.cfg.publisher, mode="stub")
        h._run([d], ["A", "N", "D"])
        self.assertEqual(h.gh_calls, [])
        self.assertIn(f"fold 1 patch(es) onto {h.line}", h.out)
        self.assertIn("(issue_A)", h.out)
        self.assertNotIn("not carried", h.err)
        h._on_base("D")
        self.assertFalse(h.fx._has(h.line))


if __name__ == "__main__":
    unittest.main()
