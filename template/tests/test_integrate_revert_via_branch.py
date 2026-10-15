"""A bundle's branch that brings a revert of work on the line stops the fold (#673).

#623 made a fold stop before it pushes when taking the base in would remove work a bundle
put on the line. A bundle's own branch was merged with no such check, so a branch cut from
(or updated from) a base that had reverted a merged PR brought that revert onto the line
when it was folded, and the fold pushed a line without that work. These cases pin the fix:
every bundle-branch merge of a fold is judged as the base merge is. The merge is undone and
the fold stops (under the carry's ``skipped``, only that bundle is left out), naming the
paths, whose work it would remove and the bundle whose branch brings the change.

A removal the branch's own commits make is the bundle's change, and it is merged: a
dependent that deletes the file on purpose, also after "Update branch" from the reverted
base. That is judged by the commit that gave the branch's tip its content at the path,
never by whether some commit of the branch touched the path: a branch that edits the file
and puts it back, or one cut from another bundle's branch (whose commit added the file),
still only brings the base's revert in. So does one cut from a base that took the revert
in through a revert PR, where the revert commit is not on the base's first-parent chain.

Who a refusal names is decided as the base merge decides it (the two share the code). A
branch that holds another bundle's tip, by any path (cut from that branch, or updated from
a base that merged it, also after that branch moved on), holds that bundle's commits, which
are not its work. A commit two branches share while neither holds the other's tip counts
for both: git cannot tell which branch it was made on. A branch's refusal says so, naming
such a commit as work from one or the other. It never names its own bundle as it names
the others, which would read as if its merge removed its own work: work the branch put on
the line earlier, and no longer has, is named as just that.

Real git: the ``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare
``origin``, a primary checkout and a ``human`` clone that merges and reverts on ``main``),
loaded by path and used as a helper object, as tests/test_flow_same_wave_conflict.py does —
never subclassed, which would re-run that suite's tests here. Only modules are imported
(never a symbol the fix adds), so with the production change reverted these cases still
load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_integrate_revert_via_branch
"""

from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pdca_harness import integrate

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TARGET = sb.TARGET
LINE = sb.LINE


class RevertBroughtByABundleBranch(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg

    # -- histories -------------------------------------------------------------------------

    def _a_on_the_line(self) -> tuple[Path, str]:
        """A adds a.txt and is folded: the run's first fold. Returns (A, the line tip T1)."""
        a = self.fx._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        return a, self.fx._tip(LINE)

    def _merge_and_revert_a(self) -> str:
        """The maintainer merges A's PR into main (its branch kept), then reverts that merge
        on main. Returns the revert commit."""
        self.fx._merge_pr("A")
        return self.fx._revert_on_main("-m", "1", "HEAD")

    def _revert_a_through_a_pr(self) -> str:
        """GitHub's "Revert" button on A's merged PR: the revert is committed on a branch of
        its own, off main, and that PR merges into main with a merge commit. The revert is
        then on main, but not on main's first-parent chain. Returns the revert commit."""
        self.fx._merge_pr("A")
        h = self.fx.human
        sb._git(h, "checkout", "-q", "-B", "fix/revert-A", "origin/main")
        sb._git(h, "revert", "--no-edit", "-m", "1", "HEAD")
        revert = sb._git(h, "rev-parse", "HEAD").strip()
        sb._git(h, "push", "-q", "origin", "fix/revert-A")
        self.fx._merge_pr("revert-A")
        return revert

    def _commit_on(self, branch: str, *, write: dict[str, str] | None = None,
                   delete: tuple[str, ...] = ()) -> str:
        """The branch's author commits on ``branch`` (from the ``human`` clone): writes
        ``write``, deletes ``delete``, pushes. Returns the commit."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", branch, f"origin/{branch}")
        for name, text in (write or {}).items():
            (h / name).write_text(text, encoding="utf-8")
        if delete:
            sb._git(h, "rm", "-q", *delete)
        sb._git(h, "add", "--all")
        sb._git(h, "commit", "-q", "-s", "-m", f"commit on {branch}")
        sb._git(h, "push", "-q", "origin", branch)
        return sb._git(h, "rev-parse", "HEAD").strip()

    # -- assertions ------------------------------------------------------------------------

    def _has_file(self, rev: str, path: str) -> bool:
        return sb._run(self.fx.origin, "cat-file", "-e", f"{rev}:{path}").returncode == 0

    def _assert_refused(self, bundles: list[Path], t1: str) -> str:
        """The in-run fold of ``bundles``, continuing from ``t1``, stops on B's branch: its
        own message (not the base merge's) names a.txt, issue_A and issue_B, and nothing is
        pushed. Returns the message."""
        with redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, bundles, folded_this_run={TARGET: t1})
        msg = str(ctx.exception)
        for part in ("a.txt (issue_A)", "issue_A's work", "issue_B's branch origin/fix/B"):
            self.assertIn(part, msg)
        self.assertNotIn("taking origin/main in would remove", msg)
        self.assertEqual(self.fx._tip(LINE), t1, "the fold pushed")
        self.assertTrue(self._has_file(t1, "a.txt"))
        return msg

    def _assert_carry_skips_b(self, bundles: list[Path], t1: str, revert: str) -> None:
        """The carry's fold of ``bundles`` (A, B, then D cut from the line), continuing from
        ``t1`` with ``skipped``, leaves out B alone, naming a.txt and issue_A. It goes on to
        merge D and pushes a line on top of T1 with a.txt and d.txt, without B and without
        the revert."""
        skipped: dict[str, str] = {}
        with redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, bundles, folded_this_run={TARGET: t1}, skipped=skipped)
        self.assertEqual(list(skipped), ["issue_B"], skipped)
        self.assertIn("a.txt (issue_A)", skipped["issue_B"])
        tip = self.fx._tip(LINE)
        self.assertTrue(self.fx._ancestor(t1, tip), "the line was reset below T1")
        self.assertEqual(self.fx._show(tip, "a.txt"), "a\n")
        self.assertEqual(self.fx._show(tip, "d.txt"), "d\n")
        self.assertFalse(self._has_file(tip, "b.txt"), "B reached the line")
        self.assertFalse(self.fx._ancestor(revert, tip), "the revert reached the line")

    def _assert_refused_then_carry_skips_b(self, a: Path, b: Path, t1: str,
                                           revert: str) -> None:
        """Both legs on one history: D is published off the line, the in-run fold of
        [A, B, D] stops on B and pushes nothing, then the carry's fold leaves out B alone."""
        d = self.fx._publish("D", {"d.txt": "d\n"}, cut_from=f"origin/{LINE}")
        self._assert_refused([a, b, d], t1)
        self._assert_carry_skips_b([a, b, d], t1, revert)

    def _assert_merged(self, bundles: list[Path], t1: str, iid: str) -> str:
        """The fold of ``bundles``, continuing from ``t1``, merges ``iid``'s branch and
        pushes. Returns the line's new tip."""
        with redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, bundles, folded_this_run={TARGET: t1})
        tip = self.fx._tip(LINE)
        self.assertNotEqual(tip, t1, "the fold pushed nothing")
        self.assertTrue(self.fx._ancestor(self.fx._tip(f"fix/{iid}"), tip))
        return tip

    # -- (1) a branch cut from a reverted base ---------------------------------------------

    def test_a_branch_cut_from_a_base_that_reverted_line_work_stops_the_fold(self) -> None:
        # Criterion (1). B is cut from main after main merged and reverted A. A is in the
        # line already, so the fold merges only B, whose merge base with the line is A's
        # commit: B's side deletes a.txt. Before the fix the line was pushed without it.
        a, t1 = self._a_on_the_line()
        self._merge_and_revert_a()
        b = self.fx._publish("B", {"b.txt": "b\n"})
        self._assert_refused([a, b], t1)

    # -- (2) under the carry's `skipped`, the bundle is skipped, not the fold --------------

    def test_the_carry_skips_only_the_branch_that_brings_the_revert(self) -> None:
        # Criterion (2). D, cut from the line, is folded after B: the fold goes on to merge
        # it and pushes a line with a.txt and d.txt, without B and without the revert.
        a, t1 = self._a_on_the_line()
        revert = self._merge_and_revert_a()
        b = self.fx._publish("B", {"b.txt": "b\n"})
        d = self.fx._publish("D", {"d.txt": "d\n"}, cut_from=f"origin/{LINE}")
        self._assert_carry_skips_b([a, b, d], t1, revert)

    # -- (2b) a branch UPDATED from the reverted base --------------------------------------

    def test_a_branch_updated_from_a_base_that_reverted_line_work_stops_the_fold(
            self) -> None:
        # Criterion (2b). B is cut from main before A merged; then main merges and reverts
        # A, and B is updated from main (GitHub's "Update branch": a merge commit on fix/B).
        # That merge commit is on B's branch, but the deletion it brings is main's.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"})
        self._merge_and_revert_a()
        self.fx._update_branch("B")
        self._assert_refused([a, b], t1)

    def test_a_branch_cut_from_the_line_then_updated_from_the_reverted_base_stops(
            self) -> None:
        # B is cut from the line, so it has a.txt; then it is updated from the reverted
        # main. The update merge's merge base is A's commit, so on fix/B's first-parent
        # chain the update merge itself deletes a.txt: judged against its first parent
        # only, it would pass for B's own deletion. It took a.txt from main, though.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        revert = self._merge_and_revert_a()
        self.fx._update_branch("B")
        self._assert_refused_then_carry_skips_b(a, b, t1, revert)

    # -- a commit of the branch that touched the path is not the removal -------------------

    def test_a_branch_cut_from_another_bundles_branch_then_updated_stops(self) -> None:
        # B is cut from fix/A, so A's commit that adds a.txt is in B's history, reached
        # through B's own commit, not through the line's or main's first-parent chain. It
        # added the file; the deletion the update merge brings is main's revert. Nothing
        # B committed removes a.txt, so the fold stops.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from="origin/fix/A")
        revert = self._merge_and_revert_a()
        self.fx._update_branch("B")
        self._assert_refused_then_carry_skips_b(a, b, t1, revert)

    def test_a_branch_that_edited_the_file_and_put_it_back_still_brings_the_revert(
            self) -> None:
        # B is cut from the line and edits a.txt, then puts A's content back: two commits
        # of B's own that touch a.txt. Updated from the reverted main, the update merge
        # takes main's deletion. B's own commits left a.txt as A had it, so the removal is
        # still main's and the fold stops.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        self._commit_on("fix/B", write={"a.txt": "a, edited\n"})
        self._commit_on("fix/B", write={"a.txt": "a\n"})
        revert = self._merge_and_revert_a()
        self.fx._update_branch("B")
        self._assert_refused_then_carry_skips_b(a, b, t1, revert)

    def test_a_revert_merged_through_a_revert_pr_is_the_bases_not_the_branchs(
            self) -> None:
        # GitHub's "Revert" button: the revert commit reaches main through a merged PR, so
        # it is not on main's first-parent chain. B, cut from main after that, reaches it
        # only through main's merge commit, so it is still not one of B's commits.
        a, t1 = self._a_on_the_line()
        revert = self._revert_a_through_a_pr()
        b = self.fx._publish("B", {"b.txt": "b\n"})
        self._assert_refused_then_carry_skips_b(a, b, t1, revert)

    # -- who the refusal names -------------------------------------------------------------

    def _refusal(self, bundles: list[Path], tip: str) -> str:
        """The in-run fold of ``bundles``, continuing from ``tip``, stops and pushes nothing.
        Returns its message."""
        with redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, bundles, folded_this_run={TARGET: tip})
        self.assertEqual(self.fx._tip(LINE), tip, "the fold pushed")
        return str(ctx.exception)

    def test_a_branch_that_holds_another_bundles_moved_tip_names_only_that_bundle(
            self) -> None:
        # B is cut from fix/A. A then gets a fixup (fix.txt), its PR merges into main with
        # it, and main reverts that merge; B is updated from main. B's walk still reaches
        # A's first commit (a.txt) along its own first-parent chain, though fix/A has moved
        # on from it. B's branch holds A's tip too, through main, so all of A's own commits
        # are A's work, not B's: a.txt and fix.txt are A's alone, and the message never says
        # merging B removes B's own work.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from="origin/fix/A")
        self._commit_on("fix/A", write={"fix.txt": "f\n"})
        self._merge_and_revert_a()
        self.fx._update_branch("B")
        msg = self._assert_refused([a, b], t1)
        self.assertIn("a.txt (issue_A); fix.txt (issue_A)", msg)
        self.assertNotIn("issue_B's work", msg)

    def test_a_commit_two_branches_share_is_work_from_either_and_git_cannot_tell(
            self) -> None:
        # B is cut from fix/A; A's PR merges into main at that commit, main reverts it, and
        # only then does A get a fixup, which B never sees. B, updated from main, holds A's
        # first commit (a.txt) but not A's tip. Its mirror (A cut from fix/B, then B's own PR
        # merged at that commit, reverted, and B moved on) has the same commit graph, and
        # there the commit is B's: git cannot say which branch it was made on. The refusal
        # says so, naming the work as A's or B's: it neither guesses (and risks leaving out
        # the one whose work it is) nor says that merging B removes B's own work.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from="origin/fix/A")
        self._merge_and_revert_a()
        self._commit_on("fix/A", write={"fix.txt": "f\n"})
        self.fx._update_branch("B")
        msg = self._refusal([a, b], t1)
        self.assertIn(f"merging issue_B's branch origin/fix/B would remove work from {LINE}: "
                      f"a.txt (work from issue_A or issue_B — git cannot tell which branch "
                      f"made it) would go back", msg)
        self.assertNotIn("issue_A, issue_B", msg)
        self.assertNotIn("issue_B's work", msg)

    def test_a_branch_that_lost_its_own_earlier_work_is_not_said_to_remove_it(self) -> None:
        # A is on the line. Main merges A's PR and reverts it, and A's branch is updated
        # from main: that merge brings the revert of A's own commit, so fix/A no longer has
        # a.txt. The fold merges A's new tip and would take a.txt off the line, so it stops.
        # The work is A's, but the change is main's: the refusal names a.txt as A's earlier
        # work that its branch no longer has, never as A removing its own work.
        a, t1 = self._a_on_the_line()
        self._merge_and_revert_a()
        self.fx._update_branch("A")
        msg = self._refusal([a], t1)
        self.assertIn(f"merging issue_A's branch origin/fix/A would remove work from {LINE}: "
                      f"a.txt (issue_A's earlier work, which its branch no longer has) would "
                      f"go back", msg)
        self.assertNotIn("issue_A's work", msg)

    def test_the_base_merge_refusal_names_only_the_bundle_whose_tip_a_branch_holds(
            self) -> None:
        # The naming is shared with the #623 base merge. As above, B is cut from fix/A and A
        # gets a fixup before its PR merges into main; then fix/A is deleted, main reverts
        # the merge and B is updated from main. The line lacks the head A's PR merged with,
        # so the fold would take main in, and that would take a.txt off the line. It stops
        # on the base merge (its own wording, unchanged) and names A alone: B's branch holds
        # A's merged head, through main.
        a, t1 = self._a_on_the_line()
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from="origin/fix/A")
        head = self._commit_on("fix/A", write={"fix.txt": "f\n"})
        self._merge_and_revert_a()
        self.fx._delete("fix/A")
        self.fx._update_branch("B")
        with sb._merged_heads({"A": head}):
            msg = self._refusal([a, b], t1)
        self.assertIn("taking origin/main in would remove issue_A's work", msg)
        self.assertIn("a.txt (issue_A) would go back", msg)

    # -- (3) a bundle's own change is not a revert -----------------------------------------

    def test_a_dependent_that_deletes_the_file_itself_is_merged(self) -> None:
        # Criterion (3). C is cut from the line and its own commit deletes a.txt on
        # purpose: that is C's change, so the fold merges it and pushes.
        a, t1 = self._a_on_the_line()
        c = self.fx._publish("C", {"c.txt": "c\n"}, cut_from=f"origin/{LINE}")
        self._commit_on("fix/C", delete=("a.txt",))
        tip = self._assert_merged([a, c], t1, "C")
        self.assertFalse(self._has_file(tip, "a.txt"))

    def test_a_dependent_that_edits_the_file_is_merged(self) -> None:
        # Criterion (3), the edit: new content removes nothing.
        a, t1 = self._a_on_the_line()
        c = self.fx._publish("C", {"a.txt": "a2\n"}, cut_from=f"origin/{LINE}")
        tip = self._assert_merged([a, c], t1, "C")
        self.assertEqual(self.fx._show(tip, "a.txt"), "a2\n")

    def test_a_dependent_deleting_the_file_then_updated_from_the_reverted_base_is_merged(
            self) -> None:
        # C deletes a.txt itself, then is updated from main after main reverted A. Both
        # sides deleted it, so the update merge's a.txt is C's own side's as much as
        # main's: the deletion is still C's change, and the fold merges C.
        a, t1 = self._a_on_the_line()
        c = self.fx._publish("C", {"c.txt": "c\n"}, cut_from=f"origin/{LINE}")
        self._commit_on("fix/C", delete=("a.txt",))
        self._merge_and_revert_a()
        self.fx._update_branch("C")
        tip = self._assert_merged([a, c], t1, "C")
        self.assertFalse(self._has_file(tip, "a.txt"))
        self.assertEqual(self.fx._show(tip, "c.txt"), "c\n")

    # -- a line that will not go back ------------------------------------------------------

    def test_a_line_that_will_not_go_back_after_refusing_a_branch_stops_even_the_carry(
            self) -> None:
        # The carry must never go on to push a line that still has the removal: when the
        # line cannot be put back after B's merge is refused, the fold stops, records
        # nothing in `skipped` and pushes nothing, and it names the merge it was undoing.
        a, t1 = self._a_on_the_line()
        self._merge_and_revert_a()
        b = self.fx._publish("B", {"b.txt": "b\n"})
        d = self.fx._publish("D", {"d.txt": "d\n"}, cut_from=f"origin/{LINE}")
        real = integrate._git

        def stuck(repo: Path, *args: str) -> int:
            # Only the reset back to the line's tip fails: the fold's opening reset of the
            # shared tree to HEAD (#631) must pass, or that one stops the fold first.
            return 1 if args[:1] == ("reset",) and args[-1] != "HEAD" else real(repo, *args)

        skipped: dict[str, str] = {}
        with mock.patch.object(integrate, "_git", stuck), redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, [a, b, d], folded_this_run={TARGET: t1}, skipped=skipped)
        msg = str(ctx.exception)
        self.assertIn(f"could not reset {LINE}", msg)
        self.assertIn("undo the merge of issue_B's branch origin/fix/B", msg)
        self.assertEqual(skipped, {})
        self.assertEqual(self.fx._tip(LINE), t1)                 # nothing pushed


if __name__ == "__main__":
    unittest.main()
