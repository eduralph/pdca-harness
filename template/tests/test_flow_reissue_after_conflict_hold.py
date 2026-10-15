"""A re-issue after a same-wave conflict hold names the partner (#694).

After a #674 hold the end of the run says: declare ``Conflicts with: 10`` in ``issue_11``'s
brief, then rebuild ``issue_11`` on a base that holds ``issue_10``. Re-issuing the batch
reaches the #646 pre-wave carry, whose fold skips ``issue_11`` for the same conflict. Before
#694 the carry called it "an undeclared cross-wave overlap" and told the operator to get
``issue_11``'s PR open (it already is), never naming ``issue_10``. These cases drive
``flow._drive_and_act`` twice through real folds, with the ``SameWaveConflict`` stubs of
tests/test_flow_same_wave_conflict.py (loaded by path, and through it the ``StackFoldGit``
fixture of tests/test_integrate_stack_bases.py) and one change: the ``gh`` stub reports each
PR OPEN with its head, the branch's tip on origin, as the carry suite's stub does
(tests/test_flow_resume_stack_prereqs.py). The run-2 line must give the run-1 step, name
every finished bundle the line carries whose own change the branch conflicts with
(whatever order the re-issue asks for them in), and name none when the clash is with the
base or with other work on the line. It is judged on the line the carry pushes: a bundle
the fold took back off that line (#672) is never named as if a line held it, and one built
on another is named only when its own change is shown to conflict — and is named then,
however the branch clashes with what it was built on (a file one side deleted included).

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_reissue_after_conflict_hold
"""

from __future__ import annotations

import difflib
import importlib.util
import io
import json
import re
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import flow, integrate, merged, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "same_wave_conflict_fixture", Path(__file__).with_name("test_flow_same_wave_conflict.py"))
swc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(swc)
sb = swc.sb                     # the StackFoldGit fixture module, loaded by path there

TODAY = "2026-10-15"
NINE = swc.NINE                 # a nine-line file
_edit = swc._edit
ORDER = ["10", "11", "12", "20", "21"]      # criterion (1)'s batch, as run 1 asks for it


class ReissueAfterConflictHold(unittest.TestCase):
    # The SameWaveConflict fixture and stubs, borrowed method by method: subclassing it
    # would collect and run its own cases again here.
    setUp = swc.SameWaveConflict.setUp
    _on_base = swc.SameWaveConflict._on_base
    _plan = swc.SameWaveConflict._plan
    _drive_wave = swc.SameWaveConflict._drive_wave
    _head = swc.SameWaveConflict._head

    # -- stubs -----------------------------------------------------------------------------

    def _patch(self, files: dict[str, str | None]) -> str:
        """SameWaveConflict's patch, plus a file mapped to None: the bundle deletes it."""
        gone = [n for n, t in files.items() if t is None]
        return swc.SameWaveConflict._patch(
            self, {n: t for n, t in files.items() if t is not None}) + "".join(
            f"diff --git a/{n} b/{n}\ndeleted file mode 100644\n" + "".join(difflib.unified_diff(
                self.base[n].splitlines(keepends=True), [], f"a/{n}", "/dev/null"))
            for n in gone)

    def _publish_bundle(self, cfg, d: Path, **kw) -> bool:
        """SameWaveConflict's publish stub, plus a file mapped to None: the commit deletes
        it. The branch is cut where that stub cuts it and recorded the same way."""
        iid = d.name.removeprefix("issue_")
        files = self.changes[iid]
        if None not in files.values():
            return swc.SameWaveConflict._publish_bundle(self, cfg, d, **kw)
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = self.cut_from.get(iid) or tip or (f"origin/{line}" if line else "origin/main")
        repo = self.fx.primary
        sb._git(repo, "fetch", "-q", "origin")
        sb._git(repo, "checkout", "-q", "-B", f"fix/{iid}", cut)
        for name, text in files.items():
            if text is None:
                sb._git(repo, "rm", "-q", "--", name)
            else:
                (repo / name).write_text(text, encoding="utf-8")
        sb._git(repo, "add", "--all")
        sb._git(repo, "commit", "-q", "-s", "-m", f"fix {iid}")
        sb._git(repo, "push", "-q", "origin", f"fix/{iid}")
        sb._git(repo, "checkout", "-q", "main")
        (d / "publish.json").write_text(json.dumps(
            {"mode": "new-pr" if cut == "origin/main" else "stacked-pr",
             "branch": f"fix/{iid}", "base": "main", "repo": "org/repo",
             "pr_url": f"https://example.test/pr/{iid}"}), encoding="utf-8")
        return True

    def _gh_stub(self, args, *a, **kw):
        """Any ``gh`` call is recorded and answered OPEN with the PR's head, its branch's tip
        on origin: the one change from SameWaveConflict's stub, whose empty head makes every
        finished id "not clean" before the carry's fold, so the fold is never reached."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        self.gh_calls.append(list(args))
        iid = str(args[3]).rsplit("/", 1)[-1]
        head = self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else ""
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"state": "OPEN", "headRefOid": head}), "")

    def _run(self, drive: list[str], batch: list[str]) -> None:
        """One stack-mode run asked for ``batch``, driving ``drive``: the shape ``flow_ids``
        gives a re-issue, whose finished ids it skips."""
        self.line = integrate.integration_branch(self.cfg, "main", batch)
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
            self.results = flow._drive_and_act(self.cfg, [self.cfg.bundle(i) for i in drive],
                                               do_publish=True, do_act=False, by="T",
                                               today=TODAY, batch=batch)
        self.err = self._err.getvalue()
        self.assertNotIn("Traceback", self.err)

    # -- helpers ---------------------------------------------------------------------------

    def _finished_open(self, iid: str) -> None:
        """``issue_<iid>`` is COMPLETE and the ``gh`` stub reports its PR OPEN with its
        branch's tip: what puts it in the carry (``flow._finished_named`` takes only COMPLETE
        ids) and keeps it clean before the fold."""
        d = self.cfg.bundle(iid)
        self.assertEqual(state.state(d), state.COMPLETE, self.err)
        url = json.loads((d / "publish.json").read_text(encoding="utf-8"))["pr_url"]
        with mock.patch.object(subprocess, "run", self._gh_stub):
            self.assertEqual(merged.pr_state(url), ("OPEN", self._head(iid)))

    def _not_carried(self, iid: str) -> str:
        """The ONE stderr line saying finished ``issue_<iid>`` is not carried."""
        lines = [x for x in self.err.splitlines()
                 if f"issue_{iid} (finished in an earlier run) is not carried" in x]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    def _held_for_10(self, ids: list[str], twenty_one: dict[str, str] | None = None) -> str:
        """Run 1 of criterion (1), asked for ``ids``: on a base where ``shared.txt`` is
        ``x``, 10 and 11 change its line two ways, 12 adds a file, 20 depends on 11 and 21
        (``twenty_one``: its files, a file of its own by default) on 12. The wave-0 fold
        holds 11 for its clash with 10 (#674), so 20 is never built; 10, 11, 12 and 21
        finish with their PRs open, 21 built on the line and on no line itself. Returns the
        step the end of the run gave for 11."""
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"shared.txt": "ten\n"})
        self._plan("11", {"shared.txt": "eleven\n"})        # same line, other text
        self._plan("12", {"twelve.txt": "twelve\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        self._plan("21", twenty_one or {"twenty-one.txt": "twenty-one\n"}, "12")
        self._run(ids, ids)
        held = [x for x in self.err.splitlines()
                if "issue_11 held out of the integration fold" in x]
        self.assertEqual(len(held), 1, self.err)
        self.assertIn("conflicts with issue_10's", held[0])
        self.assertEqual(self.results["20"], state.PLANNED, self.err)
        for iid in ("10", "11", "12", "21"):
            self._finished_open(iid)
        self.assertFalse(self.fx._ancestor(self._head("21"), self.fx._tip(self.line)))
        end = [x for x in self.err.splitlines()
               if x.startswith("flow: issue_11 was held out of this run's integration line")]
        self.assertEqual(len(end), 1, self.err)
        return end[0].split("Proposed, for you to confirm: ", 1)[1].removesuffix(".")

    def _reissue(self, batch: list[str]) -> None:
        """Run 2: the same batch (the line is the same, in whatever order it is asked for),
        driving 20 only; the finished ids are skipped and judged by the carry."""
        self.assertEqual(integrate.integration_branch(self.cfg, "main", batch),
                         integrate.integration_branch(self.cfg, "main", ORDER))
        self.seen.clear()
        self._run(["20"], batch)

    def _named_with_10(self, said: str, step: str) -> None:
        """11's carry line names 10 and the file, with run 1's step, and nothing else."""
        self.assertIn("its change conflicts with issue_10's (shared.txt), finished in this "
                      "batch", said)
        self.assertIn(step, said)
        self.assertIn("declare `Conflicts with: 10` in issue_11's brief, then rebuild issue_11 "
                      "on a base that holds issue_10", said)
        for wrong in ("get issue_11's PR open", "pdca publish 11", "cross-wave", "issue_12",
                      "issue_21", "account for"):
            self.assertNotIn(wrong, said)

    def _carried_but_11(self) -> None:
        """20 waits, unbuilt; 10, 12 and 21 are carried onto the line, and 11 is not."""
        self.assertNotIn("issue_20", self.seen, self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)
        carried = [x for x in self.err.splitlines() if x.startswith("flow: carried ")]
        self.assertEqual(len(carried), 1, self.err)
        for iid in ("10", "12", "21"):
            self.assertIn(f"issue_{iid}", carried[0])
            self.assertTrue(self.fx._ancestor(self._head(iid), self.fx._tip(self.line)), iid)
        self.assertNotIn("issue_11", carried[0])
        self.assertFalse(self.fx._ancestor(self._head("11"), self.fx._tip(self.line)))

    # -- (1) a #674 hold, re-issued: the partner is named, with the #674 step ---------------

    def test_a_reissue_after_a_conflict_hold_names_the_partner(self):
        step = self._held_for_10(ORDER)
        self.assertEqual(step, "declare `Conflicts with: 10` in issue_11's brief, then "
                               "rebuild issue_11 on a base that holds issue_10")
        self._reissue(ORDER)
        self._named_with_10(self._not_carried("11"), step)
        self._carried_but_11()

    def test_a_partner_already_on_the_line_is_named_when_asked_for_after(self):
        # The re-issue asks for 11 before 10: the carry's fold tries 11 first, on origin's
        # line, which holds 10 from run 1 though 10 is not merged by this fold yet.
        step = self._held_for_10(ORDER)
        self._reissue(["11", "10", "12", "20", "21"])
        self._named_with_10(self._not_carried("11"), step)
        self._carried_but_11()

    def test_a_partner_whose_branch_moved_on_since_is_named(self):
        # After run 1 a fixup lands on fix/10 (a fast-forward), so 10's tip is no longer on
        # the line, though its first commit is. Asked for after 11, it is still named.
        step = self._held_for_10(ORDER)
        before = self._head("10")
        self.fx._push_from_human({"ten-fixup.txt": "fixup\n"}, to="fix/10",
                                 off="origin/fix/10")
        self.assertTrue(self.fx._ancestor(before, self._head("10")))
        self.assertFalse(self.fx._ancestor(self._head("10"), self.fx._tip(self.line)))
        self._finished_open("10")
        self._reissue(["11", "10", "12", "20", "21"])
        self._named_with_10(self._not_carried("11"), step)
        self._carried_but_11()

    def test_one_the_fold_merges_after_it_is_named_too(self):
        # 21's own change sets shared.txt's line as well (on top of 10's). It is on no line
        # after run 1, and the re-issue's fold merges it after it skips 11: 21 is on the line
        # the fold pushes, so it is named beside 10, with its file.
        self._held_for_10(ORDER, {"shared.txt": "twenty-one\n"})
        self._reissue(ORDER)
        said = self._not_carried("11")
        self.assertIn("its change conflicts with issue_10's (shared.txt), issue_21's "
                      "(shared.txt), finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 21` in issue_11's brief, then rebuild "
                      "issue_11 on a base that holds issue_10, issue_21", said)
        self.assertNotIn("issue_12", said)
        self._carried_but_11()

    def test_a_finished_id_held_as_well_is_never_named(self):
        # 10, 11 and 13 all change shared.txt's line. Run 1 holds 11 and 13 for their clash
        # with 10, and the re-issue's fold skips both again. Each is named with 10 only: the
        # other is on no line (it is held too), so no base holds it to rebuild on.
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"shared.txt": "ten\n"})
        self._plan("11", {"shared.txt": "eleven\n"})
        self._plan("13", {"shared.txt": "thirteen\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        ids = ["10", "11", "13", "20"]
        self._run(ids, ids)
        for iid in ("11", "13"):
            self.assertEqual(len([x for x in self.err.splitlines()
                                  if f"issue_{iid} held out of the integration fold" in x]),
                             1, self.err)
        for iid in ("10", "11", "13"):
            self._finished_open(iid)
        self.seen.clear()
        self._run(["20"], ids)
        for iid, other in (("11", "13"), ("13", "11")):
            said = self._not_carried(iid)
            self.assertIn("its change conflicts with issue_10's (shared.txt), finished in this "
                          "batch", said)
            self.assertIn(f"declare `Conflicts with: 10` in issue_{iid}'s brief", said)
            self.assertNotIn(f"issue_{other}", said)
        self.assertNotIn("issue_20", self.seen, self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)

    def test_one_built_on_the_partner_is_not_named_for_the_partners_change(self):
        # 21 is cut from the line that holds 10 and 12, so its branch holds 10's change.
        # Asked for before 11, it is merged onto the line first; its own change (a file of
        # its own) does not conflict with 11's, so only 10 is named.
        step = self._held_for_10(ORDER)
        self._reissue(["10", "12", "21", "11", "20"])
        self._named_with_10(self._not_carried("11"), step)
        self._carried_but_11()

    # -- partners built on one another: each named for its own change ----------------------

    def _stacked_on_10(self, base: dict[str, str], files: dict[str, dict[str, str]], *,
                       cut_12_from: str = "") -> str:
        """10 changes the base; 12 (``Depends on: 10``) is built on the line holding 10 (or
        cut from ``cut_12_from``); 11 conflicts with 10, so run 1 holds it and never builds
        20 (``Depends on: 11``). Run 2 re-issues the batch driving 20, with 10 and 12 on the
        line before 11 is tried. ``files``: each id's files, written in full where it is
        cut. Returns 11's line."""
        self._on_base(base)
        self._plan("10", files["10"])
        self._plan("12", files["12"], "10")
        self._plan("11", files["11"])
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        if cut_12_from:
            self.cut_from["12"] = cut_12_from
        ids = ["10", "12", "11", "20"]
        self._run(ids, ids)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)
        self.assertEqual(self.seen["issue_12"][0], self.line, self.err)    # its stack base
        self.line_under_12 = self.seen["issue_12"][1]       # the line tip 12 was built on
        for iid in ("10", "11", "12"):
            self._finished_open(iid)
        self.assertTrue(self.fx._ancestor(self._head("10"), self._head("12")))
        self.seen.clear()
        self._run(["20"], ids)
        self.assertNotIn("issue_20", self.seen, self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)
        return self._not_carried("11")

    def test_a_partner_built_on_another_is_named_for_its_own_file(self):
        # 11 changes a.txt as 10 does and b.txt as 12 does: both are named, 12 for b.txt
        # only — its branch holds 10's a.txt change, which is 10's.
        said = self._stacked_on_10({"a.txt": "x\n", "b.txt": "x\n"}, {
            "10": {"a.txt": "ten\n"},
            "12": {"b.txt": "twelve\n"},
            "11": {"a.txt": "eleven\n", "b.txt": "eleven\n"}})
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_12's (b.txt), "
                      "finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 12` in issue_11's brief, then rebuild "
                      "issue_11 on a base that holds issue_10, issue_12", said)

    def test_a_partner_cut_from_anothers_branch_is_named_for_its_own_file(self):
        # As above, but 12's branch is cut from fix/10 itself, not from the line: 10's
        # commit is on 12's branch, and is still 10's.
        said = self._stacked_on_10({"a.txt": "x\n", "b.txt": "x\n"}, {
            "10": {"a.txt": "ten\n"},
            "12": {"a.txt": "ten\n", "b.txt": "twelve\n"},
            "11": {"a.txt": "eleven\n", "b.txt": "eleven\n"}}, cut_12_from="origin/fix/10")
        self.assertFalse(self.fx._ancestor(self.line_under_12, self._head("12")))
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_12's (b.txt), "
                      "finished in this batch", said)

    def test_a_partner_built_on_another_is_named_for_its_own_lines_of_a_shared_file(self):
        # All three change one file: 10 line 2, 12 line 8 (on top of 10's line 2), 11 lines 2
        # and 8. 12's own change is line 8, which 11 changes too: 12 is named.
        said = self._stacked_on_10({"a.txt": NINE}, {
            "10": {"a.txt": _edit(NINE, l2="ten")},
            "12": {"a.txt": _edit(NINE, l2="ten", l8="twelve")},
            "11": {"a.txt": _edit(NINE, l2="eleven", l8="eleven")}})
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_12's (a.txt), "
                      "finished in this batch", said)
        self.assertIn("`Conflicts with: 10, 12`", said)

    def test_a_partner_built_on_another_is_not_named_when_its_own_lines_merge(self):
        # As above, but 12's own change is line 5, which merges with 11's lines 2 and 8:
        # only 10 is named, though 12's branch, holding 10's line 2, conflicts with 11's.
        said = self._stacked_on_10({"a.txt": NINE}, {
            "10": {"a.txt": _edit(NINE, l2="ten")},
            "12": {"a.txt": _edit(NINE, l2="ten", l5="twelve")},
            "11": {"a.txt": _edit(NINE, l2="eleven", l8="eleven")}})
        self.assertIn("its change conflicts with issue_10's (a.txt), finished in this batch",
                      said)
        self.assertIn("`Conflicts with: 10` in issue_11's brief", said)
        self.assertNotIn("issue_12", said)

    def test_a_partner_built_on_a_deletion_is_not_named_for_it(self):
        # 10 deletes shared.txt, which 11 changes; 12, built on the line holding 10, only
        # adds a file. 12's branch holds 10's deletion, so 11 does not merge onto base + 12,
        # but none of 12's own change touches shared.txt: only 10 is named. (11 is tried on
        # what 12 was built on with its side winning, the deleted file included, and 12's
        # own change then merges.)
        said = self._stacked_on_10({"shared.txt": "x\n"}, {
            "10": {"shared.txt": None},
            "12": {"unrelated.txt": "twelve\n"},
            "11": {"shared.txt": "eleven\n"}})
        self.assertNotIn("shared.txt", sb._git(self.fx.origin, "ls-tree", "--name-only",
                                               "refs/heads/fix/12").split())
        self.assertIn("its change conflicts with issue_10's (shared.txt), finished in this "
                      "batch", said)
        self.assertIn("declare `Conflicts with: 10` in issue_11's brief, then rebuild issue_11 "
                      "on a base that holds issue_10", said)
        self.assertNotIn("issue_12", said)

    def test_a_partner_built_on_a_deletion_is_named_for_its_own_file(self):
        # 10 deletes a.txt; 12, built on the line holding 10, changes b.txt; 11 changes both.
        # Trying 11 on what 12 was built on leaves a.txt conflicting in a way no hunk can
        # settle (one side deleted it), so 11's version of it is taken, and the trial gets
        # to 12's own change: that conflicts over b.txt. Each is named for its own file.
        said = self._stacked_on_10({"a.txt": "x\n", "b.txt": "x\n"}, {
            "10": {"a.txt": None},
            "12": {"b.txt": "twelve\n"},
            "11": {"a.txt": "eleven\n", "b.txt": "eleven\n"}})
        self.assertNotIn("a.txt", sb._git(self.fx.origin, "ls-tree", "--name-only",
                                          "refs/heads/fix/12").split())
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_12's (b.txt), "
                      "finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 12` in issue_11's brief, then rebuild "
                      "issue_11 on a base that holds issue_10, issue_12", said)

    def test_a_partner_two_deep_on_a_deletion_is_named_for_its_own_file(self):
        # 10 deletes a.txt; 12, built on the line holding 10, adds a file; 13, built on the
        # line holding 12, changes b.txt; 11 changes a.txt and b.txt. 10 is named for a.txt
        # and 13 for b.txt, its own change; 12, whose own change 11 does not touch, is not.
        self._on_base({"a.txt": "x\n", "b.txt": "x\n"})
        self._plan("10", {"a.txt": None})
        self._plan("12", {"twelve.txt": "twelve\n"}, "10")
        self._plan("13", {"b.txt": "thirteen\n"}, "12")
        self._plan("11", {"a.txt": "eleven\n", "b.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        ids = ["10", "12", "13", "11", "20"]
        self._run(ids, ids)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)
        for iid in ("10", "11", "12", "13"):
            self._finished_open(iid)
        self.assertTrue(self.fx._ancestor(self._head("10"), self._head("12")))
        self.assertTrue(self.fx._ancestor(self._head("12"), self._head("13")))
        self.seen.clear()
        self._run(["20"], ids)
        said = self._not_carried("11")
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_13's (b.txt), "
                      "finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 13` in issue_11's brief, then rebuild "
                      "issue_11 on a base that holds issue_10, issue_13", said)
        self.assertNotIn("issue_12", said)
        self.assertNotIn("issue_20", self.seen, self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)

    def test_a_partner_built_on_another_is_named_when_it_changes_a_file_the_branch_deletes(
            self):
        # 11 deletes a.txt, which 10 changes on line 2 and 12, built on 10, on line 8. Tried
        # on what 12 was built on, 11's side wins the file: it is gone, so 12's own change
        # to it conflicts. Both are named for a.txt.
        said = self._stacked_on_10({"a.txt": NINE}, {
            "10": {"a.txt": _edit(NINE, l2="ten")},
            "12": {"a.txt": _edit(NINE, l2="ten", l8="twelve")},
            "11": {"a.txt": None}})
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_12's (a.txt), "
                      "finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 12` in issue_11's brief", said)

    def test_a_partner_that_puts_back_a_file_its_base_deleted_is_named(self):
        # 10 deletes shared.txt, which 11 changes; 12, built on 10, writes it again its own
        # way. Tried on what 12 was built on, 11's version of the file is kept, and 12's
        # own copy of it conflicts with that: both are named for shared.txt.
        said = self._stacked_on_10({"shared.txt": "x\n"}, {
            "10": {"shared.txt": None},
            "12": {"shared.txt": "twelve\n"},
            "11": {"shared.txt": "eleven\n"}})
        self.assertNotIn("shared.txt", sb._git(self.fx.origin, "ls-tree", "--name-only",
                                               self.line_under_12).split())
        self.assertIn("its change conflicts with issue_10's (shared.txt), issue_12's "
                      "(shared.txt), finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 12` in issue_11's brief", said)

    def test_a_partner_built_on_another_is_named_when_its_own_change_deletes_the_file(self):
        # The other way round: 12's own change deletes shared.txt, which 11 changes (and 11
        # clashes with 10 over a.txt). The trial gets to 12's own change and it conflicts:
        # 12 is named, for shared.txt only.
        said = self._stacked_on_10({"a.txt": "x\n", "shared.txt": "x\n"}, {
            "10": {"a.txt": "ten\n"},
            "12": {"shared.txt": None},
            "11": {"a.txt": "eleven\n", "shared.txt": "eleven\n"}})
        self.assertIn("its change conflicts with issue_10's (a.txt), issue_12's (shared.txt), "
                      "finished in this batch", said)
        self.assertIn("declare `Conflicts with: 10, 12` in issue_11's brief, then rebuild "
                      "issue_11 on a base that holds issue_10, issue_12", said)

    # -- a partner the fold then takes off the line: named, as not carried ------------------

    def test_a_partner_the_fold_takes_off_the_line_is_named_as_not_carried(self):
        # Finished in an earlier run: 10 and 11 change a.txt two ways; 12 is cut from fix/11,
        # sets a.txt as 10 does and changes b.txt; 13 changes b.txt as well. No line is on
        # origin, so the carry starts one: 10 merges, 11 is skipped, 12 merges, 13 is
        # skipped for its clash with 12 — then 12 comes off the line, as it holds 11's work
        # (#672), and only 10 is carried. 13 would merge onto that line, so it is named
        # with 12, said not to be carried, and given the step for once a line carries it —
        # never a rebuild on a base that holds 12 as if one did.
        self.err = ""
        self._on_base({"a.txt": "x\n", "b.txt": "x\n"})
        for iid, files, cut in (("10", {"a.txt": "ten\n"}, "origin/main"),
                                ("11", {"a.txt": "eleven\n"}, "origin/main"),
                                ("12", {"a.txt": "ten\n", "b.txt": "twelve\n"}, "origin/fix/11"),
                                ("13", {"b.txt": "thirteen\n"}, "origin/main")):
            sb._accept(self.fx._publish(iid, files, cut_from=cut))
            self._finished_open(iid)
        self.assertTrue(self.fx._ancestor(self._head("11"), self._head("12")))
        self._plan("20", {"twenty.txt": "twenty\n"}, "13")
        batch = ["10", "11", "12", "13", "20"]
        self.assertFalse(self.fx._has(integrate.integration_branch(self.cfg, "main", batch)))
        self._run(["20"], batch)

        carried = [x for x in self.err.splitlines() if x.startswith("flow: carried ")]
        self.assertEqual(len(carried), 1, self.err)
        self.assertIn("carried issue_10 (finished", carried[0])
        for iid in ("11", "12", "13"):
            self.assertNotIn(f"issue_{iid}", carried[0])
        self.assertIn("its change conflicts with issue_10's (a.txt)", self._not_carried("11"))
        self.assertIn("issue_12 was built on issue_11, which is not carried",
                      self._not_carried("12"))
        said = self._not_carried("13")
        self.assertIn("its change conflicts with issue_12's (b.txt), finished in this batch, "
                      "which this run does not carry (see its line)", said)
        self.assertIn("once a line carries issue_12: declare `Conflicts with: 12` in issue_13's "
                      "brief, then rebuild issue_13 on a base that holds issue_12, then "
                      "re-issue", said)
        for wrong in ("issue_10", "issue_11", "cross-wave", "get issue_13's PR open",
                      "pdca publish 13"):
            self.assertNotIn(wrong, said)
        self.assertIn("holding issue_20 this run", said)
        self.assertNotIn("issue_20", self.seen, self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)

    # -- (2) a conflict with the base: no partner is blamed --------------------------------

    def test_a_conflict_with_the_base_names_no_partner(self):
        self._on_base({"shared.txt": "x\n"})
        self._plan("11", {"shared.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        batch = ["11", "20"]
        self._run(["11"], batch)
        self._finished_open("11")

        # The base changes the same line, and no line is left on origin, so run 2's carry
        # starts a fresh line from the base.
        self.fx._push_from_human({"shared.txt": "y\n"}, to="main")
        lines = sb._git(self.fx.origin, "for-each-ref", "--format=%(refname:short)",
                        "refs/heads/pdca-integration/").split()
        if lines:
            self.fx._delete(*lines)
        self.assertFalse(self.fx._has(self.line))

        self._run(["20"], batch)
        said = self._not_carried("11")
        self.assertIn(f"onto {self.line}", said)
        self.assertIn("its change conflicts with origin/main itself (shared.txt)", said)
        self.assertIn("update issue_11's branch from origin/main, resolving the conflict", said)
        self.assertNotIn("Conflicts with", said)
        for wrong in ("get issue_11's PR open", "pdca publish 11", "cross-wave"):
            self.assertNotIn(wrong, said)
        # No bundle but issue_11 is named, apart from the dependent it holds.
        self.assertIn("holding issue_20 this run", said)
        named = set(re.findall(r"issue_\d+", said.replace("holding issue_20 this run", "")))
        self.assertEqual(named, {"issue_11"}, said)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)

    # -- work on the line that no finished bundle brings: no partner is blamed --------------

    def test_a_conflict_with_other_work_on_the_line_names_no_partner(self):
        # Run 1 finishes 10 (a file of its own) and 11 in one wave, so it folds nothing.
        # Then someone puts 10 on the run's line by hand, with a merge commit that also
        # changes shared.txt: the carry accounts for every commit on that line, but no
        # finished bundle brings that change, and 11's change conflicts with it.
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"ten.txt": "ten\n"})
        self._plan("11", {"shared.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        batch = ["10", "11", "20"]
        self._run(["10", "11"], batch)
        for iid in ("10", "11"):
            self._finished_open(iid)
        self._by_hand("10", {"shared.txt": "by hand\n"})

        self._run(["20"], batch)
        said = self._not_carried("11")
        self.assertIn("its change conflicts with work already on the line (shared.txt)", said)
        self.assertIn("rebuild issue_11 on a base that holds the work it conflicts with", said)
        for wrong in ("issue_10", "Conflicts with", "cross-wave", "get issue_11's PR open"):
            self.assertNotIn(wrong, said)
        self.assertIn("flow: carried issue_10 (finished", self.err)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)

    def _by_hand(self, iid: str, files: dict[str, str]) -> None:
        """Someone puts ``issue_<iid>`` on the run's line by hand: a merge commit onto the
        base that also writes ``files``, pushed as the line."""
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", "by-hand", "origin/main")
        sb._git(human, "merge", "-q", "--no-ff", "--no-commit", f"origin/fix/{iid}")
        for name, text in files.items():
            (human / name).write_text(text, encoding="utf-8")
        sb._git(human, "commit", "-q", "-a", "-s", "-m", f"merge issue_{iid} by hand")
        sb._git(human, "push", "-q", "origin", f"HEAD:refs/heads/{self.line}")

    def _merges_after_a_later_merge(self, batch: list[str]) -> None:
        """As above, but 12, finished too, is cut from that hand-made line and puts
        shared.txt back as the base has it. The re-issue (driving 20) skips 11, then merges
        12, so 11 merges onto the line the fold pushes: no bundle is named, and the step is
        a plain re-issue — which, run, carries 11. With 30 and 31 in ``batch`` (asked for
        after 11), 30 adds ten.txt as 10 does, another way, and 31 is cut from fix/30 with
        fix/10 then merged in, keeping its side: the carry skips 30, and takes 31 back off
        the line after 11 is tried — which changes none of that."""
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"ten.txt": "ten\n"})
        self._plan("11", {"shared.txt": "eleven\n"})
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        self._run(["10", "11"], batch)
        self._by_hand("10", {"shared.txt": "by hand\n"})
        sb._accept(self.fx._publish("12", {"shared.txt": "x\n"},
                                    cut_from=f"origin/{self.line}"))
        finished = [i for i in batch if i != "20"]
        if "31" in batch:
            sb._accept(self.fx._publish("30", {"ten.txt": "thirty\n"}))
            sb._accept(self.fx._publish("31", {"thirty-one.txt": "thirty-one\n"},
                                        cut_from="origin/fix/30"))
            human = self.fx.human
            sb._git(human, "fetch", "-q", "origin")
            sb._git(human, "checkout", "-q", "-B", "fix/31", "origin/fix/31")
            sb._git(human, "merge", "-q", "-X", "ours", "--no-edit", "origin/fix/10")
            sb._git(human, "push", "-q", "origin", "fix/31")
        for iid in finished:
            self._finished_open(iid)

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
            self._run(["20"], batch)
        self.assertEqual(tried[:2], [("issue_10", True), ("issue_11", False)], tried)
        if "31" in batch:       # 31 merged after 11 failed, then came back off the line
            self.assertIn(("issue_31", True), tried[2:], tried)
            self.assertNotIn(("issue_31", False), tried, tried)
            self.assertIn("issue_31 was built on issue_30, which is not carried",
                          self._not_carried("31"))
        said = self._not_carried("11")
        self.assertIn("its branch origin/fix/11 did not merge cleanly onto the line as the fold "
                      "had it then (shared.txt), but merges onto the line it pushes", said)
        self.assertIn("re-issue: the carry tries it again on that line", said)
        for wrong in ("issue_10", "issue_12", "issue_30", "issue_31", "Conflicts with",
                      "cross-wave", "get issue_11's PR open", "those bundles"):
            self.assertNotIn(wrong, said)
        self.assertEqual(self.results["20"], state.PLANNED, self.err)

        self._run(["20"], batch)        # the re-issue that line asks for
        carried = [x for x in self.err.splitlines() if x.startswith("flow: carried ")]
        self.assertEqual(len(carried), 1, self.err)
        self.assertIn("issue_11", carried[0])
        self.assertNotIn("issue_11 (finished in an earlier run) is not carried", self.err)
        self.assertEqual(self.results["20"], state.COMPLETE, self.err)

    def test_one_a_later_merge_lets_onto_the_line_is_carried_by_a_plain_reissue(self):
        self._merges_after_a_later_merge(["10", "11", "12", "20"])

    def test_a_bundle_taken_back_off_after_it_was_tried_changes_nothing(self):
        # 31 is merged, then taken back off, only after 11 was tried: it was not on the line
        # 11 failed on, so it is not what 11's merge failed on.
        self._merges_after_a_later_merge(["10", "11", "30", "31", "12", "20"])

    # -- work a bundle taken back off brought, with no partner of its own: none named -------

    def test_work_a_bundle_taken_back_off_brought_names_no_partner(self):
        # Finished in an earlier run: 30 changes base.txt and d.txt; 31 is cut from fix/30
        # and adds a file of its own; 32 changes d.txt as well. Then the base changes
        # base.txt, and 31 is updated from it, keeping its own side. The carry starts a
        # line: 30 is skipped (it conflicts with the base), 31 merges, and 32 is skipped
        # for its clash with 30's d.txt, which 31 brought; then 31 comes off the line, as
        # it holds 30's work (#672). 32 merges onto the line the fold leaves, and 31's own
        # change is not what it conflicts with: no bundle is named, and the step is to
        # re-issue once the ones the fold took back off are carried, or out of the batch.
        self.err = ""
        self._on_base({"d.txt": "x\n"})
        for iid, files, cut in (("30", {"base.txt": "thirty\n", "d.txt": "thirty\n"},
                                 "origin/main"),
                                ("31", {"thirty-one.txt": "thirty-one\n"}, "origin/fix/30"),
                                ("32", {"d.txt": "thirty-two\n"}, "origin/main")):
            sb._accept(self.fx._publish(iid, files, cut_from=cut))
        self.fx._push_from_human({"base.txt": "moved\n"}, to="main")
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", "fix/31", "origin/fix/31")
        sb._git(human, "merge", "-q", "-X", "ours", "--no-edit", "origin/main")
        sb._git(human, "push", "-q", "origin", "fix/31")
        for iid in ("30", "31", "32"):
            self._finished_open(iid)
        self._plan("40", {"forty.txt": "forty\n"}, "32")
        batch = ["30", "31", "32", "40"]
        self._run(["40"], batch)

        self.assertIn("its change conflicts with origin/main itself (base.txt)",
                      self._not_carried("30"))
        self.assertIn("issue_31 was built on issue_30, which is not carried",
                      self._not_carried("31"))
        said = self._not_carried("32")
        self.assertIn("its branch origin/fix/32 did not merge cleanly onto the line as the fold "
                      "had it then (d.txt), but merges onto the line it pushes: its change "
                      "conflicts with work that bundles the fold then took back off the line "
                      "brought onto it (see their lines)", said)
        self.assertIn("re-issue once those bundles are carried, or out of the batch", said)
        for wrong in ("issue_30", "issue_31", "Conflicts with", "cross-wave", "PR open"):
            self.assertNotIn(wrong, said)
        self.assertNotIn("flow: carried", self.err)
        self.assertEqual(self.results["40"], state.PLANNED, self.err)


if __name__ == "__main__":
    unittest.main()
