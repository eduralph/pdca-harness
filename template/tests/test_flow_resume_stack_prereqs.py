"""A re-issued stack-mode run carries its finished prerequisites onto its line (#646).

The recovery for a stack-mode run that stopped part-way is to re-issue the same
`pdca flow <ids>`. The ids the earlier run finished are skipped as terminal, so they never
reached the run's fold, and a dependent of one was cut from the plain base without it —
built, verified and published, with nothing said. These cases drive
``flow._drive_and_act(cfg, [<drive set>], batch=[<all requested ids>])`` — the shape
``flow_ids`` produces when finished ids are skipped — against real git: the ``StackFoldGit``
fixture of tests/test_integrate_stack_bases.py (a bare ``origin`` and a primary checkout),
loaded by path and used as a helper object, so its own cases are not collected here. The
build and sign-off leaves are stubbed, ``flow._publish_bundle`` is the fixture's
``_publish``, and ``gh`` is answered by a stub in front of ``subprocess.run``. The publisher
is NOT a stub (a stub turns every fold into a dry-run), except in the dry-run case.

A finished id the carry's fold skips (its merge fails there) keeps all its work off the
line: a finished id whose branch holds any of it, with a ``Depends on`` edge or without,
merged before the skip or after, is held for it, and the run still pushes once (#672).

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_resume_stack_prereqs
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import io
import json
import os
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import flow, gates, integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-08"


class ResumeStackPrereqs(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.gh: dict[str, str] | None = {}   # PR state by id; None: `gh` is not installed
        self.gh_heads: dict[str, str] = {}    # PR head by id, where it is not the branch tip
        self.gh_calls: list[list[str]] = []
        self.events: list[str] = []
        # bundle → (stack base, recorded tip, origin's line tip) when its wave was driven
        self.seen: dict[str, tuple[str, str, str | None]] = {}
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _finished(self, iid: str, files: dict[str, str] | None, *, pr: str = "OPEN",
                  patch: str | None = "") -> Path:
        """``iid`` finished in an earlier run: accepted at sign-off, then published (its
        branch pushed, ``publish.json`` written after the accept) with its PR in state
        ``pr``. ``files`` None: no change at all — ``patch`` is then its patch.diff text, or
        None for none (a close disposition, whose marker stands in for it)."""
        d = self.cfg.bundle(iid)
        sb._brief(d)
        sb._accept(d)
        if files is None:
            name, text = ((state.CLOSE_MARKER, "likely-close\n") if patch is None
                          else ("patch.diff", patch))
            (d / name).write_text(text, encoding="utf-8")
        else:
            self.fx._publish(iid, files)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.gh[iid] = pr
        return d

    def _planned(self, iid: str, depends_on: str = "") -> Path:
        d = self.cfg.bundle(iid)
        sb._brief(d, extra=f"- **Depends on:** {depends_on}\n" if depends_on else "")
        return d

    def _line(self, batch: list[str]) -> str:
        return integrate.integration_branch(self.cfg, "main", batch)

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """``gh pr view <url> --json state,headRefOid`` answered from ``self.gh``; the PR head
        is the branch's tip on origin, or ``self.gh_heads``' entry. Everything else runs for
        real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        self.gh_calls.append(list(args))
        if self.gh is None:
            raise FileNotFoundError(2, "No such file or directory", "gh")
        iid = str(args[3]).rsplit("/", 1)[-1]
        head = self.gh_heads.get(iid) or (
            self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else "")
        out = json.dumps({"state": self.gh.get(iid, "OPEN"), "headRefOid": head})
        return subprocess.CompletedProcess(args, 0, out, "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle, recording what it was built on."""
        for d in wave:
            line = self.fx._tip(self.line) if self.fx._has(self.line) else None
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d),
                                 line)
            self.events.append(f"wave:{d.name}")
            (d / "patch.diff").write_text(sb._new_file_patch(f"{d.name}.txt", d.name),
                                          encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded line tip (else the line,
        else the base), push it, record it."""
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        self.fx._publish(d.name.removeprefix("issue_"), {f"{d.name}.txt": d.name + "\n"},
                         cut_from=tip or (f"origin/{line}" if line else "origin/main"))
        return True

    def _run(self, drive: list[Path], batch: list[str], *, regate=None) -> None:
        """The re-issued run: drive ``drive`` with the run asked for ``batch``."""
        self.line = self._line(batch)
        real_fold = integrate.fold

        def fold(*a, **kw):
            self.events.append("fold")
            return real_fold(*a, **kw)

        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(integrate, "fold", fold))
            if regate is not None:
                stack.enter_context(mock.patch.object(gates, "run_integration", regate))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            self.results = flow._drive_and_act(self.cfg, drive, do_publish=True,
                                               do_act=False, by="T", today=TODAY,
                                               batch=batch)
        self.err, self.out = err.getvalue(), out.getvalue()
        self.assertNotIn("Traceback", self.err)

    def _held(self, prereq: str, dependent: str, *reason: str) -> str:
        """``dependent`` was never built and is left PLANNED, and ONE stderr line names
        ``prereq``, ``dependent`` and the reason. Returns that line."""
        self.assertNotIn(f"issue_{dependent}", self.seen, self.err)
        self.assertEqual(self.results[dependent], state.PLANNED)
        lines = [x for x in self.err.splitlines()
                 if f"issue_{prereq}" in x and f"issue_{dependent}" in x]
        self.assertEqual(len(lines), 1, self.err)
        for part in reason:
            self.assertIn(part, lines[0])
        return lines[0]

    def _built(self, iid: str) -> tuple[str, str, str | None]:
        """What ``iid`` was built on — (stack base, recorded tip, origin's line tip) — once
        it is checked that it was built at all."""
        self.assertIn(f"issue_{iid}", self.seen, self.err)
        return self.seen[f"issue_{iid}"]

    def _on_base(self, iid: str) -> None:
        """``iid`` was built, on the plain base."""
        self.assertEqual(self._built(iid)[:2], ("", ""), self.err)
        self.assertEqual(self.results[iid], state.COMPLETE)

    # -- (1)-(3): a clean finished prerequisite is carried ----------------------------------

    def test_a_clean_finished_prerequisite_is_on_the_line_its_dependent_builds_on(self):
        # (1) Before #646 D's stale-clear left it on the plain base, and no line held P.
        self._finished("P", {"p.txt": "p\n"})
        d = self._planned("D", "P")
        self._run([d], ["P", "D"])
        base, tip, line_tip = self._built("D")
        self.assertEqual(base, self._line(["P", "D"]), self.err)
        self.assertIsNotNone(line_tip)
        self.assertEqual(tip, line_tip)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/P"), line_tip))
        self.assertEqual(self.results["D"], state.COMPLETE)

    def test_an_earlier_runs_line_is_continued_never_replaced(self):
        # (2) The earlier run's line holds P on the base. Origin refuses non-fast-forwards:
        # a rebuilt line (its merge commit dated differently) could not land.
        p = self._finished("P", {"p.txt": "p\n"})
        d = self._planned("D", "P")
        line = self._line(["P", "D"])
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_DATE": "@1700000000 +0000",
                                          "GIT_COMMITTER_DATE": "@1700000000 +0000"}):
            integrate.fold(self.cfg, [p], folded_this_run={}, batch=["P", "D"])
        old = self.fx._tip(line)
        sb._git(self.fx.origin, "config", "receive.denyNonFastForwards", "true")
        pushes = self.fx._pushes(lambda: self._run([d], ["P", "D"]))
        self.assertEqual(self._built("D")[:2], (line, old), self.err)
        self.assertTrue(self.fx._ancestor(old, line))
        self.assertTrue(pushes)
        self.assertEqual([x for x in pushes if "--force" in x], [], pushes)
        self.assertEqual(self.results["D"], state.COMPLETE)

    def test_the_fold_after_wave_0_continues_the_carried_line(self):
        # (3) E depends on D: the fold after wave 0 continues the carried line — no force,
        # no IntegrationError — and E builds on a line holding P and D.
        self._finished("P", {"p.txt": "p\n"})
        d, e = self._planned("D", "P"), self._planned("E", "D")
        line = self._line(["P", "D", "E"])
        pushes = self.fx._pushes(lambda: self._run([d, e], ["P", "D", "E"]))
        self.assertNotIn("did not integrate", self.err)
        d_base, d_tip, carried = self._built("D")
        self.assertEqual((d_base, d_tip), (line, carried), self.err)
        self.assertEqual(len(pushes), 2, pushes)        # the carry, then the wave-0 fold
        self.assertNotIn("--force", pushes[1])
        e_base, e_tip, e_line = self._built("E")
        self.assertEqual((e_base, e_tip), (line, e_line))
        for commit in (carried, self.fx._tip("fix/P"), self.fx._tip("fix/D")):
            self.assertTrue(self.fx._ancestor(commit, e_line), commit)
        self.assertEqual(self.results["E"], state.COMPLETE)

    # -- (4): not clean ⇒ hold only its dependents; the run goes on -------------------------

    def test_a_closed_prerequisite_holds_only_its_dependents(self):
        # (4a)
        self._finished("P", {"p.txt": "p\n"}, pr="CLOSED")
        d, u = self._planned("D", "P"), self._planned("U")
        self._run([d, u], ["P", "D", "U"])
        self._held("P", "D", "CLOSED", "re-issue")
        self._on_base("U")
        self.assertFalse(self.fx._has(self.line))

    def test_an_unreadable_pr_state_holds_only_its_dependents(self):
        # (4b) `gh` is not installed: an OSError, never a traceback.
        self._finished("P", {"p.txt": "p\n"})
        d, u = self._planned("D", "P"), self._planned("U")
        self.gh = None
        self._run([d, u], ["P", "D", "U"])
        self._held("P", "D", "could not be read")
        self.assertTrue(self.gh_calls)
        self._on_base("U")
        self.assertFalse(self.fx._has(self.line))

    def test_of_two_conflicting_prerequisites_the_one_asked_for_first_is_carried(self):
        # (4c) Q is asked for before P (not name order): Q is carried, P conflicts with it,
        # so P's dependent A is held and Q's dependent B builds on the line.
        self._finished("P", {"c.txt": "p\n"})
        self._finished("Q", {"c.txt": "q\n"})
        a, b = self._planned("A", "P"), self._planned("B", "Q")
        self._run([a, b], ["Q", "P", "A", "B"])
        self._held("P", "A", "conflict")
        base, tip, line_tip = self._built("B")
        self.assertEqual((base, tip), (self.line, line_tip), self.err)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/Q"), line_tip))
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/P"), self.line))
        self.assertEqual(self.results["B"], state.COMPLETE)

    # -- (5): an untrusted line is not built on, and the run's first fold replaces it -------

    def test_an_untrusted_line_holds_the_carry_and_the_first_fold_replaces_it(self):
        self._finished("P", {"p.txt": "p\n"})
        d, u, w = self._planned("D", "P"), self._planned("U"), self._planned("W", "U")
        batch = ["P", "D", "U", "W"]
        line = self._line(batch)
        rejected = self.fx._push_from_human({"old.txt": "rejected\n"}, to=line)
        self._run([d, u, w], batch)
        held = self._held("P", "D", line, "delete")
        self.assertIn("re-issue", held)
        self._on_base("U")
        self.assertEqual(self._built("U")[2], rejected)        # the carry pushed nothing
        self.assertFalse(self.fx._ancestor(rejected, line))    # the wave-0 fold replaced it
        w_base, w_tip, w_line = self._built("W")
        self.assertEqual((w_base, w_tip), (line, w_line))
        self.assertFalse(self.fx._ancestor(rejected, w_line))
        self.assertEqual(self.results["W"], state.COMPLETE)

    # -- (6): the trigger -------------------------------------------------------------------

    def test_a_prerequisite_without_a_patch_carries_nothing(self):
        self._finished("P", None, patch=None)        # COMPLETE, no patch.diff at all
        d = self._planned("D", "P")
        self._run([d], ["P", "D"])
        self._on_base("D")
        self.assertNotIn("fold", self.events)
        self.assertEqual(self.gh_calls, [])
        self.assertFalse(self.fx._has(self.line))

    def test_an_unrelated_carryable_prerequisite_is_not_carried(self):
        self._finished("P", None, patch="")          # COMPLETE, empty patch.diff
        self._finished("Q", {"q.txt": "q\n"})       # carryable, but nothing depends on it
        d = self._planned("D", "P")
        self._run([d], ["P", "Q", "D"])
        self._on_base("D")
        self.assertNotIn("fold", self.events)
        self.assertEqual(self.gh_calls, [])
        self.assertFalse(self.fx._has(self.line))
        self.assertNotIn("issue_Q", self.err)

    # -- (7): unchanged without a trigger; a dry-run only prints -----------------------------

    def test_without_a_finished_prerequisite_the_run_is_unchanged(self):
        d, e = self._planned("D"), self._planned("E", "D")
        publish.write_stack_base(d, "pdca-integration/main-rstale", sb.ABSENT)   # stale
        self._run([d, e], ["D", "E"])
        self._on_base("D")                                   # the stale stack base cleared
        self.assertEqual(self.events, ["wave:issue_D", "fold", "wave:issue_E"])
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self._built("E")[0], self.line)

    def test_a_dry_run_asks_no_host_holds_nothing_and_prints_the_plan(self):
        self._finished("P", {"p.txt": "p\n"})
        d = self._planned("D", "P")
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
        self._run([d], ["P", "D"])
        self.assertEqual(self.gh_calls, [])
        self._on_base("D")
        self.assertNotIn("not carried", self.err)
        self.assertIn(f"fold 1 patch(es) onto {self.line}", self.out)
        self.assertIn("(issue_P)", self.out)
        self.assertFalse(self.fx._has(self.line))

    # -- (8): a red re-gate of the carried line ----------------------------------------------

    def test_a_red_regate_of_the_carried_line_holds_and_the_run_goes_on(self):
        self.cfg.regate_between_waves = True
        self._finished("P", {"p.txt": "p\n"})
        d, u, w = self._planned("D", "P"), self._planned("U"), self._planned("W", "U")
        p_head = self.fx._tip("fix/P")

        def regate(cfg, wt, *, hold_lock=True):              # red for a line holding P
            red = sb._run(wt, "merge-base", "--is-ancestor", p_head, "HEAD").returncode == 0
            return {"overall": "fail" if red else "pass"}

        self._run([d, u, w], ["P", "D", "U", "W"], regate=regate)
        self._held("P", "D", "re-gate")
        self._on_base("U")
        self.assertNotIn("STOPPING", self.err)
        w_base, w_tip, w_line = self._built("W")
        self.assertEqual((w_base, w_tip), (self.line, w_line))
        self.assertFalse(self.fx._ancestor(p_head, w_line))
        self.assertEqual(self.results["W"], state.COMPLETE)

    # -- fold(skipped=…) ----------------------------------------------------------------------

    def test_a_fold_given_skipped_passes_over_only_the_bundle_that_fails(self):
        self.assertIn("skipped", inspect.signature(integrate.fold).parameters)
        p = self.fx._publish("P", {"c.txt": "p\n"})
        q = self.fx._publish("Q", {"c.txt": "q\n"})          # conflicts with P
        r = self.fx._publish("R", {"r.txt": "r\n"})
        skipped: dict[str, str] = {}
        folded = integrate.fold(self.cfg, [p, q, r], folded_this_run={}, batch=["X"],
                                skipped=skipped)
        line = folded[sb.TARGET][0]
        self.assertEqual(list(skipped), ["issue_Q"])
        self.assertIn("conflict", skipped["issue_Q"])
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/P"), line))
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/R"), line))  # after the abort
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/Q"), line))
        # Every bundle of a target skipped: nothing pushed, no entry.
        s = self.fx._publish("S", {"s.txt": "s\n"})
        self.fx._delete("fix/S")
        skipped = {}
        with sb._merged_heads({}):
            pushes = self.fx._pushes(lambda: self.assertEqual(integrate.fold(
                self.cfg, [s], folded_this_run={}, batch=["Y"], skipped=skipped), {}))
        self.assertEqual((list(skipped), pushes), (["issue_S"], []))
        self.assertFalse(self.fx._has(self._line(["Y"])))

    # -- (9): not clean spreads to the finished ids built on it (#652, #624) -----------------

    def _finished_on(self, iid: str, prereq: str, *, pr: str = "OPEN") -> Path:
        """``iid`` finished in an earlier run, built on finished ``prereq``: its brief says
        ``Depends on: <prereq>`` and its branch is cut from ``origin/fix/<prereq>``, so it
        holds ``prereq``'s commit."""
        d = self.fx._publish(iid, {f"{iid.lower()}.txt": f"{iid}\n"},
                             cut_from=f"origin/fix/{prereq}")
        sb._brief(d, extra=f"- **Depends on:** {prereq}\n")   # after: _publish rewrites it
        sb._accept(d)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertTrue(self.fx._ancestor(self.fx._tip(f"fix/{prereq}"),
                                          self.fx._tip(f"fix/{iid}")))
        self.gh[iid] = pr
        return d

    def _not_carried(self, iid: str) -> str:
        """The ONE stderr line saying finished ``iid`` is not carried."""
        lines = [x for x in self.err.splitlines()
                 if x.startswith(f"flow: issue_{iid} (finished in an earlier run) is not")]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    def test_a_finished_id_built_on_a_closed_one_is_not_carried(self):
        # Q's PR is open, but its branch holds P's closed work. Before: Q was carried, and D
        # was built on a line holding P.
        self._finished("P", {"p.txt": "p\n"}, pr="CLOSED")
        self._finished_on("Q", "P")
        d, u = self._planned("D", "Q"), self._planned("U")
        self._run([d, u], ["P", "Q", "D", "U"])
        held = self._held("Q", "D", "issue_P", "built on")
        self.assertNotIn("get issue_Q's PR open", held)         # not the `open` hint
        self.assertIn("get issue_P carried, or re-drive issue_Q on a base without it", held)
        self.assertIn("CLOSED", self._not_carried("P"))
        self.assertNotIn("flow: carried", self.err)
        self.assertFalse(self.fx._has(self.line))               # no line holds anything
        self._on_base("U")

    def test_a_hold_cascades_through_a_dependent_finished_before_the_run(self):
        # (#624) U is held; D finished before the run (`Depends on: U`); E depends on D.
        self._finished("U", {"u.txt": "u\n"}, pr="CLOSED")
        self._finished_on("D", "U")
        e = self._planned("E", "D")
        self._run([e], ["U", "D", "E"])
        self._held("D", "E", "issue_U")
        self.assertFalse(self.fx._has(self.line))

    def test_not_clean_spreads_through_a_chain_of_finished_ids(self):
        # P (CLOSED) ← Q ← R ← D, asked for from the far end: any length, in any order.
        self._finished("P", {"p.txt": "p\n"}, pr="CLOSED")
        self._finished_on("Q", "P")
        self._finished_on("R", "Q")
        d = self._planned("D", "R")
        self._run([d], ["R", "Q", "P", "D"])
        self._held("R", "D", "issue_Q", "built on")
        self.assertIn("issue_P", self._not_carried("Q"))
        self.assertFalse(self.fx._has(self.line))

    def test_a_clean_finished_id_beside_them_is_still_carried(self):
        # X is clean, so it is carried: the line exists and is built on, and it holds
        # nothing of P's or Q's.
        self._finished("P", {"p.txt": "p\n"}, pr="CLOSED")
        self._finished_on("Q", "P")
        self._finished("X", {"x.txt": "x\n"})
        d, e, u = self._planned("D", "Q"), self._planned("E", "X"), self._planned("U")
        self._run([d, e, u], ["P", "Q", "X", "D", "E", "U"])
        self._held("Q", "D", "issue_P", "built on")
        self.assertIn("flow: carried issue_X (finished", self.err)
        self.assertTrue(self.fx._has(self.line), self.err)
        for iid in ("E", "U"):
            base, tip, line_tip = self._built(iid)
            self.assertEqual((base, tip), (self.line, line_tip), self.err)
            self.assertTrue(self.fx._ancestor(self.fx._tip("fix/X"), tip))
            for gone in ("P", "Q"):
                self.assertFalse(self.fx._ancestor(self.fx._tip(f"fix/{gone}"), tip), gone)
            self.assertEqual(self.results[iid], state.COMPLETE)
        for gone in ("P", "Q"):
            self.assertFalse(self.fx._ancestor(self.fx._tip(f"fix/{gone}"), self.line), gone)

    def test_a_held_ids_head_does_not_vouch_for_an_old_line(self):
        # An earlier run's line holds P and Q, and P's PR was closed since. Q is not clean,
        # so its head accounts for nothing on that line: nothing is carried onto it (X
        # neither), and the run's first fold replaces it.
        p = self._finished("P", {"p.txt": "p\n"}, pr="CLOSED")
        q = self._finished_on("Q", "P")
        self._finished("X", {"x.txt": "x\n"})
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        u, w = self._planned("U"), self._planned("W", "U")
        batch = ["P", "Q", "X", "D", "E", "U", "W"]
        line = self._line(batch)
        integrate.fold(self.cfg, [p, q], folded_this_run={}, batch=batch)   # the earlier run
        old = self.fx._tip(line)
        self._run([d, e, u, w], batch)
        self._held("Q", "D", line, "delete")
        self._held("X", "E", line, "delete")
        self._on_base("U")
        self.assertEqual(self._built("U")[2], old)              # the carry pushed nothing
        w_base, w_tip, w_line = self._built("W")
        self.assertEqual((w_base, w_tip), (line, w_line))
        for gone in ("P", "Q"):
            self.assertFalse(self.fx._ancestor(self.fx._tip(f"fix/{gone}"), w_line), gone)
        self.assertEqual(self.results["W"], state.COMPLETE)

    # -- (10): a skip in the carry's fold holds the finished ids holding its work (#672) ----

    def _update_ours(self, iid: str) -> None:
        """Someone updates ``fix/<iid>`` from the base, every conflict resolved in the
        branch's favour (``git merge -X ours``), and pushes it."""
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", f"fix/{iid}", f"origin/fix/{iid}")
        sb._git(human, "merge", "-q", "-X", "ours", "--no-edit", "origin/main")
        sb._git(human, "push", "-q", "origin", f"fix/{iid}")

    def _merge_into(self, iid: str, other: str) -> None:
        """Someone merges ``fix/<other>`` into ``fix/<iid>`` and pushes it."""
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", f"fix/{iid}", f"origin/fix/{iid}")
        sb._git(human, "merge", "-q", "--no-ff", "--no-edit", f"origin/fix/{other}")
        sb._git(human, "push", "-q", "origin", f"fix/{iid}")

    def _drop_edge(self, d: Path) -> None:
        """Rewrite finished ``d``'s brief with no ``Depends on`` (its branch is unchanged)
        and accept it again: the accept follows the brief."""
        sb._brief(d)
        sb._accept(d)
        self.assertNotIn("Depends on", (d / "brief.md").read_text(encoding="utf-8"))
        self.assertEqual(state.state(d), state.COMPLETE)

    def _merges_cleanly(self, iid: str, other: str) -> bool:
        """Whether ``fix/<other>`` merges onto ``fix/<iid>`` with no conflict, tried in the
        scratch clone (origin is not touched)."""
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", "trial", f"origin/fix/{iid}")
        ok = sb._run(human, "merge", "-q", "--no-edit", f"origin/fix/{other}").returncode == 0
        sb._run(human, "merge", "--abort")      # after a conflict; a no-op after a merge
        return ok

    def _finished_from(self, iid: str, files: dict[str, str], cut_from: str) -> Path:
        """``iid`` finished in an earlier run, its branch cut from ``cut_from`` and its brief
        naming no ``Depends on``; its PR is open."""
        d = self.fx._publish(iid, files, cut_from=cut_from)
        sb._accept(d)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertNotIn("Depends on", (d / "brief.md").read_text(encoding="utf-8"))
        self.gh[iid] = "OPEN"
        return d

    def _carried_beside(self, drive: list[Path], batch: list[str]) -> str:
        """Run with finished X and planned E (``Depends on: X``) beside the others: X is
        carried, E is built on the line and COMPLETE, and the run pushes exactly once — no
        push, then a fix-up. Returns the line tip E was built on."""
        pushes = self.fx._pushes(lambda: self._run(drive, batch))
        self.assertEqual(len(pushes), 1, pushes)
        carried = [x for x in self.err.splitlines() if x.startswith("flow: carried ")]
        self.assertEqual(len(carried), 1, self.err)
        self.assertIn("issue_X", carried[0])
        base, tip, line_tip = self._built("E")
        self.assertEqual((base, tip), (self.line, line_tip), self.err)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/X"), tip))
        self.assertEqual(self.results["E"], state.COMPLETE)
        return tip

    def _off_the_line(self, commit: str, e_tip: str) -> None:
        """``commit`` is on neither the carried line nor the tip E was built on."""
        for of in (self.line, e_tip):
            self.assertFalse(self.fx._ancestor(commit, of), f"{commit} is on {of}\n{self.err}")

    def test_a_skipped_ids_work_does_not_reach_the_line_through_one_built_on_it(self):
        # (1) Q is built on P (`Depends on: P`, cut from fix/P), then updated from a base
        # that conflicts with P, the conflict resolved for Q. P's merge fails in the fold.
        # Before: Q merged, so P's commit was on the line D was built on.
        self._finished("P", {"base.txt": "p\n"})
        self._finished_on("Q", "P")
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n"}, to="main")
        self._update_ours("Q")
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], ["P", "Q", "X", "D", "E"])
        held = self._held("Q", "D", "issue_P")
        self.assertIn("get issue_P carried, or re-drive issue_Q on a base without it", held)
        self.assertIn("does not merge cleanly", self._not_carried("P"))
        for gone in ("P", "Q"):
            self._off_the_line(self.fx._tip(f"fix/{gone}"), e_tip)

    def test_a_branch_holding_a_skipped_ids_work_is_held_with_no_depends_on_edge(self):
        # (2) As (1), but Q's brief names no prerequisite: its branch is still cut from
        # fix/P. The hold goes by commit, not by the edge.
        self._finished("P", {"base.txt": "p\n"})
        q = self._finished_on("Q", "P")
        self._drop_edge(q)
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n"}, to="main")
        self._update_ours("Q")
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], ["P", "Q", "X", "D", "E"])
        self._held("Q", "D", "issue_P")
        self.assertIn("does not merge cleanly", self._not_carried("P"))
        for gone in ("P", "Q"):
            self._off_the_line(self.fx._tip(f"fix/{gone}"), e_tip)

    def test_one_merged_before_the_skip_is_taken_off_the_line_before_the_push(self):
        # (3) Q is cut from P's first commit p1; P then adds p2, which conflicts with the
        # base. Asked for first, Q merges (with p1) before P's merge fails.
        self._finished("P", {"p1.txt": "p1\n"})
        p1 = self.fx._tip("fix/P")
        self._finished_on("Q", "P")
        self.fx._push_from_human({"base.txt": "p\n"}, to="fix/P", off="origin/fix/P")
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n"}, to="main")
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], ["Q", "P", "X", "D", "E"])
        self._held("Q", "D", "issue_P")
        self._off_the_line(p1, e_tip)

    def test_history_shared_through_an_earlier_line_is_held_too(self):
        # (4) An earlier run's line L held p1. P was rebuilt on L with a change that
        # conflicts with the base, and Q was published from L with no `Depends on: P`;
        # both record L as their stack base. L is gone, so the carry starts its line fresh.
        batch = ["P", "Q", "X", "D", "E"]
        old = self._line(batch)
        self._finished("P", {"p1.txt": "p1\n"})
        p1 = self.fx._tip("fix/P")
        sb._git(self.fx.origin, "branch", old, p1)
        p = self.fx._publish("P", {"base.txt": "p\n"}, cut_from=f"origin/{old}")
        q = self.fx._publish("Q", {"q.txt": "Q\n"}, cut_from=f"origin/{old}")
        for b in (p, q):
            sb._accept(b)
            publish.write_stack_base(b, old, p1)
            self.assertEqual(state.state(b), state.COMPLETE)
        self.assertNotIn("Depends on", (q / "brief.md").read_text(encoding="utf-8"))
        self.gh["Q"] = "OPEN"
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n"}, to="main")
        self.fx._delete(old)
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], batch)
        self._held("Q", "D", "issue_P")
        self._off_the_line(p1, e_tip)

    def test_a_holder_whose_own_merge_fails_too_is_held_for_the_skipped_ids_work(self):
        # (5) Q is built on P and not updated, so its merge fails the same way P's does.
        # Its line names P as the cause, not its own conflict; P keeps its own reason.
        self._finished("P", {"base.txt": "p\n"})
        self._finished_on("Q", "P")
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n"}, to="main")
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        self._carried_beside([d, e], ["P", "Q", "X", "D", "E"])
        line = self._not_carried("Q")
        self.assertIn("issue_P", line)
        self.assertNotIn("get issue_Q's PR open", line)
        self.assertIn("does not merge cleanly", self._not_carried("P"))

    def test_a_skip_that_is_not_a_conflict_holds_the_ids_holding_its_work(self):
        # (7) fix/P is deleted on origin while P's PR stays open, the host still reporting
        # its head: P is clean before the fold, and the fold skips it for the gone branch
        # (a plain IntegrationError, not a conflict). Q, cut from fix/P with no edge, holds
        # P's commit.
        self._finished("P", {"p1.txt": "p1\n"})
        q = self._finished_on("Q", "P")
        self._drop_edge(q)
        self._finished("X", {"x.txt": "x\n"})
        p1 = self.fx._tip("fix/P")
        self.gh_heads["P"] = p1
        self.fx._delete("fix/P")
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], ["P", "Q", "X", "D", "E"])
        self._held("Q", "D", "issue_P")
        self.assertIn("is not known to be merged", self._not_carried("P"))   # it reached the fold
        self._off_the_line(p1, e_tip)

    def test_a_failing_holder_of_an_earlier_commit_of_a_skipped_id_names_it(self):
        # Q is cut from P's first commit p1 with no `Depends on: P`; P then adds p2, which
        # conflicts with the base, and Q's own change conflicts with the base too, so both
        # merges fail (P and Q merge cleanly with each other). Q holds p1, P's work though
        # not P's tip: Q's line names P with the `built_on` hint, not its own conflict, and
        # P, asked for first, keeps its own reason.
        self._finished("P", {"p1.txt": "p1\n"})
        p1 = self.fx._tip("fix/P")
        self._finished_from("Q", {"other.txt": "q\n"}, "origin/fix/P")
        self.fx._push_from_human({"base.txt": "p\n"}, to="fix/P", off="origin/fix/P")
        self.assertTrue(self._merges_cleanly("P", "Q"))
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n", "other.txt": "r\n"}, to="main")
        d, e = self._planned("D", "Q"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], ["P", "Q", "X", "D", "E"])
        held = self._held("Q", "D", "issue_P")
        self.assertIn("get issue_P carried, or re-drive issue_Q on a base without it", held)
        self.assertNotIn("get issue_Q's PR open", held)
        self.assertIn("does not merge cleanly", self._not_carried("P"))
        self._off_the_line(p1, e_tip)

    def test_one_holding_only_a_commit_of_a_held_id_is_held_too(self):
        # R is cut from Q's own commit q1 (`Depends on: Q`, no edge to P). fix/Q then takes
        # in fix/P (p1), and P adds p2, which conflicts with the base. P's merge fails; Q
        # merges holding p1, so Q is held for P's work; R holds q1 and none of P's work, so
        # it is held for Q's. R is asked for before Q: the hold reaches it only once Q's
        # work has joined.
        self._finished("P", {"p1.txt": "p1\n"})
        p1 = self.fx._tip("fix/P")
        self._finished("Q", {"q.txt": "Q\n"})
        q1 = self.fx._tip("fix/Q")
        self._finished_on("R", "Q")
        self._merge_into("Q", "P")
        self.fx._push_from_human({"base.txt": "p\n"}, to="fix/P", off="origin/fix/P")
        self._finished("X", {"x.txt": "x\n"})
        self.fx._push_from_human({"base.txt": "r\n"}, to="main")
        self.assertTrue(self.fx._ancestor(p1, self.fx._tip("fix/Q")))
        self.assertFalse(self.fx._ancestor(p1, self.fx._tip("fix/R")))
        d, e = self._planned("D", "R"), self._planned("E", "X")
        e_tip = self._carried_beside([d, e], ["P", "R", "Q", "X", "D", "E"])
        held = self._held("R", "D", "issue_Q")
        self.assertIn("get issue_Q carried, or re-drive issue_R on a base without it", held)
        self.assertIn("issue_P", self._not_carried("Q"))
        for commit in (p1, q1, self.fx._tip("fix/Q"), self.fx._tip("fix/R")):
            self._off_the_line(commit, e_tip)

    def test_one_merged_only_on_a_held_ids_work_fails_on_the_restarted_line(self):
        # H is cut from P's first commit p1 and adds s.txt; S, cut from the base, adds the
        # same s.txt; T is cut from S. The base then adds a different s.txt, and H and T are
        # updated from it, keeping their own; P adds p2, which conflicts with the base. The
        # first pass: P fails, H merges, S merges on top of H (the same s.txt), T merges.
        # H holds p1, so it comes off and the line starts again: now S's merge fails, and T,
        # which holds S's commit, comes off in that pass. Only X is carried, with one push.
        self._finished("P", {"p1.txt": "p1\n"})
        p1 = self.fx._tip("fix/P")
        self._finished_from("H", {"s.txt": "s\n"}, "origin/fix/P")
        self._finished_from("S", {"s.txt": "s\n"}, "origin/main")
        s1 = self.fx._tip("fix/S")
        self._finished_from("T", {"t.txt": "t\n"}, "origin/fix/S")
        self.fx._push_from_human({"base.txt": "r\n", "s.txt": "m\n"}, to="main")
        self._update_ours("H")
        self._update_ours("T")
        self.fx._push_from_human({"base.txt": "p\n"}, to="fix/P", off="origin/fix/P")
        self._finished("X", {"x.txt": "x\n"})
        d, e = self._planned("D", "T"), self._planned("E", "X")
        tried: list[tuple[str, bool]] = []      # each merge the fold tried: (bundle, merged)
        real = integrate._merge_published

        def merge(cfg, wt, b, *a, **kw):
            try:
                real(cfg, wt, b, *a, **kw)
            except integrate.IntegrationError:
                tried.append((b.name, False))
                raise
            tried.append((b.name, True))

        with mock.patch.object(integrate, "_merge_published", merge):
            e_tip = self._carried_beside([d, e], ["P", "H", "S", "T", "X", "D", "E"])
        self.assertEqual([ok for name, ok in tried if name == "issue_S"], [True, False], tried)
        self.assertIn("flow: carried issue_X (finished", self.err)
        held = self._held("T", "D", "issue_S")
        self.assertIn("get issue_S carried, or re-drive issue_T on a base without it", held)
        self.assertIn("does not merge cleanly", self._not_carried("S"))
        self.assertIn("issue_P", self._not_carried("H"))
        for commit in (p1, s1, self.fx._tip("fix/H"), self.fx._tip("fix/T")):
            self._off_the_line(commit, e_tip)


if __name__ == "__main__":
    unittest.main()
