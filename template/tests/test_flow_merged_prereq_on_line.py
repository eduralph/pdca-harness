"""A prerequisite merged into the base counts only when the run's line has its work (#655).

In stack mode a dependent of an out-of-batch prerequisite ``P`` — a ``Depends on (merged):
P``, or a plain ``Depends on: P`` the run was not asked for (#647) — waits until ``P``'s PR
merges into the dependent's own target base. Once the run has a line for that target, the
dependent is cut from the line, not from the base, and the line does not follow the base.
So a merge after the line was started may not be on it, and the dependent must be held,
named, with a way out that works: a re-run whose carry (#646) finds the line on origin
continues it and holds the dependent again, so the line is deleted on origin first. The
line has ``P``'s work when it has the commit the merge put on the base (``mergeCommit``,
which a squash merge has and ``headRefOid`` does not) or ``P``'s PR head (which the carry
puts on a line it carries ``P`` onto).

These cases drive ``flow._drive_and_act`` against real git: the ``StackFoldGit`` fixture of
tests/test_integrate_stack_bases.py (a bare ``origin``, a primary checkout and a scratch
clone that plays the maintainer), loaded by path and used as a helper object, as
tests/test_flow_resume_stack_prereqs.py does. The build and sign-off leaves are stubbed,
``flow._publish_bundle`` is the fixture's ``_publish``, and ``gh`` is answered by a stub in
front of ``subprocess.run``. The publisher is NOT a stub (a stub turns every fold into a
dry-run, which holds nothing).

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_merged_prereq_on_line
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import flow, integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-14"
MERGED_EDGE = "- **Depends on:** W\n- **Depends on (merged):** P\n"
PLAIN_EDGE = "- **Depends on:** W, P\n"


class MergedPrereqOnTheLine(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.gh: dict[str, str] = {}          # PR state by id
        self.merge_oid: dict[str, str] = {}   # id → the commit its PR merge put on main
        self.gh_fail_merge_commit = False     # `gh … --json …mergeCommit` exits 1
        self.seen: dict[str, tuple[str, str]] = {}   # bundle → (stack base, recorded tip)
        self.after_first_fold = None          # run once, after the run's first fold
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _prereq(self) -> Path:
        """``P`` finished in an earlier run: accepted, published (``fix/P`` pushed), its PR
        OPEN."""
        d = self.cfg.bundle("P")
        sb._brief(d)
        sb._accept(d)
        self.fx._publish("P", {"p.txt": "p\n"})
        self.assertEqual(state.state(d), state.COMPLETE)
        self.gh["P"] = "OPEN"
        return d

    def _planned(self, iid: str, extra: str = "") -> Path:
        d = self.cfg.bundle(iid)
        sb._brief(d, extra=extra)
        return d

    def _merge(self) -> None:
        """The maintainer merges P's PR into main with a merge commit."""
        self.fx._merge_pr("P")
        self.gh["P"], self.merge_oid["P"] = "MERGED", self.fx._tip("main")

    def _squash(self) -> None:
        """The maintainer squash-merges P's PR into main: main gets one new commit, and
        P's head is never on it."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "main", "origin/main")
        sb._git(h, "merge", "-q", "--squash", "origin/fix/P")
        sb._git(h, "commit", "-q", "-s", "-m", "fix P (#P)")
        sb._git(h, "push", "-q", "origin", "main")
        self.gh["P"], self.merge_oid["P"] = "MERGED", self.fx._tip("main")
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/P"), "main"))

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """``gh pr view <url> --json <fields>`` answered from ``self.gh`` / ``merge_oid``:
        the PR head is the branch's tip on origin. Everything else runs for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        iid = str(args[3]).rsplit("/", 1)[-1]
        if "mergeCommit" in str(args[-1]) and self.gh_fail_merge_commit:
            return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
        head = self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else ""
        oid = self.merge_oid.get(iid)
        out = json.dumps({"state": self.gh.get(iid, "OPEN"), "headRefOid": head,
                          "mergeCommit": {"oid": oid} if oid else None})
        return subprocess.CompletedProcess(args, 0, out, "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle, recording what it was built on."""
        for d in wave:
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d))
            (d / "patch.diff").write_text(sb._new_file_patch(f"{d.name}.txt", d.name),
                                          encoding="utf-8")
            sb._accept(d)
        return 1

    def _origin(self, url: Path) -> None:
        """Point the primary checkout's ``origin`` at ``url``."""
        sb._git(self.fx.primary, "remote", "set-url", "origin", str(url))

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded line tip (else the base),
        push it, record it. Origin is reachable again for it: only the gate's fetch fails."""
        self._origin(self.fx.origin)
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        self.fx._publish(d.name.removeprefix("issue_"), {f"{d.name}.txt": d.name + "\n"},
                         cut_from=tip or (f"origin/{line}" if line else "origin/main"))
        return True

    def _run(self, drive: list[Path], batch: list[str]) -> None:
        """One run: drive ``drive`` with the run asked for ``batch``."""
        self.line = integrate.integration_branch(self.cfg, "main", batch)
        self.seen = {}
        real_fold = integrate.fold

        def fold(*a, **kw):
            out = real_fold(*a, **kw)
            hook, self.after_first_fold = self.after_first_fold, None
            if hook is not None:
                hook()
            return out

        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(integrate, "fold", fold))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            self.results = flow._drive_and_act(self.cfg, drive, do_publish=True,
                                               do_act=False, by="T", today=TODAY,
                                               batch=batch)
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)

    def _wd(self, edge: str) -> list[Path]:
        return [self._planned("W"), self._planned("D", edge)]

    # -- assertions ------------------------------------------------------------------------

    def _held(self, *parts: str) -> str:
        """D was never built and is left PLANNED, and ONE stderr line names issue_D,
        issue_P, main, the line and its short tip, and the way out. Returns that line."""
        self.assertNotIn("issue_D", self.seen, self.err)
        self.assertEqual(self.results["D"], state.PLANNED)
        lines = [x for x in self.err.splitlines() if "issue_D" in x and "issue_P" in x]
        self.assertEqual(len(lines), 1, self.err)
        line = lines[0]
        tip = self.fx._tip(self.line)
        for part in ("merged into main", self.line, tip[:12],
                     f"delete {self.line} on origin, then re-run", *parts):
            self.assertIn(part, line)
        # The line may have been continued by an earlier run's carry (#646), not cut.
        self.assertNotIn("this run cut", line)
        self.assertNotIn("was cut", line)
        return line

    def _built_on_line(self) -> str:
        """D was built, on the run's line; returns the tip it was built on."""
        self.assertIn("issue_D", self.seen, self.err)
        base, tip = self.seen["issue_D"]
        self.assertEqual(base, self.line, self.err)
        self.assertTrue(tip)
        self.assertEqual(self.results["D"], state.COMPLETE)
        return tip

    # -- (1)/(2): merged after the line was started -----------------------------------------

    def test_a_merge_after_the_line_was_started_holds_a_depends_on_merged_dependent(self):
        # (1) Pre-fix: D passes the gate and is built on a line without P's merge.
        self._prereq()
        self.after_first_fold = self._merge
        self._run(self._wd(MERGED_EDGE), ["W", "D"])
        self.assertEqual(self.results["W"], state.COMPLETE)
        self.assertFalse(self.fx._ancestor(self.merge_oid["P"], self.line))
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/P"), self.line))
        self._held("does not have that merge", self.merge_oid["P"][:12])

    def test_a_merge_after_the_line_was_started_holds_a_plain_depends_on_dependent(self):
        # (2) The #647 edge: a plain `Depends on: P`, P not requested.
        self._prereq()
        self.after_first_fold = self._merge
        self._run(self._wd(PLAIN_EDGE), ["W", "D"])
        self.assertEqual(self.results["W"], state.COMPLETE)
        self._held("does not have that merge")

    # -- (3)/(4): merged before the line was started ----------------------------------------

    def test_a_merge_already_on_the_line_builds_the_dependent_on_it(self):
        # (3) Unchanged: the fresh line holds P's merge commit.
        self._prereq()
        self._merge()
        self._run(self._wd(MERGED_EDGE), ["W", "D"])
        tip = self._built_on_line()
        self.assertTrue(self.fx._ancestor(self.merge_oid["P"], tip))

    def test_a_squash_merge_already_on_the_line_builds_the_dependent_on_it(self):
        # (4) A squash merge never puts P's head on the base: the check is on mergeCommit.
        self._prereq()
        self._squash()
        self._run(self._wd(MERGED_EDGE), ["W", "D"])
        tip = self._built_on_line()
        self.assertTrue(self.fx._ancestor(self.merge_oid["P"], tip))
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/P"), tip))

    # -- (5): unreadable, and no line -------------------------------------------------------

    def test_an_unreadable_merge_commit_holds_the_dependent(self):
        # (5) Fail-closed: the PR reads MERGED, but its merge commit cannot be read.
        self._prereq()
        self._merge()
        self.gh_fail_merge_commit = True
        self._run(self._wd(MERGED_EDGE), ["W", "D"])
        self._held("is not known to have that merge", "could not be read")

    def test_a_checkout_that_cannot_be_fetched_holds_the_dependent(self):
        # (5) Fail-closed: after wave 0's fold the checkout's origin is unreachable.
        self._prereq()
        self._merge()
        self.after_first_fold = lambda: self._origin(self.fx.tmp / "gone.git")
        self._run(self._wd(MERGED_EDGE), ["W", "D"])
        self._held("is not known to have that merge", "could not be fetched")

    def test_a_target_with_no_line_builds_the_dependent_from_the_base(self):
        # (5) No fold of D's target this run: D is cut from the fetched base, as today.
        self._prereq()
        self._merge()
        d = self._planned("D", "- **Depends on (merged):** P\n")
        self._run([d], ["D"])
        self.assertIn("issue_D", self.seen, self.err)
        self.assertEqual(self.seen["issue_D"], ("", ""))
        self.assertEqual(self.results["D"], state.COMPLETE)

    # -- a carried prerequisite: its head on the line is its work ---------------------------

    def test_a_carried_prerequisite_whose_head_is_on_the_line_counts(self):
        # A resumed run (#646). An earlier run's line holds P's PR head; P's PR then merged
        # into main with a merge commit, which that line never took in. The re-issued run
        # names P, and E's plain `Depends on: P` has its carry continue the line, P's head
        # already on it. D (`Depends on (merged): P`) is cut from that line: P's work is on
        # it, though main's merge commit is not, so D is built, as before #655.
        p = self._prereq()
        d = self._planned("D", "- **Depends on (merged):** P\n")
        e = self._planned("E", "- **Depends on:** P\n")
        batch = ["P", "D", "E"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [p], folded_this_run={}, batch=batch)  # earlier run
        old = self.fx._tip(integrate.integration_branch(self.cfg, "main", batch))
        self._merge()
        self._run([d, e], batch)
        self.assertIn("carried issue_P", self.err)
        tip = self._built_on_line()
        self.assertEqual(tip, old)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/P"), tip))
        self.assertFalse(self.fx._ancestor(self.merge_oid["P"], tip))
        self.assertEqual(self.results["E"], state.COMPLETE)

    # -- the way out the held line gives, followed ------------------------------------------

    def test_the_way_out_is_to_delete_the_line_then_re_run(self):
        # D is held as in (1). W is finished, so a re-run of the same batch drives D alone,
        # and its carry (#646) puts W on the line: while origin has the line, the carry
        # continues it at the same tip and D is held again. Once the line is deleted on
        # origin, the carry starts it from main, which has P's merge, and D is built on it.
        self._prereq()
        self.after_first_fold = self._merge
        w, d = self._wd(MERGED_EDGE)
        self._run([w, d], ["W", "D"])
        self._held("does not have that merge")
        old = self.fx._tip(self.line)
        self._run([d], ["W", "D"])                   # a re-run, the line still on origin
        self.assertEqual(self.fx._tip(self.line), old)
        self._held("does not have that merge", "may continue it")
        self.fx._delete(self.line)                   # the way out
        self._run([d], ["W", "D"])
        self.assertIn("carried issue_W", self.err)
        tip = self._built_on_line()
        self.assertNotEqual(tip, old)
        self.assertTrue(self.fx._ancestor(self.merge_oid["P"], tip))
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/W"), tip))


if __name__ == "__main__":
    unittest.main()
