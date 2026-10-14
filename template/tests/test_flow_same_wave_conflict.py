"""A same-wave conflict holds one bundle, not the run (#674).

In stack mode two bundles of one wave can both pass Check while their branches conflict
textually. Before #674 nothing found that until the fold, which raised on the second of the
pair, and the flow stopped: every later wave was dropped, bundles unrelated to the pair
included, and the message named only the branch that failed. These cases drive
``flow._drive_and_act`` through real folds — the ``StackFoldGit`` fixture of
tests/test_integrate_stack_bases.py (a bare ``origin`` and a primary checkout), loaded by
path and used as a helper object, exactly as tests/test_flow_resume_stack_prereqs.py does.
The build and sign-off leaves are stubbed (the build writes a ``patch.diff`` naming the
files it changes), ``flow._publish_bundle`` cuts and pushes the PR branch from the recorded
line tip, and ``gh`` is answered by a stub in front of ``subprocess.run``. The publisher is
NOT a stub (a stub turns every fold into a dry-run).

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_same_wave_conflict
"""

from __future__ import annotations

import dataclasses
import difflib
import importlib.util
import io
import json
import subprocess
import unittest
from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import flow, integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-14"
NINE = "".join(f"line {i}\n" for i in range(1, 10))   # a nine-line file on the base


def _edit(text: str, **lines: str) -> str:
    """``text`` with line ``n`` (``l<n>=...``) replaced."""
    out = text.splitlines(keepends=True)
    for key, new in lines.items():
        out[int(key.removeprefix("l")) - 1] = new + "\n"
    return "".join(out)


class SameWaveConflict(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.base: dict[str, str] = {}               # files committed on the base
        self.changes: dict[str, dict[str, str]] = {}  # id → the files its build changes
        self.cut_from: dict[str, str] = {}           # id → where publish cuts it (override)
        self.briefs: dict[str, str] = {}             # id → its brief as planned
        self.after_publish: dict[str, Callable[[], None]] = {}   # id → run once it published
        self.merged_prs: dict[str, str] = {}         # PR url → the head it merged with
        self.gh_calls: list[list[str]] = []
        # bundle → (stack base, recorded tip, origin's line tip) when its wave was driven
        self.seen: dict[str, tuple[str, str, str | None]] = {}
        self.err_at: dict[str, str] = {}             # bundle → stderr so far when it was built
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _on_base(self, files: dict[str, str]) -> None:
        """Commit ``files`` on origin's ``main`` (the base every bundle targets)."""
        for name, text in files.items():
            (self.fx.primary / name).write_text(text, encoding="utf-8")
        sb._git(self.fx.primary, "add", "--all")
        sb._git(self.fx.primary, "commit", "-q", "-m", "base files")
        sb._git(self.fx.primary, "push", "-q", "origin", "main")
        self.base.update(files)

    def _plan(self, iid: str, files: dict[str, str], depends_on: str = "", *,
              conflicts_with: str = "") -> Path:
        d = self.cfg.bundle(iid)
        sb._brief(d, extra=(f"- **Depends on:** {depends_on}\n" if depends_on else "")
                  + (f"- **Conflicts with:** {conflicts_with}\n" if conflicts_with else ""))
        self.changes[iid] = files
        self.briefs[iid] = (d / "brief.md").read_text(encoding="utf-8")
        return d

    def _patch(self, files: dict[str, str]) -> str:
        """A unified diff of ``files`` against the base (new files included)."""
        out = []
        for name, text in files.items():
            old = self.base.get(name)
            out.append(f"diff --git a/{name} b/{name}\n"
                       + ("new file mode 100644\n" if old is None else "")
                       + "".join(difflib.unified_diff(
                           (old or "").splitlines(keepends=True),
                           text.splitlines(keepends=True),
                           "/dev/null" if old is None else f"a/{name}", f"b/{name}")))
        return "".join(out)

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """Any ``gh`` call is recorded and answered: a PR of ``merged_prs`` MERGED, with
        the head it merged with, any other OPEN; everything else runs for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        self.gh_calls.append(list(args))
        head = next((h for url, h in self.merged_prs.items() if url in args), None)
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"state": "MERGED", "headRefOid": head} if head else
            {"state": "OPEN", "headRefOid": ""}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle, recording what it was built on."""
        for d in wave:
            line = self.fx._tip(self.line) if self.fx._has(self.line) else None
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d),
                                 line)
            self.err_at[d.name] = self._err.getvalue()
            (d / "patch.diff").write_text(
                self._patch(self.changes[d.name.removeprefix("issue_")]), encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded line tip (else the line,
        else the base), push it, record it. The run's brief is kept as it was. Then what
        ``after_publish`` names for it happens (the world moving before the fold)."""
        iid = d.name.removeprefix("issue_")
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = self.cut_from.get(iid) or tip or (f"origin/{line}" if line else "origin/main")
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.fx._publish(iid, self.changes[iid], cut_from=cut)
        (d / "brief.md").write_text(brief, encoding="utf-8")
        self.after_publish.pop(iid, lambda: None)()
        return True

    def _run(self, ids: list[str]) -> None:
        """One stack-mode run asked for ``ids``, driving all of them."""
        self.line = integrate.integration_branch(self.cfg, "main", ids)
        drive = [self.cfg.bundle(i) for i in ids]
        self._err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(self._err))
            self.results = flow._drive_and_act(self.cfg, drive, do_publish=True,
                                               do_act=False, by="T", today=TODAY,
                                               batch=ids)
        self.err = self._err.getvalue()
        self.assertNotIn("Traceback", self.err)

    # -- helpers ---------------------------------------------------------------------------

    def _head(self, iid: str) -> str:
        return self.fx._tip(f"fix/{iid}")

    def _built_on_line(self, iid: str) -> str:
        """``iid`` was built on the run's line and is COMPLETE; returns the line commit it
        was built on (its recorded tip, which is origin's line tip at the time)."""
        self.assertIn(f"issue_{iid}", self.seen, self.err)
        base, tip, line_tip = self.seen[f"issue_{iid}"]
        self.assertEqual(base, self.line, self.err)
        self.assertIsNotNone(line_tip, self.err)
        self.assertEqual(tip, line_tip, self.err)
        self.assertEqual(self.results[iid], state.COMPLETE, self.err)
        return tip

    def _holds(self, iid: str) -> list[str]:
        """The stderr lines saying ``issue_<iid>`` was held out of the fold."""
        return [x for x in self.err.splitlines()
                if f"issue_{iid} held out of the integration fold" in x]

    def _briefs_unchanged(self, ids: list[str]) -> None:
        """No brief of the batch was edited by the run (a proposed `Conflicts with` stays a
        proposal)."""
        for iid in ids:
            text = (self.cfg.bundle(iid) / "brief.md").read_text(encoding="utf-8")
            self.assertEqual(text, self.briefs[iid], iid)

    # -- (1) + (2): held, not stopped; named by pair and file, before the next wave ---------

    def test_a_same_wave_conflict_holds_the_later_bundle_and_the_run_goes_on(self):
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"shared.txt": "ten\n"})
        self._plan("11", {"shared.txt": "eleven\n"})        # same line, other text
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "12")
        self._plan("30", {"thirty.txt": "thirty\n"}, "21")
        ids = ["10", "11", "12", "20", "21", "30"]
        self._run(ids)

        # (1) Held, not stopped.
        self.assertNotIn("did not integrate", self.err)
        self.assertNotIn("STOPPING", self.err)
        on21 = self._built_on_line("21")
        for iid in ("10", "12"):
            self.assertTrue(self.fx._ancestor(self._head(iid), on21), iid)
        self.assertFalse(self.fx._ancestor(self._head("11"), on21))
        self.assertNotIn("issue_20", self.seen, self.err)        # its prerequisite is held
        self.assertEqual(self.results["20"], state.PLANNED)
        # The wave-1 fold leaves 11 out too: the hold lasts the whole run.
        on30 = self._built_on_line("30")
        for iid in ("10", "12", "21"):
            self.assertTrue(self.fx._ancestor(self._head(iid), on30), iid)
        self.assertFalse(self.fx._ancestor(self._head("11"), on30))
        self.assertFalse(self.fx._ancestor(self._head("11"), self.fx._tip(self.line)))

        # (2) Named by pair and file, before the next wave builds.
        held = self._holds("11")
        self.assertEqual(len(held), 1, self.err)
        self.assertIn("conflicts with issue_10's", held[0])
        self.assertIn("shared.txt", held[0])
        self.assertIn(held[0], self.err_at["issue_21"])          # printed before wave 1
        # Again at the end of the run, after the last wave, with the step to take.
        tail = self.err[len(self.err_at["issue_30"]):].splitlines()
        end = [x for x in tail if "issue_11" in x and "issue_10" in x and x not in held]
        self.assertEqual(len(end), 1, self.err)
        self.assertIn("shared.txt", end[0])
        self.assertIn("`Conflicts with: 10`", end[0])
        self.assertIn("issue_11's brief", end[0])
        self.assertIn("rebuild issue_11 on a base that holds issue_10", end[0])
        self.assertEqual(self._holds("10"), [])
        self._briefs_unchanged(ids)                              # a proposal, never written
        self.assertEqual(self.gh_calls, [])

    # -- (3): a shared file that merges cleanly changes nothing ------------------------------

    def test_a_shared_file_that_merges_cleanly_holds_nothing(self):
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"shared.txt": _edit(NINE, l8="eleven")})   # apart: merges cleanly
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "12")
        self._plan("30", {"thirty.txt": "thirty\n"}, "21")
        self._run(["10", "11", "12", "20", "21", "30"])

        self.assertNotIn("did not integrate", self.err)
        self.assertNotIn("held", self.err)
        for iid in ("20", "21"):
            on = self._built_on_line(iid)
            for prereq in ("10", "11", "12"):
                self.assertTrue(self.fx._ancestor(self._head(prereq), on), (iid, prereq))
        self._built_on_line("30")
        # Nothing new on stderr about the pair beyond the overlap audit's warning.
        about = [x for x in self.err.splitlines() if "issue_11" in x]
        self.assertEqual(len(about), 1, self.err)
        self.assertIn("issue_10 and issue_11 both touch shared.txt", about[0])

    # -- (4): a three-way chain holds only the bundle that really conflicts -----------------

    def test_of_a_three_way_chain_only_the_bundle_that_conflicts_is_held(self):
        self._on_base({"chain.txt": NINE})
        self._plan("40", {"chain.txt": _edit(NINE, l2="forty")})
        self._plan("41", {"chain.txt": _edit(NINE, l2="forty-one", l8="forty-one")})
        self._plan("42", {"chain.txt": _edit(NINE, l8="forty-two")})
        self._plan("50", {"fifty.txt": "fifty\n"}, "42")
        ids = ["40", "41", "42", "50"]
        self._run(ids)

        self.assertNotIn("did not integrate", self.err)
        on50 = self._built_on_line("50")
        for iid in ("40", "42"):
            self.assertTrue(self.fx._ancestor(self._head(iid), on50), iid)
        self.assertFalse(self.fx._ancestor(self._head("41"), on50))
        held = self._holds("41")
        self.assertEqual(len(held), 1, self.err)
        self.assertIn("conflicts with issue_40's", held[0])
        self.assertIn("chain.txt", held[0])
        self.assertNotIn("issue_42", held[0])                    # judged against 40 only
        self.assertEqual(self._holds("40") + self._holds("42"), [])
        self.assertIn("`Conflicts with: 40`", self.err)
        self._briefs_unchanged(ids)

    # -- a `Conflicts with` partner of a held bundle is held too ----------------------------

    def test_a_conflicts_with_partner_of_a_held_bundle_is_held_too(self):
        # 25 declares `Conflicts with: 11` and 11 declares it with 26 (either side counts),
        # so the scheduler puts both in the wave after 11, to build on 11's work. The fold
        # holds 11 for its clash with 10, so no line of this run holds that work: neither
        # partner is built on a line without it, as for a bundle that pushed no branch
        # (#629). 25 changes the line 11 changed, so building it there would clash with
        # 11's rebuild. 21, unrelated, is built.
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"shared.txt": _edit(NINE, l2="ten")})
        self._plan("11", {"shared.txt": _edit(NINE, l2="eleven", l8="eleven")},
                   conflicts_with="26")
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "12")
        self._plan("25", {"shared.txt": _edit(NINE, l8="twenty-five")}, conflicts_with="11")
        self._plan("26", {"twenty-six.txt": "twenty-six\n"})
        ids = ["10", "11", "12", "21", "25", "26"]
        self._run(ids)

        self.assertNotIn("did not integrate", self.err)
        on21 = self._built_on_line("21")
        self.assertFalse(self.fx._ancestor(self._head("11"), on21))
        for iid in ("25", "26"):
            self.assertNotIn(f"issue_{iid}", self.seen, self.err)      # never built
            self.assertEqual(self.results[iid], state.PLANNED, self.err)
        held = self._holds("11")
        self.assertEqual(len(held), 1, self.err)
        self.assertIn("`Conflicts with` partners are held", held[0])
        for iid in ("25", "26"):
            why = [x for x in self.err.splitlines() if x.startswith(f"flow: issue_{iid} held")]
            self.assertEqual(len(why), 1, self.err)
            self.assertIn("`Conflicts with` issue_11", why[0])
            self.assertIn("conflicts with issue_10's", why[0])        # why 11 is held
            self.assertIn("rebuilt on a base that holds issue_10", why[0])
            self.assertNotIn("pushed no branch", why[0])              # 11 did push one
            self.assertIn(why[0], self.err_at["issue_21"])           # before wave 1 builds
        self._briefs_unchanged(ids)

    # -- the trial merges leave the integration tree on the line, clean --------------------

    def test_a_hold_leaves_the_integration_tree_clean_on_the_line(self):
        # The fold the flow runs, called directly: Q conflicts with P, merged before it in
        # the same wave, so Q is held; R still merges after it. The tree the next fold and
        # the re-gate use is on the line's branch, at the pushed tip, with nothing left over.
        self._on_base({"shared.txt": "x\n"})
        p = self.fx._publish("P", {"shared.txt": "p\n"})
        q = self.fx._publish("Q", {"shared.txt": "q\n"})
        r = self.fx._publish("R", {"r.txt": "r\n"})
        held: dict[str, tuple[list[str], list[str]]] = {}
        folded = integrate.fold(self.cfg, [p, q, r], folded_this_run={}, batch=["X"],
                                held_out=held, wave=[p.name, q.name, r.name])
        branch, wt = folded[sb.TARGET]
        self.assertEqual(held, {"issue_Q": (["issue_P"], ["shared.txt"])})
        self.assertEqual(sb._git(wt, "symbolic-ref", "--short", "HEAD").strip(), branch)
        self.assertEqual(sb._git(wt, "rev-parse", "HEAD").strip(), self.fx._tip(branch))
        self.assertEqual(sb._git(wt, "status", "--porcelain", "--untracked-files=all"), "")
        for iid in ("P", "R"):
            self.assertTrue(self.fx._ancestor(self._head(iid), branch), iid)
        self.assertFalse(self.fx._ancestor(self._head("Q"), branch))

    # -- a conflict with earlier-wave work still stops the fold, as before -----------------

    def test_a_conflict_with_earlier_wave_work_still_stops_the_run(self):
        # 21 is cut from the plain base, not the line, so it conflicts with 10's change
        # already on the line — not with 20, the same-wave bundle merged before it.
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"shared.txt": "ten\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "10")
        self._plan("21", {"shared.txt": "twenty-one\n"}, "10")
        self._plan("30", {"thirty.txt": "thirty\n"}, "21")
        self.cut_from["21"] = "origin/main"
        self._run(["10", "20", "21", "30"])

        self.assertIn("wave 1 did not integrate", self.err)
        self.assertIn("issue_21's branch", self.err)
        self.assertIn("STOPPING", self.err)
        self.assertEqual(self._holds("21"), [])
        self.assertNotIn("issue_30", self.seen, self.err)

    # -- a base the fold takes in mid-wave is never blamed on a bundle of the wave ----------

    def _a_pr_of_the_wave_merges_before_the_fold(self, line_of_22: str) -> list[str]:
        """Wave 0 is ``10``; wave 1 is ``20`` (line 2 of ``shared.txt``), ``21`` (a file of
        its own) and ``22`` (``line_of_22``), each depending on ``10``; ``30`` depends on
        ``20``. Right after the wave publishes, 21's PR merges into main with a merge commit
        and its branch is deleted, and someone else changes line 8 of ``shared.txt`` on
        main. So the wave-1 fold merges 20, then takes main in for 21 (its branch is gone,
        its PR merged, #593), which puts that line-8 change on the line, then tries 22.
        Runs the batch; returns its ids."""
        self._on_base({"shared.txt": NINE})
        self._plan("10", {"ten.txt": "ten\n"})
        self._plan("20", {"shared.txt": _edit(NINE, l2="twenty")}, "10")
        self._plan("21", {"twenty-one.txt": "twenty-one\n"}, "10")
        self._plan("22", {"shared.txt": _edit(NINE, **{line_of_22: "twenty-two"})}, "10")
        self._plan("30", {"thirty.txt": "thirty\n"}, "20")

        def merge_21_and_move_main() -> None:       # 22 is the wave's last to publish
            self.merged_prs["https://example.test/pr/21"] = self._head("21")
            self.fx._merge_pr("21")
            self.fx._delete("fix/21")
            self.fx._push_from_human({"shared.txt": _edit(NINE, l8="someone else's")},
                                     to="main")

        self.after_publish["22"] = merge_21_and_move_main
        ids = ["10", "20", "21", "22", "30"]
        self._run(ids)
        # The fold did take main in for 21, after 20 and before 22.
        self.assertIn("issue_21's branch origin/fix/21 is gone and its PR merged", self.err)
        return ids

    def test_a_conflict_only_with_a_base_taken_in_mid_wave_still_stops_the_run(self):
        # 22 changes line 8, as main now does. It merges cleanly onto the line the wave
        # found and onto that line plus 20 (line 2): its conflict is with the base the
        # fold took in for 21, not with 20. Holding it would blame 20 and propose a wrong
        # `Conflicts with: 20`; the fold stops on it, as before #674.
        self._a_pr_of_the_wave_merges_before_the_fold("l8")

        self.assertIn("wave 1 did not integrate (issue_22's branch origin/fix/22 does not "
                      "merge cleanly", self.err)
        self.assertIn("STOPPING", self.err)
        self.assertEqual(self._holds("22"), [])
        self.assertNotIn("issue_22 was held", self.err)
        self.assertNotIn("Conflicts with: 20", self.err)
        self.assertNotIn("issue_30", self.seen, self.err)

    def test_a_base_taken_in_mid_wave_does_not_stop_a_same_wave_hold(self):
        # 22 changes line 2, as 20 does, and not line 8: its conflict is with 20, so it is
        # held and named with 20 and the file, though the line took main in before it.
        ids = self._a_pr_of_the_wave_merges_before_the_fold("l2")

        self.assertNotIn("did not integrate", self.err)
        held = self._holds("22")
        self.assertEqual(len(held), 1, self.err)
        self.assertIn("conflicts with issue_20's", held[0])
        self.assertIn("(conflicts in shared.txt)", held[0])
        self.assertNotIn("issue_21", held[0])
        on30 = self._built_on_line("30")
        self.assertTrue(self.fx._ancestor(self._head("20"), on30))
        self.assertFalse(self.fx._ancestor(self._head("22"), on30))
        self.assertIn("`Conflicts with: 20`", self.err)
        self._briefs_unchanged(ids)


if __name__ == "__main__":
    unittest.main()
