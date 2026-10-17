"""In stack mode an in-batch bundle is built on what it declares, not on when it was
scheduled (#727, for #463). Before, every bundle was cut from the run's one line, so ``B``
(``Depends on: A``) carried ``Z``, unrelated, of the same earlier wave (#630). Now its base
(``integrate.build_bundle_base``, #723) holds exactly the accepted work of the bundles this
run built that it is ordered after by a declared edge, or it is the real base; at a seam it
keeps the line. The #633 and #695 checks hold each branch to every head the run's PRs hold.

Real folds on the ``StackFoldGit`` fixture (tests/test_integrate_stack_bases.py, loaded by
path) through ``flow._drive_and_act``: build and sign-off stubbed, ``flow._publish_bundle``
cutting each PR branch from the recorded stack-base tip, ``gh`` stubbed, the publisher not
a stub. Only modules are imported, so with the fix reverted these load, run and fail.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_dependency_scoped_bases
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import shutil
import subprocess
import unittest
from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import brief, flow, integrate, publish, signoff, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-17"
TWO = "two commits"
_FIELDS = {"depends_on": "Depends on", "merged": "Depends on (merged)",
           "conflicts_with": "Conflicts with", "stacks_on": "Stacks on",
           "onto": "Onto branch"}
#: How the run starts the line naming the line's branches its end check could not judge,
#: and the one naming the heads bundles' own bases took (`flow._report_late_rewrites`).
UNCHECKED_LINE = "flow: could not check whether a branch this run folded onto its line"
UNCHECKED_BASE = "flow: could not check whether a branch a bundle's own base took"
#: Today's wording for the line (`integrate.py`, `flow.py`): never said of a bundle's base.
LINE_WORDS = ("the commit the line took", "every PR this run cut from")


class DependencyScopedBases(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.commits: dict[str, list[dict[str, str]]] = {}   # id → its commits' files
        self.close: set[str] = set()                         # closed with no patch
        self.unpushed: set[str] = set()                      # publish pushes no branch
        self.failing: set[str] = set()                       # build fails: stays PLANNED
        self.discontinue: set[str] = set()                   # signed off discontinued
        self.during: dict[str, Callable[[], None]] = {}      # id → run as it is built
        self.after_line_fold: Callable[[], None] | None = None   # run once, after a fold
        # bundle → (stack base, recorded tip, origin's tip of that base) when it was built
        self.seen: dict[str, tuple[str, str, str | None]] = {}
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _plan(self, iid: str, commits: str | None = None, *,
              target: str = "org/repo @ main", **edges: str) -> Path:
        """Bundle ``iid``'s brief on ``target`` with ``edges`` (``_FIELDS``' keys), and what
        its build commits: two files of its own in two commits (``TWO``), else one."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n- **Repo + branch target:** {target}\n"
            + "".join(f"- **{_FIELDS[k]}:** {v}\n" for k, v in edges.items()), encoding="utf-8")
        low = iid.lower()
        self.commits[iid] = ([{f"{low}1.txt": f"{iid} one\n"}, {f"{low}2.txt": f"{iid} two\n"}]
                             if commits == TWO else [{f"{low}.txt": f"{iid}\n"}])
        return d

    def _finished(self, iid: str) -> Path:
        """``iid`` finished in an earlier run: published from the base and accepted."""
        d = self.fx._publish(iid, {f"{iid.lower()}.txt": f"{iid}\n"})
        sb._accept(d)
        return d

    def _second_target(self) -> str:
        """A second target: org/zrepo, a clone of the same origin, at base ``zbase``."""
        sb._git(self.fx.origin, "branch", "zbase", "main")
        z = self.fx.tmp / "zrepo"
        subprocess.run(["git", "clone", "-q", str(self.fx.origin), str(z)], check=True,
                       capture_output=True)
        sb._identity(z)
        self.cfg.repo_checkouts["org/zrepo"] = str(z)
        return "org/zrepo @ zbase"

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        """Any ``gh`` call is answered OPEN with the PR's head, its branch's tip on origin."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        iid = str(args[3]).rsplit("/", 1)[-1]
        head = self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else ""
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"state": "OPEN", "headRefOid": head}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Record what each bundle is built on, run its ``during`` hook, then build it."""
        for d in wave:
            iid = d.name.removeprefix("issue_")
            base = publish.read_stack_base(d)
            self.seen[d.name] = (base, publish.read_stack_base_tip(d),
                                 self.fx._tip(base) if base and self.fx._has(base) else None)
            self.during.pop(iid, lambda: None)()
            if iid in self.failing:
                continue
            if iid in self.close:
                (d / state.CLOSE_MARKER).write_text("likely-close\n", encoding="utf-8")
            else:
                (d / "patch.diff").write_text("".join(
                    sb._new_file_patch(n, t.rstrip("\n"))
                    for c in self.commits[iid] for n, t in c.items()), encoding="utf-8")
            if iid in self.discontinue:
                (d / "check-gates.json").write_text("{}", encoding="utf-8")
                shutil.copyfile(sb.TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
                signoff.record(d / "SUMMARY.md", action="discontinue", by="T", date=TODAY)
            else:
                sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut ``fix/<iid>`` where publish cuts it, commit each of the
        bundle's commits signed off, push it and record it. An ``Onto branch`` bundle
        commits onto that branch instead (a ``"stacked"`` record). The brief is untouched."""
        iid = d.name.removeprefix("issue_")
        if iid in self.close or iid in self.unpushed:
            return False
        bp = d / "brief.md"
        onto, parents = brief.onto_branch(bp), brief.stacks_on(bp)
        tip, base = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        cut = (f"{onto[0]}/{onto[1]}" if onto else tip or (
            f"origin/{base}" if base else f"origin/fix/{parents[0]}" if parents
            else "origin/main"))
        branch, repo = onto[1] if onto else f"fix/{iid}", self.fx.primary
        sb._git(repo, "fetch", "-q", "origin")
        sb._git(repo, "checkout", "-q", "-B", branch, cut)
        for i, files in enumerate(self.commits[iid], 1):
            for name, text in files.items():
                (repo / name).write_text(text, encoding="utf-8")
            sb._git(repo, "add", "--all")
            sb._git(repo, "commit", "-q", "-s", "-m", f"fix {iid} ({i})")
        sb._git(repo, "push", "-q", "origin", branch)
        sb._git(repo, "checkout", "-q", "main")
        record = ({"mode": "stacked", "branch": branch, "base": cut} if onto else
                  {"mode": "new-pr" if cut == "origin/main" else "stacked-pr",
                   "branch": branch, "base": "main"})
        (d / "publish.json").write_text(json.dumps(
            {**record, "repo": "org/repo", "pr_url": f"https://example.test/pr/{iid}"}),
            encoding="utf-8")
        return True

    def _run(self, ids: list[str], *, drive: list[str] | None = None,
             git: Callable | None = None) -> None:
        """One stack-mode run asked for ``ids``, driving ``drive`` (default: all; a re-issue
        drives only the unfinished, as ``pdca flow <ids>`` does). ``git`` replaces
        ``integrate._git``. The holds it reports are in ``self.holds``."""
        self.ids = list(ids)
        self.line = integrate.integration_branch(self.cfg, "main", ids)
        self.seen, self.holds = {}, {}
        real_fold = integrate.fold

        def fold(*a, **kw):
            out = real_fold(*a, **kw)
            hook, self.after_line_fold = self.after_line_fold, None
            if hook is not None:
                hook()
            return out

        err = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(integrate, "fold", fold))
            if git is not None:
                stack.enter_context(mock.patch.object(integrate, "_git", git))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(err))
            self.results = flow._drive_and_act(
                self.cfg, [self.cfg.bundle(i) for i in (drive or ids)], do_publish=True,
                do_act=False, by="T", today=TODAY, batch=ids, holds=self.holds)
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)

    # -- helpers ---------------------------------------------------------------------------

    def _head(self, iid: str) -> str:
        return self.fx._tip(f"fix/{iid}")

    def _base(self, iid: str) -> str:
        return integrate.bundle_base_branch(self.cfg, "main", self.ids, f"issue_{iid}")

    def _work(self, iid: str, *under: str) -> list[str]:
        """Every commit of ``origin/main..fix/<iid>`` — or, given ``under``, every non-merge
        one no branch of ``under`` has: ``iid``'s own work above them."""
        work = sb._git(self.fx.origin, "rev-list", *(("--no-merges",) if under else ()),
                       f"fix/{iid}", "--not", "main", *(f"fix/{u}" for u in under)).split()
        self.assertTrue(work, iid)
        return work

    def _has_all(self, rev: str, iid: str) -> None:
        self.assertEqual([c for c in self._work(iid) if not self.fx._ancestor(c, rev)], [],
                         (rev, iid, self.err))

    def _has_none(self, rev: str, iid: str, *under: str) -> None:
        self.assertEqual([c for c in self._work(iid, *under) if self.fx._ancestor(c, rev)], [],
                         (rev, iid, self.err))

    def _own_base(self, iid: str, *closure: str) -> str:
        """``iid`` was built on a base of its own — the branch ``bundle_base_branch`` names,
        at the tip origin had then — holding every commit of each of ``closure``'s
        branches, and is COMPLETE; returns that tip."""
        self.assertIn(f"issue_{iid}", self.seen, self.err)
        base, tip, at = self.seen[f"issue_{iid}"]
        self.assertEqual(base, self._base(iid), self.err)
        self.assertEqual(tip, at, self.err)
        for c in closure:
            self._has_all(tip, c)
        self.assertEqual(self.results[iid], state.COMPLETE, self.err)
        return tip

    def _on_line(self, iid: str) -> str:
        """``iid`` was built on the run's line, at origin's line tip then, as before #727,
        and is COMPLETE; returns that tip."""
        self.assertIn(f"issue_{iid}", self.seen, self.err)
        base, tip, at = self.seen[f"issue_{iid}"]
        self.assertEqual(base, self.line, self.err)
        self.assertIsNotNone(at, self.err)
        self.assertEqual(tip, at, self.err)
        self.assertEqual(self.results[iid], state.COMPLETE, self.err)
        return tip

    def _on_real_base(self, iid: str) -> None:
        """``iid`` was built with its stack base cleared — on the real base — and is
        COMPLETE."""
        self.assertIn(f"issue_{iid}", self.seen, self.err)
        self.assertEqual(self.seen[f"issue_{iid}"][:2], ("", ""), self.err)
        self.assertEqual(publish.read_stack_base(self.cfg.bundle(iid)), "")
        self.assertEqual(self.results[iid], state.COMPLETE, self.err)

    def _lines(self, part: str) -> list[str]:
        return [x for x in self.err.splitlines() if part in x]

    # -- (1) the #630 case: B carries A, and nothing of Z -----------------------------------

    def test_a_dependent_carries_its_prerequisite_and_nothing_of_an_unrelated_bundle(self):
        self._plan("A", TWO)
        self._plan("Z", TWO)
        self._plan("B", depends_on="A")
        self._run(["A", "Z", "B"])
        for iid in ("A", "Z", "B"):
            self.assertEqual(self.results[iid], state.COMPLETE, self.err)
        self._has_all("fix/B", "A")
        self._has_none("fix/B", "Z")
        self._has_none(self._own_base("B", "A"), "Z")
        for iid in ("A", "Z"):                   # the line still folds both: the run's check
            self._has_all(self.line, iid)

    # -- (2) transitive ---------------------------------------------------------------------

    def test_the_base_carries_every_prerequisite_reached_through_another(self):
        self._plan("A", TWO)
        self._plan("Z", TWO)
        self._plan("B", depends_on="A")
        self._plan("C", depends_on="B")
        self._run(["A", "Z", "B", "C"])
        for iid in ("A", "B"):
            self._has_all("fix/C", iid)
        self._has_none("fix/C", "Z")
        self._own_base("C", "A", "B")

    def test_what_one_with_no_patch_was_built_on_is_on_the_base(self):
        # M (`Depends on: A`) and N (`Conflicts with: A`) close with no patch, each built on
        # a base holding A. H (`Depends on: M`), J (`Depends on: N`) and P (`Conflicts with:
        # N`) are built on what those were built on: A, never Z.
        self._plan("A", TWO)
        self._plan("Z", TWO)
        self._plan("M", depends_on="A")
        self._plan("N", conflicts_with="A")
        self.close.update({"M", "N"})
        for iid, edge in (("H", "depends_on"), ("J", "depends_on"), ("P", "conflicts_with")):
            self._plan(iid, **{edge: "M" if iid == "H" else "N"})
        self._run(["A", "Z", "M", "N", "H", "J", "P"])
        for iid in ("M", "N", "H", "J", "P"):
            self._has_none(self._own_base(iid, "A"), "Z")
        for iid in ("H", "J", "P"):
            self._has_all(f"fix/{iid}", "A")
            self._has_none(f"fix/{iid}", "Z")

    # -- (3) an earlier-wave `Conflicts with` partner ---------------------------------------

    def test_an_earlier_wave_conflicts_with_partner_is_on_the_base(self):
        self._plan("A", TWO)
        self._plan("Z", TWO)
        self._plan("E", conflicts_with="A")      # the name-higher id builds in a later wave
        self._run(["A", "Z", "E"])
        self._has_all("fix/E", "A")
        self._has_none("fix/E", "Z")
        self._own_base("E", "A")

    def test_a_members_partner_built_after_it_is_not_on_the_base(self):
        # E `Conflicts with: A` builds after A, beside Y (`Depends on: A`); D `Depends on: Y`.
        # A's base held no E, nor did Y's: D's holds A and Y, never E.
        self._plan("A", TWO)
        self._plan("E", TWO, conflicts_with="A")
        self._plan("Y", depends_on="A")
        self._plan("D", depends_on="Y")
        self._run(["A", "E", "Y", "D"])
        self._has_all(self._own_base("E", "A"), "A")
        self._has_none(self._own_base("D", "A", "Y"), "E", "A")   # E's own work, above A
        self._has_none("fix/D", "E", "A")
        self._has_all(self.line, "E")            # the line holds E; D's base leaves it out

    # -- (4) an in-batch `Depends on (merged)` ----------------------------------------------

    def test_an_in_batch_depends_on_merged_prerequisite_is_on_the_base(self):
        self._plan("A", TWO)
        self._plan("Z", TWO)
        self._plan("G", merged="A")
        self._run(["A", "Z", "G"])
        self._has_all("fix/G", "A")
        self._has_none("fix/G", "Z")
        self._own_base("G", "A")

    # -- (5) empty closure → the real base --------------------------------------------------

    def test_a_bundle_whose_only_prerequisite_has_no_patch_is_built_on_the_real_base(self):
        self._plan("A", TWO)
        self._plan("Z", TWO)
        self._plan("N")
        self.close.add("N")
        self._plan("H", depends_on="N")
        self._run(["A", "N", "Z", "H"])
        self._on_real_base("H")
        self._has_none("fix/H", "A")
        self._has_none("fix/H", "Z")
        for iid in ("A", "Z"):
            self._has_all(self.line, iid)

    # -- (6b) the walk goes only through bundles this run built -----------------------------

    def test_a_partner_that_never_completed_brings_nothing_of_its_prerequisite(self):
        # E `Conflicts with: A`, A `Depends on: Q`; A is discontinued. E declared nothing
        # about Q, and this run never built A: E is built on the real base, without Q.
        self._plan("Q", TWO)
        self._plan("Z", TWO)
        self._plan("A", depends_on="Q")
        self._plan("E", conflicts_with="A")
        self.discontinue.add("A")
        self._run(["Q", "Z", "A", "E"])
        self.assertEqual(self.results["A"], state.DISCONTINUED, self.err)
        self._on_real_base("E")
        self._has_none("fix/E", "Q")
        self._has_none("fix/E", "Z")

    # -- (6) seams keep the line, exactly as before, and are built --------------------------

    def test_a_dependent_of_a_carried_bundle_keeps_the_line(self):
        # Run 1 finishes P; X fails, so D (`Depends on: P, X`) is never built. The re-issue
        # carries P onto the line before wave 0 and builds X: D's closure holds a bundle
        # this run did not build, so D is built on the line, which holds P and X.
        self._plan("P")
        self._plan("X")
        self._plan("D", depends_on="P, X")
        self.failing.add("X")
        self._run(["P", "X", "D"])
        self.assertEqual(self.results["P"], state.COMPLETE, self.err)
        self.failing.clear()
        self._run(["P", "X", "D"], drive=["X", "D"])
        self.assertIn("flow: carried issue_P (finished", self.err)
        tip = self._on_line("D")
        for iid in ("P", "X"):
            self._has_all(tip, iid)

    def test_a_conflicts_with_partner_that_was_carried_keeps_the_line(self):
        # Run 1 finishes K only. The re-issue carries K (W `Depends on: K`) and builds X;
        # D `Conflicts with: K` and `Depends on: X`, so it is built on the line holding both.
        self._plan("K")
        self._plan("X")
        self._plan("W", depends_on="K")
        self._plan("D", depends_on="X", conflicts_with="K")
        self.failing.update({"X", "W"})
        self._run(["K", "X", "W", "D"])
        self.assertEqual(self.results["K"], state.COMPLETE, self.err)
        self.failing.clear()
        self._run(["K", "X", "W", "D"], drive=["X", "W", "D"])
        self.assertIn("flow: carried issue_K (finished", self.err)
        tip = self._on_line("D")
        for iid in ("K", "X"):
            self._has_all(tip, iid)

    def test_a_dependent_of_an_onto_branch_bundle_keeps_the_line(self):
        # O commits onto an existing PR's branch `feat`: no cut point is recorded for it, so
        # no base of D's own can take it (#723 refuses one). D keeps the line and is built.
        self.fx._push_from_human({"feat.txt": "an open PR\n"}, to="feat")
        self._plan("O", onto="origin/feat")
        self._plan("A")
        self._plan("D", depends_on="O, A")
        self._run(["O", "A", "D"])
        self.assertNotIn("STOPPING", self.err)
        tip = self._on_line("D")
        self.assertTrue(self.fx._ancestor(self.fx._tip("feat"), tip))
        self._has_all(tip, "A")

    def test_a_dependent_of_a_legacy_stacks_on_bundle_keeps_the_line(self):
        # M `Stacks on: L`, finished in an earlier run and not asked for: M is cut from L's
        # branch as before, and D (`Depends on: M`) keeps the line and is built.
        self._finished("L")
        self._plan("M", stacks_on="L")
        self._plan("Z")
        self._plan("D", depends_on="M")
        self._run(["M", "Z", "D"])
        self.assertNotIn("STOPPING", self.err)
        self.assertTrue(self.fx._ancestor(self._head("L"), self._head("M")))
        tip = self._on_line("D")
        for iid in ("M", "Z"):
            self._has_all(tip, iid)

    # -- (7) the rewrite checks: each base, its head and its PR; the line as before ---------

    def _two_bases_and_the_line(self, *extra: str, after_fold: bool = False) -> tuple[str, str]:
        """A (two commits) in wave 0; B and C (``Depends on: A``), each on a base of its
        own, and S (``Stacks on: A``, a seam: on the line) in wave 1; then ``extra``
        (``Depends on: B``). While B builds — both bases and the line took A's head h2 —
        or, ``after_fold``, once the fold after wave 1 has, fix/A is forced back to its
        first commit h1. Returns ``(h1, h2)``."""
        self._plan("A", TWO)
        self._plan("B", depends_on="A")
        self._plan("C", depends_on="A")
        self._plan("S", stacks_on="A")
        for iid in extra:
            self._plan(iid, depends_on="B")
        heads: list[str] = []

        def back_to_h1() -> None:
            heads.extend([sb._git(self.fx.origin, "rev-parse", "fix/A~1").strip(),
                          self._head("A")])
            sb._git(self.fx.origin, "branch", "-f", "fix/A", heads[0])

        self.during["B"] = ((lambda: setattr(self, "after_line_fold", back_to_h1))
                            if after_fold else back_to_h1)
        self._run(["A", "B", "C", "S", *extra])
        h1, h2 = heads
        self.assertTrue(self.fx._ancestor(h2, self._head("B")))
        self.assertTrue(self.fx._ancestor(h2, self._head("C")))
        self.assertTrue(self.fx._ancestor(h2, self._head("S")))
        self.assertEqual(self._head("A"), h1)
        return h1, h2

    def test_a_rewrite_after_the_last_fold_names_each_base_its_head_and_its_pr(self):
        _h1, h2 = self._two_bases_and_the_line()
        self.assertNotIn("STOPPING", self.err)
        self._on_line("S")
        for iid in ("B", "C"):
            self._own_base(iid)
            said = self._lines(f"issue_A's branch origin/fix/A was rewritten after "
                               f"issue_{iid}'s own base {self._base(iid)} took {h2[:12]}")
            self.assertEqual(len(said), 1, self.err)
            self.assertIn(f"rebuild issue_{iid}'s PR on the rewritten branch", said[0])
            for words in LINE_WORDS:
                self.assertNotIn(words, said[0])
        line = self._lines(f"was rewritten after this run folded it onto {self.line}")
        self.assertEqual(len(line), 1, self.err)
        self.assertIn(f"every PR this run cut from {self.line} after it took {h2[:12]} "
                      f"(issue_S)", line[0])
        self.assertEqual(self.holds, {"A": "rewritten"})

    def test_the_next_fold_stops_naming_each_base_its_head_and_its_pr(self):
        _h1, h2 = self._two_bases_and_the_line("D")
        stop = self._lines("wave 1 did not integrate")
        self.assertEqual(len(stop), 1, self.err)
        for iid in ("B", "C"):
            self.assertIn(f"issue_A's branch origin/fix/A was rewritten after issue_{iid}'s own "
                          f"base {self._base(iid)} took {h2[:12]} from it", stop[0])
            self.assertIn(f"issue_{iid} was built on {self._base(iid)}, so issue_{iid}'s PR "
                          f"holds commits origin/fix/A no longer has", stop[0])
        self.assertIn(f"was rewritten after this run folded it onto {self.line}", stop[0])
        self.assertIn(f"Every PR this run cut from {self.line} after it took {h2[:12]} "
                      f"(issue_S)", stop[0])
        self.assertIn("rebuild the PRs of issue_S, issue_B, issue_C on the rewritten branch",
                      stop[0])
        for words in LINE_WORDS:                 # said of the line alone, once
            self.assertEqual(stop[0].lower().count(words.lower()), 1, stop[0])
        self.assertNotIn("issue_D", self.seen, self.err)

    def test_a_later_base_that_reads_the_rewrite_stops_naming_each_base_and_pr(self):
        # The rewrite comes after the fold after wave 1: D's own base, built next, reads it
        # and stops the run on ONE line naming the line's PR and each base's, as a fold does.
        _h1, h2 = self._two_bases_and_the_line("D", after_fold=True)
        stop = self._lines("STOPPING")
        self.assertEqual(len(stop), 1, self.err)
        for part in ("could not build issue_D's own base from issue_A, issue_B",
                     f"was rewritten after this run folded it onto {self.line}",
                     f"Every PR this run cut from {self.line} after it took {h2[:12]} (issue_S)",
                     *(f"issue_{i} was built on {self._base(i)}, so issue_{i}'s PR holds "
                       f"commits origin/fix/A no longer has" for i in ("B", "C")),
                     f"Restore origin/fix/A to {h2[:12]} or a commit that contains it, then "
                     f"re-run; or rebuild the PRs of issue_S, issue_B, issue_C on the "
                     f"rewritten branch"):
            self.assertIn(part, stop[0])
        self.assertNotIn("issue_D", self.seen, self.err)
        self.assertEqual(self._lines("No fold of this run ran after"), [], self.err)  # once

    def test_a_base_that_stops_the_run_for_a_clash_leaves_the_end_check_on(self):
        # After the fold after wave 1, fix/Z goes back a commit and main gains a file that
        # clashes with A's. C's base (A, B) stops the run on the clash; it never read fix/Z,
        # so the end of the run still names Z's rewrite, with S, cut from the line holding it.
        self._plan("A")
        self._plan("Z", TWO)
        self._plan("S", stacks_on="Z")
        self._plan("B", depends_on="A")
        self._plan("C", depends_on="B")

        def back_and_clash() -> None:
            sb._git(self.fx.origin, "branch", "-f", "fix/Z", "fix/Z~1")
            self.fx._push_from_human({"a.txt": "main's own\n"}, to="main")

        self.during["B"] = lambda: setattr(self, "after_line_fold", back_and_clash)
        self._run(["A", "Z", "S", "B", "C"])
        stop = self._lines("STOPPING")
        self.assertEqual(len(stop), 1, self.err)
        self.assertIn("could not build issue_C's own base from issue_A, issue_B", stop[0])
        self.assertNotIn("issue_Z", stop[0])
        said = self._lines(f"issue_Z's branch origin/fix/Z was rewritten after this run folded "
                           f"it onto {self.line}")
        self.assertEqual(len(said), 1, self.err)
        self.assertIn("(issue_S)", said[0])
        self.assertEqual(self.holds, {"Z": "rewritten"})

    def _a_taken_twice(self, rewrite: Callable[[dict[str, str]], None],
                       git: Callable | None = None) -> dict[str, str]:
        """A in wave 0; S (``Stacks on: A``: on the line) and B (``Depends on: A``) in wave
        1; C (``Depends on: B``) in wave 2. B's base and S took a1; while S builds, fix/A
        moves on to a2 (a fast-forward), which the next fold and C's base take; while C
        builds, ``rewrite`` (given the heads) rewrites it. ``git``: as :meth:`_run` takes
        it. Returns the heads."""
        self._plan("A")
        self._plan("S", stacks_on="A")
        self._plan("B", depends_on="A")
        self._plan("C", depends_on="B")
        heads: dict[str, str] = {}

        def move_on() -> None:
            heads["a1"] = self._head("A")
            heads["a2"] = self.fx._push_from_human({"a2.txt": "A, after review\n"},
                                                   to="fix/A", off="origin/fix/A")

        self.during.update(S=move_on, C=lambda: rewrite(heads))
        self._run(["A", "S", "B", "C"], git=git)
        self._on_line("S")
        self.assertFalse(self.fx._ancestor(heads["a2"], self._head("S")))   # S holds a1 only
        self.assertTrue(self.fx._ancestor(heads["a1"], self._own_base("B")))
        self.assertFalse(self.fx._ancestor(heads["a2"], self._own_base("B")))
        self.assertTrue(self.fx._ancestor(heads["a2"], self._own_base("C", "B")))
        self.assertEqual(self.holds, {"A": "rewritten"})
        self.assertEqual(self._lines("issue_S's own base"), [], self.err)   # S has none
        return heads

    def test_a_pr_cut_from_the_line_at_an_older_head_is_named_when_it_is_lost(self):
        heads = self._a_taken_twice(lambda h: self.fx._push_from_human(
            {"a3.txt": "A, rebuilt\n"}, to="fix/A", force=True))
        line = self._lines(f"was rewritten after this run folded it onto {self.line}")
        self.assertEqual(len(line), 1, self.err)
        self.assertIn(f"which does not contain {heads['a2'][:12]}", line[0])
        self.assertIn(f"every PR this run cut from {self.line} after it took "
                      f"{heads['a1'][:12]} (issue_S)", line[0])
        for iid, h in (("B", "a1"), ("C", "a2")):
            self.assertEqual(len(self._lines(
                f"after issue_{iid}'s own base {self._base(iid)} took {heads[h][:12]}")), 1)

    def test_a_pr_cut_from_the_line_at_a_head_still_on_the_branch_is_not_named(self):
        # fix/A goes back to a1: the line and C's base lost a2; S's PR and B's base hold a1.
        heads = self._a_taken_twice(lambda h: sb._git(self.fx.origin, "branch", "-f", "fix/A",
                                                      h["a1"]))
        line = self._lines(f"was rewritten after this run folded it onto {self.line}")
        self.assertEqual(len(line), 1, self.err)
        self.assertIn(f"no PR this run cut from {self.line} holds them", line[0])
        self.assertNotIn("issue_S", line[0])
        self.assertEqual(self._lines("issue_B's own base"), [], self.err)
        self.assertEqual(len(self._lines(
            f"after issue_C's own base {self._base('C')} took {heads['a2'][:12]}")), 1)

    def test_a_head_git_cannot_judge_beside_a_lost_one_is_named_to_check_by_hand(self):
        # fix/A is rewritten, and git then fails to judge a1, which S and B's base took: the
        # line's report names S, which may hold a lost commit; B's base is named to check by
        # hand; and the branch counts as rewritten (the helper's holds).
        real, bad = integrate._git, []

        def broken(repo: Path, *args: str) -> int:
            if bad and args[:2] == ("merge-base", "--is-ancestor") and args[2] == bad[0]:
                return 128
            return real(repo, *args)

        def rewrite(h: dict[str, str]) -> None:
            self.fx._push_from_human({"a3.txt": "A, rebuilt\n"}, to="fix/A", force=True)
            bad.append(h["a1"])

        heads = self._a_taken_twice(rewrite, git=broken)
        line, base = (self._lines(f"was rewritten after this run folded it onto {self.line}"),
                      self._lines(UNCHECKED_BASE))
        self.assertEqual((len(line), len(base)), (1, 1), self.err)
        self.assertIn(f"after it took {heads['a1'][:12]} (issue_S)", line[0])
        self.assertIn(f"issue_A's origin/fix/A ({heads['a1'][:12]}), which issue_B's own base",
                      base[0])

    def test_a_fold_that_finds_two_heads_lost_asks_for_a_commit_holding_both(self):
        # The line took a1, B's base a2 (a fast-forward); while B builds, fix/A is rewritten.
        self._plan("A")
        self._plan("B", depends_on="A")
        self._plan("C", depends_on="B")
        heads: dict[str, str] = {}
        self.after_line_fold = lambda: heads.update(
            a1=self._head("A"), a2=self.fx._push_from_human(
                {"a2.txt": "A, after review\n"}, to="fix/A", off="origin/fix/A"))
        self.during["B"] = lambda: self.fx._push_from_human({"a3.txt": "A, rebuilt\n"},
                                                            to="fix/A", force=True)
        self._run(["A", "B", "C"])
        stop = self._lines("wave 1 did not integrate")
        self.assertEqual(len(stop), 1, self.err)
        self.assertIn(f"Restore origin/fix/A to a commit that contains {heads['a1'][:12]} and "
                      f"{heads['a2'][:12]}, then re-issue the run", stop[0])

    def test_a_head_only_a_base_took_is_checked_for_that_base(self):
        # The fold after wave 0 took a1; then fix/A moves on to a2 (a fast-forward), which
        # B's base takes; while B builds, fix/A goes back to a1. The line still matches A's
        # PR, B's does not.
        self._plan("A")
        self._plan("B", depends_on="A")
        heads: dict[str, str] = {}

        def push_a2() -> None:
            heads["a1"] = self._head("A")
            heads["a2"] = self.fx._push_from_human({"a2.txt": "A, after review\n"},
                                                   to="fix/A", off="origin/fix/A")

        self.after_line_fold = push_a2
        self.during["B"] = lambda: sb._git(self.fx.origin, "branch", "-f", "fix/A",
                                           heads["a1"])
        self._run(["A", "B"])
        a2 = heads["a2"]
        self.assertTrue(self.fx._ancestor(a2, self._head("B")))
        self.assertFalse(self.fx._ancestor(a2, self.fx._tip(self.line)))
        said = self._lines("issue_A's branch origin/fix/A was rewritten")
        self.assertEqual(len(said), 1, self.err)
        self.assertIn(f"after issue_B's own base {self._base('B')} took {a2[:12]}", said[0])
        self.assertEqual(self.holds, {"A": "rewritten"})

    def test_a_check_that_cannot_fetch_names_each_base_with_its_own_head(self):
        # As above, a1 on the line and a2 on B's base; S (`Stacks on: A`) is cut from the
        # line. Once B's base is pushed every fetch fails, so the end check judges nothing.
        self._plan("A")
        self._plan("B", depends_on="A")
        self._plan("S", stacks_on="A")
        heads: dict[str, str] = {}
        self.after_line_fold = lambda: heads.update(
            a1=self._head("A"), a2=self.fx._push_from_human(
                {"a2.txt": "A, after review\n"}, to="fix/A", off="origin/fix/A"))
        ids = ["A", "B", "S"]
        base_b = integrate.bundle_base_branch(self.cfg, "main", ids, "issue_B")
        real, armed = integrate._git, []

        def flaky(repo: Path, *args: str) -> int:
            if armed and args[:1] == ("fetch",):
                return 1
            rc = real(repo, *args)
            if args[:1] == ("push",) and args[-1] == base_b:
                armed.append(args)
            return rc

        self._run(ids, git=flaky)
        line, base = self._lines(UNCHECKED_LINE), self._lines(UNCHECKED_BASE)
        self.assertEqual((len(line), len(base)), (1, 1), self.err)
        self.assertIn(f"issue_A's origin/fix/A ({heads['a1'][:12]})", line[0])
        self.assertIn("every PR this run cut from the line while it held a commit of that "
                      "branch (issue_S)", line[0])
        self.assertIn(f"issue_A's origin/fix/A ({heads['a2'][:12]}), which issue_B's own base "
                      f"{base_b} took", base[0])
        for words in (*LINE_WORDS, "issue_S"):
            self.assertNotIn(words, base[0])
        self.assertEqual(self.holds, {"A": "unchecked"})

    # -- only a PR that was pushed, and only a bundle on the same target, counts -----------

    def _a2_on_bs_base_alone(self, *unpushed: str) -> dict[str, str]:
        """A and Q in wave 0; B (``Depends on: A``: a base of its own), S (``Stacks on: A``:
        the line) and Y (``Depends on: Q``) in wave 1; W (``Depends on: Y``) in wave 2; the
        publish of each of ``unpushed`` pushes nothing. The fold after wave 0 took a1; then
        fix/A moves on to a2, which B's base takes; while B builds it goes back to a1, which
        the line and S hold; while W builds it is rewritten. Returns the heads."""
        for iid, edges in (("A", {}), ("Q", {}), ("B", {"depends_on": "A"}),
                           ("S", {"stacks_on": "A"}), ("Y", {"depends_on": "Q"}),
                           ("W", {"depends_on": "Y"})):
            self._plan(iid, **edges)
        self.unpushed.update(unpushed)
        heads: dict[str, str] = {}

        def move_on() -> None:
            heads["a1"] = self._head("A")
            heads["a2"] = self.fx._push_from_human({"a2.txt": "A, after review\n"},
                                                   to="fix/A", off="origin/fix/A")

        self.after_line_fold = move_on
        self.during.update(
            B=lambda: sb._git(self.fx.origin, "branch", "-f", "fix/A", heads["a1"]),
            W=lambda: self.fx._push_from_human({"a3.txt": "A, rebuilt\n"}, to="fix/A",
                                               force=True))
        self._run(["A", "Q", "B", "S", "Y", "W"])
        self.assertTrue(self.fx._ancestor(heads["a2"], self._own_base("B")))
        self.assertTrue(self.fx._ancestor(heads["a1"], self._on_line("S")))
        return heads

    def test_a_head_only_a_published_base_took_stops_the_next_fold_for_that_base(self):
        # The line and S's PR still hold a1; B's PR holds a2, which fix/A lost: the fold
        # after wave 1 stops for B's base alone, naming neither the line's rewrite nor S.
        heads = self._a2_on_bs_base_alone()
        stop = self._lines("STOPPING")
        self.assertEqual(len(stop), 1, self.err)
        for part in (f"wave 1 did not integrate (issue_A's branch origin/fix/A was rewritten "
                     f"after issue_B's own base {self._base('B')} took {heads['a2'][:12]}",
                     "rebuild issue_B's PR on the rewritten branch"):
            self.assertIn(part, stop[0])
        for words in ("folded it onto", "issue_S"):
            self.assertNotIn(words, stop[0])
        self.assertNotIn("issue_W", self.seen, self.err)

    def test_a_bundle_that_pushed_no_branch_is_held_to_nothing_and_never_named(self):
        # B and S push nothing, so they have no PR: the fold after wave 1 does not stop for
        # the a2 B's base took, and the end check, which finds the line's a1 lost, names no
        # PR cut from the line.
        self._a2_on_bs_base_alone("B", "S")
        self.assertNotIn("STOPPING", self.err)
        self._own_base("W", "Q", "Y")
        line = self._lines(f"was rewritten after this run folded it onto {self.line}")
        self.assertEqual(len(line), 1, self.err)
        self.assertIn(f"no PR this run cut from {self.line} holds them", line[0])
        self.assertEqual(self._lines("issue_B's own base"), [], self.err)
        self.assertEqual(self.holds, {"B": "unpushed", "S": "unpushed", "A": "rewritten"})

    def test_a_bundle_on_another_target_is_neither_a_member_nor_named(self):
        # A (org/repo) and Z (org/zrepo) in wave 0. S (`Stacks on: A`) is cut from A's line,
        # which never holds Z. X (org/zrepo, `Depends on: A`) cannot hold A: it is built on
        # the real base, not stopped. Z, rewritten while X builds, is named with no PR.
        zt = self._second_target()
        self._plan("A")
        self._plan("Z", target=zt)
        self._plan("S", stacks_on="A")
        self._plan("X", target=zt, depends_on="A")
        self.during["X"] = lambda: self.fx._push_from_human({"z2.txt": "Z, rebuilt\n"},
                                                            to="fix/Z", force=True)
        self._run(["A", "Z", "S", "X"])
        self.assertNotIn("STOPPING", self.err)
        self._on_line("S")
        self._on_real_base("X")
        zline = integrate.integration_branch(self.cfg, "zbase", self.ids)
        said = self._lines(f"origin/fix/Z was rewritten after this run folded it onto {zline}")
        self.assertEqual(len(said), 1, self.err)
        self.assertIn(f"no PR this run cut from {zline} holds them", said[0])
        self.assertEqual(self.holds, {"Z": "rewritten"})


if __name__ == "__main__":
    unittest.main()
