"""A re-issued run's refresh is judged by the line texts folds put on the line (#894).

The carry's refresh (#893) merges the base into a line it continues. It used to be judged as
the in-run folds judge a base merge (#623): a file counts as removed only when it is back to
its content where the line left the base. That missed a revert of a bundle's lines once the
base also edited the same file (the refresh was kept, the work was gone from the line, and the
run said it carried that bundle); it counted the base's own changes as the line's, so a base
reverting one of them stopped every later refresh; and a file name on the base that is not
valid UTF-8 stopped the run.

Now the refresh is kept iff, in every file the merge changes that folded work changed, each
line text folded work added has at least as many copies after the merge as before it, each
text it removed has no more, and the file is not back to its content at the cut point.
Folded work is every commit a fold put on the line (reached from a non-first parent of the
line's first-parent chain, never one of the base's first-parent commits), so no publish
record and no host read is needed, and a squash, a cherry-pick or a rewritten branch does not
hide it. Blank and whitespace-only lines never count; a submodule's commit is one line. The
base's own changes, and their reverts, never cost the refresh; a base edit of a bundle's line
does, on every re-run, and so does a base that deletes its own copy of a text a bundle also
added (the accepted false alarm, with deleting the line as its way out). Names and contents
are read as bytes. The in-run folds (a gone branch taking the base in; a bundle's branch
merge) keep the path check, word for word.

These cases drive ``flow._drive_and_act`` against real git through the helpers of
tests/test_flow_continued_line_refresh.py (the ``StackFoldGit`` fixture of
tests/test_integrate_stack_bases.py, ``gh`` stubbed), loaded by path as that module loads the
fixture, and used as a helper object.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions (or, for a name that
is not UTF-8, on the error the old check raises).
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_continued_line_refresh_guard
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from pdca_harness import integrate

_SPEC = importlib.util.spec_from_file_location(
    "continued_line_refresh", Path(__file__).with_name("test_flow_continued_line_refresh.py"))
cr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cr)
sb = cr.sb

# A file of twelve lines on main before the line is started; W and X each change one line of
# it on the line, far enough apart to merge cleanly.
SHARED = "".join(f"s{i}\n" for i in range(1, 13))
W_LINE, X_LINE = 2, 10
BATCH = ["W", "X", "D"]
TWICE = ["W", "X", "D", "E"]          # a batch re-run twice: D, then E
# The re-run's dependent, built on W and X, on the run's line (child-1's seam).
DX = "- **Depends on:** W, X\n- **Stacks on:** W\n"
# Names that are not valid UTF-8: as bytes, and as Python holds them (surrogateescape).
CAFE = b"caf\xe9.txt"
NFF = b"n\xff.txt"


def _str(name: bytes) -> str:
    return name.decode("utf-8", "surrogateescape")


def _edit(text: str, lines: dict[int, str]) -> str:
    """``text`` with each line number of ``lines`` (from 1) replaced."""
    rows = text.splitlines()
    for n, row in lines.items():
        rows[n - 1] = row
    return "".join(f"{r}\n" for r in rows)


def _ways_out(names: str | None, line: str) -> list[str]:
    """The steps a warning gives for work ``names`` put on ``line`` (None: folded work no
    bundle can be named for)."""
    whose = names or "the bundle whose work it is"
    it = "them" if names and "," in names else "it"
    return ["the line keeps that work",
            "put it back on origin/main (revert the revert) if that was a mistake",
            f"if it was meant, drop {whose} from the briefs that name {it} and re-issue "
            f"without {it}",
            f"if origin/main changed only its own copy of the same text, delete {line} on "
            f"origin once this run ends, then re-run: the re-run starts the line from "
            f"origin/main"]


class RefreshGuard(unittest.TestCase):

    def setUp(self) -> None:
        self.h = cr.ContinuedLineRefresh()     # child-1's helpers, real git, gh stubbed
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.fx = self.h.fx

    # -- histories -------------------------------------------------------------------------

    def _shared_on_line(self, batch: list[str]) -> str:
        """``shared.txt`` on main; W and X change lines of it and are on the line of an
        earlier run of ``batch``. Returns the line's tip."""
        self.h._main({"shared.txt": SHARED})
        w = self.h._finished("W", {"shared.txt": _edit(SHARED, {W_LINE: "w"})})
        x = self.h._finished("X", {"shared.txt": _edit(SHARED, {X_LINE: "x"})})
        return self.h._earlier(batch, w, x)

    def _w_and_x_on_line(self, batch: list[str], w: dict[str, str]) -> str:
        """W (``w``) and X (``xx.txt``) on the line of an earlier run of ``batch``; returns
        the line's tip."""
        return self.h._earlier(batch, self.h._finished("W", w),
                               self.h._finished("X", {"xx.txt": "x\n"}))

    def _merged_and_reverted(self, iid: str) -> str:
        """main merges ``iid``'s PR with a merge commit, then reverts that merge; returns
        the revert."""
        self.h._merge(iid)
        return self.fx._revert_on_main("-m", "1", "HEAD")

    def _cherry_pick_on_main(self, iid: str) -> str:
        """main takes ``iid``'s change as a cherry-pick (a new commit of its own); ``iid``'s
        PR stays open. Returns the new commit. ``-x`` records the pick in its message, so
        the pick is never the very commit it copies (same tree, parent, people and second)."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "main", "origin/main")
        sb._git(h, "cherry-pick", "-x", f"origin/fix/{iid}")
        sb._git(h, "push", "-q", "origin", "main")
        return sb._git(h, "rev-parse", "HEAD").strip()

    def _on_main_raw(self, name: bytes, text: bytes) -> str:
        """Commit ``text`` as ``name`` on main through ``git hash-object`` / ``update-index``,
        so the name reaches git as these bytes; returns the commit."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "scratch", "origin/main")
        blob = subprocess.run(["git", "-C", str(h), "hash-object", "-w", "--stdin"],
                              input=text, capture_output=True, check=True).stdout.strip()
        subprocess.run([b"git", b"-C", os.fsencode(h), b"update-index", b"--add",
                        b"--cacheinfo", b"100644," + blob + b"," + name], check=True)
        sb._git(h, "commit", "-q", "-s", "-m", "a file whose name is not UTF-8")
        sb._git(h, "push", "-q", "origin", "HEAD:refs/heads/main")
        sb._git(h, "reset", "-q", "--hard")         # the work tree as committed
        return sb._git(h, "rev-parse", "HEAD").strip()

    def _names_on(self, rev: str) -> list[bytes]:
        """Every file name in ``rev``'s tree, as git has it."""
        out = subprocess.run(["git", "-C", str(self.fx.origin), "ls-tree", "-r", "-z",
                              "--name-only", rev], capture_output=True, check=True).stdout
        return [n for n in out.split(b"\0") if n]

    def _line_by_hand(self, batch: list[str], *iids: str) -> str:
        """An earlier run's line of ``batch``, made as its fold makes it (main, then each of
        ``iids``' branches merged on top, signed off) but by hand: that fold checks each
        branch merge with the in-run path check, which still reads names strictly
        (`_keep_merge`, unchanged). Returns its tip."""
        h, line = self.fx.human, integrate.integration_branch(self.h.cfg, "main", batch)
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "line", "origin/main")
        for iid in iids:
            sb._git(h, "merge", "-q", "--no-ff", "--signoff", "-m",
                    f"pdca-integrate: issue_{iid}", f"origin/fix/{iid}")
        sb._git(h, "push", "-q", "origin", f"HEAD:refs/heads/{line}")
        return sb._git(h, "rev-parse", "HEAD").strip()

    def _sub_at(self, commit: str, *, on: str) -> str:
        """Commit the submodule ``sub`` at ``commit`` on top of origin's ``on`` and push it
        there (a gitlink, as `git submodule` records one; the submodule itself is never
        fetched); returns the commit."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "scratch", f"origin/{on}")
        sb._git(h, "update-index", "--add", "--cacheinfo", f"160000,{commit},sub")
        (h / "sub").mkdir(exist_ok=True)              # as a checkout of it leaves it
        sb._git(h, "commit", "-q", "-s", "-m", f"sub at {commit[:7]}")
        sb._git(h, "push", "-q", "origin", f"HEAD:refs/heads/{on}")
        return sb._git(h, "rev-parse", "HEAD").strip()

    def _drive(self, iid: str, batch: list[str], extra: str = DX) -> None:
        self.h._run([self.h._planned(iid, extra)], batch)

    # -- what a re-run did -------------------------------------------------------------------

    def _dropped(self, names: str | None, *parts: str,
                 carried: str = "carried issue_W, issue_X") -> str:
        """ONE warning naming the line, origin/main, ``parts``, whose work it is (``names``;
        None: folded work no bundle can be named for) and the ways out, ending on the line
        as found without origin/main; ``carried`` said. Returns the warning."""
        said = self.h._one_warning(
            f"would remove {names}'s work" if names else "would remove folded work",
            *parts, *_ways_out(names, self.h.line))
        self.assertTrue(said.endswith(cr.WITHOUT), said)
        self.assertNotIn("git step failed", said)
        self.assertIn(carried, self.h.err)
        return said

    def _kept(self, iid: str = "D") -> str:
        """No warning, and ``iid`` built on a line that has main's tip. Returns that tip."""
        self.assertEqual(self.h._warnings(), [], self.h.err)
        t = self.h._built_on_line(iid)
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), t))
        return t

    # -- (i) a revert plus an edit beside it -------------------------------------------------

    def test_i_a_revert_of_w_beside_a_base_edit_drops_the_refresh(self):
        old = self._shared_on_line(BATCH)
        self._merged_and_reverted("W")
        self.h._main({"shared.txt": _edit(SHARED, {W_LINE + 1: "m"})})
        self._drive("D", BATCH)
        said = self._dropped("issue_W", "the lines a bundle put in shared.txt (issue_W) would "
                                        "be gone or changed")
        self.assertNotIn("issue_X", said)
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", X_LINE: "x"}))

    def test_i_a_revert_of_x_beside_a_base_edit_drops_the_refresh_naming_x(self):
        old = self._shared_on_line(BATCH)
        self._merged_and_reverted("X")
        self.h._main({"shared.txt": _edit(SHARED, {X_LINE - 1: "m"})})
        self._drive("D", BATCH)
        said = self._dropped("issue_X", "the lines a bundle put in shared.txt (issue_X) would "
                                        "be gone or changed")
        self.assertNotIn("issue_W", said)
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", X_LINE: "x"}))

    # -- (ii) a text the file holds many times ------------------------------------------------

    def test_ii_a_reverted_line_of_repeated_text_is_counted(self):
        # The merged file still holds nine `alpha`s, so "is the text there" would pass; the
        # line as found has ten.
        rep = "alpha\nbeta\n" * 9
        self.h._main({"rep.txt": rep})
        rows = rep.splitlines()
        w = self.h._finished("W", {"rep.txt": "".join(
            f"{r}\n" for r in [*rows[:3], "alpha", *rows[3:]])})        # inside pair 2
        x = self.h._finished("X", {"rep.txt": _edit(rep, {16: "gamma"})})   # pair 8's beta
        old = self.h._earlier(BATCH, w, x)
        self._merged_and_reverted("W")
        self._drive("D", BATCH)
        said = self._dropped("issue_W", "rep.txt (issue_W)")
        self.assertNotIn("issue_X", said)
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        text = self.fx._show(t, "rep.txt")
        self.assertEqual(text.count("alpha\n"), 10)
        self.assertIn("gamma\n", text)

    def test_ii_b_a_reverted_cherry_pick_where_the_text_repeats_drops_the_next_refresh(self):
        # Round 7's reviewer probe. main takes W's second `c` as a cherry-pick while W's PR
        # stays open: re-run 1's merge changes nothing, kept. main reverts the pick and adds
        # a `b` on top; re-run 2's merge would make dup.txt `b b b b c b a z`: two `c`s
        # before it, one after.
        self.h._main({"dup.txt": "b\nb\nb\nc\nb\na\na\n"})
        w = self.h._finished("W", {"dup.txt": "b\nb\nb\nc\nc\nb\na\na\n"})
        x = self.h._finished("X", {"dup.txt": "b\nb\nb\nc\nb\na\nz\n"})
        self.h._earlier(TWICE, w, x)
        pick = self._cherry_pick_on_main("W")
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/W"), self.fx._tip("main")))
        self._drive("D", TWICE)
        self._kept()
        self.fx._revert_on_main(pick)
        self.h._main({"dup.txt": "b\nb\nb\nb\nc\nb\na\na\n"})
        self._drive("E", TWICE)
        said = self._dropped("issue_W", "the lines a bundle put in dup.txt (issue_W) would be "
                                        "gone or changed")
        self.assertNotIn("issue_X", said)
        self.assertEqual(self.fx._show(self.h._built_on_line("E"), "dup.txt"),
                         "b\nb\nb\nc\nc\nb\na\nz\n")

    # -- (iii) the base reverts a change of its own --------------------------------------------

    def test_iii_a_revert_of_the_bases_own_change_keeps_the_refresh(self):
        self.h._main({"x.txt": "x1\n"})
        self._w_and_x_on_line(TWICE, {"w.txt": "w\n"})
        self.h._main({"x.txt": "x2\n"})                 # no bundle touched x.txt
        self._drive("D", TWICE)
        self.assertEqual(self.fx._show(self._kept(), "x.txt"), "x2\n")
        self.fx._revert_on_main("HEAD")
        self._drive("E", TWICE)
        t = self._kept("E")
        self.assertEqual(self.fx._show(t, "x.txt"), "x1\n")
        self.assertEqual(self.fx._show(t, "w.txt"), "w\n")

    # -- (iv) the base edits a bundle's line on purpose ----------------------------------------

    def test_iv_a_base_edit_of_ws_line_is_refused_on_every_re_run(self):
        old = self._shared_on_line(TWICE)
        self.h._merge("W")
        self.h._main({"shared.txt": _edit(SHARED, {W_LINE: "m"})})
        self._drive("D", TWICE)
        first = self._dropped("issue_W", "the lines a bundle put in shared.txt (issue_W) would "
                                         "be gone or changed")
        self.assertEqual(self.h._built_on_line(), old)
        self._drive("E", TWICE)                       # nothing changed: refused again
        self.assertEqual(self._dropped("issue_W"), first)
        self.assertEqual(self.fx._show(self.h._built_on_line("E"), "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", X_LINE: "x"}))

    # -- (v) the base edits beside a bundle's lines --------------------------------------------

    def test_v_a_base_edit_beside_ws_lines_keeps_the_refresh(self):
        self._shared_on_line(BATCH)
        self.h._main({"shared.txt": _edit(SHARED, {6: "m"})})
        self._drive("D", BATCH)
        self.assertEqual(self.fx._show(self._kept(), "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", 6: "m", X_LINE: "x"}))

    # -- (vi) a squash merge reverted after a kept refresh -------------------------------------

    def test_vi_a_squash_reverted_after_a_kept_refresh_drops_the_next(self):
        # main squash-merges W's PR: one commit of main's own, W's head never on main. The
        # squash's content is the line's, so re-run 1's merge changes nothing: kept. main
        # then reverts the squash and edits beside W's line. W's own commits on the line are
        # still folded work, so re-run 2 is refused, naming W, with no squash step and no
        # read of the host for it (the host answers W once, for the carry's own check).
        self._shared_on_line(TWICE)
        squash = self.h._main({"shared.txt": _edit(SHARED, {W_LINE: "w"})})
        self.h.gh["W"], self.h.merge_oid["W"] = "MERGED", squash
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/W"), self.fx._tip("main")))
        self._drive("D", TWICE)
        self._kept()
        self.fx._revert_on_main("HEAD")
        self.h._main({"shared.txt": _edit(SHARED, {W_LINE + 1: "m"})})
        self.h.gh_ok["W"] = 1
        self._drive("E", TWICE)
        said = self._dropped("issue_W", "the lines a bundle put in shared.txt (issue_W) would "
                                        "be gone or changed")
        for wrong in ("issue_X", "squash", "stack mode needs merge commits"):
            self.assertNotIn(wrong, said)
        self.assertEqual(self.h.gh_refused, [])
        self.assertEqual(self.fx._show(self.h._built_on_line("E"), "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", X_LINE: "x"}))

    # -- (vii) file names that are not valid UTF-8 ---------------------------------------------

    def test_vii_a_a_non_utf8_name_on_the_base_never_raises(self):
        self._w_and_x_on_line(TWICE, {"w.txt": "w\n"})
        self._on_main_raw(CAFE, b"c1\n")
        self.assertIn(CAFE, self._names_on("main"))  # the bytes, not a repaired name
        self._drive("D", TWICE)                       # returns: nothing raised
        self._kept()
        # main edits that file. D's PR is closed first, so re-run 2 carries W and X only and
        # merges no bundle branch onto the refreshed line: that merge's own check
        # (`_keep_merge`, unchanged) still reads names strictly and would stop on this one,
        # a limit the docs give. What this pins is the refresh's own judgement.
        self.h.gh["D"] = "CLOSED"
        self._on_main_raw(CAFE, b"c2\n")
        self._drive("E", TWICE)
        t = self._kept("E")
        self.assertEqual(self.fx._show(t, _str(CAFE)), "c2\n")    # git got the bytes back

    def test_vii_b_a_bundles_line_in_a_non_utf8_name_is_kept_and_named(self):
        self._on_main_raw(NFF, b"base1\n")
        self.h._finished("W", {_str(NFF): "base1\nw1\n"})            # W appends `w1`
        self.h._finished("X", {"xx.txt": "x\n"})
        self.assertIn(NFF, self._names_on("fix/W"))
        old = self._line_by_hand(BATCH, "W", "X")
        self._merged_and_reverted("W")
        self._on_main_raw(NFF, b"base2\n")
        self._drive("D", BATCH)
        said = self._dropped("issue_W", "the lines a bundle put in n�.txt (issue_W) "
                                        "would be gone or changed")
        self.assertNotIn("issue_X", said)
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, _str(NFF)), "base1\nw1\n")

    # -- (viii) the in-run folds keep the path check, word for word ---------------------------

    def test_viii_a_gone_branch_taking_the_base_in_refuses_as_before(self):
        a, t1, head, _revert = self.fx._fixup_merged_then_reverted()
        b = self.fx._publish("B", {"b.txt": "b\n"})
        with sb._merged_heads({"A": head}), redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.fx.cfg, [a, b], folded_this_run={sb.TARGET: t1})
        line = sb.LINE
        self.assertEqual(str(ctx.exception), (
            f"issue_A's branch origin/fix/A is gone and its PR merged at {head[:12]}, which "
            f"{line} does not have, but taking origin/main in would remove issue_A's work "
            f"from {line}: a.txt (issue_A) would go back to how origin/main had it before the "
            f"line changed it (a revert on origin/main?). Not building the next wave on a "
            f"line without that work — get it back into origin/main, or restore "
            f"origin/fix/A, then re-run"))
        self.assertEqual(self.fx._tip(line), t1)

    def test_viii_a_branch_bringing_a_revert_refuses_as_before(self):
        a = self.fx._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.fx.cfg, [a], folded_this_run={})
        t1 = self.fx._tip(sb.LINE)
        self.fx._merge_pr("A")
        self.fx._revert_on_main("-m", "1", "HEAD")
        b = self.fx._publish("B", {"b.txt": "b\n"})        # cut from the reverted main
        with redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.fx.cfg, [a, b], folded_this_run={sb.TARGET: t1})
        line = sb.LINE
        self.assertEqual(str(ctx.exception), (
            f"merging issue_B's branch origin/fix/B would remove issue_A's work from {line}: "
            f"a.txt (issue_A) would go back to how origin/main had it before the line changed "
            f"it, and that change is not one of issue_B's own commits: its branch brings it "
            f"in (cut from, or updated from, origin/main after a revert there?). Not building "
            f"the next wave on a line without that work — put it back on origin/fix/B "
            f"(revert the revert there, or update it from origin/main once origin/main has "
            f"the work again), then re-run"))
        self.assertEqual(self.fx._tip(line), t1)

    # -- (ix) a bundle whose work is a removal -------------------------------------------------

    def test_ix_a_reverted_removal_drops_the_refresh(self):
        # Refused before #894 too (both files are back to their content at the cut point),
        # with the same names; what both wordings say is pinned.
        self.h._main({"del.txt": "d1\nd2\nd3\n", "gone.txt": "g\n"})
        w = self.h._finished("W", {"del.txt": "d1\nd3\n"})
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "fix/W", "origin/fix/W")
        sb._git(h, "rm", "-q", "gone.txt")
        sb._git(h, "commit", "-q", "-s", "-m", "W removes gone.txt")
        sb._git(h, "push", "-q", "origin", "fix/W")
        x = self.h._finished("X", {"xx.txt": "x\n"})
        old = self.h._earlier(BATCH, w, x)
        self._merged_and_reverted("W")
        self._drive("D", BATCH)
        said = self.h._one_warning(
            "would remove issue_W's work", "del.txt (issue_W)", "gone.txt (issue_W)",
            "would go back to how origin/main had it before the line changed it",
            "the line keeps that work", "put it back on origin/main (revert the revert) if",
            "drop issue_W from the briefs that name it and re-issue without it")
        self.assertTrue(said.endswith(cr.WITHOUT), said)
        self.assertIn("carried issue_W, issue_X", self.h.err)
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "del.txt"), "d1\nd3\n")
        self.assertNotEqual(sb._run(self.fx.origin, "cat-file", "-e", f"{t}:gone.txt")
                            .returncode, 0)

    def test_ix_b_a_reverted_removal_beside_another_base_edit_drops_the_refresh(self):
        # The loss the round-7 brief accepted: del2.txt is not back to its content at the
        # cut point (main edited another line of it), but the text `old` W removed comes back.
        self.h._main({"del2.txt": "k1\nold\nk3\nk4\nk5\nk6\n"})
        old = self._w_and_x_on_line(BATCH, {"del2.txt": "k1\nk3\nk4\nk5\nk6\n"})
        self._merged_and_reverted("W")
        self.h._main({"del2.txt": "k1\nold\nk3\nk4\nk5\nm6\n"})
        self._drive("D", BATCH)
        self._dropped("issue_W", "lines a bundle took out of del2.txt (issue_W) would come back")
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "del2.txt"), "k1\nk3\nk4\nk5\nk6\n")

    # -- (x) a branch rewritten after its PR merged --------------------------------------------

    def test_x_a_rewritten_branch_is_folded_work_no_bundle_is_named_for(self):
        # main merged W's PR at h1; W's branch is then force-pushed to h2 (no commit of h1 on
        # it), the PR still recorded merged at h1. W's commit on the line is folded work all
        # the same, so main's revert of it beside an edit drops the refresh; W's branch reads
        # as h2, whose own commits hold none of h1's, so no bundle can be named for it. The
        # edit is three lines from W's, so h2 merges cleanly onto the refreshed line too:
        # the path check kept that refresh, never a conflict that left W off.
        self.h._main({"shared.txt": SHARED})
        cut = self.fx._tip("main")
        w = self.h._finished("W", {"shared.txt": _edit(SHARED, {W_LINE: "w"})})
        x = self.h._finished("X", {"shared.txt": _edit(SHARED, {X_LINE: "x"})})
        old = self.h._earlier(BATCH, w, x)
        h1 = self.fx._tip("fix/W")
        self.h._merge("W")
        h2 = self.fx._push_from_human({"shared.txt": _edit(SHARED, {W_LINE: "w"})},
                                      to="fix/W", off=cut, force=True)
        self.assertFalse(self.fx._ancestor(h1, h2))
        self.fx._revert_on_main("-m", "1", "HEAD")
        self.h._main({"shared.txt": _edit(SHARED, {W_LINE + 3: "m"})})
        real = self.h._gh_stub

        def gh(args, *a, **kw):                       # the PR is recorded as merged at h1
            r = real(args, *a, **kw)
            if isinstance(args, list) and args[:1] == ["gh"] and str(args[3]).endswith("/W"):
                r = subprocess.CompletedProcess(args, 0, r.stdout.replace(h2, h1), "")
            return r
        self.h._gh_stub = gh
        self._drive("D", BATCH)
        self.assertEqual(sb._git(self.fx.primary, "rev-parse",
                                 "refs/remotes/origin/fix/W").strip(), h2)
        said = self._dropped(None, "the lines a bundle put in shared.txt (folded work) would "
                                   "be gone or changed")
        self.assertNotIn("issue_W", said)
        t = self.h._built_on_line()
        self.assertTrue(self.fx._ancestor(old, t))
        self.assertEqual(self.fx._show(t, "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", X_LINE: "x"}))

    # -- (xi) an owner with no record --------------------------------------------------------

    def test_xi_work_of_an_owner_with_no_record_is_still_folded_work(self):
        # W's publish record is gone and the host fails for W: the carry cannot carry W (it
        # has no PR on record), so D builds on X alone. W's commit on the line is folded work
        # all the same: main's revert of it beside an edit drops the refresh, as folded work.
        old = self._shared_on_line(BATCH)
        self._merged_and_reverted("W")
        self.h._main({"shared.txt": _edit(SHARED, {W_LINE + 1: "m"})})
        (self.h.cfg.bundle("W") / "publish.json").unlink()
        self.h.gh_ok["W"] = 0
        self._drive("D", BATCH, "- **Depends on:** X\n- **Stacks on:** X\n")
        said = self._dropped(None, "the lines a bundle put in shared.txt (folded work) would "
                                   "be gone or changed", carried="carried issue_X")
        self.assertNotIn("issue_W", said)
        self.assertIn("no PR with a pushed branch on record", self.h._not_carried("W"))
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", X_LINE: "x"}))

    # -- (xii) whitespace-only lines ---------------------------------------------------------

    def test_xii_whitespace_only_lines_are_not_counted(self):
        doc = "# Title\n\nIntro.\n\nBody one.\n\nBody two.\n\nEnd.\n"
        self.h._main({"doc.md": doc})
        ws = "# Title\n\nIntro.\n\nBody one.\n\nW one.\nW two.\n\nBody two.\n\nEnd.\n"
        self._w_and_x_on_line(BATCH, {"doc.md": ws})
        self.h._main({"doc.md": doc.replace("# Title\n\n", "# Title\n")})   # a blank line
        self._drive("D", BATCH)
        self.assertEqual(self.fx._show(self._kept(), "doc.md"),
                         ws.replace("# Title\n\n", "# Title\n"))

    # -- (xiii) the accepted false alarm ------------------------------------------------------

    def test_xiii_a_base_deleting_its_own_copy_of_a_bundles_text_is_refused(self):
        # W adds a `return None` to f.py, which held one from main already; main deletes its
        # own (not W's). The count cannot tell the two apart, so the refresh is refused: the
        # documented cost, with deleting the line as its way out.
        base = "def a():\n    return None\n\n\ndef c():\n    pass\n"
        self.h._main({"f.py": base})
        ws = base + "\n\ndef b():\n    return None\n"
        old = self._w_and_x_on_line(BATCH, {"f.py": ws})
        self.h._main({"f.py": base.replace("return None", "pass")})
        self._drive("D", BATCH)
        self._dropped("issue_W", "the lines a bundle put in f.py (issue_W) would be gone or "
                                 "changed")
        t = self.h._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "f.py"), ws)

    # -- (xiv) the way out, followed ----------------------------------------------------------

    def test_xiv_putting_ws_lines_back_on_main_ends_the_refusal(self):
        # (i)'s refusal; then main reverts its revert, which puts W's line back: the next
        # refresh is kept. main's edit is three lines from W's, so its revert of the revert
        # applies cleanly.
        self._shared_on_line(TWICE)
        revert = self._merged_and_reverted("W")
        self.h._main({"shared.txt": _edit(SHARED, {W_LINE + 3: "m"})})
        self._drive("D", TWICE)
        self._dropped("issue_W", "shared.txt (issue_W)")
        self.fx._revert_on_main(revert)
        self._drive("E", TWICE)
        self.assertEqual(self.fx._show(self._kept("E"), "shared.txt"),
                         _edit(SHARED, {W_LINE: "w", W_LINE + 3: "m", X_LINE: "x"}))

    # -- (xv) a submodule's commit -----------------------------------------------------------

    def test_xv_a_submodule_commit_a_bundle_set_is_one_line_of_its(self):
        # W moves the submodule `sub` from A to B; main merges W, then moves it to C. The
        # repo's `.gitmodules` says to ignore the submodule, which hides it from a plain
        # diff: the check still sees W's commit go, and refuses with the normal warning.
        a, b, c = "1" * 40, "2" * 40, "3" * 40
        self.h._main({".gitmodules": '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n'
                                     "\tignore = all\n"})
        self._sub_at(a, on="main")
        w = self.h._finished("W", {"w.txt": "w\n"})
        mine = self._sub_at(b, on="fix/W")
        x = self.h._finished("X", {"xx.txt": "x\n"})
        self.h._earlier(BATCH, w, x)
        self.h._merge("W")
        self._sub_at(c, on="main")
        self._drive("D", BATCH)
        self._dropped("issue_W", "the lines a bundle put in sub (issue_W) would be gone or "
                                 "changed")
        t = self.h._built_on_line()
        self.assertEqual(sb._git(self.fx.origin, "rev-parse", f"{t}:sub").strip(), b)
        self.assertTrue(self.fx._ancestor(mine, t))


if __name__ == "__main__":
    unittest.main()
