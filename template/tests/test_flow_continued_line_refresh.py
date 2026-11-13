"""A re-issued run's carry brings the line it continues up to date with the base (#893).

The pre-wave carry (#646) continues the integration line an earlier run of the batch left on
origin. Before this change that line kept the base it was cut from, so a dependent whose
prerequisite merged into the base after the cut was held (#655) on every re-run, and the only
way out was to delete the line on origin. Now the carry's fold first merges the base into a
line it continues — one signed-off merge commit, pushed without force — and the dependent is
built. When that merge cannot be kept (it conflicts, it would remove work a bundle put on the
line, it would leave off a finished id the line as found takes, or one of its own git steps
fails), the carry does what it did before on the line as found, and says so on one stderr line
with the step for that cause — and with whether the line it pushes has the base all the same
(a carried branch can bring it in), or that it pushes nothing when every bundle is left off.
A file the base itself put on the line and then removed is named as the base's, with deleting
the line as the way out, unless a bundle's commits could not be read.
When the bundle passes run on both lines, only the one that stands is heard: what the other
said about a bundle, a PR state the host would not give included, is never printed. A PR
state the refresh's own check could not read, and the passes then could not read either, is
said once. A fold that stops on an error still prints what its passes said and, after a
dropped refresh, the warning, which then says that the fold stops.

These cases drive ``flow._drive_and_act`` against real git: the ``StackFoldGit`` fixture of
tests/test_integrate_stack_bases.py (a bare ``origin``, a primary checkout and a scratch
clone that plays the maintainer), loaded by path and used as a helper object, as
tests/test_flow_merged_prereq_on_line.py does. The build and sign-off leaves are stubbed,
``flow._publish_bundle`` cuts and pushes the PR branch, and ``gh`` is answered by a stub in
front of ``subprocess.run``. The publisher is NOT a stub (a stub turns every fold into a
dry-run), except in the dry-run case.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_continued_line_refresh
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

TODAY = "2026-11-12"
MERGED_EDGE = "- **Depends on:** W\n- **Depends on (merged):** P\n"
PLAIN_EDGE = "- **Depends on:** W\n"
# A legacy edge to W beside D's `Depends on: W`: a seam, so D is built on the run's line
# (#727) and judged on it (#729).
ON_LINE = "- **Stacks on:** W\n"
# What every warning of a refresh not kept says (the fold's one stderr line for it).
NOT_REFRESHED = "integrate: refresh dropped"
# How that line ends: the line pushed lacks the base, or a carried branch brought it in, or
# it has the base and whether the line as found had it is not said; or nothing is pushed.
WITHOUT = "This fold continues it from its tip on origin without origin/main."
BROUGHT = ("This fold continues it from its tip on origin; origin/main reaches it through a "
           "carried bundle instead.")
HAS = "This fold continues it from its tip on origin; the line it pushes has origin/main."
NOTHING = "This fold pushes nothing: no bundle is carried onto it."
STOPS = "This fold stops on an error and pushes nothing."
LACKS = "does not have that merge"
# What `merged.merged_head` says when the host does not answer for G.
NO_STATE = "merged: could not read PR state for G ("


class ContinuedLineRefresh(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.gh: dict[str, str] = {}          # PR state by id
        self.merge_oid: dict[str, str] = {}   # id → the commit its PR merge put on main
        self.merged_at: dict[str, str] = {}   # id → its PR's head, once its branch is gone
        self.gh_fail_merge_commit = False     # `gh … --json …mergeCommit` exits 1
        self.gh_ok: dict[str, int] = {}       # id → `gh pr view` answers left, then it fails
        self.gh_refused: list[str] = []       # each id a `gh pr view` failed for, in order
        self.seen: dict[str, tuple[str, str]] = {}   # bundle → (stack base, recorded tip)
        self.pushes: list[list[str]] = []     # every `git push` the run made
        self.refuse: str | None = None        # a branch whose every push origin refuses
        self.after_first_fold = None          # run once, after the run's first fold
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _finished(self, iid: str, files: dict[str, str], *, cut_from: str = "origin/main",
                  extra: str = "") -> Path:
        """``iid`` finished in an earlier run: accepted, published (``fix/<iid>`` cut from
        ``cut_from`` with ``files``), its PR OPEN; ``extra``: more brief fields."""
        d = self.fx._publish(iid, files, cut_from=cut_from)
        sb._brief(d, extra=extra)             # _publish writes a bare brief
        sb._accept(d)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.gh[iid] = "OPEN"
        return d

    def _planned(self, iid: str, extra: str = "") -> Path:
        d = self.cfg.bundle(iid)
        sb._brief(d, extra=extra)
        return d

    def _merge(self, iid: str = "P") -> None:
        """The maintainer merges ``iid``'s PR into main with a merge commit."""
        self.fx._merge_pr(iid)
        self.gh[iid], self.merge_oid[iid] = "MERGED", self.fx._tip("main")

    def _gone(self, iid: str = "G") -> None:
        """``iid``'s PR merges into main with a merge commit, and its branch is deleted."""
        self._merge(iid)
        self.merged_at[iid] = self.fx._tip(f"fix/{iid}")
        self.fx._delete(f"fix/{iid}")

    def _main(self, files: dict[str, str]) -> str:
        """A commit lands on main (someone else's PR); returns it."""
        return self.fx._push_from_human(files, to="main")

    def _earlier(self, batch: list[str], *bundles: Path) -> str:
        """An earlier run of ``batch`` folded ``bundles`` onto its line; returns its tip."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, list(bundles), folded_this_run={}, batch=batch)
        return self.fx._tip(integrate.integration_branch(self.cfg, "main", batch))

    def _fixup(self, iid: str, files: dict[str, str]) -> str:
        """A reviewer pushes a fixup onto ``fix/<iid>``; returns the new head."""
        return self.fx._push_from_human(files, to=f"fix/{iid}", off=f"origin/fix/{iid}")

    def _updated_from_main(self, iid: str, path: str, text: str) -> None:
        """``iid``'s author updates ``fix/<iid>`` from main ("Update branch"), which conflicts
        in ``path``, and resolves it as ``text``."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", f"fix/{iid}", f"origin/fix/{iid}")
        self.assertNotEqual(sb._run(h, "merge", "-q", "--no-edit", "origin/main").returncode, 0)
        (h / path).write_text(text, encoding="utf-8")
        sb._git(h, "add", path)
        sb._git(h, "commit", "-q", "-s", "--no-edit")
        sb._git(h, "push", "-q", "origin", f"fix/{iid}")

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """``gh pr view <url> --json <fields>`` answered from ``self.gh`` / ``merge_oid``:
        the PR head is the branch's tip on origin, or once the branch is gone the head its PR
        merged with (``merged_at``). An id in ``gh_ok`` is answered that many times, then
        never again (a host that starts failing mid-run). Every ``git push`` is recorded, and
        one of ``refuse`` is refused. Everything else runs for real."""
        if isinstance(args, list) and args[:1] == ["git"] and "push" in args:
            self.pushes.append([str(x) for x in args])
            if args[-1] == self.refuse:
                return subprocess.CompletedProcess(args, 1, "", "! [rejected] (test)")
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        iid = str(args[3]).rsplit("/", 1)[-1]
        if iid in self.gh_ok:
            if self.gh_ok[iid] <= 0:
                self.gh_refused.append(iid)
                return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
            self.gh_ok[iid] -= 1
        if "mergeCommit" in str(args[-1]) and self.gh_fail_merge_commit:
            return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
        head = (self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}")
                else self.merged_at.get(iid, ""))
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

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded line tip (else the base),
        push it, record it."""
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        self.fx._publish(d.name.removeprefix("issue_"), {f"{d.name}.txt": d.name + "\n"},
                         cut_from=tip or (f"origin/{line}" if line else "origin/main"))
        self.gh.setdefault(d.name.removeprefix("issue_"), "OPEN")
        return True

    def _run(self, drive: list[Path], batch: list[str], *, patches=()) -> None:
        """One run: drive ``drive`` with the run asked for ``batch``. ``patches``: more
        ``(object, name, value)`` to patch for it."""
        self.line = integrate.integration_branch(self.cfg, "main", batch)
        self.seen, self.pushes = {}, []
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
            for obj, name, value in patches:
                stack.enter_context(mock.patch.object(obj, name, value))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            self.results = flow._drive_and_act(self.cfg, drive, do_publish=True,
                                               do_act=False, by="T", today=TODAY,
                                               batch=batch)
        self.err, self.out = err.getvalue(), out.getvalue()
        self.assertNotIn("Traceback", self.err)

    # -- assertions ------------------------------------------------------------------------

    def _warnings(self) -> list[str]:
        """The stderr lines saying the line was not brought up to date."""
        return [x for x in self.err.splitlines() if NOT_REFRESHED in x]

    def _one_warning(self, *parts: str) -> str:
        """Exactly ONE such line, naming the line, origin/main and ``parts``."""
        lines = self._warnings()
        self.assertEqual(len(lines), 1, self.err)
        for part in (self.line, "origin/main", *parts):
            self.assertIn(part, lines[0])
        return lines[0]

    def _built_on_line(self, iid: str = "D") -> str:
        """``iid`` was built (COMPLETE) on the run's line; returns the tip it was cut from."""
        self.assertIn(f"issue_{iid}", self.seen, self.err)
        base, tip = self.seen[f"issue_{iid}"]
        self.assertEqual(base, self.line, self.err)
        self.assertTrue(tip)
        self.assertEqual(self.results[iid], state.COMPLETE)
        return tip

    def _held(self, iid: str = "D") -> None:
        self.assertNotIn(f"issue_{iid}", self.seen, self.err)
        self.assertEqual(self.results[iid], state.PLANNED)

    def _line_pushes(self) -> list[list[str]]:
        return [p for p in self.pushes if p[-1] == self.line]

    def _subjects(self, old: str, tip: str) -> list[str]:
        """The first-parent commit subjects on ``tip`` above ``old``, newest first."""
        return sb._git(self.fx.origin, "log", "--first-parent", "--format=%s",
                       f"{old}..{tip}").splitlines()

    def _not_carried(self, iid: str) -> str:
        lines = [x for x in self.err.splitlines()
                 if x.startswith(f"flow: issue_{iid} (finished in an earlier run) is not")]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    # -- (i): the line is brought up to date and the dependent built ------------------------

    def test_a_re_run_brings_the_line_up_to_date_and_builds_the_dependent(self):
        # An earlier run of [W, D] left the line with W's head on it; P's PR merged into main
        # after it was started, so that run held D (#655). The re-run, with the line still on
        # origin and nothing deleted, merges main into the line before wave 0, pushes it
        # without force, and builds D on it. Before: it continued the line as found and held
        # D again.
        self._finished("P", {"p.txt": "p\n"})
        self.after_first_fold = self._merge
        w = self._planned("W")
        d = self._planned("D", MERGED_EDGE + ON_LINE)
        self._run([w, d], ["W", "D"])
        self._held()
        self.assertIn(LACKS, self.err)
        old = self.fx._tip(self.line)
        self.assertFalse(self.fx._ancestor(self.merge_oid["P"], old))

        self._run([d], ["W", "D"])                    # the re-run, the line still on origin
        self.assertIn("carried issue_W", self.err)
        t = self._built_on_line()
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), t))
        self.assertTrue(self.fx._ancestor(self.merge_oid["P"], t))
        self.assertTrue(self.fx._ancestor(old, t))
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/W"), t))
        self.assertNotEqual(t, old)
        self.assertEqual(self._subjects(old, t), ["pdca-integrate: origin/main (brings "
                                                  f"{self.line} up to date)"])
        self.assertTrue(self.fx._ancestor(old, self.line))     # never rewritten
        self.assertTrue(self._line_pushes(), self.pushes)
        for push in self._line_pushes():                       # without force
            self.assertEqual([x for x in push if x.startswith("--force")], [], push)
        self.assertNotIn(LACKS, self.err)
        self.assertEqual(self._warnings(), [])

    # -- (ii): the base does not merge onto the line ----------------------------------------

    def test_a_conflict_drops_the_refresh_with_one_warning_and_the_carry_is_todays(self):
        # W's change to base.txt is on the line; main changes base.txt too, so main does not
        # merge onto the line. The run warns once (the line, origin/main, the file and the
        # bundle that changed it there, the step) and carries onto the line as found: E,
        # built today, is built; D, held today by #655, is held with that message.
        p = self._finished("P", {"p.txt": "p\n"})
        w = self._finished("W", {"base.txt": "w\n"})
        self._finished("Q", {"q.txt": "q\n"})
        batch = ["P", "W", "D", "E"]
        old = self._earlier(batch, p, w)
        self._merge("Q")                              # after the line was started
        self._main({"base.txt": "main\n"})
        d = self._planned("D", "- **Depends on:** W\n- **Depends on (merged):** Q\n"
                          + ON_LINE)
        e = self._planned("E", "- **Depends on:** P\n")
        self._run([d, e], batch)
        said = self._one_warning("base.txt (issue_W)")
        self.assertIn(f"update the PR branch that changed base.txt (issue_W) on {self.line} "
                      "from origin/main (resolving the conflict), then re-run", said)
        self.assertTrue(said.endswith(WITHOUT), said)
        self.assertIn("carried issue_P, issue_W", self.err)
        self.assertEqual(self._built_on_line("E"), old)          # E: as today
        self._held("D")
        held = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(held), 1, self.err)
        self.assertIn(LACKS, held[0])
        self.assertIn(f"delete {self.line} on origin, then re-run", held[0])
        tip = self.fx._tip(self.line)
        self.assertTrue(self.fx._ancestor(old, tip))
        self.assertFalse(self.fx._ancestor(self.fx._tip("main"), tip))

    # -- (iii): the base would remove work a bundle put on the line --------------------------

    def _w_merged_then_reverted(self) -> tuple[str, Path]:
        """W is on the line of a run of [W, D]; main merged W's PR with a merge commit, then
        reverted that merge. Returns the line's tip and D."""
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self._merge("W")
        self.fx._revert_on_main("-m", "1", "HEAD")
        self.assertNotEqual(sb._run(self.fx.origin, "cat-file", "-e", "main:w.txt")
                            .returncode, 0)
        return old, self._planned("D", PLAIN_EDGE + ON_LINE)

    def test_a_base_that_would_remove_work_drops_the_refresh_and_keeps_the_work(self):
        old, d = self._w_merged_then_reverted()
        self._run([d], ["W", "D"])
        said = self._one_warning("would remove issue_W's work", "w.txt (issue_W)")
        for step in ("the line keeps that work",
                     "put it back on origin/main (revert the revert) if the revert was a "
                     "mistake",
                     "drop issue_W from the briefs that name it and re-issue without it",
                     f"deleting {self.line} on origin would build on origin/main without "
                     "that work"):
            self.assertIn(step, said)
        # Not the gone-branch sentence of `_take_in_base`: W's branch is there, and the run
        # is building.
        self.assertNotIn("is gone", said)
        self.assertNotIn("Not building the next wave", said)
        self.assertTrue(said.endswith(WITHOUT), said)
        self.assertIn("carried issue_W", self.err)
        t = self._built_on_line()
        self.assertEqual(t, old)                      # the carry's result is today's
        self.assertEqual(self.fx._show(t, "w.txt"), "w\n")

    def test_a_file_the_base_added_then_deleted_is_said_to_come_from_the_base(self):
        # main adds tmp.txt after the line was started, and a re-run's refresh brings it onto
        # the line. main then deletes it (a cleanup, no one's revert). The line's cut point
        # does not move past a refresh, so the next refresh reads as removing work from the
        # line and is dropped (a known limit: once #894 moves that point, this changes). No
        # bundle put tmp.txt there: the one warning says it came from origin/main and gives
        # deleting the line as the way out, not a revert's steps.
        w = self._finished("W", {"w.txt": "w\n"})
        batch = ["W", "D", "E"]
        self._earlier(batch, w)
        added = self._main({"tmp.txt": "tmp\n"})
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch)     # refresh kept
        self.assertEqual(self._warnings(), [])
        self.assertTrue(self.fx._ancestor(added, self._built_on_line()))
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "scratch", "origin/main")
        sb._git(h, "rm", "-q", "tmp.txt")
        sb._git(h, "commit", "-q", "-s", "-m", "remove tmp.txt")
        sb._git(h, "push", "-q", "origin", "HEAD:refs/heads/main")
        self._run([self._planned("E", PLAIN_EDGE + ON_LINE)], batch)
        said = self._one_warning("tmp.txt", "came onto the line from origin/main, not from a "
                                 "bundle", f"deleting {self.line} on origin loses no bundle's "
                                 "work and is the way out")
        for wrong in ("a revert on origin/main", "revert the revert", "the bundle whose work",
                      "without that work"):
            self.assertNotIn(wrong, said)
        self.assertIn("carried issue_W", self.err)
        self._built_on_line("E")

    def test_a_path_whose_bundle_the_host_cannot_name_keeps_the_revert_steps(self):
        # G is on the line; main merged G's PR with a merge commit, G's branch was deleted,
        # and main reverted that merge. The host answers the carry's own check of G, then
        # fails, so the refresh's check cannot read G's commits and names no bundle for g.txt.
        # The warning must not call deleting the line safe: it keeps the revert's steps.
        w = self._finished("W", {"w.txt": "w\n"})
        g = self._finished("G", {"g.txt": "g\n"})
        batch = ["W", "G", "D"]
        old = self._earlier(batch, w, g)
        self._gone("G")
        self.fx._revert_on_main("-m", "1", "HEAD")
        self.gh_ok["G"] = 1
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch)
        self.assertIn("G", self.gh_refused)
        said = self._one_warning("would remove folded work", "g.txt (last changed by",
                                 f"deleting {self.line} on origin would build on origin/main "
                                 "without that work")
        self.assertNotIn("loses no bundle's work", said)
        t = self._built_on_line()
        self.assertEqual(t, old)
        self.assertEqual(self.fx._show(t, "g.txt"), "g\n")

    # -- (iv): the refreshed line would leave off a carried branch ---------------------------

    def test_a_branch_that_conflicts_only_with_the_refreshed_line_keeps_the_line_as_found(self):
        # A fixup on W's PR changes base.txt; main changes it too. W merges onto the line as
        # found, but not once main is in it: the refresh is dropped, named with W, the file
        # and the step, and W is carried and D built as today.
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        head = self._fixup("W", {"base.txt": "w\n"})
        self._main({"base.txt": "main\n"})
        d = self._planned("D", PLAIN_EDGE + ON_LINE)
        self._run([d], ["W", "D"])
        said = self._one_warning("issue_W", "(base.txt)")
        self.assertIn("update issue_W's PR branch from origin/main, resolving the conflict, "
                      "then re-run", said)
        self.assertTrue(said.endswith(WITHOUT), said)
        self.assertIn("carried issue_W", self.err)
        t = self._built_on_line()
        self.assertEqual(self._subjects(old, t), ["pdca-integrate: issue_W"])   # today's tip
        self.assertTrue(self.fx._ancestor(head, t))
        self.assertFalse(self.fx._ancestor(self.fx._tip("main"), t))

    def test_a_branch_left_off_either_way_does_not_cost_the_refresh(self):
        # (iv-b) X conflicts with W, on the line as found and on the refreshed line alike: the
        # refresh stands, X is held with its own reason (#694), and nothing warns.
        w = self._finished("W", {"w.txt": "w\n"})
        self._finished("X", {"w.txt": "x\n"})
        batch = ["W", "X", "D"]
        old = self._earlier(batch, w)
        self._main({"m.txt": "m\n"})
        d = self._planned("D", PLAIN_EDGE + ON_LINE)
        self._run([d], batch)
        self.assertEqual(self._warnings(), [])
        t = self._built_on_line()
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), t))
        self.assertTrue(self.fx._ancestor(old, t))
        x = self._not_carried("X")
        self.assertIn("Conflicts with: W", x)
        self.assertIn("w.txt", x)
        self.assertNotIn("issue_W (finished in an earlier run) is not", self.err)

    def _v_updated_from_main(self, batch: list[str]) -> str:
        """W is on the line of a run of ``batch``; a fixup on W's PR changes base.txt, main
        changes it too, and V, cut from W's branch then, was updated from main (the conflict
        resolved). Returns the line's tip."""
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(batch, w)
        self._fixup("W", {"base.txt": "w\n"})
        self._main({"base.txt": "main\n"})
        self._finished("V", {"v.txt": "v\n"}, cut_from="origin/fix/W",
                       extra="- **Depends on:** W\n")
        self._updated_from_main("V", "base.txt", "w+main\n")
        return old

    def test_a_hold_inside_the_refreshed_attempt_names_only_the_bundle_left_off(self):
        # (iv-c) On the refreshed line W does not merge and V, which holds W's fixup, comes
        # back off (#672); on the line as found both merge. So the line as found stands, one
        # warning names W (not V), and nothing is held from the abandoned attempt. V brings
        # main in itself, so the line pushed has main, and the warning says so rather than
        # that the line goes on without it.
        batch = ["W", "V", "D"]
        old = self._v_updated_from_main(batch)
        d = self._planned("D", PLAIN_EDGE + ON_LINE)
        self._run([d], batch)
        said = self._one_warning("issue_W", "(base.txt)")
        self.assertNotIn("issue_V", said)
        self.assertTrue(said.endswith(BROUGHT), said)
        self.assertNotIn("without origin/main", said)
        self.assertIn("carried issue_W, issue_V", self.err)
        self.assertNotIn("is not carried", self.err)          # no hold from the abandoned try
        t = self._built_on_line()
        self.assertEqual(self._subjects(old, t),
                         ["pdca-integrate: issue_V", "pdca-integrate: issue_W"])
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), t))

    def test_a_git_step_that_fails_on_the_refreshed_line_keeps_the_line_as_found(self):
        # The brief's default rule: a plain git failure in the passes on the refreshed line
        # (here the read of a skipped branch's work) drops the refresh with one warning that
        # names it, and the carry runs as today on the line as found. The read fails only on
        # the refreshed line (the one the passes measure work from that is not `old`).
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self._fixup("W", {"base.txt": "w\n"})
        self._main({"base.txt": "main\n"})
        real_work, calls = integrate._work, []

        def work(wt, heads, ends, **kw):
            if ends[-1] != old:
                calls.append(ends)
                raise integrate.IntegrationError("could not read issue_W's work (test)")
            return real_work(wt, heads, ends, **kw)

        d = self._planned("D", PLAIN_EDGE + ON_LINE)
        self._run([d], ["W", "D"], patches=[(integrate, "_work", work)])
        self.assertTrue(calls)
        said = self._one_warning("could not read issue_W's work (test)",
                                 "check that checkout, then re-run")
        self.assertTrue(said.endswith(WITHOUT), said)
        self.assertIn("carried issue_W", self.err)
        t = self._built_on_line()
        self.assertEqual(self._subjects(old, t), ["pdca-integrate: issue_W"])   # today's tip

    def test_a_dropped_refresh_with_every_bundle_left_off_says_nothing_is_pushed(self):
        # main conflicts with W's change on the line, so the refresh is dropped. On the line
        # as found every bundle is skipped too: a fixup on each PR conflicts with the other's
        # change there. So nothing is pushed (case 4), and the one warning says that, not
        # that the fold continues the line.
        w = self._finished("W", {"a.txt": "w\n"})
        x = self._finished("X", {"b.txt": "x\n"})
        batch = ["W", "X", "D"]
        old = self._earlier(batch, w, x)
        self._fixup("W", {"b.txt": "w\n"})
        self._fixup("X", {"a.txt": "x\n"})
        self._main({"a.txt": "main\n"})
        d = self._planned("D", PLAIN_EDGE + ON_LINE)
        self._run([d], batch)
        said = self._one_warning("a.txt (issue_W)")
        self.assertTrue(said.endswith(NOTHING), said)
        self.assertNotIn("This fold continues it", said)
        self.assertEqual(self._line_pushes(), [], self.pushes)
        self.assertEqual(self.fx._tip(self.line), old)
        self.assertNotIn("carried issue_", self.err)
        self._not_carried("W")
        self._not_carried("X")
        self._held()

    def test_a_refused_push_after_a_dropped_refresh_says_the_fold_stops(self):
        # (ii)'s conflict drops the refresh; a fixup on W's PR gives the line as found a new
        # merge, and origin refuses the push (#591). The one warning says the fold stops and
        # pushes nothing, not that it continues the line, and W is held with the refusal.
        w = self._finished("W", {"base.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self._fixup("W", {"w2.txt": "w2\n"})
        self._main({"base.txt": "main\n"})
        self.refuse = integrate.integration_branch(self.cfg, "main", ["W", "D"])
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], ["W", "D"])
        self.assertTrue(self._line_pushes(), self.pushes)        # tried, and refused
        said = self._one_warning("base.txt (issue_W)")
        self.assertTrue(said.endswith(STOPS), said)
        self.assertIn(f"could not push {self.line} to origin", self._not_carried("W"))
        self.assertNotIn("carried issue_", self.err)
        self.assertEqual(self.fx._tip(self.line), old)
        self._held()

    # -- the refresh's own git steps failing (case 3): the line as found, as today ----------

    def _behind(self) -> tuple[str, Path]:
        """W is on the line of a run of [W, D]; main moved on since (a file of its own).
        Returns the line's tip and D."""
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self._main({"m.txt": "m\n"})
        return old, self._planned("D", PLAIN_EDGE + ON_LINE)

    def _as_found(self, old: str, *parts: str) -> None:
        """The refresh was dropped with ONE warning naming ``parts``, and the carry did what
        it does today: W carried, D built on the line as found, origin's line left there."""
        said = self._one_warning(*parts)
        self.assertTrue(said.endswith(WITHOUT), said)
        self.assertIn("carried issue_W", self.err)
        self.assertEqual(self._built_on_line(), old)
        self.assertEqual(self.fx._tip(self.line), old)
        self.assertFalse(self.fx._ancestor(self.fx._tip("main"), old))

    def _fails_once(self, name: str, when, result=None, error=integrate.IntegrationError):
        """``integrate.<name>`` failing the first time ``when(*args, **kw)`` holds — raising
        ``error``, or returning ``result`` when given — and real otherwise. Returns the patch
        and the list of the calls it failed."""
        real, hit = getattr(integrate, name), []

        def fake(*a, **kw):
            if not hit and when(*a, **kw):
                hit.append(a)
                if result is not None:
                    return result
                raise error(f"{name} failed (test)")
            return real(*a, **kw)
        return (integrate, name, fake), hit

    def test_a_failed_read_of_whether_the_line_has_the_base_drops_the_refresh(self):
        old, d = self._behind()
        patch, hit = self._fails_once("_in_line", lambda _wt, rev, *a, **k: rev == "origin/main")
        self._run([d], ["W", "D"], patches=[patch])
        self.assertTrue(hit)
        self._as_found(old, "_in_line failed (test)", "check that checkout, then re-run")

    def test_a_base_merge_that_fails_with_no_conflict_drops_the_refresh(self):
        old, d = self._behind()
        patch, hit = self._fails_once("_merge", lambda _wt, rev, _msg: rev == "origin/main",
                                      result=(1, []))
        self._run([d], ["W", "D"], patches=[patch])
        self.assertTrue(hit)
        self._as_found(old, "`git merge` exited 1 with no conflicting path: a git step "
                            "failed; check that checkout, then re-run")

    def test_a_failed_check_for_removed_work_puts_the_line_back(self):
        # The base merged cleanly, then the check for work it would remove fails: the merge
        # is undone (`_reset_line`) before any bundle merges, so what is pushed is the line
        # as found — never a base merge nothing checked.
        old, d = self._behind()
        patch, hit = self._fails_once("_undone_paths", lambda *a, **k: k.get("head") is None)
        self._run([d], ["W", "D"], patches=[patch])
        self.assertTrue(hit)
        self._as_found(old, "_undone_paths failed (test)", "check that checkout, then re-run")
        self.assertNotIn("pdca-integrate: origin/main", sb._git(
            self.fx.origin, "log", "--format=%s", self.line))

    # -- a gone branch while both attempts run: only the one that stands is heard ------------

    def _v_updated_then_g_gone(self) -> tuple[str, list[str], Path]:
        """As in (iv-c), and then G's PR — cut from main once V was updated — merged into
        main with a merge commit, and its branch was deleted. On the refreshed line G's merged
        head is there already ("already carries that commit"); on the line as found it is
        not, and the fold takes main in for it (`_take_in_base`). Returns the line's tip, the
        batch and D."""
        batch = ["W", "V", "G", "D"]
        old = self._v_updated_from_main(batch)
        self._finished("G", {"g.txt": "g\n"})
        self._gone("G")
        return old, batch, self._planned("D", PLAIN_EDGE + ON_LINE)

    def test_a_gone_branch_is_reported_once_by_the_attempt_that_stands(self):
        # The line as found stands (the refreshed line leaves W off). Its one line about G is
        # printed; the refreshed attempt's, which said the opposite twice (once per pass), is
        # not. The fold took main in for G, so the warning says main reaches the line.
        old, batch, d = self._v_updated_then_g_gone()
        self._run([d], batch)
        gone = [x for x in self.err.splitlines() if "issue_G's branch origin/fix/G is gone" in x]
        self.assertEqual(len(gone), 1, self.err)
        self.assertIn(f"the merged work reaches {self.line} through origin/main", gone[0])
        self.assertNotIn("already carries that commit", self.err)
        said = self._one_warning("issue_W", "(base.txt)")
        self.assertTrue(said.endswith(BROUGHT), said)
        self.assertIn("carried issue_W, issue_V, issue_G", self.err)
        t = self._built_on_line()
        self.assertTrue(self.fx._ancestor(old, t))
        self.assertTrue(self.fx._ancestor(self.merge_oid["G"], t))

    def test_a_red_re_gate_of_a_line_a_carried_bundle_brought_the_base_to(self):
        # The same fold with a red re-gate: the refresh was dropped, but the fold put main on
        # the line all the same, so main may be what is red — the hold says so and gives the
        # line step, as after a kept refresh (v).
        old, batch, d = self._v_updated_then_g_gone()
        self.cfg.regate_between_waves = True
        self._run([d], batch, patches=[(flow.gates, "run_integration",
                                        lambda *a, **k: {"overall": "fail"})])
        said = self._not_carried("W")
        for part in (f"this run merged origin/main into {self.line} before the re-gate",
                     "origin/main itself may be red",
                     f"delete {self.line} on origin unless this run's own fold already "
                     "replaced it, then re-issue"):
            self.assertIn(part, said)
        self.assertNotIn("get issue_W's PR open", said)
        self.assertTrue(self._one_warning("issue_W").endswith(BROUGHT))
        self._held()
        tip = self.fx._tip(self.line)
        self.assertTrue(self.fx._ancestor(old, tip))
        self.assertTrue(self.fx._ancestor(self.merge_oid["G"], tip))

    def _red_re_gate_with_a_failed_first_read(self, batch: list[str], d: Path) -> str:
        """Run with a red re-gate, the fold's first read of whether the line has main failing
        once (so the refresh is dropped). Returns that one warning."""
        self.cfg.regate_between_waves = True
        patch, hit = self._fails_once("_in_line",
                                      lambda _wt, rev, *a, **k: rev == "origin/main")
        self._run([d], batch, patches=[patch, (flow.gates, "run_integration",
                                               lambda *a, **k: {"overall": "fail"})])
        self.assertEqual(len(hit), 1)
        return self._one_warning("_in_line failed (test)", "check that checkout, then re-run")

    def test_a_failed_first_read_does_not_hide_a_base_merge_from_a_red_re_gate(self):
        # The same line, but the first read of whether it has main fails, so nothing is
        # known of the line as found then. V and G bring main onto it all the same. Read
        # again once the passes are over, the line as found lacked main, so this fold put
        # main there: the hold names that merge and gives the line step, as above.
        old, batch, d = self._v_updated_then_g_gone()
        said = self._red_re_gate_with_a_failed_first_read(batch, d)
        self.assertTrue(said.endswith(BROUGHT), said)
        held = self._not_carried("W")
        for part in (f"this run merged origin/main into {self.line} before the re-gate",
                     "origin/main itself may be red",
                     f"delete {self.line} on origin unless this run's own fold already "
                     "replaced it, then re-issue"):
            self.assertIn(part, held)
        self.assertNotIn("get issue_W's PR open", held)
        self._held()
        tip = self.fx._tip(self.line)
        self.assertTrue(self.fx._ancestor(old, tip))
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), tip))

    def test_a_failed_first_read_of_a_line_that_had_the_base_is_no_base_merge(self):
        # The same failed first read, on a line that had main already (P merged before the
        # line was started). Read again, the line as found had it: this fold put nothing of
        # main on it, so a red re-gate gets today's hold, and the warning does not say that
        # a carried bundle brought main in.
        self._finished("P", {"p.txt": "p\n"})
        self._merge()                                  # before the line was started
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), old))
        said = self._red_re_gate_with_a_failed_first_read(
            ["W", "D"], self._planned("D", PLAIN_EDGE + ON_LINE))
        self.assertTrue(said.endswith(HAS), said)
        held = self._not_carried("W")
        self.assertIn(f"the re-gate of {self.line} carrying it is red", held)
        self.assertNotIn("this run merged origin/main", held)
        self.assertIn("get issue_W's PR open", held)               # today's step
        self._held()
        self.assertEqual(self.fx._tip(self.line), old)

    def test_a_host_read_that_fails_in_both_attempts_is_printed_once(self):
        # W is on the line; G's PR merged with a merge commit and its branch is gone; main
        # moved on. The host answers the carry's own check of G's PR, then fails (a rate
        # limit, say). Both attempts run and each leaves G off, so the refreshed one stands;
        # what the host's failure made the other one say is not printed.
        w = self._finished("W", {"w.txt": "w\n"})
        batch = ["W", "G", "D"]
        old = self._earlier(batch, w)
        self._g_gone_and_host_down()
        self._main({"m.txt": "m\n"})
        d = self._planned("D", PLAIN_EDGE + ON_LINE)
        self._run([d], batch)
        self.assertGreaterEqual(self.gh_refused.count("G"), 2)   # once in each attempt
        said = [x for x in self.err.splitlines() if x.startswith(NO_STATE)]
        self.assertEqual(len(said), 1, self.err)
        self.assertEqual(self._warnings(), [])
        self.assertIn("is not known to be merged", self._not_carried("G"))
        self.assertIn("carried issue_W", self.err)
        t = self._built_on_line()
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), t))
        self.assertTrue(self.fx._ancestor(old, t))

    # -- a host read the refresh's own check makes, which the passes make again ---------------

    def _g_gone_and_host_down(self) -> None:
        """G finished; its PR merged into main with a merge commit and its branch was deleted.
        The host answers the carry's own check of G's PR, then fails (a rate limit, say)."""
        self._finished("G", {"g.txt": "g\n"})
        self._gone("G")
        self.gh_ok["G"] = 1

    def _host_failure_said_once(self) -> None:
        """The host failed for G in the refresh's check (naming whose a path is) and again in
        the passes on the line as found: one line says so, before the one warning."""
        self.assertGreaterEqual(self.gh_refused.count("G"), 2)
        said = [x for x in self.err.splitlines() if x.startswith(NO_STATE)]
        self.assertEqual(len(said), 1, self.err)
        self.assertLess(self.err.index(NO_STATE), self.err.index(NOT_REFRESHED))
        self.assertIn("is not known to be merged", self._not_carried("G"))
        self.assertIn("carried issue_W", self.err)

    def test_a_host_read_the_conflict_check_and_the_passes_both_make_is_said_once(self):
        # W's change to base.txt is on the line and main changes base.txt, so the refresh
        # conflicts. Naming whose change base.txt is, its check asks the host about G's gone
        # branch; the passes on the line as found ask again. Both fail; one line says so.
        w = self._finished("W", {"base.txt": "w\n"})
        batch = ["W", "G", "D"]
        old = self._earlier(batch, w)
        self._g_gone_and_host_down()
        self._main({"base.txt": "main\n"})
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch)
        self._one_warning("base.txt (issue_W)")
        self._host_failure_said_once()
        self.assertEqual(self._built_on_line(), old)

    def test_a_host_read_the_removal_check_and_the_passes_both_make_is_said_once(self):
        # The same with main having merged and then reverted W's PR: the check for work the
        # refresh would remove asks the host about G, and the passes ask again.
        w = self._finished("W", {"w.txt": "w\n"})
        batch = ["W", "G", "D"]
        old = self._earlier(batch, w)
        self._merge("W")
        self.fx._revert_on_main("-m", "1", "HEAD")
        self._g_gone_and_host_down()
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch)
        self._one_warning("would remove issue_W's work", "w.txt (issue_W)")
        self._host_failure_said_once()
        self.assertEqual(self._built_on_line(), old)

    # -- a fold that stops on an error still says what it kept back ---------------------------

    def _w_x_g(self, w: dict[str, str], *, g_on_line: bool = True) -> tuple[str, list[str]]:
        """W (w.txt and ``w``) and, ``g_on_line``, G are on the line of a run of [W, X, G, D];
        X's branch conflicts with W's w.txt on any line, so every pass skips X and reads its
        work. Then G's PR merged into main with a merge commit and its branch was deleted: a
        pass on a line with G's merged head says that the line already carries it; one on a
        line without it takes main in for G and says so. Returns the line's tip and the
        batch."""
        bundles = [self._finished("W", {"w.txt": "w\n", **w})]
        bundles.append(self._finished("G", {"g.txt": "g\n"}))
        self._finished("X", {"w.txt": "x\n"})
        batch = ["W", "X", "G", "D"]
        old = self._earlier(batch, *bundles[:2 if g_on_line else 1])
        self._gone("G")
        return old, batch

    def _work_fails(self, old: str, *, refreshed=None, found=None):
        """``integrate._work`` raising ``refreshed`` (an exception class) when it reads a
        bundle's work on the refreshed line, and ``found`` when on the line as found (``old``);
        None: the real read. Returns the patch and, in order, the line of each read that
        failed and of every read."""
        real, failed, reads = integrate._work, [], []

        def work(wt, heads, ends, **kw):
            where = "the line as found" if ends[-1] == old else "the refreshed line"
            error = found if ends[-1] == old else refreshed
            reads.append(where)
            if error is None:
                return real(wt, heads, ends, **kw)
            failed.append(where)
            raise error(f"could not read {kw.get('what')} on {where} (test)")
        return (integrate, "_work", work), failed, reads

    def _stopped(self, old: str, error: str) -> tuple[list[str], list[str]]:
        """The fold stopped on ``error``: every clean id is held with it and nothing is pushed.
        Returns stderr's lines and those of them about G's gone branch."""
        lines = self.err.splitlines()
        for iid in ("W", "X", "G"):
            self.assertIn(error, self._not_carried(iid))
        self.assertNotIn("carried issue_", self.err)
        self.assertEqual(self._line_pushes(), [], self.pushes)
        self.assertEqual(self.fx._tip(self.line), old)
        self._held()
        return lines, [x for x in lines if "issue_G's branch origin/fix/G is gone" in x]

    def test_a_fold_that_stops_in_the_passes_on_the_line_as_found_says_what_they_said(self):
        # A fixup on W's PR conflicts with main, so the refreshed line leaves W off and the
        # line as found is tried after it; the fold stops there. What those passes said is
        # printed, as a fold without a refresh prints it before it stops. Whether the
        # refresh would have stood was never decided, so no warning.
        old, batch = self._w_x_g({})
        self._fixup("W", {"base.txt": "w\n"})
        self._main({"base.txt": "main\n"})
        patch, failed, reads = self._work_fails(old, found=integrate.IntegrationError)
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch, patches=[patch])
        self.assertEqual(failed, ["the line as found"])
        self.assertEqual(reads[0], "the refreshed line")       # the refreshed line came first
        _lines, gone = self._stopped(
            old, "could not read issue_X's work on the line as found (test)")
        self.assertEqual(len(gone), 1, self.err)
        self.assertIn("already carries that commit", gone[0])
        self.assertEqual(self._warnings(), [])

    def test_a_fold_that_stops_after_a_dropped_refresh_still_warns(self):
        # W's change to base.txt is on the line and main changes it too, so the refresh is
        # dropped and only the line as found is tried; the fold stops there. The one warning
        # still names the conflict and its step, after what the passes said, and says that
        # the fold stops.
        old, batch = self._w_x_g({"base.txt": "w\n"})
        self._main({"base.txt": "main\n"})
        patch, failed, reads = self._work_fails(old, found=integrate.IntegrationError)
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch, patches=[patch])
        self.assertEqual(failed, ["the line as found"])
        self.assertEqual(reads, ["the line as found"])         # only the line as found
        lines, gone = self._stopped(
            old, "could not read issue_X's work on the line as found (test)")
        self.assertEqual(len(gone), 1, self.err)
        self.assertIn("already carries that commit", gone[0])
        said = self._one_warning("base.txt (issue_W)", "then re-run")
        self.assertTrue(said.endswith(STOPS), said)
        self.assertLess(lines.index(gone[0]), lines.index(said))

    def test_a_fold_that_stops_in_the_passes_on_the_refreshed_line_says_what_they_said(self):
        # The refresh is kept and the passes on it stop the fold (a tree error). What they
        # said before that is printed; the refresh was not dropped, so no warning.
        old, batch = self._w_x_g({})
        patch, failed, reads = self._work_fails(old, refreshed=integrate.IntegrationTreeError)
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch, patches=[patch])
        self.assertEqual(failed, ["the refreshed line"])
        self.assertEqual(reads, ["the refreshed line"])
        _lines, gone = self._stopped(
            old, "could not read issue_X's work on the refreshed line (test)")
        self.assertEqual(len(gone), 1, self.err)
        self.assertIn("already carries that commit", gone[0])
        self.assertEqual(self._warnings(), [])

    def test_a_fold_that_stops_after_the_refreshed_passes_failed_says_only_the_last(self):
        # G is not on the line as found. A git step fails in the passes on the refreshed line,
        # which drops the refresh (they said the line carries G's merged head); the passes on
        # the line as found take main in for G, say so, and stop the fold. Only what the
        # passes the fold stopped in said is printed, then the warning saying it stops.
        old, batch = self._w_x_g({}, g_on_line=False)
        patch, failed, reads = self._work_fails(old, refreshed=integrate.IntegrationError,
                                                found=integrate.IntegrationError)
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch, patches=[patch])
        self.assertEqual(failed, ["the refreshed line", "the line as found"])
        self.assertEqual(reads, failed)
        lines, gone = self._stopped(
            old, "could not read issue_X's work on the line as found (test)")
        self.assertEqual(len(gone), 1, self.err)
        self.assertIn(f"the merged work reaches {self.line} through origin/main", gone[0])
        self.assertNotIn("already carries that commit", self.err)
        said = self._one_warning("could not read issue_X's work on the refreshed line (test)",
                                 "check that checkout, then re-run")
        self.assertTrue(said.endswith(STOPS), said)
        self.assertLess(lines.index(gone[0]), lines.index(said))

    def test_a_fold_that_stops_leaving_the_refreshed_line_says_what_it_heard(self):
        # The refreshed line leaves X and G off (X conflicts with W; the host fails for G, so
        # G is not known to be merged), so the line as found is tried next, and checking it
        # out fails. The host's failure for G, said on the refreshed line, is still printed.
        old, batch = self._w_x_g({})
        self.gh_ok["G"] = 1         # the carry's own check of G is answered, then the host fails
        patch, hit = self._fails_once(      # checking out the line as found off the refreshed one
            "_git", lambda repo, *args: args == ("checkout", "-B", self.line, old) and sb._run(
                repo, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode == 0,
            result=1)
        self._run([self._planned("D", PLAIN_EDGE + ON_LINE)], batch, patches=[patch])
        self.assertEqual(len(hit), 1)
        self.assertEqual(self.gh_refused, ["G"])                 # on the refreshed line only
        self._stopped(old, f"could not start {self.line} off {old}")
        said = [x for x in self.err.splitlines() if x.startswith(NO_STATE)]
        self.assertEqual(len(said), 1, self.err)
        self.assertEqual(self._warnings(), [])

    # -- (v): a red re-gate after a kept refresh ---------------------------------------------

    def test_a_red_re_gate_after_a_refresh_names_the_merge_and_gives_the_line_step(self):
        self._finished("P", {"p.txt": "p\n"})
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self._merge()
        d = self._planned("D", MERGED_EDGE + ON_LINE)
        self.cfg.regate_between_waves = True
        self._run([d], ["W", "D"], patches=[(flow.gates, "run_integration",
                                             lambda *a, **k: {"overall": "fail"})])
        said = self._not_carried("W")
        for part in ("re-gate", self.line,
                     f"this run merged origin/main into {self.line} before the re-gate",
                     "origin/main itself may be red",
                     f"delete {self.line} on origin unless this run's own fold already "
                     "replaced it, then re-issue"):
            self.assertIn(part, said)
        self.assertNotIn("get issue_W's PR open", said)
        self._held()
        tip = self.fx._tip(self.line)                  # at the tip the carry pushed
        self.assertNotEqual(tip, old)
        self.assertTrue(self.fx._ancestor(old, tip))
        self.assertTrue(self.fx._ancestor(self.merge_oid["P"], tip))

    # -- (vi): a line already up to date ------------------------------------------------------

    def test_a_line_that_has_the_base_gets_no_new_commit(self):
        self._finished("P", {"p.txt": "p\n"})
        self._merge()                                  # before the line was started
        w = self._finished("W", {"w.txt": "w\n"})
        old = self._earlier(["W", "D"], w)
        self.assertTrue(self.fx._ancestor(self.fx._tip("main"), old))
        d = self._planned("D", MERGED_EDGE + ON_LINE)
        self._run([d], ["W", "D"])
        self.assertIn("carried issue_W", self.err)
        self.assertEqual(self._built_on_line(), old)
        self.assertEqual(self._warnings(), [])

    # -- (vii): a dry-run plans the merge, conditionally ---------------------------------------

    def test_a_dry_run_plans_the_merge_and_a_wave_fold_does_not(self):
        self._finished("W", {"w.txt": "w\n"})
        d = self._planned("D", PLAIN_EDGE)
        e = self._planned("E", "- **Depends on:** D\n")
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
        self._run([d, e], ["W", "D", "E"])
        plans = ["integrate --dry-run" + x for x in self.out.split("integrate --dry-run")[1:]]
        carry = [x for x in plans if "(issue_W)" in x]
        wave = [x for x in plans if "(issue_D)" in x]
        self.assertEqual(len(carry), 1, self.out)
        self.assertEqual(len(wave), 1, self.out)
        self.assertIn(f"git merge --no-ff --signoff origin/main   (into {self.line}, if origin "
                      "has it and it is behind origin/main)", carry[0])
        self.assertNotIn("git merge --no-ff --signoff origin/main", wave[0])
        self.assertFalse(self.fx._has(self.line))      # nothing pushed

    # -- (viii): the #655 hold gives the re-run as the way out --------------------------------

    def test_the_hold_names_the_re_run_and_keeps_the_delete_step(self):
        self._finished("P", {"p.txt": "p\n"})
        self.after_first_fold = self._merge
        self._run([self._planned("W"), self._planned("D", MERGED_EDGE + ON_LINE)], ["W", "D"])
        self._held()
        held = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(held), 1, self.err)
        for part in (LACKS, "re-run with the same ids",
                     f"brings {self.line} up to date with main",
                     "before wave 0", "builds issue_D once the line has the merge",
                     "reports that refresh dropped",
                     f"delete {self.line} on origin, then re-run"):
            self.assertIn(part, held[0])

    def test_a_line_not_known_to_lack_the_merge_keeps_the_delete_step_alone(self):
        self._finished("P", {"p.txt": "p\n"})
        self._merge()
        self.gh_fail_merge_commit = True
        self._run([self._planned("W"), self._planned("D", MERGED_EDGE + ON_LINE)], ["W", "D"])
        held = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(held), 1, self.err)
        self.assertIn("is not known to have that merge", held[0])
        self.assertIn(f"delete {self.line} on origin, then re-run", held[0])
        self.assertIn("may continue it and hold issue_D again", held[0])
        self.assertNotIn("re-run with the same ids", held[0])

    # -- (ix): a refresh that cannot be undone stops the fold ----------------------------------

    def test_a_refresh_that_cannot_be_undone_stops_the_fold_and_holds_every_clean_id(self):
        old, d = self._w_merged_then_reverted()
        broken = mock.Mock(side_effect=integrate._LineBroken("could not put the line back"))
        self._run([d], ["W", "D"], patches=[(integrate, "_reset_line", broken)])
        self.assertTrue(broken.called)
        self.assertEqual(self.fx._tip(self.line), old)          # nothing pushed
        self.assertNotIn("carried issue_W", self.err)
        self.assertIn("could not put the line back", self._not_carried("W"))
        self.assertEqual(self._warnings(), [])
        self._held()

    def test_a_refresh_merge_that_cannot_read_the_line_stops_the_fold(self):
        # The other error that stops the fold: the refresh's merge cannot read the line's tip
        # (`IntegrationTreeError`). Nothing is pushed and every clean id is held, as when a
        # fold fails today.
        old, d = self._behind()
        patch, hit = self._fails_once("_merge", lambda _wt, rev, _msg: rev == "origin/main",
                                      error=integrate.IntegrationTreeError)
        self._run([d], ["W", "D"], patches=[patch])
        self.assertEqual(len(hit), 1)
        self.assertEqual(self._line_pushes(), [], self.pushes)
        self.assertEqual(self.fx._tip(self.line), old)
        self.assertNotIn("carried issue_W", self.err)
        self.assertIn("_merge failed (test)", self._not_carried("W"))
        self.assertEqual(self._warnings(), [])
        self._held()

    # -- (x): a re-run with no carry -----------------------------------------------------------

    def test_a_re_run_with_no_carry_starts_the_line_fresh_with_no_refresh(self):
        # D names P only in `Depends on (merged)`: no plain edge to a finished id, so the
        # re-run has no carry, and the target's first fold starts fresh from main.
        self._finished("P", {"p.txt": "p\n"})
        self.after_first_fold = self._merge
        w = self._planned("W")
        d = self._planned("D", "- **Depends on (merged):** P\n" + ON_LINE)
        e = self._planned("E", "- **Depends on:** D\n")
        self._run([w, d, e], ["W", "D", "E"])
        self._held()
        old = self.fx._tip(self.line)
        self._run([d, e], ["W", "D", "E"])
        self.assertNotIn("carried", self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self._warnings(), [])
        self.assertTrue(self._line_pushes(), self.pushes)
        self.assertIn("--force", self._line_pushes()[0])        # fresh, as today
        tip = self.fx._tip(self.line)
        self.assertFalse(self.fx._ancestor(old, tip))
        self.assertTrue(self.fx._ancestor(self.merge_oid["P"], tip))


if __name__ == "__main__":
    unittest.main()
