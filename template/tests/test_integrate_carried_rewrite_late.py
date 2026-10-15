"""A carried branch rewritten after the run's last fold is reported, and the run exits 1;
one failed fetch does not stop a fold (#695).

#633 made a fold refuse a branch an earlier fold of the run carried and that was rewritten
since — but only at the start of the NEXT fold, and a fold runs only between waves. A
rewrite during the run's last fold (after its check read the tip) or after it (while the
last wave builds, publishes, or Act runs) left the line with commits no PR holds any more,
and the run ended as if all was well: every bundle COMPLETE, exit 0. And the fetch that
check makes before the fold takes any lock failed the fold on the first clash with another
git process in the same checkout.

The run cases drive the WHOLE CLI route, ``cli._flow``, through real folds on the
``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare ``origin`` and a
primary checkout), loaded by path and used as a helper object, exactly as
tests/test_flow_hold_exit_code.py does, with that file's stubs: the build and sign-off
leaves stubbed (``flow._drive_wave`` writes a ``patch.diff`` and accepts),
``flow._publish_bundle`` cutting and pushing the PR branch, and ``gh`` answered by a stub in
front of ``subprocess.run``. The publisher is NOT a stub (a stub turns every fold into a
dry-run). The exit code is read ONLY from what ``cli._flow`` returns.

Three run cases make the end-of-run check fail in part, through an ``integrate._git`` spy
that starts failing once the run's last fold has pushed: every fetch (the run cannot tell
whether its line still matches its PRs, so it names what to check by hand, reports no
rewrite, and exits 1); the fetch in the checkout of a second target, checked after or
before a target with a rewrite; and the ancestry check of one branch, beside a rewritten
one on the same target. A branch that cannot be checked must never hide a rewrite found
on another, nor stop the others being checked. The line that names it says what failed in
those words only — never the step a fold's error gives (fix the checkout, then re-run),
which is wrong once the run has pushed its line — and what to do for a branch found
rewritten by hand.
The fetch cases call ``integrate.fold`` on the same fixture, with an ``integrate._git``
spy failing fetches; a fold stopped by its own check still gives its error word for word.

Only modules are imported (never a symbol the fix adds), and the fix's retry pause is
patched to 0 with ``create=True``, so with the production change reverted these cases still
load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_integrate_carried_rewrite_late
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import subprocess
import unittest
from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, flow, integrate, leaves, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

#: How the run starts the line it prints for branches its end-of-run check could not judge
#: (`flow.py`).
_UNCHECKED = "could not check whether a branch this run folded onto its line was rewritten"


#: Words of the step a fold's own error tells it to take (`_in_line`, `_fetch`) — wrong at
#: the end of a run that has pushed its line, so never in that line.
_FOLD_ADVICE = ("re-run", "check that checkout", "the fold cannot see")


def _rewritten(iid: str) -> str:
    """The words `_carried_tips` names bundle ``iid``'s rewritten branch with
    (`integrate.py`)."""
    return f"issue_{iid}'s branch origin/fix/{iid} was rewritten after this run folded it onto"


def _no_retry_pause(test: unittest.TestCase) -> None:
    """No real pause between two tries of a fetch (``create=True``: absent before the fix)."""
    pause = mock.patch.object(integrate, "_FETCH_RETRY_PAUSE", 0, create=True)
    pause.start()
    test.addCleanup(pause.stop)


class LateRewrite(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        _no_retry_pause(self)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.changes: dict[str, dict[str, str]] = {}   # id → the files its build adds
        self.no_push: set[str] = set()                 # ids whose publish pushes no branch
        self.during: dict[str, Callable[[], None]] = {}  # id → run while it builds
        self.heads: dict[str, str] = {}                # id → the head its publish pushed
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _plan(self, iid: str, depends_on: str = "", *,
              target: str = "org/repo @ main") -> None:
        """Bundle ``iid``'s brief, as the fixture's ``_brief`` writes it but on ``target``,
        and the file its build adds."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        dep = f"- **Depends on:** {depends_on}\n" if depends_on else ""
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** {target}\n{dep}",
            encoding="utf-8")
        self.changes[iid] = {f"{iid.lower()}.txt": f"{iid}\n"}

    def _rewrite(self, iid: str = "A") -> str:
        """Force-push ``fix/<iid>`` as a commit cut from the base: it lacks the head the
        line took (a re-publish leaves it so). Returns the rewrite."""
        head = self.fx._tip(f"fix/{iid}")
        rebuilt = self.fx._push_from_human({f"{iid.lower()}2.txt": f"{iid}, rebuilt\n"},
                                           to=f"fix/{iid}", force=True)
        self.assertFalse(self.fx._ancestor(head, rebuilt))   # a rewrite, not a fast-forward
        return rebuilt

    def _second_checkout(self, repo_spec: str) -> Path:
        """The checkout of a second target repo, ``repo_spec``: a clone of the same origin,
        so a bundle on it (base ``zbase``) folds onto a line of its own, fetched in a
        checkout of its own. Returns its resolved path."""
        sb._git(self.fx.origin, "branch", "zbase", "main")
        z = self.fx.tmp / repo_spec.rsplit("/", 1)[-1]
        subprocess.run(["git", "clone", "-q", str(self.fx.origin), str(z)], check=True,
                       capture_output=True)
        sb._identity(z)
        self.cfg.repo_checkouts[repo_spec] = str(z)
        return z.resolve()

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """Any ``gh`` call is answered OPEN; everything else runs for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"state": "OPEN", "headRefOid": ""}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle, running its ``during`` hook first."""
        for d in wave:
            iid = d.name.removeprefix("issue_")
            if iid in self.during:
                self.during[iid]()
            (d / "patch.diff").write_text(
                "".join(sb._new_file_patch(n, t.rstrip("\n"))
                        for n, t in self.changes[iid].items()), encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded line tip (else the line,
        else the base), push it, record it — except for an id in ``no_push``, whose publish
        fails before its push."""
        iid = d.name.removeprefix("issue_")
        if iid in self.no_push:
            return False
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = tip or (f"origin/{line}" if line else "origin/main")
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.fx._publish(iid, self.changes[iid], cut_from=cut)
        (d / "brief.md").write_text(brief, encoding="utf-8")
        self.heads[iid] = self.fx._tip(f"fix/{iid}")
        return True

    def _flow(self, ids: list[str], *, git: Callable | None = None) -> int:
        """One stack-mode ``pdca flow <ids…>``, publishing on, Act off, through
        ``cli._flow``; returns its exit code. ``git`` replaces ``integrate._git``."""
        args = SimpleNamespace(issue_ids=list(ids), from_csv=None, from_briefs=None,
                               no_publish=False, no_act=True, by="T", lanes=None)
        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(leaves, "do_plan_batch",
                                                  lambda *a, **k: None))
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            if git is not None:
                stack.enter_context(mock.patch.object(integrate, "_git", git))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            rc = cli._flow(self.cfg, args)
        self.err, self.out = err.getvalue(), out.getvalue()
        self.assertNotIn("Traceback", self.err)
        return rc

    def _two_waves(self) -> list[str]:
        """``A`` (wave 0), then ``B`` depending on it (wave 1, the last)."""
        self._plan("A")
        self._plan("B", "A")
        return ["A", "B"]

    def _rewrite_lines(self, iid: str = "A") -> list[str]:
        return [ln for ln in self.err.splitlines() if _rewritten(iid) in ln]

    def _unchecked_lines(self) -> list[str]:
        return [ln for ln in self.err.splitlines() if _UNCHECKED in ln]

    def _assert_reported(self, iid: str = "A") -> None:
        """One stderr line names ``iid``'s rewrite: its branch, the commit the line took
        from it (the head its publish pushed), and what to do."""
        lines = self._rewrite_lines(iid)
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn(self.heads[iid][:12], lines[0])
        self.assertIn(f"Restore origin/fix/{iid}", lines[0])
        self.assertNotIn("STOPPING", self.err)

    def _assert_unchecked(self, iid: str, cause: str, *, not_named: str) -> None:
        """One stderr line names ``iid``'s branch for a by-hand check, with the commit the
        line took from it, and what failed — ``cause``, in those words only: none of the
        step a fold's error gives (fix the checkout, then re-run), as the run has pushed its
        line — then what to do for a branch that no longer holds that commit. It does not
        name ``not_named``."""
        lines = self._unchecked_lines()
        self.assertEqual(len(lines), 1, self.err)
        line = lines[0]
        for part in (f"({cause})", "Check by hand",
                     f"issue_{iid}'s origin/fix/{iid} ({self.heads[iid][:12]})",
                     "Restore one that does not to that commit"):
            self.assertIn(part, line)
        for advice in _FOLD_ADVICE:
            self.assertNotIn(advice, line)
        self.assertNotIn(not_named, line)

    # -- (1) a rewrite after the last fold → reported, exit 1 ------------------------------

    def test_a_rewrite_while_the_last_wave_builds_is_reported_and_exits_1(self):
        ids = self._two_waves()
        self.during["B"] = self._rewrite
        rc = self._flow(ids)
        for iid in ids:
            self.assertEqual(state.state(self.cfg.bundle(iid)), state.COMPLETE, self.err)
        self._assert_reported()
        self.assertEqual(self._unchecked_lines(), [], self.err)
        self.assertEqual(rc, 1, self.err)

    # -- (1b) a rewrite outranks a hold → 1, not 3 -----------------------------------------

    def test_a_late_rewrite_outranks_a_hold(self):
        ids = self._two_waves()
        self._plan("C")
        self.no_push.add("C")
        self.during["B"] = self._rewrite
        rc = self._flow([*ids, "C"])
        self.assertIn("issue_C pushed no branch this run", self.err)   # the hold (3)
        self._assert_reported()
        self.assertEqual(rc, 1, self.err)

    # -- (1c) a rewrite during the last fold → reported, exit 1 ----------------------------

    def test_a_rewrite_during_the_last_fold_is_reported_and_exits_1(self):
        ids = self._two_waves()
        real, rewrote = integrate._git, []

        def racing(repo: Path, *args: str) -> int:
            # The fold after wave 0 has merged fix/A's head; just before it pushes the line,
            # fix/A is rewritten.
            if args[:1] == ("push",) and not rewrote:
                rewrote.append(self._rewrite())
            return real(repo, *args)

        rc = self._flow(ids, git=racing)
        self.assertEqual(len(rewrote), 1, self.err)
        line = integrate.integration_branch(self.cfg, "main", ids)
        self.assertTrue(self.fx._ancestor(self.heads["A"], self.fx._tip(line)))
        self.assertFalse(self.fx._ancestor(rewrote[0], self.fx._tip(line)))
        self._assert_reported()
        self.assertEqual(rc, 1, self.err)

    # -- the end-of-run check cannot judge a branch → named, exit 1 ------------------------

    def test_an_end_of_run_check_that_cannot_fetch_is_named_and_exits_1(self):
        # Once the run's one fold (after wave 0) has pushed, every fetch fails — so the
        # check at the end of the run cannot see fix/A. fix/A was not rewritten: the run
        # must not report a rewrite, and must not end as a success either. It names A's
        # branch with the commit the line took from it, to check by hand, says the fetch
        # failed — not that a fold cannot see its branches — and exits 1.
        ids = self._two_waves()
        real, pushed, failed = integrate._git, [], []

        def flaky(repo: Path, *args: str) -> int:
            if args[:1] == ("fetch",) and pushed:
                failed.append(args)
                return 1
            rc = real(repo, *args)
            if args[:1] == ("push",):
                pushed.append(args)
            return rc

        rc = self._flow(ids, git=flaky)
        for iid in ids:
            self.assertEqual(state.state(self.cfg.bundle(iid)), state.COMPLETE, self.err)
        self._assert_unchecked("A", f"could not fetch origin in {self.fx.primary.resolve()}",
                               not_named="issue_B")
        self.assertNotIn("was rewritten after this run folded", self.err)
        self.assertNotIn("STOPPING", self.err)
        self.assertEqual(rc, 1, self.err)
        self.assertEqual(len(pushed), 1, self.err)    # the fold after wave 0: the run's last
        self.assertEqual(len(failed), 2, failed)      # the check's fetch, tried twice

    def _a_rewrite_and_a_target_that_cannot_be_fetched(self, other: str) -> None:
        """Two targets: A and B on org/repo's main, Z on ``other``'s checkout. The fold
        after wave 0 carries fix/A and fix/Z. While wave 1 builds, fix/A is rewritten, and
        from then on every fetch in Z's checkout fails. Both are named — A's rewrite with
        its facts and remedy, Z's branch to check by hand — and the run exits 1."""
        zrepo = self._second_checkout(other)
        self._plan("A")
        self._plan("B", "A")
        self._plan("Z", target=f"{other} @ zbase")
        ids = ["A", "B", "Z"]
        real, armed, failed = integrate._git, [], []

        def flaky(repo: Path, *args: str) -> int:
            if armed and args[:1] == ("fetch",) and Path(repo).resolve() == zrepo:
                failed.append(args)
                return 1
            return real(repo, *args)

        def rewrite_a_then_fail_z() -> None:
            self._rewrite("A")
            armed.append(True)

        self.during["B"] = rewrite_a_then_fail_z
        rc = self._flow(ids, git=flaky)
        for iid in ids:
            self.assertEqual(state.state(self.cfg.bundle(iid)), state.COMPLETE, self.err)
        zline = integrate.integration_branch(self.cfg, "zbase", ids)
        self.assertTrue(self.fx._ancestor(self.heads["Z"], self.fx._tip(zline)))  # carried
        self._assert_reported("A")
        self._assert_unchecked("Z", f"could not fetch origin in {zrepo}", not_named="issue_A")
        self.assertEqual(rc, 1, self.err)
        self.assertEqual(len(failed), 2, failed)      # Z's checkout's fetch, tried twice

    def test_a_target_that_cannot_be_fetched_does_not_hide_a_rewrite_on_another(self):
        # Targets are checked in sorted order: org/repo, then org/zrepo. A's rewrite is
        # found first; Z's target then cannot be fetched — and must not drop that report.
        self._a_rewrite_and_a_target_that_cannot_be_fetched("org/zrepo")

    def test_a_target_that_cannot_be_fetched_does_not_stop_the_check_of_the_next(self):
        # Now org/arepo sorts first: Z's target cannot be fetched before org/repo is
        # checked — and the check must still go on to org/repo and find A's rewrite.
        self._a_rewrite_and_a_target_that_cannot_be_fetched("org/arepo")

    def test_a_branch_that_cannot_be_judged_does_not_hide_a_rewrite_beside_it(self):
        # One target, two carried branches. While wave 1 builds, fix/A moves on (a
        # fast-forward, so the check must ask git whether it still holds A's head) and
        # fix/Y is rewritten; from then on that ancestry check for A's head fails (git
        # exits 128). A is judged first: the check must still judge Y, name Y's rewrite,
        # and name A's branch to check by hand, saying git failed — not to re-run — and
        # the run exits 1.
        self._plan("A")
        self._plan("B", "A")
        self._plan("Y")
        ids = ["A", "B", "Y"]
        real, armed, failed = integrate._git, [], []

        def broken(repo: Path, *args: str) -> int:
            if (armed and args[:2] == ("merge-base", "--is-ancestor")
                    and args[2] == self.heads["A"]):
                failed.append(args)
                return 128                            # git failing — not "no"
            return real(repo, *args)

        def move_a_rewrite_y() -> None:
            self.fx._push_from_human({"a.txt": "A, after review\n"}, to="fix/A",
                                     off="origin/fix/A")
            self._rewrite("Y")
            armed.append(True)

        self.during["B"] = move_a_rewrite_y
        rc = self._flow(ids, git=broken)
        for iid in ids:
            self.assertEqual(state.state(self.cfg.bundle(iid)), state.COMPLETE, self.err)
        self._assert_reported("Y")
        self.assertEqual(self._rewrite_lines("A"), [], self.err)
        self._assert_unchecked(
            "A", f"`git merge-base --is-ancestor` exited 128 in {self.fx.primary.resolve()}",
            not_named="issue_Y")
        self.assertEqual(rc, 1, self.err)
        self.assertEqual(len(failed), 1, failed)      # A's ancestry check, once

    # -- (2) no false report ---------------------------------------------------------------

    def test_a_fast_forward_of_a_carried_branch_reports_nothing(self):
        ids = self._two_waves()
        self.during["B"] = lambda: self.fx._push_from_human(
            {"a.txt": "a, after review\n"}, to="fix/A", off="origin/fix/A")
        rc = self._flow(ids)
        self.assertNotIn("rewritten", self.err)
        self.assertEqual(rc, 0, self.err)

    def test_a_carried_branch_merged_and_deleted_reports_nothing(self):
        ids = self._two_waves()

        def merge_and_delete() -> None:
            self.fx._merge_pr("A")
            self.fx._delete("fix/A")

        self.during["B"] = merge_and_delete
        rc = self._flow(ids)
        self.assertNotIn("rewritten", self.err)
        self.assertEqual(rc, 0, self.err)

    # -- (2b) no double report when the next fold already stopped the run ------------------

    def test_a_rewrite_the_next_fold_stops_on_is_reported_once(self):
        ids = self._two_waves()
        self._plan("C", "B")
        self.during["B"] = self._rewrite
        rc = self._flow([*ids, "C"])
        self.assertIn("wave 1 did not integrate", self.err)
        self.assertEqual(len(self._rewrite_lines()), 1, self.err)
        self.assertEqual(state.state(self.cfg.bundle("C")), state.PLANNED)
        self.assertEqual(rc, 1, self.err)


class CarriedFetchRetry(unittest.TestCase):
    """The fetch the carried-branch check makes before the fold's locks (#695), and the
    errors that check stops a fold with: unchanged, the step to take included."""

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        _no_retry_pause(self)
        self.cfg = self.fx.cfg

    def _second_fold(self, fail: Callable[[int], bool]) -> None:
        """Fold A, publish B off the line, then fold A and B with A's head carried, through
        an ``integrate._git`` spy that fails the n-th fetch (from 1) when ``fail(n)``.
        Leaves the line's tip before that fold in ``t1``, and the fetches and pushes that
        fold made in ``fetches`` / ``pushes``; re-raises what the fold raised."""
        a = self.fx._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        self.t1, head = self.fx._tip(sb.LINE), self.fx._tip("fix/A")
        b = self.fx._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{sb.LINE}")
        real = integrate._git
        self.fetches: list[tuple[str, ...]] = []
        self.pushes: list[tuple[str, ...]] = []

        def spy(repo: Path, *args: str) -> int:
            if args[:1] == ("fetch",):
                self.fetches.append(args)
                if fail(len(self.fetches)):
                    return 1
            if args[:1] == ("push",):
                self.pushes.append(args)
            return real(repo, *args)

        with mock.patch.object(integrate, "_git", spy):
            integrate.fold(self.cfg, [a, b], folded_this_run={sb.TARGET: self.t1},
                           carried={"issue_A": head})

    def test_one_failed_fetch_does_not_stop_the_fold(self):
        # The first fetch the fold makes is the carried-branch check's, before any lock.
        try:
            self._second_fold(lambda n: n == 1)
        except integrate.IntegrationError as exc:
            self.fail(f"one failed fetch stopped the fold: {exc}")
        tip = self.fx._tip(sb.LINE)
        self.assertNotEqual(tip, self.t1)
        self.assertTrue(self.fx._ancestor(self.t1, tip))
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/B"), tip))
        self.assertEqual(len(self.pushes), 1)

    def test_a_fetch_that_keeps_failing_still_stops_the_fold(self):
        with self.assertRaises(integrate.IntegrationError) as ctx:
            self._second_fold(lambda n: True)
        self.assertEqual(str(ctx.exception),
                         f"could not fetch origin in {self.fx.primary.resolve()} — the fold "
                         f"cannot see the branches it merges")
        self.assertEqual(len(self.fetches), 2)        # tried twice, then the fold stopped
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.fx._tip(sb.LINE), self.t1)

    def test_a_fold_stopped_by_a_git_failure_in_its_check_still_says_to_re_run(self):
        # The end-of-run report leaves out the step a fold's error gives; the fold's own
        # error keeps it, word for word. fix/A moved on (a fast-forward), so the check asks
        # git whether it still holds A's head — and git fails (exit 128).
        a = self.fx._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1, head = self.fx._tip(sb.LINE), self.fx._tip("fix/A")
        self.fx._push_from_human({"a.txt": "a, after review\n"}, to="fix/A",
                                 off="origin/fix/A")
        real, failed = integrate._git, []

        def broken(repo: Path, *args: str) -> int:
            if args[:2] == ("merge-base", "--is-ancestor") and args[2] == head:
                failed.append(args)
                return 128
            return real(repo, *args)

        with mock.patch.object(integrate, "_git", broken), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, [a], folded_this_run={sb.TARGET: t1},
                           carried={"issue_A": head})
        self.assertEqual(str(ctx.exception),
                         f"could not tell whether issue_A's carried head {head[:12]} is "
                         f"already in origin/fix/A — `git merge-base --is-ancestor` exited "
                         f"128 in {self.fx.primary.resolve()}; a git step failed (not an "
                         f"overlap): check that checkout, then re-run")
        self.assertEqual(len(failed), 1, failed)
        self.assertEqual(self.fx._tip(sb.LINE), t1)   # nothing pushed


if __name__ == "__main__":
    unittest.main()
