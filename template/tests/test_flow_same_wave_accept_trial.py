"""An accept that would add a same-wave conflict is refused at sign-off (#782, part of #395).

In stack mode two bundles of one wave can both pass Check while their changes conflict
textually. Before #782 nothing looked until the fold after the wave: both bundles were
accepted, both PRs were opened, and only then did the fold hold one of them out of the line
(#674) — an accepted bundle whose PR conflicts with its sibling's. Now, in a wave the fold
runs after, an accept is checked first, by the fold's own rule (``integrate._held_by``) on
the commits publish would push, merged onto the line the fold will find. One that would add
a hold is refused the way C6 refuses one: an open §6 row, the bundle left AWAITING_SIGNOFF,
no PR. So is one whose check could not complete; a tick on that row only retries the check.
A sibling publish can never push — its patch no longer applies where publish cuts its
branch, that ref is gone, or publish cuts it no branch at all — is no partner (the
2026-11-04 ruling): it is left out of the check, named on stderr, and the fold holds it as
unpushed, as before. Only that sibling's own failures leave it out, read off its ref and its
patch, never off a file the shared worktree keeps; a failed step of building it otherwise
is the tree's, and refuses. The bundle being accepted is never left out of its own check.
Nor does the check leave a file of its own in that worktree: one a patch adds that
``.gitignore`` covers is removed once the commit is built, so no later check finds it in the
way of its apply — while a file the check did not write is never removed by it.

Every run here goes through ``flow.flow_ids`` (as ``cli._flow`` drives it), or, in two cases,
``flow.flow_batch`` (the ``--from-csv`` sweep), and every accept through the real
``flow._drive_wave`` → ``flow._apply_decision``: the sign-off session
(``leaves.run_signoff_batch``) only writes ``leaves.SIGNOFF_DECISION`` and ticks §6 rows, and
nothing here calls ``signoff.record(... "accept")``. Do is stubbed: ``flow._build_all``
writes each ``patch.diff`` from ``changes[iid]``, the content the reused ``_publish_bundle``
stub pushes, as a diff against the commit that stub cuts the bundle's branch from — what a
build on that commit changes. Check is the real stub path (``driver.run_issue``: gates, the
stub reviewer, the real SUMMARY assembly). The run's Plan step (``leaves.do_plan_batch``)
writes the brief a re-plan gives a bundle. Two cases make the real ``driver.run_issue`` fail
once, in its archive step (``driver._archive_iteration``). Git is real: the ``StackFoldGit``
fixture of tests/test_integrate_stack_bases.py (a bare ``origin`` and a primary checkout),
with ``gh`` answered by a stub in front of ``subprocess.run``. The publisher is NOT a stub (a
stub turns every fold into a dry-run). The ``_edit`` helper, the ``gh`` stub, the base / plan
helpers and the ``_publish_bundle`` stub are tests/test_flow_same_wave_conflict.py's, loaded
by path and reused — except for a bundle of ``real_publish``, which the real
``flow._publish_bundle`` publishes: ``publish.publish``'s own fetch, ``checkout -B`` and
``git apply`` on the primary checkout, so a publish that fails there fails for real.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_same_wave_accept_trial
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import difflib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import unittest
from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import (assemble, cli, driver, flow, integrate, leaves, publish, signoff,
                          state)

_SPEC = importlib.util.spec_from_file_location(
    "same_wave_conflict_cases", Path(__file__).with_name("test_flow_same_wave_conflict.py"))
_swc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_swc)
sb = _swc.sb                          # the StackFoldGit fixture module, as that file loads it
_edit = _swc._edit

TODAY = "2026-11-04"
NINE = _swc.NINE                      # a nine-line file on the base
SAME_WAVE = "same-wave"               # every §6 row of this feature starts with it
BUDGET = "raise [driver].max_passes"  # what a bundle walked away from on the budget is told
MOVED = _edit(NINE, l2="moved")       # shared.txt once origin's main moved under a bundle


# -- what a sign-off session does to one bundle --------------------------------------------

def _tick(d: Path, *, ours: bool) -> None:
    """Tick §6's open rows — this feature's too only with ``ours``."""
    s = d / "SUMMARY.md"
    out = []
    for line in s.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.lstrip().startswith("- [ ]") and (ours or SAME_WAVE not in line):
            line = line.replace("- [ ]", "- [x]", 1)
        out.append(line)
    s.write_text("".join(out), encoding="utf-8")


def _decide(d: Path, text: str) -> None:
    (d / leaves.SIGNOFF_DECISION).write_text(text, encoding="utf-8")


def ACCEPT(t: "_Trial", d: Path) -> None:
    """The human clears what Check asked of them and accepts; a row of this feature stays."""
    _tick(d, ours=False)
    _decide(d, "accept\n")


def OVERRIDE(t: "_Trial", d: Path) -> None:
    """The human ticks every row, this feature's too, and accepts."""
    _tick(d, ours=True)
    _decide(d, "accept\n")


def NOTHING(t: "_Trial", d: Path) -> None:
    """The session records nothing for this bundle."""


def RAISE(t: "_Trial", d: Path) -> None:
    raise RuntimeError("the sign-off session was dropped")


def ITERATE_PLAN(t: "_Trial", d: Path) -> None:
    _decide(d, "iterate-plan\nre-plan it: declare Conflicts with 10\n")


def ITERATE_DO(t: "_Trial", d: Path) -> None:
    _decide(d, "iterate-do\nrebuild it\n")


def DISCONTINUE(t: "_Trial", d: Path) -> None:
    _decide(d, "discontinue\n")


class _Trial(unittest.TestCase):
    """The fixture: real git, ``flow.flow_ids``, the real ``_drive_wave``."""

    # Borrowed from tests/test_flow_same_wave_conflict.py: the base and plan helpers, the
    # `gh` stub and the publish stub. Not inherited, and its class is bound to no name here,
    # so its cases do not run a second time from this module.
    _on_base = _swc.SameWaveConflict._on_base
    _plan = _swc.SameWaveConflict._plan
    _gh_stub = _swc.SameWaveConflict._gh_stub
    _push_and_record = _swc.SameWaveConflict._publish_bundle

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.base: dict[str, str] = {}
        self.changes: dict[str, dict[str, str]] = {}
        self.cut_from: dict[str, str] = {}
        self.briefs: dict[str, str] = {}
        self.replans: dict[str, str] = {}             # id → the brief its next re-plan writes
        self.after_publish: dict[str, Callable[[], None]] = {}
        self.merged_prs: dict[str, str] = {}
        self.gh_calls: list[list[str]] = []
        self._real_run = subprocess.run
        # bundle → (stack base, recorded tip, origin's line tip) when it was last built
        self.seen: dict[str, tuple[str, str, str | None]] = {}
        self.script: dict[str, list[Callable[[_Trial, Path], None]]] = {}
        self.offered: dict[str, int] = {}             # id → sessions it was offered, all runs
        self.sessions: list[tuple[int, list[str]]] = []   # (run, bundles) per session call
        self.at_session: dict[str, list[tuple[str, list[str], list[str]]]] = {}
        self.builds: list[tuple[int, list[str]]] = []      # (run, bundles) per _build_all call
        self.events: list[tuple[int, str, list[str]]] = []  # (run, "plan" / "build", names)
        self.replanned: list[tuple[int, str]] = []          # by the run's Plan step
        self.replanned_in_wave: list[tuple[int, str]] = []  # by a wave's build (_build_all)
        self.after_build: dict[str, Callable[[Path], None]] = {}
        self.publish_fails: set[str] = set()
        self.real_publish: set[str] = set()            # ids the real _publish_bundle publishes
        self.open_prs: dict[str, str] = {}             # PR head branch → its owner's login
        self._real_publish_bundle = flow._publish_bundle
        # id → (its stdout, its stderr, what git wrote) while the real publish ran for it
        self.publish_seen: dict[str, tuple[str, str, str]] = {}
        self.extra: list = []                          # per-case patches for a run
        self.trials: list[tuple[str, str]] = []        # (phase, onto) per integrate._trial
        self.in_trial = 0                              # integrate._trial calls under way
        self.would_be: list[tuple[str, str, str]] = []  # (bundle, commit, cut) the check built
        self.building = ""                             # whose would-be commit is being built
        self.merged_in_trial: list[list[str]] = []     # the names each check's trial merged
        self.before_move = ""                          # origin's main before a case moved it
        self.checked = 0                               # scratch worktrees of _apply_check
        self.phase = ""
        self.run_no = 0
        self.errs: list[str] = []

    # -- stubs -----------------------------------------------------------------------------

    def _gh(self, args, *a, **kw):
        """The reused ``gh`` stub, except that ``gh pr list --head <branch>`` lists the open
        PR of ``open_prs`` with that head (what an `Onto branch` publish looks up first)."""
        if (isinstance(args, list) and args[:3] == ["gh", "pr", "list"] and "--head" in args
                and args[args.index("--head") + 1] in self.open_prs):
            self.gh_calls.append(list(args))
            head = args[args.index("--head") + 1]
            return subprocess.CompletedProcess(args, 0, json.dumps([{
                "url": f"https://example.test/pr/{head}", "number": 7, "headRefName": head,
                "headRepositoryOwner": {"login": self.open_prs[head]}}]), "")
        return self._gh_stub(args, *a, **kw)

    def _cut(self, d: Path) -> str:
        """Where the reused publish stub cuts ``d``'s branch from, by that stub's own rule
        (tests/test_flow_same_wave_conflict.py, ``_publish_bundle``): the recorded line tip,
        else the stack branch, else the base — as origin names it."""
        iid = d.name.removeprefix("issue_")
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = self.cut_from.get(iid) or tip or (f"origin/{line}" if line else "origin/main")
        return f"refs/heads/{cut.removeprefix('origin/')}" if cut.startswith("origin/") else cut

    def _patch_on(self, rev: str, files: dict[str, str]) -> str:
        """A unified diff of ``files`` against what origin has at ``rev`` (new files
        included): the change a build on ``rev`` makes."""
        out = []
        for name, text in files.items():
            r = sb._run(self.fx.origin, "show", f"{rev}:{name}")
            old = r.stdout if r.returncode == 0 else None
            out.append(f"diff --git a/{name} b/{name}\n"
                       + ("new file mode 100644\n" if old is None else "")
                       + "".join(difflib.unified_diff(
                           (old or "").splitlines(keepends=True),
                           text.splitlines(keepends=True),
                           "/dev/null" if old is None else f"a/{name}", f"b/{name}")))
        return "".join(out)

    def _build_all(self, cfg, bundles: list[Path]) -> None:
        """Do stubbed, Check real: archive an iterated attempt (the real ``driver.advance``),
        re-plan one a wave re-opened for Plan from its brief, write each PLANNED bundle's
        ``patch.diff`` from ``changes`` against where it is cut (:meth:`_cut`) and run the
        real stub Check to AWAITING_SIGNOFF."""
        self.phase = "drive"
        self.builds.append((self.run_no, [d.name for d in bundles]))
        self.events.append((self.run_no, "build", [d.name for d in bundles]))
        for d in bundles:
            iid = d.name.removeprefix("issue_")
            if state.state(d) in (state.ITERATE_DO, state.ITERATE_PLAN):
                driver.advance(d, cfg)
            if state.state(d) == state.UNPLANNED:
                (d / "brief.md").write_text(self.replans.pop(iid, self.briefs[iid]),
                                            encoding="utf-8")
                self.replanned_in_wave.append((self.run_no, iid))
            if state.state(d) != state.PLANNED:
                continue
            line = self.fx._tip(self.line) if self.fx._has(self.line) else None
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d),
                                 line)
            (d / "patch.diff").write_text(self._patch_on(self._cut(d), self.changes[iid]),
                                          encoding="utf-8")
            driver.run_issue(d, cfg)
            hook = self.after_build.pop(iid, None)
            if hook:
                hook(d)

    def _plan_batch(self, cfg, csv=None, ids=None) -> None:
        """The run's Plan step (``leaves.do_plan_batch``): each listed id is briefed — with
        its ``replans`` brief, else the brief it was first planned with. The CSV session of a
        sweep (no ids) briefs nothing new."""
        self.events.append((self.run_no, "plan", [str(i) for i in ids or []]))
        for iid in ids or []:
            d = cfg.bundle(iid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "brief.md").write_text(self.replans.pop(iid, self.briefs[iid]),
                                        encoding="utf-8")
            self.replanned.append((self.run_no, iid))

    def _signoff_batch(self, cfg, chunk: list[Path]) -> None:
        """One sign-off session over ``chunk``: each bundle gets the next step of its script
        (accept, once, by default), after its §6 is noted."""
        self.sessions.append((self.run_no, [d.name for d in chunk]))
        for d in chunk:
            iid = d.name.removeprefix("issue_")
            s = d / "SUMMARY.md"
            self.at_session.setdefault(iid, []).append(
                (state.state(d), signoff.open_needs_human(s), signoff.cleared_needs_human(s)))
            n = self.offered.get(iid, 0)
            self.offered[iid] = n + 1
            steps = self.script.get(iid, [ACCEPT])
            (steps[n] if n < len(steps) else NOTHING)(self, d)

    def _publish_bundle(self, cfg, d: Path, **kw) -> bool:
        """The reused publish stub (cut from the recorded tip, push, record), then the draft
        PR it opens — or, for an id of ``publish_fails``, nothing pushed at all; or, for an id
        of ``real_publish``, the real ``flow._publish_bundle`` (:meth:`_publish_for_real`)."""
        self.phase = "publish"
        iid = d.name.removeprefix("issue_")
        if iid in self.publish_fails:
            return False
        if iid in self.real_publish:
            return self._publish_for_real(cfg, d, **kw)
        ok = self._push_and_record(cfg, d, **kw)
        subprocess.run(["gh", "pr", "create", "--draft", "--repo", "org/repo", "--base",
                        "main", "--head", f"org:fix/{iid}", "--title", f"fix {iid}"],
                       capture_output=True, text=True)
        return ok

    def _publish_for_real(self, cfg, d: Path, **kw) -> bool:
        """``flow._publish_bundle`` as production has it: ``publish.publish`` runs its own
        steps on the primary checkout (fetch, ``checkout -B`` off its cut, ``git apply``, …),
        with ``gh`` stubbed. What it prints is kept in ``publish_seen`` and passed on to the
        run's streams; what git itself writes (publish runs its steps on the process's own
        descriptors) is kept there too, instead of on the test's console."""
        iid = d.name.removeprefix("issue_")
        out, err = io.StringIO(), io.StringIO()
        git = self.fx.tmp / f"publish-{iid}.out"
        with redirect_stdout(out), redirect_stderr(err), self._descriptors_to(git):
            ok = self._real_publish_bundle(cfg, d, **kw)
        sys.stdout.write(out.getvalue())
        sys.stderr.write(err.getvalue())
        self.publish_seen[iid] = (out.getvalue(), err.getvalue(),
                                  git.read_text(encoding="utf-8", errors="replace"))
        return ok

    @contextlib.contextmanager
    def _descriptors_to(self, path: Path):
        """Point file descriptors 1 and 2 at ``path`` for the block, then back."""
        for stream in (sys.__stdout__, sys.__stderr__):
            if stream is not None:
                stream.flush()
        saved = [os.dup(1), os.dup(2)]
        try:
            with open(path, "wb") as sink:
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
                yield
        finally:
            for fd, keep in zip((1, 2), saved):
                os.dup2(keep, fd)
                os.close(keep)

    def _trial_spy(self):
        real = integrate._trial

        def spy(wt, branch, onto, revs, **kw):
            self.trials.append((self.phase, onto))
            self.in_trial += 1
            try:
                return real(wt, branch, onto, revs, **kw)
            finally:
                self.in_trial -= 1
        return mock.patch.object(integrate, "_trial", spy)

    def _would_be_spy(self):
        """Note each would-be commit the accept-time check builds, and whose is being built
        while it is (absent before #782, so patched with ``create=True``: nothing calls it
        there)."""
        real = getattr(integrate, "_would_be", None)

        def spy(wt, d, ref):
            self.building = d.name
            try:
                head = real(wt, d, ref)
            finally:
                self.building = ""
            self.would_be.append((d.name, head, ref))
            return head
        return mock.patch.object(integrate, "_would_be", spy, create=True)

    def _wave_holds_spy(self):
        """Note the bundles each trial of the accept-time check merges, by name (absent before
        #782, so patched with ``create=True``)."""
        real = getattr(integrate, "_wave_holds", None)

        def spy(wt, branch, pre, built):
            self.merged_in_trial.append([name for name, _head in built])
            return real(wt, branch, pre, built)
        return mock.patch.object(integrate, "_wave_holds", spy, create=True)

    def _run(self, ids: list[str], *, max_passes: int | None = None,
             sweep: bool = False) -> None:
        """One `pdca flow <ids>` run, as ``cli._flow`` drives it — or, with ``sweep``, one
        `pdca flow --from-csv` run (``flow.flow_batch``), whose sweep is ``ids``."""
        self.run_no += 1
        self.ids = list(ids)
        self.line = integrate.integration_branch(self.cfg, "main", ids)
        self._err = io.StringIO()
        self.holds: dict[str, str] = {}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh))
            stack.enter_context(mock.patch.object(flow, "_build_all", self._build_all))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(leaves, "run_signoff_batch",
                                                  self._signoff_batch))
            stack.enter_context(mock.patch.object(leaves, "do_plan_batch", self._plan_batch))
            stack.enter_context(self._trial_spy())
            stack.enter_context(self._would_be_spy())
            stack.enter_context(self._wave_holds_spy())
            for p in self.extra:
                stack.enter_context(p)
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(self._err))
            if sweep:
                self.results = flow.flow_batch(self.cfg, csv=None, do_publish=True,
                                               do_act=False, by="T", today=TODAY,
                                               max_passes=max_passes, holds=self.holds)
            else:
                self.results = flow.flow_ids(self.cfg, list(ids), plan_missing=True,
                                             do_publish=True, do_act=False, by="T",
                                             today=TODAY, max_passes=max_passes,
                                             holds=self.holds)
        self.err = self._err.getvalue()
        self.errs.append(self.err)
        self.rc = cli._results_rc(self.results, holds=self.holds)
        self.assertNotIn("Traceback", self.err)

    # -- helpers ---------------------------------------------------------------------------

    def _pair(self, a_files: dict[str, str], b_files: dict[str, str]) -> list[str]:
        """Wave 0: ``10`` and ``11`` (the pair, no `Conflicts with`) and ``12`` (a file of
        its own); wave 1: ``20``, which depends on ``12`` only. Returns the ids."""
        self._on_base({"shared.txt": NINE})
        self._plan("10", a_files)
        self._plan("11", b_files)
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "12")
        return ["10", "11", "12", "20"]

    def _conflicting_pair(self) -> list[str]:
        return self._pair({"shared.txt": _edit(NINE, l2="ten")},
                          {"shared.txt": _edit(NINE, l2="eleven")})

    def _d(self, iid: str) -> Path:
        return self.cfg.bundle(iid)

    def _ours(self, iid: str) -> tuple[list[str], list[str]]:
        """This feature's §6 rows of ``iid``: (open, ticked)."""
        s = self._d(iid) / "SUMMARY.md"
        return ([r for r in signoff.open_needs_human(s) if SAME_WAVE in r],
                [r for r in signoff.cleared_needs_human(s) if SAME_WAVE in r])

    def _named(self, iid: str, run: int | None = None) -> int:
        """How many sign-off sessions named ``issue_<iid>`` (in run ``run``, or any)."""
        return sum(1 for r, names in self.sessions
                   if f"issue_{iid}" in names and (run is None or r == run))

    def _pr_created(self, iid: str) -> bool:
        return any(c[:3] == ["gh", "pr", "create"] and f"org:fix/{iid}" in c
                   for c in self.gh_calls)

    def _published(self, iid: str) -> None:
        self.assertEqual(self.results[iid], state.COMPLETE, self.err)
        self.assertTrue(self.fx._has(f"fix/{iid}"), (iid, self.err))
        self.assertTrue(self._pr_created(iid), (iid, self.gh_calls))

    def _not_published(self, iid: str) -> None:
        self.assertFalse(self.fx._has(f"fix/{iid}"), (iid, self.err))
        self.assertFalse(self._pr_created(iid), (iid, self.gh_calls))

    def _lines(self, iid: str) -> list[str]:
        return [x for x in self.err.splitlines() if f"issue_{iid}" in x]

    def _refused(self, iid: str, partner: str, path: str) -> str:
        """``iid`` is AWAITING_SIGNOFF, its §6 has ONE open row of this feature naming
        ``partner`` and ``path`` (read through ``signoff.open_needs_human``, as C6 reads it),
        and nothing of it was published. Returns the row."""
        self.assertEqual(state.state(self._d(iid)), state.AWAITING_SIGNOFF, self.err)
        opened, ticked = self._ours(iid)
        self.assertEqual(len(opened), 1, (opened, self.err))
        self.assertEqual(ticked, [], self.err)
        self.assertIn(f"issue_{partner}", opened[0])
        self.assertIn(path, opened[0])
        self._not_published(iid)
        self.assertNotIn(iid, self.holds, self.err)
        return opened[0]

    def _parked_line(self, iid: str) -> str:
        """The one stderr line that names ``iid`` parked with its refusal and the step."""
        lines = [x for x in self._lines(iid) if "not accepted" in x and "To go on" in x]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    def _no_pr_of_a_partner(self, partner: str, *texts: str) -> None:
        """This feature's row, step and lines send the human to the partner's CHANGE on the
        base, never to a PR of it, which may not exist — its publish may have failed (the
        2026-11-04 ruling). No word of them is "PR"."""
        for text in texts:
            self.assertIn(f"once issue_{partner}'s change is on your base", text)
            self.assertNotIn("PR", text)

    def _left_out(self, sibling: str, iid: str, why: str = "") -> str:
        """The one stderr line saying ``sibling`` was left out of ``iid``'s check, and why:
        ``why``, else that its would-be commit could not be built. Returns it."""
        lines = [x for x in self.err.splitlines()
                 if f"issue_{sibling} is left out of issue_{iid}'s same-wave check" in x]
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn(why or f"issue_{sibling}'s would-be commit could not be built", lines[0])
        return lines[0]

    def _cut_base(self, d: Path) -> tuple[str, str] | None:
        """``publish.cut_base`` of ``d``: where the check builds its would-be commit (added by
        #782, so asserted there before it is called)."""
        cut_base = getattr(publish, "cut_base", None)
        self.assertTrue(callable(cut_base), "publish.cut_base is what the check cuts from")
        return cut_base(self.cfg, d)

    def _stacks_on_unpublished(self, iid: str, files: dict[str, str]) -> Path:
        """Plan ``iid`` to `Stacks on` 05: a bundle accepted in an earlier run whose publish
        never ran, so it has no published branch, and publish cuts no branch for ``iid``
        (it refuses it)."""
        if not self._d("05").exists():
            self.fx._bundle("05", {"five.txt": "five\n"})   # COMPLETE, nothing published
        d = self._plan(iid, files)
        self.briefs[iid] += "- **Stacks on:** 05\n"
        (d / "brief.md").write_text(self.briefs[iid], encoding="utf-8")
        return d

    def _texts_then(self, step: Callable[["_Trial", Path], None]
                    ) -> Callable[["_Trial", Path], None]:
        """A session step that first writes the bundle's commit message and PR body — what
        the real publish of a bundle of ``real_publish`` reads — then does ``step``."""
        def session(t: "_Trial", d: Path) -> None:
            iid = d.name.removeprefix("issue_")
            (d / publish.COMMIT_MSG).write_text(f"fix {iid}\n\nThe fix.\n", encoding="utf-8")
            (d / publish.PR_BODY).write_text("The fix.\n", encoding="utf-8")
            step(t, d)
        return session

    def _left_out_of_both(self, why: str) -> None:
        """10 was left out of 11's and of 12's check, each line saying ``why``; 11 and 12 were
        accepted and published with no row of this feature, and 12 was still checked — by a
        trial merging 11 and 12, never 10. 20, on 12, built and was published."""
        for iid in ("11", "12"):
            self._published(iid)
            self.assertEqual(self._ours(iid), ([], []), (iid, self.err))
            self.assertFalse([x for x in self._lines(iid) if "cannot accept" in x], self.err)
            self._left_out("10", iid, why)
        self.assertIn(["issue_11", "issue_12"], self.merged_in_trial, self.merged_in_trial)
        self.assertFalse([names for names in self.merged_in_trial if "issue_10" in names])
        self._published("20")

    def _apply_check(self, rev: str, patch: str) -> int:
        """``git apply --check`` of ``patch`` on a clean checkout of ``rev`` — a worktree of
        its own off the primary checkout, so nothing the run uses moves. Its exit code."""
        self.checked += 1
        wt = self.fx.tmp / f"apply-check-{self.checked}"
        sb._git(self.fx.primary, "worktree", "add", "-q", "--detach", str(wt), rev)
        return sb._run(wt, "apply", "--check", patch).returncode

    def _run_events(self, run: int) -> list[tuple[str, list[str]]]:
        return [(kind, names) for r, kind, names in self.events if r == run]

    def _never_built_together(self, run: int, *names: str) -> None:
        """No wave of run ``run`` built all of ``names`` together."""
        together = [n for kind, n in self._run_events(run)
                    if kind == "build" and set(names) <= set(n)]
        self.assertEqual(together, [], self.events)

    # -- (a) refused, named, never published; the later wave builds ------------------------

    def test_a_conflicting_sibling_is_refused_and_the_later_wave_builds(self):
        ids = self._conflicting_pair()
        self._run(ids)

        self._published("10")
        self._published("12")
        self._refused("11", "10", "shared.txt")
        self.assertEqual(self.holds, {}, self.err)
        self.assertEqual(self.rc, 1, self.err)
        self._published("20")                                # depends on neither of the pair
        # Named with the conflict, the partner and the step, never with the pass budget.
        line = self._parked_line("11")
        self.assertIn("issue_10", line)
        self.assertIn("shared.txt", line)
        self.assertIn("pdca signoff 11 --iterate-plan", line)
        self.assertFalse([x for x in self._lines("11") if BUDGET in x], self.err)

    # -- (a2) a middle wave: the check merges onto the line's tip --------------------------

    def test_in_a_middle_wave_the_check_merges_onto_the_line_tip(self):
        self._on_base({"shared.txt": NINE})
        self._plan("05", {"five.txt": "five\n"})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")}, "05")
        self._plan("11", {"shared.txt": _edit(NINE, l2="eleven")}, "05")
        self._plan("30", {"thirty.txt": "thirty\n"}, "10")
        self._run(["05", "10", "11", "30"])

        line_tip = self.seen["issue_10"][2]                # the line when wave 1 was built
        self.assertIsNotNone(line_tip, self.err)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/05"), line_tip))   # it holds 05
        self.assertNotEqual(line_tip, self.fx._tip("main"))
        # The accept-time trial ran onto that tip — not onto origin/main.
        onto = [o for phase, o in self.trials if phase == "drive"]
        self.assertTrue(onto, self.err)
        self.assertEqual(set(onto), {line_tip}, self.err)
        self._refused("11", "10", "shared.txt")
        self._published("10")
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/10"), self.fx._tip(self.line)))
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/05"), self.fx._tip(self.line)))
        self._published("30")
        self.assertEqual(self.holds, {}, self.err)

    # -- (a3) a conflict with the line alone is the fold's, not this feature's -------------

    def test_a_conflict_with_the_line_alone_is_not_refused(self):
        self._on_base({"shared.txt": NINE})
        self._plan("05", {"shared.txt": _edit(NINE, l2="five")})
        self._plan("06", {"six.txt": "six\n"})
        self._plan("15", {"fifteen.txt": "fifteen\n"}, "06")
        self._plan("16", {"shared.txt": _edit(NINE, l2="sixteen")}, "06")
        self._plan("25", {"twenty-five.txt": "twenty-five\n"}, "15")
        self._run(["05", "06", "15", "16", "25"])

        # The check ran for 16 (beside 15, accepted before it) and saw the clash with the
        # line — then did not count it: 16 merges onto the line alone not at all.
        self.assertTrue([o for phase, o in self.trials if phase == "drive"], self.err)
        self.assertEqual(self._ours("16"), ([], []), self.err)
        self.assertFalse([x for x in self._lines("16") if "cannot accept" in x], self.err)
        self._published("16")
        self._published("15")
        # …and the fold handles it as before: held for the line work that explains it (#747).
        self.assertEqual(self.holds, {"16": "conflict"}, self.err)
        self.assertIn("issue_05", " ".join(x for x in self._lines("16") if "held" in x))
        self._published("25")

    # -- (a4) each would-be commit is the one publish pushes ------------------------------

    def test_the_check_builds_each_commit_where_publish_cuts_its_branch(self):
        # Wave 0: 05 changes line 2 of shared.txt, 07 line 5. Wave 1: 15 and 16 depend on 05,
        # so each is built on a base of its own holding 05 and not 07 (#727), and 15 changes
        # 05's line again: its patch applies only there — not on main (no 05) and not on the
        # line (07's line 5 is in its context). Wave 2: 25 (depends on 15) makes wave 1 one a
        # fold runs after. 16's accept is checked beside 15's: the check must build each
        # would-be commit where publish cuts that bundle's branch, and then they merge
        # cleanly, so both are accepted and published. Built anywhere else, 15's would not
        # build at all, and 16 would be refused for a check that could not complete.
        self._on_base({"shared.txt": NINE})
        five = _edit(NINE, l2="five")
        self._plan("05", {"shared.txt": five})
        self._plan("07", {"shared.txt": _edit(NINE, l5="seven")})
        self._plan("15", {"shared.txt": _edit(five, l2="fifteen")}, "05")
        self._plan("16", {"shared.txt": _edit(five, l8="sixteen")}, "05")
        self._plan("25", {"twenty-five.txt": "twenty-five\n"}, "15")
        ids = ["05", "07", "15", "16", "25"]
        self._run(ids)

        for iid in ids:
            self._published(iid)
            self.assertEqual(self._ours(iid), ([], []), (iid, self.err))
        self.assertEqual(self.holds, {}, self.err)
        self.assertEqual(self.rc, 0, self.err)
        _base, own, line_tip = self.seen["issue_15"]
        self.assertIsNotNone(own, self.err)
        self.assertNotIn(own, (line_tip, self.fx._tip("main")))    # a base of its own
        # The check built 15's and 16's would-be commits (beside 07's and 05's in wave 0),
        # 15's on its own base, and each is the commit publish then pushed: the same tree,
        # cut from the same commit.
        names = {name for name, _commit, _cut in self.would_be}
        self.assertTrue({"issue_15", "issue_16"} <= names, self.would_be)
        self.assertIn(("issue_15", own), [(n, cut) for n, _c, cut in self.would_be])
        for name, commit, _cut in self.would_be:
            pushed = f"refs/heads/fix/{name.removeprefix('issue_')}"
            for rev in ("^{tree}", "^"):
                self.assertEqual(sb._git(self.fx.primary, "rev-parse", commit + rev),
                                 sb._git(self.fx.origin, "rev-parse", pushed + rev),
                                 (name, rev))

    def test_cut_base_is_what_publish_cuts_from(self):
        # The check builds a bundle's would-be commit on `publish.cut_base`; the real
        # `publish.publish` (its dry-run prints the steps it would run) fetches and cuts from
        # the same ref, for each shape of bundle: one cut from the base (40); a wave>0 one,
        # from the line commit recorded for it (#593, 41); a legacy `Stacks on` one, from its
        # prerequisite's published branch (#123, 43 on 42); an `Onto branch` one, onto that
        # PR's branch on its own remote (#54, 44).
        cut_base = getattr(publish, "cut_base", None)
        self.assertTrue(callable(cut_base), "publish.cut_base is what the check cuts from")
        self._on_base({"shared.txt": NINE})
        self.fx._publish("42", {"forty-two.txt": "forty-two\n"})
        shapes = {"40": "", "41": "", "43": "- **Stacks on:** 42\n",
                  "44": "- **Onto branch:** fork/feature-x\n"}
        for iid, extra in shapes.items():
            d = self._d(iid)
            sb._brief(d, extra=extra)
            (d / "patch.diff").write_text(sb._new_file_patch(f"{iid}.txt", iid),
                                          encoding="utf-8")
            (d / publish.COMMIT_MSG).write_text(f"fix {iid}\n\nThe fix.\n", encoding="utf-8")
            (d / publish.PR_BODY).write_text("The fix.\n", encoding="utf-8")
            sb._accept(d)
        publish.write_stack_base(self._d("41"), "pdca-integration/main-rx",
                                 self.fx._tip("main"))
        expected = {"40": ("origin", "origin/main"), "41": ("origin", self.fx._tip("main")),
                    "43": ("origin", "origin/fix/42"), "44": ("fork", "fork/feature-x")}
        for iid in shapes:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = publish.publish(self.cfg, iid, dry_run=True, texts_prevalidated=True)
            self.assertEqual(rc, 0, (iid, out.getvalue()))
            steps = [shlex.split(x) for x in out.getvalue().splitlines()
                     if x.startswith("  git ")]
            fetched = [s[-1] for s in steps if s[3] == "fetch"]
            cut = [s[-1] for s in steps if s[3:5] == ["checkout", "-B"]]
            self.assertEqual(len(fetched), 1, (iid, steps))
            self.assertEqual(len(cut), 1, (iid, steps))
            self.assertEqual((fetched[0], cut[0]), expected[iid], (iid, steps))
            self.assertEqual(cut_base(self.cfg, self._d(iid)), expected[iid], iid)

    # -- (b) the same file, other lines: both accepted -------------------------------------

    def test_a_shared_file_that_merges_cleanly_is_accepted(self):
        ids = self._pair({"shared.txt": _edit(NINE, l2="ten")},
                         {"shared.txt": _edit(NINE, l8="eleven")})
        self._run(ids)

        for iid in ids:
            self._published(iid)
            self.assertEqual(self._ours(iid), ([], []), iid)
        self.assertEqual(self.holds, {}, self.err)
        self.assertEqual(self.rc, 0, self.err)

    # -- (c) whichever is accepted first is kept -------------------------------------------

    def test_whichever_is_accepted_first_is_kept(self):
        ids = self._conflicting_pair()
        # 11 carries a decision an earlier, interrupted session left: the pre-apply records
        # it before 10's session, so 11 is accepted first and 10 — first by name — refused.
        self.after_build["11"] = lambda d: ACCEPT(self, d)
        self.script["11"] = []
        self._run(ids)

        self.assertEqual(self._named("11"), 0, self.sessions)
        self._published("11")
        self._refused("10", "11", "shared.txt")
        self.assertEqual([self.results[i] for i in ("10", "11")].count(state.COMPLETE), 1)
        self.assertEqual(self.holds, {}, self.err)
        self._published("20")

    # -- (d) the human overrides in the same run -------------------------------------------

    def test_a_ticked_conflict_row_overrides_in_the_same_run(self):
        ids = self._conflicting_pair()
        self.script["11"] = [ACCEPT, OVERRIDE]
        self._run(ids)

        self.assertEqual(self._named("11"), 2, self.sessions)
        self._published("11")
        opened, ticked = self._ours("11")
        self.assertEqual((len(opened), len(ticked)), (0, 1), self.err)   # never added twice
        self.assertIn("issue_10", ticked[0])
        # The fold then holds it, as before (#674).
        self.assertEqual(self.holds, {"11": "conflict"}, self.err)
        self.assertEqual(self.rc, 3, self.err)
        self._published("20")

    # -- (D) …unless an earlier override already has the fold holding one ------------------

    def test_after_an_override_a_later_accept_can_trade_its_hold_for_the_first_accepted(self):
        # The accepted loophole the docs name: the check counts the bundles the fold would
        # hold. 10 (lines 2 and 8) is accepted first; 12 (line 8) is refused for it, then
        # overridden, so the fold would hold 12. 09 (line 2) is then accepted plainly: with
        # it, the fold holds 10 instead of 12 — one hold either way — so the bundle accepted
        # first is the one held.
        self._on_base({"shared.txt": NINE})
        self._plan("09", {"shared.txt": _edit(NINE, l2="nine")})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten", l8="ten")})
        self._plan("12", {"shared.txt": _edit(NINE, l8="twelve")})
        self._plan("20", {"twenty.txt": "twenty\n"}, "12")
        self.after_build["10"] = lambda d: ACCEPT(self, d)   # applied before any session
        self.script["10"] = []
        self.script["12"] = [ACCEPT, OVERRIDE]
        self.script["09"] = [NOTHING, NOTHING, ACCEPT]       # once 12's override is in
        self._run(["09", "10", "12", "20"])

        self.assertEqual(self._named("10"), 0, self.sessions)
        self.assertEqual(self._named("12"), 2, self.sessions)         # refused, then overridden
        _st, opened, _ticked = self.at_session["12"][1]
        self.assertTrue([r for r in opened if SAME_WAVE in r and "issue_10" in r], opened)
        self.assertEqual(len(self._ours("12")[1]), 1, self.err)          # the override
        self.assertEqual(self._ours("09"), ([], []), self.err)          # a plain accept
        for iid in ("09", "10", "12", "20"):
            self._published(iid)
        self.assertEqual(self.holds, {"10": "conflict"}, self.err)
        self.assertEqual(self.rc, 3, self.err)

    # -- (e) (e2) (e4) a check that cannot complete refuses; a tick only retries ------------

    def test_a_check_that_cannot_complete_refuses_and_a_tick_only_retries(self):
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"eleven.txt": "eleven\n"})          # shares NO file with 10
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        # 12 has an empty patch.diff: the fold never merges it, so it owes no check. The real
        # publish takes it (nothing to contribute).
        self._plan("12", {})
        self.real_publish = {"12"}
        # First 11's OWN would-be commit cannot be built — its patch is rejected, 10's still
        # applies, so the fault is keyed on 11's patch, not on every `git apply` — then a fetch
        # fails: both are the candidate's or the check's, never a reason to leave 10 out.
        patch11 = str((self._d("11") / "patch.diff").resolve())
        fault = {"step": "apply"}
        seen: dict[str, list[str]] = {}
        checked: list[str] = []

        real_git = integrate._git
        real_clash = getattr(integrate, "same_wave_clash", None)

        def git(repo, *args):
            if (self.phase == "drive" and args[:1] == (fault["step"],)
                    and (fault["step"] != "apply" or patch11 in args)):
                return 1
            return real_git(repo, *args)

        def clash(cfg, d, *a, **k):
            checked.append(d.name)
            return real_clash(cfg, d, *a, **k)

        def retry_while_broken(t, d):        # (e2): tick it, accept; the fetch fails now
            seen["built"] = [name for name, _commit, _cut in t.would_be]
            fault["step"] = "fetch"
            OVERRIDE(t, d)

        def retry_once_fixed(t, d):          # the fault cleared: tick it, accept
            fault["step"] = None
            OVERRIDE(t, d)

        self.script["11"] = [ACCEPT, retry_while_broken, retry_once_fixed]
        self.extra = [mock.patch.object(integrate, "_git", git),
                      mock.patch.object(integrate, "same_wave_clash", clash, create=True)]
        self._run(["10", "11", "12", "20"])

        # 10's accept needed no check (nothing of its wave accepted yet): published as ever.
        self._published("10")
        # 12 — nothing to publish — was accepted after 10 with no check and no row.
        self.assertEqual(self.results["12"], state.COMPLETE, self.err)
        self.assertNotIn("issue_12", checked, self.err)
        self.assertEqual(self._ours("12"), ([], []), self.err)
        self.assertNotIn("12", self.holds, self.err)
        self.assertIn("nothing to contribute", self.publish_seen["12"][1])
        # The first check built 10's would-be commit and failed on 11's own: 10 was a
        # partner, and nothing was left out of the check.
        self.assertEqual(seen.get("built"), ["issue_10"], self.would_be)
        self.assertNotIn("left out", self.err)
        # 11 was never accepted while the check could not complete — through two failures
        # with different causes and one tick — and once it completed it was.
        self.assertEqual(self._named("11"), 3, self.sessions)
        second, third = self.at_session["11"][1], self.at_session["11"][2]
        for st, opened, _ticked in (second, third):
            self.assertEqual(st, state.AWAITING_SIGNOFF, self.err)
            ours = [r for r in opened if SAME_WAVE in r]
            self.assertEqual(len(ours), 1, opened)               # ONE row, open again (e2)
            self.assertIn("could not complete", ours[0])
            # The row names no cause: what failed is on stderr only (e4).
            for cause in ("apply", "fetch", "would-be commit"):
                self.assertNotIn(cause, ours[0])
        self.assertFalse([r for r in third[2] if SAME_WAVE in r], third)   # no ticked twin
        refusals = [x for x in self._lines("11") if "cannot accept: the same-wave" in x]
        self.assertEqual(len(refusals), 2, self.err)
        self.assertIn("would-be commit could not be built", refusals[0])
        self.assertIn("could not fetch", refusals[1])
        self.assertIn("re-opened", refusals[1])
        self._published("11")
        opened, ticked = self._ours("11")
        self.assertEqual((opened, len(ticked)), ([], 1), self.err)
        self._published("20")

    # -- (e3) a broken tree refuses the same way; the fold keeps its stop -----------------

    def test_a_broken_tree_refuses_under_the_same_retry_and_the_fold_still_stops(self):
        # 10 and 11 change the same line, so 11's check reaches the trial merge that judges
        # the hold (`_held_by` → `_trial`). That merge fails first; then the tree will not
        # reset — neither in the trial's own clean-up nor in the check's. 20 (on 10) is the
        # wave the fold after this one would build.
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"shared.txt": _edit(NINE, l2="eleven")})
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        broke: list[bool] = []
        real_merge, real_reset = integrate._merge, integrate._reset_tree

        def merge(wt, rev, message):
            if self.phase == "drive" and self.in_trial and not broke:   # the first failure
                broke.append(True)
                raise integrate.IntegrationError("injected: the trial merge failed")
            return real_merge(wt, rev, message)

        def reset_tree(wt, rev):              # …after which the tree will not reset
            if broke:
                raise integrate.IntegrationTreeError("injected: the tree will not reset")
            return real_reset(wt, rev)

        raised: list[BaseException] = []
        real_clash = getattr(integrate, "same_wave_clash", None)

        def clash(*a, **k):
            try:
                return real_clash(*a, **k)
            except BaseException as exc:
                raised.append(exc)
                raise

        self.script["11"] = [ACCEPT, OVERRIDE]
        self.extra = [mock.patch.object(integrate, "_merge", merge),
                      mock.patch.object(integrate, "_reset_tree", reset_tree),
                      mock.patch.object(integrate, "same_wave_clash", clash, create=True)]
        self._run(["10", "11", "20"])

        # The check got as far as the trial merge, and failed there first.
        self.assertTrue(broke, self.err)
        self.assertTrue(raised, self.err)
        self.assertIsInstance(raised[0], integrate.IntegrationTreeError)
        # Chained with `raise … from` through both resets that failed after it — the trial's
        # own and the check's — back to that first failure.
        chain = [raised[0]]
        while chain[-1].__cause__ is not None:
            chain.append(chain[-1].__cause__)
        self.assertEqual(len(chain), 3, chain)
        self.assertIsInstance(chain[1], integrate.IntegrationTreeError)
        self.assertNotIsInstance(chain[2], integrate.IntegrationTreeError)
        self.assertIn("the trial merge failed", str(chain[2]))
        # Both are reported: what failed first and the tree that will not reset.
        first = [x for x in self._lines("11") if "cannot accept: the same-wave" in x][0]
        self.assertIn("the trial merge failed", first)
        self.assertIn("the tree will not reset", first)
        # Refused, retried on a tick while the tree is still broken, refused again: never
        # accepted, and the run did not stop at accept time — 10 is published.
        self.assertEqual(self._named("11"), 3, self.sessions)
        self.assertEqual(signoff.outcome_token(self._d("11") / "SUMMARY.md"), "")
        opened, ticked = self._ours("11")
        self.assertEqual((len(opened), ticked), (1, []), self.err)
        self.assertIn("could not complete", opened[0])
        self._not_published("11")
        self._published("10")
        # The fold after the wave keeps its own stop on a broken tree.
        self.assertIn("wave 0 did not integrate", self.err)
        self.assertIn("STOPPING", self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)
        self.assertEqual(self.rc, 1, self.err)

    # -- (e5) (e5b) (a4-neg) a sibling publish can never push is no partner ----------------

    def _base_moves_under_10(self, ten: dict[str, str], eleven: dict[str, str], *,
                             eleven_moved: dict[str, str] | None = None) -> None:
        """Wave 0: 10, 11 and 12 (a file of its own); wave 1: 20, on 12. After the builds and
        before any accept, origin's main gets a commit that changes line 2 of shared.txt,
        which 10's patch changes too, so 10's patch.diff no longer applies where publish cuts
        its branch. 10's session makes that commit, then accepts: 10 is accepted first, by
        name. 10 is published by the real ``flow._publish_bundle``, so its publish fails at
        publish's own ``git apply`` — never by the ``publish_fails`` hook. Once the base
        moved, the stub publishes ``eleven_moved`` for 11 when given: what a build of 11's
        patch on the moved base holds, so the fold merges what the check built."""
        self._on_base({"shared.txt": NINE})
        self._plan("10", ten)
        self._plan("11", eleven)
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "12")

        def move_then_accept(t, d):
            t.before_move = t.fx._tip("main")
            t.fx._push_from_human({"shared.txt": MOVED}, to="main")
            if eleven_moved is not None:
                t.changes["11"] = eleven_moved
            (d / publish.COMMIT_MSG).write_text("fix 10\n\nThe fix.\n", encoding="utf-8")
            (d / publish.PR_BODY).write_text("The fix.\n", encoding="utf-8")
            ACCEPT(t, d)

        self.script["10"] = [move_then_accept]
        self.real_publish = {"10"}
        self._run(["10", "11", "12", "20"])

    def _held_as_unpushed(self, iid: str) -> None:
        """``iid`` is accepted, its publish pushed nothing, and the fold held it as a bundle
        that pushed no branch — as on main, with no dependent in the run (exit 3)."""
        self.assertEqual(self.results[iid], state.COMPLETE, self.err)
        self.assertFalse(self._pr_created(iid), self.gh_calls)
        self.assertEqual(sb._git(self.fx.origin, "for-each-ref", f"refs/heads/fix/{iid}*"),
                         "", self.err)
        self.assertEqual(self.holds, {iid: "unpushed"}, self.err)
        self.assertIn(f"issue_{iid} pushed no branch this run", self.err)
        self.assertEqual(self.rc, 3, self.err)

    def test_a_sibling_whose_patch_no_longer_applies_is_left_out_of_the_check(self):
        self._base_moves_under_10({"shared.txt": _edit(NINE, l2="ten")},
                                  {"eleven.txt": "eleven\n"})

        self._held_as_unpushed("10")
        # 11 and 12 are accepted and published: 10 is no partner. Each check named 10 as
        # left out, and why; neither has a row of this feature, of either kind.
        for iid in ("11", "12"):
            self._published(iid)
            self.assertEqual(self._ours(iid), ([], []), (iid, self.err))
            self.assertFalse([x for x in self._lines(iid) if "cannot accept" in x], self.err)
            self.assertIn("does not apply onto origin/main", self._left_out("10", iid))
        # 12 was still checked, against the partner left: 11.
        self.assertIn(["issue_11", "issue_12"], self.merged_in_trial, self.merged_in_trial)
        self.assertFalse([names for names in self.merged_in_trial if "issue_10" in names])
        # 20 builds and completes on the line that holds 11 and 12.
        self._published("20")
        line_tip = self.seen["issue_20"][2]
        self.assertIsNotNone(line_tip, self.err)
        for iid in ("11", "12"):
            self.assertTrue(self.fx._ancestor(self.fx._tip(f"fix/{iid}"), line_tip), iid)

        # (a4-neg) "Left out of the check" and "publish could not push it" are one fact: on
        # the ref `publish.cut_base` gives 10, its patch no longer applies (it did on the
        # base it was built on), and publish's own steps cut 10's branch from that ref and
        # failed at exactly that `git apply`.
        d = self._d("10")
        patch = str((d / "patch.diff").resolve())
        remote, ref = publish.cut_base(self.cfg, d)
        self.assertEqual((remote, ref), ("origin", "origin/main"))
        self.assertNotEqual(self._apply_check(ref, patch), 0)
        self.assertEqual(self._apply_check(self.before_move, patch), 0)
        out, err, git = self.publish_seen["10"]
        steps = [x.removeprefix("→ ").split() for x in out.splitlines() if x.startswith("→ ")]
        self.assertEqual([s[0] for s in steps], ["fetch", "checkout", "apply"], out)
        self.assertEqual(steps[1][:2], ["checkout", "-B"], out)
        self.assertEqual(steps[1][-1], ref, out)                 # cut from that ref …
        self.assertEqual(steps[2], ["apply", patch], out)        # … and failed applying
        self.assertIn("publish: step failed: git -C", err)
        self.assertIn(f" apply {patch}", err)
        self.assertIn("patch does not apply", git)
        self.assertIn("NOT published", self.err)

    def test_a_sibling_left_out_is_no_partner_even_where_it_would_conflict(self):
        # (e5b) 11 changes line 8 of shared.txt, which 10's patch changes too: were 10 a
        # partner, 11 would be refused for that conflict, as in (a). 10's patch no longer
        # applies, so 10 is none, and nothing of this feature holds or refuses 11.
        self._base_moves_under_10({"shared.txt": _edit(NINE, l2="ten", l8="ten")},
                                  {"shared.txt": _edit(NINE, l8="eleven")},
                                  eleven_moved={"shared.txt": _edit(MOVED, l8="eleven")})

        self._published("11")
        self.assertEqual(self._ours("11"), ([], []), self.err)
        self.assertFalse([x for x in self._lines("11") if "cannot accept" in x], self.err)
        self._left_out("10", "11")
        self._published("12")
        self._published("20")
        self._held_as_unpushed("10")

    # -- (e6) a tree fault while a sibling is built refuses; it never leaves it out --------

    def _tree_fault_while_10_is_built(self, step: str, what: str) -> None:
        """10 is accepted (no check). Then, for 11's check, one step of building 10's
        would-be commit fails although 10's cut ref resolves and `git apply --check` of its
        patch passes there: the tree's fault, not 10's. 11 (sharing no file with 10) is
        refused with the "could not complete" row, stderr names 10's step, and no line says
        10 was left out. Cleared, ticked, accepted: the check runs with 10 as a partner,
        and 11 is accepted."""
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"eleven.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        patch10 = str((self._d("10") / "patch.diff").resolve())
        fails = {"checkout": lambda args: args[:3] == ("checkout", "-q", "--detach"),
                 "apply": lambda args: args == ("apply", patch10),     # not its `--check`
                 "commit": lambda args: args[:1] == ("commit",)}[step]
        fault = {"on": True}
        real_git = integrate._git
        # The apply runs through the git helper that keeps what git said (added by #782, so
        # patched with `create=True`); every other step, through `integrate._git`.
        real_git_stderr = getattr(integrate, "_git_stderr", None)

        def faulted(args) -> bool:
            return fault["on"] and self.building == "issue_10" and fails(args)

        def git(repo, *args):
            return 1 if faulted(args) else real_git(repo, *args)

        def git_stderr(repo, *args):
            return (1, "") if faulted(args) else real_git_stderr(repo, *args)

        def retry_once_cleared(t, d):
            fault["on"] = False
            OVERRIDE(t, d)

        self.script["11"] = [ACCEPT, retry_once_cleared]
        self.extra = [mock.patch.object(integrate, "_git", git),
                      mock.patch.object(integrate, "_git_stderr", git_stderr, create=True)]
        self._run(["10", "11", "20"])

        self._published("10")
        self.assertEqual(self._named("11"), 2, self.sessions)
        st, opened, _ticked = self.at_session["11"][1]           # the fault still on
        self.assertEqual(st, state.AWAITING_SIGNOFF, self.err)
        ours = [r for r in opened if SAME_WAVE in r]
        self.assertEqual(len(ours), 1, opened)
        self.assertIn("could not complete", ours[0])
        refusals = [x for x in self._lines("11") if "cannot accept: the same-wave" in x]
        self.assertEqual(len(refusals), 1, self.err)
        self.assertIn(f"issue_10's would-be commit could not be built: {what}", refusals[0])
        self.assertNotIn("left out", self.err)
        # Once cleared: the check merged 10 and 11 — 10 a partner — and 11 is published.
        self.assertIn(["issue_10", "issue_11"], self.merged_in_trial, self.merged_in_trial)
        self._published("11")
        self._published("20")

    def test_a_failed_checkout_of_a_siblings_cut_refuses(self):
        self._tree_fault_while_10_is_built("checkout", "could not check out origin/main")

    def test_a_failed_apply_after_a_siblings_check_passed_refuses(self):
        self._tree_fault_while_10_is_built(
            "apply",
            "`git apply` failed onto origin/main, though `git apply --check --cached` passed")

    def test_a_failed_commit_of_a_siblings_would_be_commit_refuses(self):
        self._tree_fault_while_10_is_built("commit", "`git commit` failed")

    # -- an ignored file the shared tree keeps never leaves a sibling out -------------------

    def test_an_ignored_leftover_where_a_siblings_patch_adds_a_file_keeps_it_a_partner(self):
        # The base ignores *.log; 10's patch changes line 2 of shared.txt and adds ten.log. An
        # ignored ten.log is left in the shared integration worktree (as a re-gate's build
        # output is: the tree's reset keeps ignored files). 10's patch still applies to the
        # ref publish cuts it from, so 10 stays a partner, and 11 — which changes 10's line —
        # is never accepted unchecked. The leftover is in the way of building 10's commit,
        # so the first check cannot complete and refuses, and stderr names the file in git's
        # own words; the check never removes it, as it is not the check's own. Once the file
        # is gone, the check runs with 10 and refuses 11 for its conflict with 10.
        self._on_base({"shared.txt": NINE, ".gitignore": "*.log\n"})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten"), "ten.log": "ten\n"})
        self._plan("11", {"shared.txt": _edit(NINE, l2="eleven")})
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        wt = integrate._integ_worktree(self.fx.primary, "main")
        leftover = wt / "ten.log"
        seen: dict[str, object] = {}

        def leave_then_accept(t, d):
            if not (wt / ".git").exists():                 # made as a fold makes it
                sb._git(t.fx.primary, "worktree", "add", "-q", "--force", str(wt),
                        "origin/main")
            leftover.write_text("left behind\n", encoding="utf-8")
            seen["ignored"] = sb._run(wt, "check-ignore", "-q", "ten.log").returncode
            seen["adds"] = "+++ b/ten.log" in (t._d("10") / "patch.diff").read_text(
                encoding="utf-8")
            ACCEPT(t, d)

        def remove_then_retry(t, d):
            seen["still_there"] = leftover.exists()
            leftover.unlink()
            OVERRIDE(t, d)

        self.script["10"] = [leave_then_accept]
        self.script["11"] = [ACCEPT, remove_then_retry]
        self._run(["10", "11", "20"])

        # The fault was there: an ignored file at a path 10's patch adds.
        self.assertEqual((seen.get("ignored"), seen.get("adds")), (0, True), seen)
        self._published("10")
        # First check: 10 was a partner, never left out, and building its commit failed on
        # the leftover — a step after its patch was found to apply — so it refused.
        self.assertNotIn("left out", self.err)
        self.assertEqual(self._named("11"), 3, self.sessions)   # refused twice, then parked
        self.assertTrue(seen.get("still_there"), seen)          # the check's reset kept it
        st, opened, _ticked = self.at_session["11"][1]
        self.assertEqual(st, state.AWAITING_SIGNOFF, self.err)
        ours = [r for r in opened if SAME_WAVE in r]
        self.assertEqual(len(ours), 1, opened)
        self.assertIn("could not complete", ours[0])
        refusals = [x for x in self._lines("11") if "cannot accept: the same-wave" in x]
        self.assertEqual(len(refusals), 1, self.err)
        self.assertIn("issue_10's would-be commit could not be built: `git apply` failed onto "
                      "origin/main, though `git apply --check --cached` passed; git said: ",
                      refusals[0])
        # What git said names the file in the way — on stderr only, never in the row.
        self.assertIn("ten.log: already exists in working directory", refusals[0])
        self.assertNotIn("ten.log", ours[0])
        # Retried with the file gone: the check merged 10 and then 11, and refused 11 for its
        # conflict with 10 — the could-not-complete row ticked, the conflict row open.
        self.assertIn(["issue_10", "issue_11"], self.merged_in_trial, self.merged_in_trial)
        opened, ticked = self._ours("11")
        self.assertEqual((len(opened), len(ticked)), (1, 1), self.err)
        self.assertIn("same-wave conflict: issue_11's change conflicts with issue_10",
                      opened[0])
        self.assertIn("shared.txt", opened[0])
        self.assertIn("could not complete", ticked[0])
        self.assertEqual(state.state(self._d("11")), state.AWAITING_SIGNOFF)
        self._not_published("11")
        self.assertEqual(self.holds, {}, self.err)
        self._published("20")
        self.assertEqual(self.rc, 1, self.err)

    def test_an_ignored_file_a_siblings_patch_adds_is_removed_after_each_build(self):
        # The base ignores *.log; 10's patch changes line 2 of shared.txt and adds ten.log,
        # which `git add --all` leaves out of 10's would-be commit, as publish's leaves it out
        # of 10's branch. Nothing is planted: 11's check builds 10's commit, and so writes
        # ten.log in the shared integration worktree, whose reset keeps ignored files. The
        # check removes it again, so 12's check builds 10's commit once more and completes:
        # 11 and 12 are accepted and published with no row, 20 (on 12) builds, and no
        # ten.log is left in the worktree. An ignored file the check did not write — a
        # re-gate's build cache, say — is kept. The run does not sweep its worktrees
        # (`[driver].sweep_worktrees = "off"`): the default removes the integration worktree
        # once the run ends, which would leave nothing to look at — and with "off", a file
        # the check left would stay in the way of every later run's check too.
        self.cfg.sweep_worktrees = "off"
        self._on_base({"shared.txt": NINE, ".gitignore": "*.log\n"})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten"), "ten.log": "ten\n"})
        self._plan("11", {"eleven.txt": "eleven\n"})
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "12")
        wt = integrate._integ_worktree(self.fx.primary, "main")
        cache = wt / "build-cache.log"

        def cache_then_accept(t, d):
            if not (wt / ".git").exists():                 # made as a fold makes it
                sb._git(t.fx.primary, "worktree", "add", "-q", "--force", str(wt),
                        "origin/main")
            cache.write_text("cached\n", encoding="utf-8")
            ACCEPT(t, d)

        self.script["10"] = [cache_then_accept]
        self._run(["10", "11", "12", "20"])

        for iid in ("10", "11", "12", "20"):
            self._published(iid)
        for iid in ("11", "12"):
            self.assertEqual(self._ours(iid), ([], []), (iid, self.err))
        self.assertNotIn("cannot accept", self.err)
        self.assertNotIn("left out", self.err)
        self.assertEqual(self.holds, {}, self.err)
        self.assertEqual(self.rc, 0, self.err)
        # 10 was a partner of both checks, so its commit was built twice in the one tree —
        # the second time after the first had applied its patch, ten.log and all.
        tens = [head for name, head, _ref in self.would_be if name == "issue_10"]
        self.assertEqual(len(tens), 2, self.would_be)
        self.assertIn(["issue_10", "issue_11"], self.merged_in_trial, self.merged_in_trial)
        self.assertIn(["issue_10", "issue_11", "issue_12"], self.merged_in_trial,
                      self.merged_in_trial)
        # Its would-be commit holds no ten.log, as the branch publish pushed holds none …
        for head in (tens[0], self.fx._tip("fix/10")):
            self.assertNotIn("ten.log", sb._git(self.fx.primary, "ls-tree", "-r",
                                                "--name-only", head).split(), head)
        # … and none is left in the worktree; the ignored file the check did not write stays.
        self.assertTrue((wt / ".git").exists())
        self.assertFalse((wt / "ten.log").exists(), self.err)
        self.assertTrue(cache.is_file(), "the check removed an ignored file it did not write")
        self.assertEqual(cache.read_text(encoding="utf-8"), "cached\n")

    # -- a sibling whose cut is gone, or that publish cuts no branch for, is left out -------

    def test_a_sibling_whose_onto_branch_is_deleted_is_left_out_of_the_check(self):
        # 10 commits onto someone's open PR branch (`Onto branch: upstream/feat`), which this
        # checkout has fetched. In 10's session that branch is deleted on its remote: after
        # the check's pruning fetch, 10's cut ref no longer resolves, so 10 is no partner.
        # 11 and 12 are accepted, 12 checked against 11; 10's real publish then fails where
        # it cuts, and the fold holds it as a bundle that pushed no branch.
        self._on_base({"shared.txt": NINE})
        upstream = self.fx.tmp / "upstream.git"
        subprocess.run(["git", "init", "--bare", "-q", str(upstream)], check=True)
        sb._git(self.fx.primary, "remote", "add", "upstream", str(upstream))
        self.fx._push_from_human({"feat.txt": "feat\n"}, to="feat", url=str(upstream))
        sb._git(self.fx.primary, "fetch", "-q", "upstream")
        d10 = self._plan("10", {"ten.txt": "ten\n"})
        self.briefs["10"] += "- **Onto branch:** upstream/feat\n"
        (d10 / "brief.md").write_text(self.briefs["10"], encoding="utf-8")
        self._plan("11", {"eleven.txt": "eleven\n"})
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "12")
        resolves = lambda: sb._run(self.fx.primary, "rev-parse", "--verify", "--quiet",
                                   "upstream/feat^{commit}").returncode == 0
        seen: dict[str, bool] = {}

        def delete_then_accept(t, d):
            seen["before"] = resolves()                 # the stale ref, fetched earlier
            sb._git(upstream, "branch", "-D", "feat")   # the PR merged elsewhere, say
            ACCEPT(t, d)

        self.script["10"] = [self._texts_then(delete_then_accept)]
        self.real_publish = {"10"}
        self.open_prs = {"feat": publish._fork_owner(self.fx.primary, "upstream")}
        self._run(["10", "11", "12", "20"])

        self.assertEqual((seen.get("before"), resolves()), (True, False), seen)
        self._left_out_of_both("issue_10's would-be commit could not be built: upstream/feat, "
                               "where publish cuts its branch, does not resolve to a commit")
        # Publish cut 10 from that same ref, and failed there.
        out, err, _git_said = self.publish_seen["10"]
        self.assertIn("→ checkout -B feat upstream/feat", out)
        self.assertIn("publish: step failed:", err)
        self.assertIn("checkout -B feat upstream/feat", err)
        self._held_as_unpushed("10")
        self.assertEqual(self._cut_base(d10), ("upstream", "upstream/feat"))

    def test_a_sibling_publish_cuts_no_branch_for_is_left_out_of_the_check(self):
        # 10 `Stacks on` 05, which has no published branch: publish cuts no branch for 10 and
        # refuses it. 10 is no partner; 11 and 12 are accepted, 12 checked against 11.
        self._on_base({"shared.txt": NINE})
        d10 = self._stacks_on_unpublished("10", {"ten.txt": "ten\n"})
        self._plan("11", {"eleven.txt": "eleven\n"})
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "12")
        self.script["10"] = [self._texts_then(ACCEPT)]
        self.real_publish = {"10"}
        self._run(["10", "11", "12", "20"])

        self._left_out_of_both("publish would cut no branch for issue_10")
        # Publish refused 10 for that same reason.
        self.assertIn("`Stacks on` a prereq with no published branch yet",
                      self.publish_seen["10"][1])
        self._held_as_unpushed("10")
        self.assertIsNone(self._cut_base(d10))

    def test_only_a_check_that_completes_names_a_sibling_it_left_out(self):
        # As above, 10 has no cut, so it is left out of 11's check. 11's first check cannot
        # complete — every fetch fails, both tries — so 11 is refused, and stderr does not
        # also say 10 was left out of a check that never got that far. Retried once the
        # fetch works, the check completes: it names 10 as left out, once, and accepts 11.
        self._on_base({"shared.txt": NINE})
        self._stacks_on_unpublished("10", {"ten.txt": "ten\n"})
        self._plan("11", {"eleven.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        fault = {"on": True}
        checking: list[str] = []
        real_git = integrate._git
        real_clash = getattr(integrate, "same_wave_clash", None)

        def clash(cfg, d, *a, **k):
            checking.append(d.name)
            try:
                return real_clash(cfg, d, *a, **k)
            finally:
                checking.pop()

        def git(repo, *args):
            if fault["on"] and checking and args[:1] == ("fetch",):
                return 1
            return real_git(repo, *args)

        def retry_once_cleared(t, d):
            fault["on"] = False
            OVERRIDE(t, d)

        self.script["10"] = [self._texts_then(ACCEPT)]
        self.script["11"] = [ACCEPT, retry_once_cleared]
        self.real_publish = {"10"}
        self.extra = [mock.patch.object(integrate, "_git", git),
                      mock.patch.object(integrate, "_FETCH_RETRY_PAUSE", 0),
                      mock.patch.object(integrate, "same_wave_clash", clash, create=True)]
        self._run(["10", "11", "20"])

        self._published("11")
        self._published("20")
        self.assertEqual(self._named("11"), 2, self.sessions)
        lines = self.err.splitlines()
        refusals = [i for i, x in enumerate(lines)
                    if "issue_11 — cannot accept: the same-wave" in x]
        self.assertEqual(len(refusals), 1, self.err)
        self.assertIn("could not fetch", lines[refusals[0]])
        left = self._left_out("10", "11", "publish would cut no branch for issue_10")
        self.assertGreater(lines.index(left), refusals[0], self.err)   # said by the retry
        self._held_as_unpushed("10")

    # -- the bundle being accepted is never left out of its own check -----------------------

    def test_a_candidate_publish_cuts_no_branch_for_is_refused_as_unchecked(self):
        # 11 `Stacks on` 05, which has no published branch, so publish would cut no branch
        # for 11. Accepted after 10, it owes a check, and the commit it would push cannot be
        # built: refused, "could not complete" — it is not left out of its own check.
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"ten.txt": "ten\n"})
        d11 = self._stacks_on_unpublished("11", {"eleven.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        self._run(["10", "11", "20"])

        self._published("10")
        self.assertNotIn("left out", self.err)
        opened, ticked = self._ours("11")
        self.assertEqual((len(opened), ticked), (1, []), self.err)
        self.assertIn("could not complete", opened[0])
        refusals = [x for x in self._lines("11") if "cannot accept: the same-wave" in x]
        self.assertEqual(len(refusals), 1, self.err)
        self.assertIn("publish would cut no branch for issue_11", refusals[0])
        self.assertEqual(self.results["11"], state.AWAITING_SIGNOFF, self.err)
        self._not_published("11")
        self.assertNotIn("11", self.holds, self.err)
        self._published("20")
        self.assertEqual(self.rc, 1, self.err)
        self.assertIsNone(self._cut_base(d11))

    # -- a fetch that fails once is tried again ---------------------------------------------

    def test_a_check_whose_fetch_fails_once_tries_again_and_refuses_nothing(self):
        # 11's check fetches on its accept, after 10's. Its first fetch fails once (another git
        # process holding a ref lock, say): the check tries that fetch again and completes,
        # so 11 is accepted, with no row — one failed fetch refuses nothing.
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"eleven.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        fetches: list[tuple[tuple[str, ...], int]] = []   # (args, rc) while a check runs
        checking: list[str] = []
        real_git = integrate._git
        real_clash = getattr(integrate, "same_wave_clash", None)

        def clash(cfg, d, *a, **k):
            checking.append(d.name)
            try:
                return real_clash(cfg, d, *a, **k)
            finally:
                checking.pop()

        def git(repo, *args):
            if checking and args[:1] == ("fetch",):
                rc = 1 if not fetches else real_git(repo, *args)   # the first one fails
                fetches.append((args, rc))
                return rc
            return real_git(repo, *args)

        self.extra = [mock.patch.object(integrate, "_git", git),
                      mock.patch.object(integrate, "_FETCH_RETRY_PAUSE", 0),
                      mock.patch.object(integrate, "same_wave_clash", clash, create=True)]
        self._run(["10", "11", "20"])

        self.assertGreaterEqual(len(fetches), 2, fetches)
        self.assertEqual(fetches[0][1], 1, fetches)                # failed once …
        self.assertEqual(fetches[1], (fetches[0][0], 0), fetches)  # … the same fetch again
        for iid in ("10", "11", "20"):
            self._published(iid)
        self.assertEqual(self._ours("11"), ([], []), self.err)
        self.assertFalse([x for x in self._lines("11") if "cannot accept" in x], self.err)
        self.assertEqual(self.rc, 0, self.err)

    # -- (f) the row is SUMMARY-only: reassembled away, it comes back ----------------------

    def test_a_reassembled_summary_drops_the_row_and_the_next_accept_restores_it(self):
        ids = self._conflicting_pair()
        seen: dict[str, object] = {}

        def reassemble_and_accept(t, d):
            seen["before"] = t._ours("11")
            seen["items"] = [i.text for i in assemble.collect_needs_human(d, t.cfg)]
            (d / "SUMMARY.md").unlink()                   # back to CHECKED …
            driver.advance(d, t.cfg)                      # … the driver's own assembly
            seen["after"] = t._ours("11")
            seen["state"] = state.state(d)
            ACCEPT(t, d)

        self.script["11"] = [ACCEPT, reassemble_and_accept]
        self._run(ids)

        self.assertEqual(self._named("11"), 3, self.sessions)
        self.assertIn("before", seen, self.err)               # the reassembly session ran
        self.assertEqual(len(seen["before"][0]), 1, seen)
        self.assertFalse([i for i in seen["items"] if SAME_WAVE in i], seen)   # no artifact
        self.assertEqual(seen["after"], ([], []), seen)
        self.assertEqual(seen["state"], state.AWAITING_SIGNOFF)
        self._refused("11", "10", "shared.txt")               # refused again, row back open
        self._published("10")

    # -- (g) the last wave is not checked --------------------------------------------------

    def test_the_last_wave_is_not_checked(self):
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"shared.txt": _edit(NINE, l2="eleven")})
        self._run(["10", "11"])

        for iid in ("10", "11"):
            self._published(iid)
            self.assertEqual(self._ours(iid), ([], []), iid)
        self.assertEqual(self.holds, {}, self.err)
        self.assertEqual(self.rc, 0, self.err)

    # -- (h) one more session after a refusal, then parked — not the pass budget -----------

    def test_a_refused_bundle_is_offered_one_more_session_then_parked(self):
        ids = self._conflicting_pair()
        # 12's session accepts without clearing §6: a plain C6 refusal, every pass.
        self.script["12"] = [lambda t, d: _decide(d, "accept\n")]
        self._run(ids, max_passes=20)

        self.assertEqual(self._named("11"), 2, self.sessions)
        row = self._refused("11", "10", "shared.txt")
        line = self._parked_line("11")
        self.assertIn("conflict", line)
        self.assertIn("issue_10", line)
        self.assertIn("iterate-plan", line)
        self.assertFalse([x for x in self._lines("11") if BUDGET in x], self.err)
        # The row, the refusal and the parked line send the human to 10's change on the
        # base, never to a PR of 10.
        refusal = [x for x in self._lines("11") if "cannot accept: same-wave conflict" in x]
        self.assertEqual(len(refusal), 1, self.err)
        self._no_pr_of_a_partner("10", row, line, refusal[0])
        # A plain C6 refusal keeps today's behaviour: a session every pass, to the budget.
        self.assertEqual(self._named("12"), 20, self.sessions)
        self.assertTrue([x for x in self._lines("12") if "resume with" in x], self.err)
        self.assertIn(BUDGET, self.err)

    def test_a_session_that_raises_is_not_the_one_more_session(self):
        ids = self._conflicting_pair()
        self.script["11"] = [ACCEPT, RAISE, NOTHING]
        self._run(ids)

        self.assertEqual(self._named("11"), 3, self.sessions)
        self._refused("11", "10", "shared.txt")
        self._parked_line("11")

    def test_a_rerun_offers_one_session_again_then_parks(self):
        ids = self._conflicting_pair()
        self._run(ids)
        self.assertEqual(self._named("11", run=1), 2, self.sessions)
        record = self._d("11") / state.SAME_WAVE_REFUSAL
        self.assertTrue(record.is_file())

        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "11")   # a later wave again
        self._run(["11", "21"])
        self.assertEqual(self._named("11", run=2), 1, self.sessions)
        self.assertTrue(record.is_file())                     # set back, not deleted
        self._refused("11", "10", "shared.txt")
        self._parked_line("11")
        self.assertFalse([x for x in self._lines("11") if BUDGET in x], self.err)

    # -- (i) an iterate-plan on a refused bundle waits for the next run ---------------------

    def _parked_by_its_iterate_plan(self) -> Path:
        """Run 1: 11 is refused for its conflict with 10, and its session then writes
        iterate-plan: recorded, not applied, and 11 is neither re-planned nor rebuilt in this
        run; 12's session runs one more pass after that, which does not spin on 11. Then
        13 (a file of its own) and 21 (on 11) are planned for a second run, and 11's re-plan
        will declare `Conflicts with: 13`. Returns 11's bundle."""
        ids = self._conflicting_pair()
        self.script["11"] = [ACCEPT, ITERATE_PLAN, ACCEPT]
        self.script["12"] = [NOTHING, NOTHING, ACCEPT]     # the wave runs a pass after 11's
        self._run(ids)

        d = self._d("11")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "iterated-to-Plan")
        self.assertEqual(self.results["11"], state.ITERATE_PLAN, self.err)
        self.assertFalse((d / "iteration-v1").exists())    # not archived …
        self.assertTrue((d / "brief.md").is_file())
        self.assertEqual((self.replanned, self.replanned_in_wave), ([], []))   # … or re-planned
        last = [names for run, names in self.builds if run == 1][-1]
        self.assertNotIn("issue_11", last)                 # … nor rebuilt
        self.assertEqual(self._named("11"), 2, self.sessions)
        self.assertFalse([x for x in self._lines("11") if BUDGET in x], self.err)
        said = [x for x in self._lines("11") if "re-plans it" in x]
        self.assertTrue(said, self.err)
        self.assertTrue(any("next `pdca flow` run" in x for x in said), said)
        self._not_published("11")
        self._published("10")
        self._published("12")

        self._plan("13", {"thirteen.txt": "thirteen\n"})
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "11")
        self.replans["11"] = self.briefs["11"] + "- **Conflicts with:** 13\n"
        return d

    def _replanned_before_its_waves(self, d: Path, run: int = 2) -> None:
        """Run ``run`` archived 11 and re-planned it in the run's Plan step, before it built
        anything — never in a wave's build — so the new brief set the run's waves: 11 and
        13, which it now names in `Conflicts with`, never share one. Then 11 was built and
        offered a fresh session, which published it."""
        self.assertTrue((d / "iteration-v1" / "brief.md").is_file(), self.err)  # archived
        self.assertEqual(self.replanned, [(run, "11")], self.events)
        self.assertEqual(self.replanned_in_wave, [], self.events)
        events = self._run_events(run)
        builds = [i for i, (kind, _names) in enumerate(events) if kind == "build"]
        self.assertTrue(builds, events)
        self.assertIn(("plan", ["11"]), events[:builds[0]], events)
        self._never_built_together(run, "issue_11", "issue_13")
        self.assertEqual(self._named("11", run=run), 1, self.sessions)
        self.assertEqual(self.at_session["11"][2][0], state.AWAITING_SIGNOFF)   # a fresh one
        self._published("11")
        self._published("13")

    def test_an_iterate_plan_on_a_refused_bundle_is_applied_by_the_next_run(self):
        d = self._parked_by_its_iterate_plan()
        self._run(["11", "13", "21"])
        self._replanned_before_its_waves(d)

    def test_a_sweep_re_plans_a_parked_bundle_before_it_sets_its_waves(self):
        # The same, when the next run is a `--from-csv` sweep (`flow.flow_batch`), which
        # picks every in-flight bundle up: 11, 13 and 21 here.
        d = self._parked_by_its_iterate_plan()
        self._run(["11", "13", "21"], sweep=True)
        self.assertEqual(sorted(self.results), ["11", "13", "21"], self.err)   # the sweep
        self._replanned_before_its_waves(d)

    # -- (i) …and one whose re-open fails stays parked for that run ------------------------

    def _re_open_fails_once(self) -> list[str]:
        """Make the next re-open of 11 fail: the real ``driver.run_issue`` raises from its
        archive step before that moves anything — once; a later archive works. Returns the
        list each injected failure is noted in."""
        failed: list[str] = []
        real_archive = driver._archive_iteration

        def archive(d, n, *, include_brief):
            if d.name == "issue_11" and include_brief and not failed:
                failed.append(d.name)
                raise PermissionError("injected: the attempt could not be archived")
            return real_archive(d, n, include_brief=include_brief)

        self.extra = [mock.patch.object(driver, "_archive_iteration", archive)]
        return failed

    def _still_parked(self, d: Path, run: int, failed: list[str]) -> None:
        """Run ``run`` tried to re-open 11 and could not, so 11 stayed parked: ITERATE_PLAN with
        its marker, its attempt not archived. The run did not re-plan it, build it in a wave
        its old brief set, offer it a session or publish it, and it said so. The rest of the
        run went on: 13 was published; 21, on 11, waits."""
        self.assertEqual(failed, ["issue_11"], self.err)            # tried once, and failed
        self.assertEqual(state.state(d), state.ITERATE_PLAN, self.err)
        self.assertEqual(self.results["11"], state.ITERATE_PLAN, self.err)
        self.assertTrue((d / state.SAME_WAVE_PARKED).is_file(), self.err)
        self.assertFalse((d / "iteration-v1").exists(), self.err)
        self.assertEqual((self.replanned, self.replanned_in_wave), ([], []), self.events)
        self.assertFalse([n for r, n in self.builds if r == run and "issue_11" in n],
                         self.builds)
        self.assertEqual(self._named("11", run=run), 0, self.sessions)
        self._not_published("11")
        self._published("13")
        self.assertEqual(self.results["21"], state.PLANNED, self.err)
        self.assertEqual(self.rc, 1, self.err)
        said = "\n".join(self._lines("11"))
        self.assertIn("injected: the attempt could not be archived", said)   # what failed
        self.assertIn("stays parked", said)
        self.assertIn("re-plans it first", said)                     # the wave-end report
        self.assertNotIn(BUDGET, said)

    def test_a_parked_bundle_whose_re_open_fails_stays_parked_for_that_run(self):
        d = self._parked_by_its_iterate_plan()
        failed = self._re_open_fails_once()
        self._run(["11", "13", "21"])
        self._still_parked(d, 2, failed)

        # The next run, the fault gone, re-opens it and re-plans it before it sets its waves.
        self.extra = []
        self._run(["11", "13", "21"])
        self.assertFalse((d / state.SAME_WAVE_PARKED).exists(), self.err)
        self._replanned_before_its_waves(d, run=3)
        self._published("21")

    def test_a_sweep_keeps_a_parked_bundle_whose_re_open_fails_parked(self):
        d = self._parked_by_its_iterate_plan()
        failed = self._re_open_fails_once()
        self._run(["11", "13", "21"], sweep=True)
        self.assertEqual(sorted(self.results), ["11", "13", "21"], self.err)   # the sweep
        self._still_parked(d, 2, failed)

    # -- (i2) an iterate-do is rebuilt this run; discontinue as today -----------------------

    def test_an_iterate_do_on_a_refused_bundle_is_rebuilt_and_offered_again(self):
        ids = self._conflicting_pair()
        self.script["11"] = [ACCEPT, ITERATE_DO, ACCEPT, NOTHING]
        self._run(ids)

        # Rebuilt this run: its own session, refused again (still the same conflict), one
        # more session, then parked.
        self.assertEqual(self._named("11"), 4, self.sessions)
        self.assertEqual(self.at_session["11"][2][0], state.AWAITING_SIGNOFF)
        self.assertFalse([r for r in self.at_session["11"][2][1] if SAME_WAVE in r])
        self._refused("11", "10", "shared.txt")
        self._parked_line("11")
        # The refused attempt went to the archive with its record and its row: the rebuilt
        # one started with neither, so its count of sessions started over.
        archived = self._d("11") / "iteration-v1"
        self.assertIn(SAME_WAVE, (archived / "SUMMARY.md").read_text(encoding="utf-8"))
        self.assertTrue((archived / state.SAME_WAVE_REFUSAL).is_file(), self.err)

    def test_a_discontinue_on_a_refused_bundle_behaves_as_today(self):
        ids = self._conflicting_pair()
        self.script["11"] = [ACCEPT, DISCONTINUE]
        self._run(ids)

        d = self._d("11")
        self.assertEqual(self.results["11"], state.DISCONTINUED, self.err)
        self.assertFalse((d / state.SAME_WAVE_PARKED).exists())
        self.assertEqual(self._named("11"), 2, self.sessions)
        self.assertEqual(len(self._ours("11")[0]), 1, self.err)   # no row added for it
        self._not_published("11")

    # -- (j) a standalone iterate-plan, then a re-run: a fresh session ----------------------

    def test_a_standalone_iterate_plan_then_a_rerun_gets_a_fresh_session(self):
        ids = self._conflicting_pair()
        self._run(ids)
        self._refused("11", "10", "shared.txt")

        args = argparse.Namespace(issue_id="11", accept=False, iterate_do=False,
                                  iterate_plan=True, by="T", delta="declare it",
                                  no_publish=True)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli._signoff(self.cfg, args), 0)
        d = self._d("11")
        self.assertEqual(state.state(d), state.UNPLANNED)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())   # 780, on the base
        (d / "brief.md").write_text(self.briefs["11"], encoding="utf-8")   # re-planned

        # The re-planned attempt is not accepted by a decision left from the refused one: it
        # is offered a session of its own, and it is that session's accept that publishes it.
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "11")
        self.script["11"] = [ACCEPT, NOTHING, ACCEPT]
        self._run(["11", "21"])
        self.assertEqual(self._named("11", run=2), 1, self.sessions)
        self.assertEqual(self.at_session["11"][2][0], state.AWAITING_SIGNOFF)
        self.assertEqual(self._ours("11"), ([], []), self.err)      # a fresh SUMMARY
        self._published("11")

    # -- (k) the partner's publish fails ---------------------------------------------------

    def test_a_partner_whose_publish_fails_leaves_the_refused_bundle_to_a_rerun(self):
        ids = self._conflicting_pair()
        self.publish_fails = {"10"}         # here the partner's publish fails for any reason
        self.script["11"] = [ACCEPT, NOTHING, ACCEPT, OVERRIDE]
        self._run(ids)

        row = self._refused("11", "10", "shared.txt")
        self.assertEqual(self.results["10"], state.COMPLETE)
        self._not_published("10")
        self.assertEqual(self.holds, {"10": "unpushed"}, self.err)
        self.assertIn("issue_10 pushed no branch this run", self.err)
        self.assertEqual(self.rc, 1, self.err)

        # Run 2 names both, and 11's session accepts it plainly, its row still open. 10 is
        # skipped as finished and not carried (no branch on record), so 11 has no accepted
        # sibling and no check runs; C6 refuses the accept on run 1's row — that row is the
        # human's (the stale row is the 0.62 follow-up, #712). One session, then parked, and
        # nothing of it names a PR of 10, which has none.
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "11")
        self.publish_fails = set()
        calls: list[str] = []
        real_clash = getattr(integrate, "same_wave_clash", None)

        def clash(cfg, d, *a, **k):
            calls.append(d.name)
            return real_clash(cfg, d, *a, **k)

        self.extra = [mock.patch.object(integrate, "same_wave_clash", clash, create=True)]
        self._run(["10", "11", "21"])
        self.assertIn("issue_10 — already terminal", self.err)
        self.assertEqual(calls, [], self.err)
        self.assertEqual(self._named("11", run=2), 1, self.sessions)
        self.assertTrue([x for x in self._lines("11") if "(C6)" in x], self.err)
        self.assertEqual(self._refused("11", "10", "shared.txt"), row)
        line = self._parked_line("11")
        self.assertIn("pdca signoff 11 --iterate-plan", line)
        self._no_pr_of_a_partner("10", row, line)
        self.assertEqual(self.results["21"], state.PLANNED, self.err)
        self.assertEqual(self.rc, 1, self.err)

        # Run 3: the row ticked, and accepted. Still no accepted sibling, so no check: 11 is
        # published, and its dependent builds.
        self._run(["10", "11", "21"])
        self.assertEqual(calls, [], self.err)
        self._published("11")
        self._published("21")
        self._not_published("10")


if __name__ == "__main__":
    unittest.main()
