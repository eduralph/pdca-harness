"""A stack-mode flow run that held a bundle out of its line exits 3, not 0 (#693).

A held bundle IS COMPLETE — built, signed off, published; only the fold left it out — so
the exit-code rule over the results map (``cli._results_rc``: 0 iff every bundle reached a
successful terminal) read such a run as clean, and the only signal was stderr. These cases
drive the WHOLE CLI route, ``cli._flow``, through real folds on the ``StackFoldGit`` fixture
of tests/test_integrate_stack_bases.py (a bare ``origin`` and a primary checkout), loaded by
path exactly as tests/test_flow_same_wave_conflict.py does, with that file's stubs: the
build and sign-off leaves stubbed (``flow._drive_wave`` writes a ``patch.diff`` and
accepts), ``flow._publish_bundle`` cutting and pushing the PR branch, and ``gh`` answered by
a stub in front of ``subprocess.run``. The publisher is NOT a stub (a stub turns every fold
into a dry-run, which holds nothing).

Both CLI shapes are covered, and all three routes ``cli._flow`` takes. The batch shape
(several ids: ``flow.flow_ids``, then ``cli._report_batch``) by the four cases of the
issue. ``--from-csv`` with no ids (``flow.flow_batch``, then ``cli._report_batch``) by one
hold, its Plan session stubbed to brief nothing new. The single-id shape (``flow.flow_ids``,
then ``cli._report_single``) by a run that names one id and still folds: one id is one
wave, with no fold after it, unless the run adopts more. ``pdca flow <parent>`` over a split
parent whose children are still PLANNED (the #473 recovery) is such a run — it drives the
children in their own waves and folds between them — so the same four bundles, as the
children of a parent split by the production ``split.accept``, give that shape a real hold:
with every child accepted, and with the last one waiting for sign-off, which that shape
counts as success.

The exit code is read ONLY from what ``cli._flow`` returns. Only modules are imported
(never a symbol the fix adds), so with the production change reverted these cases still
load and run — and fail on their exit-code assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_hold_exit_code
"""

from __future__ import annotations

import dataclasses
import difflib
import importlib.util
import io
import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, flow, integrate, leaves, publish, split, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

#: What the run prints on stderr, after the bundle's name, for each hold: the #674 line
#: (``flow._announce_conflict_holds``, printed at the fold for every bundle it holds) and the
#: #593 line (the fold step of ``flow._drive_and_act``, for an accepted bundle that pushed no
#: branch). The hold cases match each after ``issue_11 ``; the no-hold cases check that
#: neither appears. One constant for both, so a wording change fails the hold cases rather
#: than leave the no-hold checks passing on a phrase the run no longer prints.
_CONFLICT_HOLD = "held out of the integration fold"
_UNPUSHED_HOLD = "pushed no branch this run"


class FlowHoldExitCode(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.base: dict[str, str] = {}               # files committed on the base
        self.changes: dict[str, dict[str, str]] = {}  # id → the files its build changes
        self.no_push: set[str] = set()               # ids whose publish pushes no branch
        self.no_signoff: set[str] = set()            # ids left waiting for the human
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

    def _plan(self, iid: str, files: dict[str, str], depends_on: str = "") -> None:
        sb._brief(self.cfg.bundle(iid),
                  extra=f"- **Depends on:** {depends_on}\n" if depends_on else "")
        self.changes[iid] = files

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
        """Any ``gh`` call is answered OPEN; everything else runs for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"state": "OPEN", "headRefOid": ""}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle — except an id in ``no_signoff``, whose Check
        finishes and which then waits for the human's sign-off (AWAITING_SIGNOFF)."""
        for d in wave:
            iid = d.name.removeprefix("issue_")
            (d / "patch.diff").write_text(self._patch(self.changes[iid]), encoding="utf-8")
            if iid in self.no_signoff:
                (d / "check-gates.json").write_text("{}", encoding="utf-8")
                shutil.copyfile(sb.TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
            else:
                sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded line tip (else the line,
        else the base), push it, record it — except for an id in ``no_push``, whose publish
        fails before its push (returns False, pushes nothing, records nothing)."""
        iid = d.name.removeprefix("issue_")
        if iid in self.no_push:
            return False
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = tip or (f"origin/{line}" if line else "origin/main")
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.fx._publish(iid, self.changes[iid], cut_from=cut)
        (d / "brief.md").write_text(brief, encoding="utf-8")
        return True

    def _flow(self, ids: list[str], *, csv: str | None = None) -> int:
        """One stack-mode ``pdca flow <ids…>`` — or, with no ids, ``pdca flow --from-csv
        <csv>`` — publishing on, Act off, through ``cli._flow``; returns its exit code. The
        Plan session briefs nothing: every bundle here is planned by the test."""
        args = SimpleNamespace(issue_ids=list(ids), from_csv=csv, from_briefs=None,
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
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            rc = cli._flow(self.cfg, args)
        self.err, self.out = err.getvalue(), out.getvalue()
        self.assertNotIn("Traceback", self.err)
        self.assertNotIn("STOPPING", self.err)
        return rc

    def _state(self, iid: str) -> str:
        return state.state(self.cfg.bundle(iid))

    def _bundles(self, *, conflict: bool) -> list[tuple[str, dict[str, str], str]]:
        """``10``, ``11``, ``12`` (wave 0) and ``21`` depending on ``12`` (wave 1): each id
        with the files its build changes and the id it depends on. With ``conflict``,
        ``10`` and ``11`` change the same line of ``shared.txt`` (on the base) to different
        text; else ``11`` adds a file of its own."""
        self._on_base({"shared.txt": "x\n"})
        return [("10", {"shared.txt": "ten\n"}, ""),
                ("11", {"shared.txt": "eleven\n"} if conflict
                 else {"eleven.txt": "eleven\n"}, ""),
                ("12", {"twelve.txt": "twelve\n"}, ""),
                ("21", {"twenty-one.txt": "twenty-one\n"}, "12")]

    def _batch(self, *, conflict: bool) -> list[str]:
        """The bundles of :meth:`_bundles`, planned; returns their ids."""
        ids = []
        for iid, files, depends_on in self._bundles(conflict=conflict):
            self._plan(iid, files, depends_on)
            ids.append(iid)
        return ids

    def _split_parent(self, iid: str, *, conflict: bool) -> list[str]:
        """``iid`` split by the production ``split.accept`` into the bundles of
        :meth:`_bundles` as its children, then accepted: COMPLETE on a split, its children
        PLANNED where the split left them. Returns the child ids."""
        parent = self.cfg.bundle(iid)
        sb._brief(parent)
        specs = self._bundles(conflict=conflict)
        label = {cid: f"child-{n}" for n, (cid, _files, _dep) in enumerate(specs, 1)}
        proposal = "<!-- pdca:split-proposal v1 -->\n\n"
        for cid, files, depends_on in specs:
            proposal += (f"<!-- pdca:child {label[cid]} -->\n"
                         f"- **Slug:** child-{cid}\n"
                         f"- **Repo + branch target:** org/repo @ main\n"
                         + (f"- **Depends on:** {label[depends_on]}\n" if depends_on else "")
                         + f"<!-- pdca:end {label[cid]} -->\n\n")
            self.changes[cid] = files
        (parent / split.PROPOSAL).write_text(proposal, encoding="utf-8")
        kids = [cid for cid, _files, _dep in specs]
        split.accept(parent, kids, self.cfg)
        sb._accept(parent)
        self.assertEqual(self._state(iid), state.COMPLETE)
        for cid in kids:
            self.assertEqual(self._state(cid), state.PLANNED, cid)
        return kids

    def _ancestor_of_line(self, iid: str, batch: list[str]) -> bool:
        """``iid``'s branch is on the line of the run asked for ``batch``."""
        line = integrate.integration_branch(self.cfg, "main", batch)
        return self.fx._ancestor(self.fx._tip(f"fix/{iid}"), self.fx._tip(line))

    def _assert_held_nothing(self) -> None:
        """The last run printed no hold line, for any bundle and either reason."""
        for phrase in (_CONFLICT_HOLD, _UNPUSHED_HOLD):
            self.assertNotIn(phrase, self.err)

    # -- (1) the #674 hold → 3 -------------------------------------------------------------

    def test_a_run_that_held_a_same_wave_conflict_exits_3(self):
        ids = self._batch(conflict=True)
        rc = self._flow(ids)
        self.assertIn(f"issue_11 {_CONFLICT_HOLD}", self.err)
        self.assertFalse(self._ancestor_of_line("11", ids))
        for iid in ids:
            self.assertEqual(self._state(iid), state.COMPLETE, (iid, self.err))
        self.assertEqual(rc, 3, self.err)

    # -- (2) the #593 hold → 3 -------------------------------------------------------------

    def test_a_run_that_held_an_unpushed_bundle_exits_3(self):
        ids = self._batch(conflict=False)
        self.no_push.add("11")
        rc = self._flow(ids)
        self.assertIn(f"issue_11 {_UNPUSHED_HOLD}", self.err)
        for iid in ids:
            self.assertEqual(self._state(iid), state.COMPLETE, (iid, self.err))
        self.assertEqual(rc, 3, self.err)

    # -- (3) no hold → 0 -------------------------------------------------------------------

    def test_a_run_that_held_nothing_exits_0(self):
        ids = self._batch(conflict=False)
        rc = self._flow(ids)
        self._assert_held_nothing()
        self.assertTrue(self._ancestor_of_line("11", ids))
        for iid in ids:
            self.assertEqual(self._state(iid), state.COMPLETE, (iid, self.err))
        self.assertEqual(rc, 0, self.err)

    # -- (4) a bundle left unfinished wins over a hold → 1 ---------------------------------

    def test_a_held_dependent_left_planned_wins_over_the_hold(self):
        ids = self._batch(conflict=True)
        self._plan("20", {"twenty.txt": "twenty\n"}, "11")
        ids = sorted([*ids, "20"])
        rc = self._flow(ids)
        self.assertIn(f"issue_11 {_CONFLICT_HOLD}", self.err)
        self.assertEqual(self._state("20"), state.PLANNED, self.err)
        self.assertEqual(rc, 1, self.err)

    # -- `--from-csv` with no ids: the sweep `flow.flow_batch` drives -----------------------

    def test_a_csv_batch_that_held_a_bundle_exits_3(self):
        ids = self._batch(conflict=True)
        rc = self._flow([], csv="tracker.csv")
        self.assertIn(f"issue_11 {_CONFLICT_HOLD}", self.err)
        self.assertFalse(self._ancestor_of_line("11", ids))   # the sweep is the batch
        for iid in ids:
            self.assertEqual(self._state(iid), state.COMPLETE, (iid, self.err))
            self.assertIn(f"{state.COMPLETE}\t{iid}", self.out.splitlines())
        self.assertEqual(rc, 3, self.err)

    # -- the single-id shape: `pdca flow 7` over a split parent folds between its children --

    def _single(self, *, conflict: bool) -> int:
        """``pdca flow 7``, ``7`` a split parent whose children are the four bundles; checks
        the run drove every child and answered in the single-id shape; returns its exit
        code."""
        kids = self._split_parent("7", conflict=conflict)
        rc = self._flow(["7"])
        for cid in kids:
            self.assertEqual(self._state(cid), state.COMPLETE, (cid, self.err))
        # `_report_single`'s output, not the batch table: the named id's `state<TAB>path`
        # line first, then one such line per child the run adopted.
        self.assertEqual(self.out.splitlines(),
                         [f"{state.COMPLETE}\t{self.cfg.bundle(i)}" for i in ["7", *kids]],
                         self.err)
        return rc

    def test_a_single_id_run_that_held_a_bundle_exits_3(self):
        rc = self._single(conflict=True)
        self.assertIn(f"issue_11 {_CONFLICT_HOLD}", self.err)
        self.assertFalse(self._ancestor_of_line("11", ["7"]))
        self.assertEqual(rc, 3, self.err)

    def test_a_single_id_run_that_held_nothing_exits_0(self):
        rc = self._single(conflict=False)
        self._assert_held_nothing()
        self.assertTrue(self._ancestor_of_line("11", ["7"]))
        self.assertEqual(rc, 0, self.err)

    def test_a_single_id_run_stopped_for_sign_off_still_exits_3_for_its_hold(self):
        # The single-id shape keeps its rule that a stop for sign-off is success: 21, of the
        # last wave, waits for the human, and the hold of 11 still makes the run 3 — not 1,
        # as the same results would be in the batch shape, and not 0.
        self.no_signoff.add("21")
        self._split_parent("7", conflict=True)
        rc = self._flow(["7"])
        self.assertEqual(self._state("21"), state.AWAITING_SIGNOFF, self.err)
        for cid in ("10", "11", "12"):
            self.assertEqual(self._state(cid), state.COMPLETE, (cid, self.err))
        self.assertIn(f"issue_11 {_CONFLICT_HOLD}", self.err)
        self.assertEqual(rc, 3, self.err)


if __name__ == "__main__":
    unittest.main()
