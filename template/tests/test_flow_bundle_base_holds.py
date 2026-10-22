"""A bundle whose own base cannot be built is held, not the run (#738).

Since #727 a stack-mode bundle is built on a base of its own (``integrate.build_bundle_base``,
#723) holding only what it declares. When that base could not be built — ``main`` moved
mid-run and clashes with a prerequisite, an in-batch prerequisite's branch was rewritten
after the line took it (#633), the fetch failed, origin refused the push of the base's
branch — the flow printed ``STOPPING`` and dropped the rest of the run, bundles that had
nothing to do with the failure included. Now that bundle is held, named on one stderr line
with the cause and a fix that works for that cause; a bundle that declared ``Depends on`` it
is skipped and one that declared ``Conflicts with`` it is held; and the rest of the wave
goes on.

Two failures still stop the run (the 2026-10-18 re-plan): a broken shared integration
worktree — the tree not put back to a known commit (``IntegrationTreeError``, #631), or the
branch checked out in it not put back (``integrate._LineBroken``, #623) — and a rewritten
prerequisite at the NEXT fold (#633). So a rewrite hold says the next fold stops the run
unless the branch is restored first, when that fold follows: something of the hold's wave is
left to build, and a later wave remains. A broken worktree after a hold in the same wave
stops the run only once that hold is named. And a hold does not take the rewritten branch
out of the end-of-run check (#695): a rewrite the line still carries at the end of the run
fails it (exit 1), and one mended before then fails nothing.

These cases drive ``flow._drive_and_act`` through real folds and real base builds on the
``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare ``origin`` and a
primary checkout), loaded by path exactly as tests/test_flow_same_wave_conflict.py does,
with that file's stubs: the build and sign-off leaves stubbed (``flow._drive_wave`` writes a
``patch.diff`` and accepts), ``flow._publish_bundle`` cutting and pushing each PR branch from
the recorded stack-base tip, and ``gh`` answered by a stub in front of ``subprocess.run``. The
publisher is NOT a stub (a stub turns every fold into a dry-run). The exit code is the one
``cli._flow`` gives the same run: ``cli._results_rc`` over the results and holds.

Failures are made for one bundle's base only — while ``integrate.build_bundle_base`` runs
for it, ``issue_B``'s but in one case (``issue_W``'s too) — and never at the flow's own
handling. Five at the git call that fails in the field (the ``integrate._git`` call returns
non-zero, so integrate's own raise fires with its own words): the fetch inside
``integrate._fetch``, the push of the base's branch, the ``reset`` of the shared worktree
(``integrate._reset_tree``), the ``checkout -B`` that starts the base's branch, and the
``checkout`` that puts that branch back after a trial merge (``integrate._trial``). Two by
changing the files on disk while that one base is built, put back after it: the checkout
moved away, and a plain directory left where the integration worktree goes. Everything else
is real git.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions, all but the two
for a broken worktree that stops the run at the base that broke it (its tree, or the branch
in it), which pin behaviour the fix keeps.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_bundle_base_holds
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import shutil
import subprocess
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import cli, flow, integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-18"
#: What a base hold's line says the hold does, after its cause and fix.
HOLD_DOES = ("is not built this run: a bundle that depends on it is skipped, one tied to it "
             "by `Conflicts with` is held, and the rest of this wave goes on.")
#: What a hold line must not promise, since the run does not do it: a dependent is skipped
#: (not held), and a later fold can still stop the run.
NOT_DONE = ("held with it", "the rest of the run goes on")
#: The fix a hold line gives for a fetch that failed — and for no other cause.
CHECK_REMOTE = "check the remote"


class BundleBaseHolds(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.changes: dict[str, dict[str, str]] = {}   # id → the files its build adds
        self.after_fold: list[Callable[[], None]] = []   # run once, after the run's next fold
        self.after_publish: dict[str, Callable[[], None]] = {}  # id → run once it published
        # id → the git step that fails (returns 1) while that id's own base is built
        self.git_fails: dict[str, tuple[str, ...]] = {}
        self.push_refused_for: set[str] = set()        # ids whose own base's push is refused
        self.checkout_gone_for: set[str] = set()       # ids built with the checkout moved away
        self.worktree_blocked_for: set[str] = set()    # ids built with no integration worktree
        self.building: str | None = None               # the bundle whose base is being built
        self.failed_git: list[tuple[str, ...]] = []    # the git steps the stubs failed
        self.folds = 0                                 # the run's folds that returned
        # bundle → (stack base, recorded tip) when it was built
        self.seen: dict[str, tuple[str, str]] = {}
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _plan(self, iid: str, files: dict[str, str], *, depends_on: str = "",
              conflicts_with: str = "", stacks_on: str = "") -> None:
        sb._brief(self.cfg.bundle(iid),
                  extra=(f"- **Depends on:** {depends_on}\n" if depends_on else "")
                  + (f"- **Conflicts with:** {conflicts_with}\n" if conflicts_with else "")
                  + (f"- **Stacks on:** {stacks_on}\n" if stacks_on else ""))
        self.changes[iid] = files

    def _a_z_then_b_w(self) -> list[str]:
        """``A`` and ``Z`` (wave 0, files of their own); ``B`` (``Depends on: A``) and ``W``
        (``Depends on: Z``) in wave 1, each built on a base of its own. ``W`` has nothing
        to do with ``B``. Returns the ids."""
        self._plan("A", {"a.txt": "a\n"})
        self._plan("Z", {"z.txt": "z\n"})
        self._plan("B", {"b.txt": "b\n"}, depends_on="A")
        self._plan("W", {"w.txt": "w\n"}, depends_on="Z")
        return ["A", "Z", "B", "W"]

    def _rewrite_a_after_the_fold(self) -> list[str]:
        """Once the fold after wave 0 has put ``A`` on the line, ``fix/A`` is force-pushed to
        a commit that does not hold the head the line took (#633). Returns a list that gets
        that old head."""
        took: list[str] = []

        def rewrite() -> None:
            took.append(self._head("A"))
            self.fx._push_from_human({"a2.txt": "A, rebuilt\n"}, to="fix/A", force=True)

        self.after_fold.append(rewrite)
        return took

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """Any ``gh`` call is answered OPEN with the PR's head, its branch's tip on origin;
        everything else runs for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        iid = str(args[3]).rsplit("/", 1)[-1]
        head = self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else ""
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"state": "OPEN", "headRefOid": head}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle, recording what it was built on."""
        for d in wave:
            iid = d.name.removeprefix("issue_")
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d))
            (d / "patch.diff").write_text("".join(
                sb._new_file_patch(n, t.rstrip("\n")) for n, t in self.changes[iid].items()),
                encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded stack-base tip (else that
        branch, else the base), push it, record it. The run's brief is kept as it was."""
        iid = d.name.removeprefix("issue_")
        tip, base = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = tip or (f"origin/{base}" if base else "origin/main")
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.fx._publish(iid, self.changes[iid], cut_from=cut)
        (d / "brief.md").write_text(brief, encoding="utf-8")
        self.after_publish.pop(iid, lambda: None)()
        return True

    def _run(self, ids: list[str]) -> None:
        """One stack-mode run asked for ``ids``, driving all of them."""
        self.ids = list(ids)
        self.line = integrate.integration_branch(self.cfg, "main", ids)
        self.holds: dict[str, str] = {}
        real_fold, real_build, real_git = integrate.fold, integrate.build_bundle_base, \
            integrate._git
        refuse = {integrate.bundle_base_branch(self.cfg, "main", ids, f"issue_{i}")
                  for i in self.push_refused_for}

        def fold(*a, **kw):
            out = real_fold(*a, **kw)
            self.folds += 1
            hooks, self.after_fold = self.after_fold, []
            for hook in hooks:
                hook()
            return out

        def build(cfg, d: Path, *a, **kw):
            """The real base build, noting whose base it is for the git stub below, with the
            files on disk changed for that one base (:meth:`_disk_fault`)."""
            iid = d.name.removeprefix("issue_")
            self.building = iid
            try:
                with self._disk_fault(iid):
                    return real_build(cfg, d, *a, **kw)
            finally:
                self.building = None

        def git(where: Path, *args: str) -> int:
            """While a base of ``git_fails`` is built, its git step fails; the push of a base
            branch of ``push_refused_for`` is refused; every other git step runs for real."""
            step = self.git_fails.get(self.building or "")
            if (step and args[:len(step)] == step) or (args[:1] == ("push",)
                                                       and args[-1] in refuse):
                self.failed_git.append(args)
                return 1
            return real_git(where, *args)

        self._err = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(integrate, "fold", fold))
            stack.enter_context(mock.patch.object(integrate, "build_bundle_base", build))
            stack.enter_context(mock.patch.object(integrate, "_git", git))
            stack.enter_context(mock.patch.object(integrate, "_FETCH_RETRY_PAUSE", 0))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(self._err))
            self.results = flow._drive_and_act(
                self.cfg, [self.cfg.bundle(i) for i in ids], do_publish=True,
                do_act=False, by="T", today=TODAY, batch=ids, holds=self.holds)
        self.err = self._err.getvalue()
        self.rc = cli._results_rc(self.results, holds=self.holds)
        self.assertNotIn("Traceback", self.err)

    @contextmanager
    def _disk_fault(self, iid: str) -> Iterator[None]:
        """While ``iid``'s own base is built: the checkout moved away
        (``checkout_gone_for``), or the integration worktree removed and a plain directory
        left at its path (``worktree_blocked_for``). Put back once that build returns or
        raises: the checkout moved back; the directory removed, so the next build makes the
        worktree again."""
        primary = self.fx.primary.resolve()
        if iid in self.checkout_gone_for:
            moved = primary.with_name(primary.name + ".moved")
            primary.rename(moved)
            try:
                yield
            finally:
                moved.rename(primary)
        elif iid in self.worktree_blocked_for:
            wt = integrate._integ_worktree(primary, "main")
            sb._git(primary, "worktree", "remove", "--force", str(wt))
            wt.mkdir()
            (wt / "left.txt").write_text("left here\n", encoding="utf-8")
            try:
                yield
            finally:
                shutil.rmtree(wt)
        else:
            yield

    # -- helpers ---------------------------------------------------------------------------

    def _lines(self, *parts: str) -> list[str]:
        """The stderr lines holding every one of ``parts``."""
        return [x for x in self.err.splitlines() if all(p in x for p in parts)]

    def _held(self, iid: str, *parts: str) -> str:
        """``iid`` was never built and is left PLANNED, and ONE stderr line says it is held,
        with each of ``parts``. Returns that line."""
        self.assertNotIn(f"issue_{iid}", self.seen, self.err)
        self.assertEqual(self.results[iid], state.PLANNED, self.err)
        lines = [x for x in self.err.splitlines() if x.startswith(f"flow: issue_{iid} held — ")]
        self.assertEqual(len(lines), 1, self.err)
        for part in parts:
            self.assertIn(part, lines[0])
        return lines[0]

    def _base_held(self, iid: str, *parts: str, fold_next: bool = False) -> str:
        """As :meth:`_held`, for a bundle whose own base could not be built: its line names
        that base and each of ``parts``, then says what the hold does — no more than the
        run does — and, only when a fold follows (``fold_next``), something after that.
        Returns that line."""
        line = self._held(iid, f"could not build issue_{iid}'s own base from ", *parts)
        self.assertIn(f"issue_{iid} {HOLD_DOES}", line)
        if not fold_next:
            self.assertTrue(line.endswith(f"issue_{iid} {HOLD_DOES}"), line)
        for words in NOT_DONE:
            self.assertNotIn(words, line)
        return line

    def _complete(self, *ids: str) -> None:
        """Each of ``ids`` was built and is COMPLETE."""
        for iid in ids:
            self.assertIn(f"issue_{iid}", self.seen, self.err)
            self.assertEqual(self.results[iid], state.COMPLETE, self.err)

    def _head(self, iid: str) -> str:
        return self.fx._tip(f"fix/{iid}")

    # -- (1) main moved under a prerequisite, in the last wave -------------------------------

    def test_a_base_that_moved_under_a_prerequisite_holds_only_its_dependent(self):
        # After wave 0's fold, main gets an a.txt of its own: A's branch no longer merges
        # onto it, so no base of B's can hold A. Before: STOPPING, and W stayed PLANNED.
        ids = self._a_z_then_b_w()
        self.after_fold.append(lambda: self.fx._push_from_human(
            {"a.txt": "main's own\n"}, to="main"))
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        line = self._base_held("B", "issue_A", "origin/main")
        self.assertIn("rebuild issue_a's branch on the new origin/main", line.lower())
        self.assertNotIn(CHECK_REMOTE, line)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    # -- (2) an in-batch prerequisite rewritten after the line took it, in the last wave ----

    def test_a_prerequisite_rewritten_after_the_fold_holds_its_dependent_and_fails_the_run(
            self):
        # After the fold carried A, fix/A is force-pushed to another commit (#633): B's base
        # refuses it, with the restore/re-publish remedy. B's wave is the last, so no fold
        # follows and the line warns of none. The end-of-run check still judges A and names
        # the rewrite again: the line carries commits no PR holds (#695 wins), exit 1.
        ids = self._a_z_then_b_w()
        took = self._rewrite_a_after_the_fold()
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        rewritten = "issue_A's branch origin/fix/A was rewritten"
        line = self._base_held("B", "issue_A", "rewritten", rewritten, took[0][:12],
                               "A re-publish (`pdca publish` force-pushes)",
                               f"Restore origin/fix/A to {took[0][:12]} or a commit that "
                               f"contains it, then re-run")
        self.assertNotIn("next fold", line)
        self.assertNotIn(CHECK_REMOTE, line)
        late = self._lines(rewritten, "No fold of this run ran after the rewrite")
        self.assertEqual(len(late), 1, self.err)
        self.assertEqual(len(self._lines(rewritten)), 2, self.err)   # the hold's, the end's
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base", "A": "rewritten"})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_rewrite_mended_before_the_run_ends_leaves_only_the_hold(self):
        # Case (2), but once W is published — after B's hold, before the run ends — fix/A is
        # put back at the head the line took, as B's hold line asks. The end-of-run check
        # reads fix/A again and finds that head: nothing more is said of A. Exit 3.
        ids = self._a_z_then_b_w()
        took = self._rewrite_a_after_the_fold()
        self.after_publish["W"] = lambda: sb._git(self.fx.origin, "branch", "-f", "fix/A",
                                                  took[0])
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        self._base_held("B", "issue_A", "rewritten", took[0][:12], "Restore origin/fix/A")
        self.assertEqual(self._head("A"), took[0])
        self.assertEqual(self._lines("No fold of this run ran after"), [], self.err)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    def test_a_rewrite_hold_before_the_last_wave_warns_the_next_fold_stops_the_run(self):
        # Case (2)'s shape and V (`Depends on: W`) in wave 2: a fold follows B's wave. B's
        # line says that fold stops the run unless fix/A is restored first — never that the
        # run goes on. Nobody restores it: the fold after wave 1 stops the run (#633), so V
        # is never built. Exit 1.
        ids = self._a_z_then_b_w()
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        took = self._rewrite_a_after_the_fold()
        self._run([*ids, "V"])
        line = self._base_held("B", "issue_A", "rewritten", took[0][:12], fold_next=True)
        self.assertIn("next fold", line)
        self.assertIn("restore", line)
        self.assertIn("the next fold stops the run unless origin/fix/A is restored first", line)
        self._complete("A", "Z", "W")
        stop = self._lines("STOPPING")
        self.assertEqual(len(stop), 1, self.err)
        self.assertIn("flow: wave 1 did not integrate (issue_A's branch origin/fix/A was "
                      "rewritten", stop[0])
        self.assertNotIn("issue_V", self.seen, self.err)
        self.assertEqual(self.results["V"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_rewrite_hold_that_leaves_its_wave_nothing_to_fold_warns_of_no_fold(self):
        # A in wave 0; B (`Depends on: A`) alone in wave 1; C (`Depends on: B`) in wave 2;
        # fix/A force-pushed after wave 0's fold. B's base refuses the rewrite and B is held:
        # nothing of wave 1 is left to build, so no fold follows it — a later wave remains,
        # but C is skipped. So B's line must not say a next fold stops the run: no fold runs
        # after the rewrite, and the end-of-run check judges A (#695 wins). Exit 1.
        self._plan("A", {"a.txt": "a\n"})
        self._plan("B", {"b.txt": "b\n"}, depends_on="A")
        self._plan("C", {"c.txt": "c\n"}, depends_on="B")
        took = self._rewrite_a_after_the_fold()
        self._run(["A", "B", "C"])
        self.assertNotIn("STOPPING", self.err)
        line = self._base_held("B", "issue_A", "rewritten", took[0][:12],
                               "Restore origin/fix/A")
        self.assertNotIn("next fold", line)
        self.assertEqual(self.folds, 1, self.err)          # wave 0's: none after the rewrite
        self.assertNotIn("issue_C", self.seen, self.err)
        self.assertEqual(self.results["C"], state.PLANNED, self.err)
        self.assertEqual(len(self._lines("flow: issue_C skipped — prerequisite(s) not ready "
                                         "(B)")), 1, self.err)
        late = self._lines("issue_A's branch origin/fix/A was rewritten",
                           "No fold of this run ran after the rewrite")
        self.assertEqual(len(late), 1, self.err)
        self._complete("A")
        self.assertEqual(self.holds, {"B": "base", "A": "rewritten"})
        self.assertEqual(self.rc, 1, self.err)

    # -- (3) the fetch of one bundle's base fails --------------------------------------------

    def test_a_fetch_that_fails_for_one_base_holds_only_that_bundle(self):
        # The `git fetch` inside integrate._fetch fails while B's base is built, so _fetch's
        # own raise fires with its own cause. The fix is the remote's, never a rebuild.
        ids = self._a_z_then_b_w()
        self.git_fails["B"] = ("fetch",)
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        self.assertEqual(len(self.failed_git), 1, self.failed_git)   # tried once, failed
        line = self._base_held("B", "issue_A", "fetch", "re-run",
                               f"could not fetch origin in {self.fx.primary.resolve()} — "
                               f"{CHECK_REMOTE} (its URL, the network, credentials), then "
                               f"re-run")
        self.assertNotIn("rebuild", line.lower())
        self.assertNotIn("the fold cannot see", line)     # a fold's words, not a base's
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    # -- (4) origin refuses the push of one bundle's base ------------------------------------

    def test_a_push_origin_refuses_holds_only_that_bundle(self):
        # build_bundle_base's own refusal fires, and its words are the line's cause and fix.
        # The fetch's fix is not given to it: that one is keyed on the fetch's cause.
        ids = self._a_z_then_b_w()
        self.push_refused_for.add("B")
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        name = integrate.bundle_base_branch(self.cfg, "main", ids, "issue_B")
        self.assertEqual([a[-1] for a in self.failed_git], [name])
        line = self._base_held("B", f"could not push {name} to origin",
                               "check origin's rules for pdca-integration/*")
        self.assertNotIn(CHECK_REMOTE, line)
        self.assertFalse(self.fx._has(name))
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    # -- the other failures whose own words name no fix, or a fold's ------------------------

    def test_a_checkout_missing_while_one_base_is_built_holds_only_that_bundle(self):
        # No checkout where the target maps it while B's base is built: the fix is to put
        # one there, not to check a remote.
        ids = self._a_z_then_b_w()
        self.checkout_gone_for.add("B")
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        line = self._base_held("B", f"checkout not found at {self.fx.primary.resolve()} — "
                                    f"clone the target's checkout there, or map the target "
                                    f"to where it is in [publisher.checkouts] in pdca.toml, "
                                    f"then re-run")
        self.assertNotIn(CHECK_REMOTE, line)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    def test_a_worktree_that_cannot_be_made_for_one_base_holds_only_that_bundle(self):
        # The integration worktree is gone while B's base is built, and a plain directory
        # left at its path keeps git from making it again: the fix is at that path.
        ids = self._a_z_then_b_w()
        self.worktree_blocked_for.add("B")
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        wt = integrate._integ_worktree(self.fx.primary.resolve(), "main")
        line = self._base_held("B", f"could not create the integration worktree at {wt} — "
                                    f"check that path (a directory left there that is not a "
                                    f"git worktree blocks it), then re-run")
        self.assertNotIn(CHECK_REMOTE, line)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    def test_a_base_branch_git_cannot_start_holds_only_that_bundle(self):
        # The `git checkout -B` that starts B's base's branch off the base fails: a git step
        # in the integration worktree, whose fix is in that checkout.
        ids = self._a_z_then_b_w()
        self.git_fails["B"] = ("checkout", "-B")
        self._run(ids)
        self.assertNotIn("STOPPING", self.err)
        name = integrate.bundle_base_branch(self.cfg, "main", ids, "issue_B")
        line = self._base_held("B", f"could not start {name} off origin/main — a git step "
                                    f"failed: check that checkout, then re-run")
        self.assertNotIn(CHECK_REMOTE, line)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 3, self.err)

    # -- (5) a `Conflicts with` partner of a held bundle ------------------------------------

    def test_a_conflicts_with_partner_of_a_held_bundle_is_held(self):
        # Case (3)'s shape, and X (`Conflicts with: B`), scheduled after B (wave 2): X must be
        # built on a base holding B's work, and no base or line of this run has it. Before
        # the fix X was not reached (STOPPING); with B held but X's partner check blind to
        # it, X would be built on the plain base.
        ids = self._a_z_then_b_w()
        self._plan("X", {"x.txt": "x\n"}, conflicts_with="B")
        self.git_fails["B"] = ("fetch",)
        self._run([*ids, "X"])
        self.assertNotIn("STOPPING", self.err)
        self._base_held("B", "could not fetch origin")
        line = self._held("X", "`Conflicts with` issue_B", "held before building it",
                          "wait until issue_B is built", "its PR merges into main (org/repo)",
                          "then re-run")
        self.assertNotIn("prerequisite", line)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 1, self.err)          # X is left PLANNED, with no hold

    # -- (6) a PR cut from the line a rewrite left stale ------------------------------------

    def test_a_pr_stacked_on_a_rewritten_prerequisite_is_named_at_the_end(self):
        # Case (2)'s shape, and S (`Stacks on: A`, a seam) in B's wave: S is cut from the
        # line, which holds A's old head. The end-of-run check, which still judges A, names
        # S's PR as holding commits fix/A no longer has (`integrate.rewritten_carried`'s
        # line_prs). Exit 1.
        ids = self._a_z_then_b_w()
        self._plan("S", {"s.txt": "s\n"}, stacks_on="A")
        took = self._rewrite_a_after_the_fold()
        self._run([*ids, "S"])
        self.assertNotIn("STOPPING", self.err)
        rewritten = "issue_A's branch origin/fix/A was rewritten"
        self._base_held("B", rewritten, took[0][:12])
        self._complete("A", "Z", "W", "S")
        self.assertEqual(self.seen["issue_S"][0], self.line)
        self.assertTrue(self.fx._ancestor(took[0], self._head("S")))     # the fixture's fact
        late = self._lines(rewritten, "No fold of this run ran after the rewrite")
        self.assertEqual(len(late), 1, self.err)
        for part in (f"every PR this run cut from {self.line} after it took {took[0][:12]} "
                     f"(issue_S): they hold commits origin/fix/A no longer has",
                     f"Restore origin/fix/A to {took[0][:12]}",
                     "rebuild those PRs on the rewritten branch"):
            self.assertIn(part, late[0])
        self.assertEqual(self.holds, {"B": "base", "A": "rewritten"})
        self.assertEqual(self.rc, 1, self.err)

    # -- (7) a dependent of a held bundle ---------------------------------------------------

    def test_a_dependent_of_a_held_bundle_is_skipped_and_fails_the_run(self):
        ids = self._a_z_then_b_w()
        self._plan("C", {"c.txt": "c\n"}, depends_on="B")
        self.git_fails["B"] = ("fetch",)
        self._run([*ids, "C"])
        self.assertNotIn("STOPPING", self.err)
        self._base_held("B", "could not fetch origin")
        self.assertNotIn("issue_C", self.seen, self.err)
        self.assertEqual(self.results["C"], state.PLANNED, self.err)
        self.assertEqual(len(self._lines("flow: issue_C skipped — prerequisite(s) not ready "
                                         "(B)")), 1, self.err)
        self._complete("A", "Z", "W")
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 1, self.err)          # C is left PLANNED, with no hold

    # -- (8) a broken shared worktree still stops the run -----------------------------------

    def test_a_shared_worktree_that_cannot_be_reset_still_stops_the_run(self):
        # The `git reset` integrate._reset_tree makes fails while B's base is built, so
        # _reset_tree raises IntegrationTreeError (#631): every later build and fold would
        # run in that tree, so this is the run's failure, not B's. The wave loop's base stop,
        # not a fold's, ends the run; B is not held. Holds the same with and without #738.
        ids = self._a_z_then_b_w()
        self.git_fails["B"] = ("reset",)
        self._run(ids)
        stop = self._lines("STOPPING")
        self.assertEqual(len(stop), 1, self.err)
        for part in ("flow: wave 1 — could not build issue_B's own base from issue_A: could "
                     "not reset the integration worktree",
                     "wave 1 and later waves not run"):
            self.assertIn(part, stop[0])
        self.assertNotIn("did not integrate", stop[0])
        self.assertEqual(self._lines("flow: issue_B held"), [], self.err)
        for iid in ("B", "W"):
            self.assertNotIn(f"issue_{iid}", self.seen, self.err)
            self.assertEqual(self.results[iid], state.PLANNED, self.err)
        self.assertNotIn("B", self.holds)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_base_branch_that_will_not_go_back_still_stops_the_run(self):
        # A, Y and Z in wave 0; B (`Depends on: A, Y`) and W (`Depends on: Z`) in wave 1.
        # After wave 0's fold main gets a y.txt of its own, so on B's base Y clashes after A:
        # the build tries Y on main alone (integrate._trial), and the checkout that puts B's
        # base branch back after that trial fails, so integrate._LineBroken is raised (#623).
        # A branch in the shared worktree not put back is no one bundle's: the wave loop's
        # base stop ends the run, as for a tree that cannot be reset; B is not held, and W,
        # whose base has nothing to do with Y, is not built. Holds the same with and without
        # #738.
        self._plan("A", {"a.txt": "a\n"})
        self._plan("Y", {"y.txt": "y\n"})
        self._plan("Z", {"z.txt": "z\n"})
        self._plan("B", {"b.txt": "b\n"}, depends_on="A, Y")
        self._plan("W", {"w.txt": "w\n"}, depends_on="Z")
        ids = ["A", "Y", "Z", "B", "W"]
        self.after_fold.append(lambda: self.fx._push_from_human(
            {"y.txt": "main's own\n"}, to="main"))
        name = integrate.bundle_base_branch(self.cfg, "main", ids, "issue_B")
        self.git_fails["B"] = ("checkout", "-q", name)
        self._run(ids)
        self.assertEqual(self.failed_git, [("checkout", "-q", name)])
        stop = self._lines("STOPPING")
        self.assertEqual(len(stop), 1, self.err)
        for part in (f"flow: wave 1 — could not build issue_B's own base from issue_A, "
                     f"issue_Y: could not put {name} back at ", " after a trial merge",
                     "wave 1 and later waves not run"):
            self.assertIn(part, stop[0])
        self.assertNotIn("did not integrate", stop[0])
        self.assertEqual(self._lines("flow: issue_B held"), [], self.err)
        self._complete("A", "Y", "Z")
        for iid in ("B", "W"):
            self.assertNotIn(f"issue_{iid}", self.seen, self.err)
            self.assertEqual(self.results[iid], state.PLANNED, self.err)
        self.assertNotIn("B", self.holds)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_broken_worktree_after_a_hold_in_its_wave_names_that_hold_first(self):
        # Wave 1 builds B's base, then W's. B's fetch fails: B is held. Then the `git reset`
        # of the shared worktree fails while W's base is built (#631): the run stops at W's
        # base. B's hold is named first, with its cause and fix, so that stop does not hide
        # it — and its line says no more than the run does: no wave goes on after it.
        ids = self._a_z_then_b_w()
        self.git_fails["B"] = ("fetch",)
        self.git_fails["W"] = ("reset",)
        self._run(ids)
        self.assertEqual([a[0] for a in self.failed_git], ["fetch", "reset"], self.failed_git)
        lines = self.err.splitlines()
        stop = [i for i, x in enumerate(lines) if "STOPPING" in x]
        self.assertEqual(len(stop), 1, self.err)
        for part in ("flow: wave 1 — could not build issue_W's own base from issue_Z: could "
                     "not reset the integration worktree", "wave 1 and later waves not run"):
            self.assertIn(part, lines[stop[0]])
        held = [i for i, x in enumerate(lines) if x.startswith("flow: issue_B held — ")]
        self.assertEqual(len(held), 1, self.err)
        self.assertLess(held[0], stop[0], self.err)
        line = lines[held[0]]
        for part in ("could not build issue_B's own base from issue_A: ",
                     f"could not fetch origin in {self.fx.primary.resolve()} — "
                     f"{CHECK_REMOTE} (its URL, the network, credentials), then re-run"):
            self.assertIn(part, line)
        self.assertTrue(line.endswith(". issue_B is not built this run."), line)
        for words in ("goes on", "next fold", *NOT_DONE):
            self.assertNotIn(words, line)
        for iid in ("B", "W"):
            self.assertNotIn(f"issue_{iid}", self.seen, self.err)
            self.assertEqual(self.results[iid], state.PLANNED, self.err)
        self.assertEqual(self.holds, {"B": "base"})
        self.assertEqual(self.rc, 1, self.err)            # W is left PLANNED, with no hold


if __name__ == "__main__":
    unittest.main()
