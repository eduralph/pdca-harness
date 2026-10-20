"""A clash at the line fold that one bundle owns holds that bundle, not the run (#747).

A bundle built on a base of its own (#727) holds only what it declares, so its branch can
clash at the line fold with a bundle already on the line from an earlier wave, or with a
finished bundle the carry put there before wave 0 (#646). Before #747 the fold held only a
clash with bundles of the bundle's own wave (#674, ``integrate._held_by``); anything else
raised, the flow printed ``STOPPING``, and bundles that had nothing to do with the clash
were never built. Now such a clash is held when trial merges pin it on bundles on the
line: the branch merges cleanly onto the base alone, ``integrate._clash`` names the line
bundles whose own change it conflicts with, and the line without them merges it cleanly.
That bundle is held, its partners are named, its dependents and `Conflicts with` partners
are held, and the run goes on. Every clash the rule cannot pin on the named partners alone
still stops the run, one where another bundle on the line was built on a partner included.
A partner that is the held bundle's own prerequisite is not taken off the line: the bundle
is tried rebuilt on it, merged last, and told to rebuild on it, never to declare
`Conflicts with` it (the flip of the old stop is in tests/test_flow_same_wave_conflict.py;
the cases (4) here add a second partner, a clash it does not explain alone, and a partner
it was itself built on).

These cases drive ``flow._drive_and_act`` through real folds and real base builds on the
``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare ``origin`` and a
primary checkout), loaded by path exactly as tests/test_flow_same_wave_conflict.py does.
The build and sign-off leaves are stubbed (``flow._drive_wave`` writes a ``patch.diff``
and accepts), ``flow._publish_bundle`` cuts and pushes each PR branch from the recorded
stack-base tip, and ``gh`` is answered by a stub in front of ``subprocess.run`` (every PR
OPEN, with its branch's tip as its head). The publisher is NOT a stub (a stub turns every
fold into a dry-run). The exit code is the one ``cli._flow`` gives the same run:
``cli._results_rc`` over the results and holds.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run, and fail on their assertions: the hold cases end
in ``wave N did not integrate … STOPPING`` with the unrelated chain never built. Each
``_merges`` / ``_rebuilt`` call states a fact of the fixture, tried by plain git in the
scratch clone after the run; what the run did is read from its results, holds and stderr.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_cross_wave_fold_holds
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
from unittest import mock

from pdca_harness import cli, flow, integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-20"


def _ten(what: str) -> str:
    """A ten-line file for the base, each line long enough that rewriting seven of them takes
    the file below git's rename-similarity threshold (:func:`_rewrite`)."""
    return "".join(f"line {i} of the {what} file, as the base has it\n" for i in range(1, 11))


TEN = _ten("shared")


def _edit(text: str, **lines: str) -> str:
    """``text`` with line ``n`` (``l<n>=...``) replaced."""
    out = text.splitlines(keepends=True)
    for key, new in lines.items():
        out[int(key.removeprefix("l")) - 1] = new + "\n"
    return "".join(out)


def _rewrite(text: str, who: str) -> str:
    """``text`` (from :func:`_ten`) with its first seven lines rewritten by ``who``."""
    return _edit(text, **{f"l{i}": f"{who} rewrote line {i} entirely: something else altogether"
                          for i in range(1, 8)})


class CrossWaveFoldHolds(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        # id → path → its text on the bundle's branch (None: the branch deletes it)
        self.changes: dict[str, dict[str, str | None]] = {}
        self.cut_from: dict[str, str] = {}           # id → where publish cuts it (override)
        # n → run once, after the run's n-th fold (1: its first)
        self.after_fold: dict[int, Callable[[], None]] = {}
        self.seen: dict[str, tuple[str, str]] = {}       # bundle → (stack base, tip) when built
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _on_base(self, files: dict[str, str]) -> None:
        """Commit ``files`` on origin's ``main`` (the base every bundle targets)."""
        for name, text in files.items():
            (self.fx.primary / name).write_text(text, encoding="utf-8")
        sb._git(self.fx.primary, "add", "--all")
        sb._git(self.fx.primary, "commit", "-q", "-m", "base files")
        sb._git(self.fx.primary, "push", "-q", "origin", "main")

    def _plan(self, iid: str, files: dict[str, str | None], *, depends_on: str = "",
              stacks_on: str = "") -> None:
        sb._brief(self.cfg.bundle(iid),
                  extra=(f"- **Depends on:** {depends_on}\n" if depends_on else "")
                  + (f"- **Stacks on:** {stacks_on}\n" if stacks_on else ""))
        self.changes[iid] = files

    def _push_branch(self, iid: str, cut_from: str) -> None:
        """What `publish` does for an accepted bundle (``StackFoldGit._publish``, which
        cannot delete a file): cut ``fix/<iid>`` off ``cut_from``, make its change, commit
        it signed off, push it, and record it in publish.json and patch.diff."""
        d, primary, branch = self.cfg.bundle(iid), self.fx.primary, f"fix/{iid}"
        sb._git(primary, "fetch", "-q", "origin")
        sb._git(primary, "checkout", "-q", "-B", branch, cut_from)
        for name, text in self.changes[iid].items():
            if text is None:
                (primary / name).unlink()
            else:
                (primary / name).write_text(text, encoding="utf-8")
        sb._git(primary, "add", "--all")
        sb._git(primary, "commit", "-q", "-s", "-m", f"fix {iid}")
        (d / "patch.diff").write_text(sb._git(primary, "diff", "HEAD~1", "HEAD"),
                                      encoding="utf-8")
        sb._git(primary, "push", "-q", "origin", branch)
        sb._git(primary, "checkout", "-q", "main")
        mode = "new-pr" if cut_from == "origin/main" else "stacked-pr"
        (d / "publish.json").write_text(json.dumps(
            {"mode": mode, "branch": branch, "base": "main", "repo": "org/repo",
             "pr_url": f"https://example.test/pr/{iid}"}), encoding="utf-8")

    def _finished(self, iid: str, files: dict[str, str]) -> None:
        """``iid`` finished in an earlier run of the batch: published off main, accepted."""
        self._plan(iid, files)
        self._push_branch(iid, "origin/main")
        sb._accept(self.cfg.bundle(iid))

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
        """Build and accept each bundle, recording what it was built on. Its patch.diff
        names the files it changes (publish records the real diff)."""
        for d in wave:
            iid = d.name.removeprefix("issue_")
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d))
            (d / "patch.diff").write_text("".join(
                sb._new_file_patch(n, "changed") for n in self.changes[iid]), encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        """What publish does: cut the PR branch from the recorded stack-base tip (else that
        branch, else the base; ``cut_from`` overrides), push it, record it. The run's brief
        is kept as it was."""
        iid = d.name.removeprefix("issue_")
        tip, base = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self._push_branch(iid, self.cut_from.get(iid) or tip
                          or (f"origin/{base}" if base else "origin/main"))
        (d / "brief.md").write_text(brief, encoding="utf-8")
        return True

    def _run(self, ids: list[str], *, finished: tuple[str, ...] = ()) -> None:
        """One stack-mode run asked for ``ids``, driving all of them but ``finished``."""
        self.ids = list(ids)
        self.line = integrate.integration_branch(self.cfg, "main", ids)
        self.holds: dict[str, str] = {}
        real_fold = integrate.fold
        folds = [0]

        def fold(*a, **kw):
            out = real_fold(*a, **kw)
            folds[0] += 1
            self.after_fold.pop(folds[0], lambda: None)()
            return out

        self._err = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(integrate, "fold", fold))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(self._err))
            self.results = flow._drive_and_act(
                self.cfg, [self.cfg.bundle(i) for i in ids if i not in finished],
                do_publish=True, do_act=False, by="T", today=TODAY, batch=ids,
                holds=self.holds)
        self.err = self._err.getvalue()
        self.rc = cli._results_rc(self.results, holds=self.holds)
        self.assertNotIn("Traceback", self.err)

    # -- helpers ---------------------------------------------------------------------------

    def _head(self, iid: str) -> str:
        return self.fx._tip(f"fix/{iid}")

    def _held(self, iid: str, *parts: str) -> str:
        """ONE stderr line says ``issue_<iid>`` was held out of the fold, with each of
        ``parts``; its branch is on no line the run pushed. Returns that line."""
        lines = [x for x in self.err.splitlines()
                 if x.startswith(f"flow: issue_{iid} held out of the integration fold — ")]
        self.assertEqual(len(lines), 1, self.err)
        for part in parts:
            self.assertIn(part, lines[0])
        self.assertFalse(self.fx._ancestor(self._head(iid), self.fx._tip(self.line)))
        return lines[0]

    def _report(self, iid: str, *parts: str) -> str:
        """ONE end-of-run stderr line says ``issue_<iid>`` was held out of the line, with each
        of ``parts``. Returns that line."""
        lines = [x for x in self.err.splitlines()
                 if x.startswith(f"flow: issue_{iid} was held out of this run's integration "
                                 f"line — ")]
        self.assertEqual(len(lines), 1, self.err)
        for part in parts:
            self.assertIn(part, lines[0])
        return lines[0]

    def _not_stopped(self) -> None:
        self.assertNotIn("did not integrate", self.err)
        self.assertNotIn("STOPPING", self.err)

    def _stopped(self, k: int, iid: str) -> None:
        """The wave-``k`` fold stopped the run on ``issue_<iid>``'s branch, holding nothing."""
        self.assertIn(f"wave {k} did not integrate (issue_{iid}'s branch", self.err)
        self.assertIn("STOPPING", self.err)
        self.assertNotIn(f"issue_{iid} held out of the integration fold", self.err)
        self.assertNotIn(f"issue_{iid} was held", self.err)

    def _complete(self, *ids: str) -> None:
        """Each of ``ids`` was built and is COMPLETE."""
        for iid in ids:
            self.assertIn(f"issue_{iid}", self.seen, self.err)
            self.assertEqual(self.results[iid], state.COMPLETE, self.err)

    def _merges(self, onto: str, *iids: str) -> int | None:
        """Where merging the branches of ``iids`` one after another onto ``onto`` first
        conflicts (its index), or None — the fixture's facts, tried in the scratch clone."""
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "--detach", onto)
        try:
            for i, iid in enumerate(iids):
                if sb._run(human, "merge", "-q", "--no-ff", "--no-edit", "-m", "trial",
                           f"origin/fix/{iid}").returncode != 0:
                    return i
            return None
        finally:
            sb._run(human, "reset", "-q", "--hard")
            sb._git(human, "checkout", "-q", "--detach", "origin/main")

    def _rebuilt(self, iid: str, on: str) -> str:
        """``iid``'s branch rebuilt on ``on``'s, as its owner is told to: ``on``'s branch with
        ``iid``'s merged in, ``iid``'s side winning every clash. Pushed to origin, once the
        run is over, as ``fix/<iid>-on-<on>``, so :func:`_merges` can try it (the fixture's
        facts); returns ``<iid>-on-<on>``."""
        human, name = self.fx.human, f"{iid}-on-{on}"
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", "rebuilt", f"origin/fix/{on}")
        sb._git(human, "merge", "-q", "--no-ff", "--no-edit", "-X", "theirs",
                f"origin/fix/{iid}")
        sb._git(human, "push", "-q", "origin", f"HEAD:refs/heads/fix/{name}")
        sb._git(human, "checkout", "-q", "--detach", "origin/main")
        return name

    # -- (1) an in-batch partner of an earlier wave ------------------------------------------

    def test_a_clash_with_an_earlier_wave_bundle_holds_only_that_bundle(self):
        # Y is built on a base of its own holding only Q, its prerequisite, so not A, whose
        # shared.txt the line took in wave 0: the fold after wave 1 cannot merge Y. Before:
        # STOPPING, and U, at the end of an unrelated chain, stayed PLANNED.
        self._plan("A", {"shared.txt": "a\n"})
        self._plan("Q", {"q.txt": "q\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("Y", {"shared.txt": "y\n"}, depends_on="Q")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        ids = ["A", "Q", "W", "Y", "V", "U"]
        self._run(ids)

        self._not_stopped()
        base, tip = self.seen["issue_Y"]
        self.assertEqual(base, integrate.bundle_base_branch(self.cfg, "main", ids, "issue_Y"))
        self.assertTrue(self.fx._ancestor(self._head("Q"), tip))
        self.assertFalse(self.fx._ancestor(self._head("A"), tip))
        line = self._held("Y", "its change conflicts with issue_A's, already on the line from "
                               "an earlier wave", "(conflicts in shared.txt)",
                          "`Conflicts with` partners are held")
        for other in ("issue_Q", "issue_W", "issue_V"):
            self.assertNotIn(other, line)
        end = self._report("Y", "conflicts with issue_A's (shared.txt)",
                           "Proposed, for you to confirm: declare `Conflicts with: A` in "
                           "issue_Y's brief, then rebuild issue_Y on a base that holds issue_A")
        self.assertNotIn("prerequisite", line + end)       # A is not Y's prerequisite
        self.assertNotIn("same wave", self.err)
        self._complete("A", "Q", "W", "Y", "V", "U")
        self.assertTrue(self.fx._ancestor(self._head("V"), self.seen["issue_U"][1]))
        self.assertEqual(self.holds, {"Y": "conflict"})
        self.assertEqual(self.rc, 3, self.err)

    # -- (2) a finished bundle the carry put on the line ---------------------------------------

    def test_a_clash_with_a_finished_bundle_carried_onto_the_line_names_it(self):
        # F finished in an earlier run; Z, unrelated to Y, depends on it, so the carry puts F
        # on the line before wave 0 (#646). Y does not depend on F: built on the base, it
        # clashes with F there. The in-run fold never merges F (it is not accepted this
        # run), so the partner must come from what the carry put on the line. Y is folded
        # before Z (one wave, name order), so no bundle on the line then was built on F.
        self._finished("F", {"shared.txt": "f\n"})
        self._plan("Z", {"z.txt": "z\n"}, depends_on="F")
        self._plan("W", {"w.txt": "w\n"})
        self._plan("Y", {"shared.txt": "y\n"})
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        ids = ["F", "Z", "W", "Y", "V", "U"]
        self._run(ids, finished=("F",))

        self.assertIn("flow: carried issue_F (finished in an earlier run) onto", self.err)
        self._not_stopped()
        self.assertFalse(self.fx._ancestor(self._head("F"), self._head("Y")))
        line = self._held("Y", "its change conflicts with issue_F's, already on the line, "
                               "finished in an earlier run", "(conflicts in shared.txt)")
        self.assertNotIn("no bundle", line)
        self.assertNotIn("account", line)
        for other in ("issue_Z", "issue_W", "issue_V"):
            self.assertNotIn(other, line)
        end = self._report("Y", "conflicts with issue_F's (shared.txt)",
                           "Proposed, for you to confirm: declare `Conflicts with: F` in "
                           "issue_Y's brief, then rebuild issue_Y on a base that holds issue_F")
        self.assertNotIn("prerequisite", line + end)       # F is not Y's prerequisite
        self.assertNotIn("same wave", self.err)
        self._complete("Z", "W", "Y", "V", "U")
        self.assertTrue(self.fx._ancestor(self._head("F"), self._head("Z")))   # built on F
        self.assertTrue(self.fx._ancestor(self._head("Z"), self.fx._tip(self.line)))
        self.assertNotIn("F", self.results)                  # finished: not driven this run
        self.assertEqual(self.holds, {"Y": "conflict"})
        self.assertEqual(self.rc, 3, self.err)

    # -- (3) clashes the rule cannot pin on the named partners alone still stop ---------------

    def test_a_clash_two_line_bundles_cause_only_together_still_stops(self):
        """Fails rule (e): the partner named alone (A) does not explain the whole clash.
        child-2 (#748, joint attribution) flips this to a hold naming A, P and R.

        P renames shared.txt to moved.txt; R rewrites most of shared.txt; H changes its last
        line, which merges onto main plus either of them alone, but not after both. H also
        adds other.txt, as A does: a clash with A alone. So A is named, but the line
        without A still does not merge H — what is left is P and R's together."""
        self._on_base({"shared.txt": TEN})
        self._plan("A", {"other.txt": "a\n"})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R")})
        self._plan("K", {"k.txt": "k\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it"),
                         "other.txt": "h\n"}, depends_on="K")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["A", "P", "R", "K", "W", "H", "V", "U"])

        self.assertEqual(self._merges("origin/main", "A", "H"), 1)       # the fixture's facts
        self.assertIsNone(self._merges("origin/main", "P", "H"))
        self.assertIsNone(self._merges("origin/main", "R", "H"))
        self.assertEqual(self._merges("origin/main", "K", "P", "R", "W", "H"), 4)
        self._stopped(1, "H")
        self.assertEqual(self.results["U"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_clash_with_a_partner_whose_head_no_longer_fits_the_base_still_stops(self):
        """Fails rule (d): A's branch, on the line, no longer merges onto the base alone, so
        no trial can name it (the one that would merges A's head onto the base first).
        child-3 (#749, a partner stranded by a moved base) flips this to a hold.

        After wave 0's fold, main gets an a.txt of its own, so A's branch no longer merges
        onto main. Y, built in wave 1 on that moved main, carries main's a.txt and also
        changes shared.txt, as A does: it clashes with A on the line. A cannot be tried
        against the base it no longer fits, so the run still stops. V is `Stacks on: W`: cut
        from the line, it holds no part of main's move, so Y is the bundle the fold stops on."""
        self._on_base({"shared.txt": "x\n"})
        self._plan("A", {"a.txt": "a\n", "shared.txt": "a\n"})
        self._plan("Q", {"q.txt": "q\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("Y", {"shared.txt": "y\n"}, depends_on="Q")
        self._plan("V", {"v.txt": "v\n"}, stacks_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        moved: list[str] = []
        self.after_fold[1] = lambda: moved.append(self.fx._push_from_human(
            {"a.txt": "main's own\n"}, to="main"))
        self._run(["A", "Q", "W", "Y", "V", "U"])

        self.assertTrue(self.fx._ancestor(moved[0], self.seen["issue_Y"][1]))  # the facts
        self.assertEqual(self._merges("origin/main", "A"), 0)
        self.assertIsNone(self._merges("origin/main", "Y"))
        self._stopped(1, "Y")
        self.assertEqual(self.results["U"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_clash_with_a_partner_another_line_bundle_was_built_on_still_stops(self):
        """Fails rule (e): P, on the line, was built on A, the partner named, so the line
        without A still brings A back, and A alone is not shown to explain the clash. child-2
        (#748) may refine this.

        Case (1), plus P (`Depends on: A`, a file of its own), built on a base of its own
        holding A and folded just before Y. A is not Y's prerequisite."""
        self._plan("A", {"shared.txt": "a\n"})
        self._plan("Q", {"q.txt": "q\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("P", {"p.txt": "p\n"}, depends_on="A")
        self._plan("Y", {"shared.txt": "y\n"}, depends_on="Q")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["A", "Q", "W", "P", "Y", "V", "U"])

        self.assertTrue(self.fx._ancestor(self._head("A"), self.seen["issue_P"][1]))  # facts
        self.assertFalse(self.fx._ancestor(self._head("A"), self.seen["issue_Y"][1]))
        self.assertIsNone(self._merges("origin/main", "Y"))
        self.assertEqual(self._merges("origin/main", "A", "Y"), 1)
        self.assertEqual(self._merges("origin/main", "Q", "W", "P", "V", "Y"), 4)
        self._stopped(1, "Y")
        self.assertEqual(self.results["U"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_clash_with_a_carried_partner_that_a_line_bundle_holds_still_stops(self):
        """Fails rule (e): Z depends on F, finished and carried onto the line, so Z is cut
        from the line (#727: an edge to a finished id is a seam) and holds F. Z is on the
        line when Y is folded, so the line without F still brings F back. child-2 (#748)
        may refine this.

        Case (2), but Y depends on Q, so it is built in wave 1, after Z."""
        self._finished("F", {"shared.txt": "f\n"})
        self._plan("Z", {"z.txt": "z\n"}, depends_on="F")
        self._plan("Q", {"q.txt": "q\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("Y", {"shared.txt": "y\n"}, depends_on="Q")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["F", "Z", "Q", "W", "Y", "V", "U"], finished=("F",))

        self.assertIn("flow: carried issue_F (finished in an earlier run) onto", self.err)
        self.assertEqual(self.seen["issue_Z"][0], self.line)                 # the facts
        self.assertTrue(self.fx._ancestor(self._head("F"), self._head("Z")))
        self.assertFalse(self.fx._ancestor(self._head("F"), self.seen["issue_Y"][1]))
        self.assertEqual(self._merges("origin/main", "F", "Y"), 1)
        self.assertEqual(self._merges("origin/main", "Q", "W", "Z", "Y"), 3)
        self._stopped(1, "Y")
        self.assertEqual(self.results["U"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    # -- (4) its own prerequisite, and more on the line ------------------------------------

    def test_a_clash_with_its_prerequisite_and_a_bundle_built_on_it_names_both(self):
        """21 depends on 10 but is cut from the plain base, so it clashes with 10, its own
        prerequisite, on shared.txt. 20, built on 10 and folded just before 21, adds y.txt,
        as 21 does: rebuilt on 10, 21 still clashes with 20's own change, so the trial that
        names partners names 20 too — it is a partner, not part of the rest of the line.
        Without 20, 21 rebuilt on 10 merges: 10 and 20 explain the whole clash. 21 is held
        naming both; its owner is told to rebuild it on 10 (never `Conflicts with: 10`) and
        proposed `Conflicts with: 20`, on a base that holds both. W→V→U goes on."""
        self._on_base({"shared.txt": "x\n"})
        self._plan("10", {"shared.txt": "ten\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("20", {"twenty.txt": "twenty\n", "y.txt": "twenty\n"}, depends_on="10")
        self._plan("21", {"shared.txt": "twenty-one\n", "y.txt": "twenty-one\n"},
                   depends_on="10")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self.cut_from["21"] = "origin/main"
        self._run(["10", "W", "20", "21", "V", "U"])

        self.assertTrue(self.fx._ancestor(self._head("10"), self._head("20")))   # the facts
        self.assertFalse(self.fx._ancestor(self._head("10"), self._head("21")))
        self.assertEqual(self._merges("origin/main", "10", "21"), 1)
        rebuilt = self._rebuilt("21", on="10")
        self.assertEqual(self._merges("origin/main", "10", "W", "20", rebuilt), 3)
        self.assertIsNone(self._merges("origin/main", "10", "W", rebuilt))
        self._not_stopped()
        line = self._held("21", "its change conflicts with issue_20's, merged before it in the "
                                "same wave 1, and issue_10's, already on the line from an "
                                "earlier wave", "(conflicts in shared.txt, y.txt)",
                          "issue_10 is issue_21's own prerequisite: rebuild issue_21's branch "
                          "on issue_10's")
        self._report("21", "conflicts with issue_10's, issue_20's (shared.txt, y.txt). "
                           "issue_10 is issue_21's own prerequisite. Proposed, for you to "
                           "confirm: declare `Conflicts with: 20` in issue_21's brief, then "
                           "rebuild issue_21 on a base that holds issue_10, issue_20.")
        self.assertNotIn("Conflicts with: 10", self.err)     # 10 is 21's own prerequisite
        self.assertNotIn("issue_W", line)
        self._complete("10", "W", "20", "21", "V", "U")
        self.assertEqual(self.holds, {"21": "conflict"})
        self.assertEqual(self.rc, 3, self.err)

    def test_a_clash_its_prerequisite_does_not_explain_alone_still_stops(self):
        """Fails rule (e): 21 depends on 10 but is cut from the plain base, so it clashes with
        10, its own prerequisite, on other.txt — and with shared.txt, which the rest of the
        line changes only together: P renames it (to moved.txt), and 20, built on 10 and
        folded just before 21, rewrites most of it; 21 changes its last line. Neither P nor
        20 clashes with 21 rebuilt on 10 alone, so only 10 is named, but rebuilt on 10, 21
        still does not merge onto the line: 10 does not explain the whole clash, and the run
        stops. (Merging 21 rebuilt on 10 first and the rest of the line after it would find
        no clash: the rename is only seen in that order.) child-2 (#748, joint attribution)
        may refine this."""
        self._on_base({"shared.txt": TEN})
        self._plan("10", {"other.txt": "ten\n"})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("20", {"shared.txt": _rewrite(TEN, "20")}, depends_on="10")
        self._plan("21", {"shared.txt": _edit(TEN, l10="line 10, as 21 has it"),
                          "other.txt": "twenty-one\n"}, depends_on="10")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self.cut_from["21"] = "origin/main"
        self._run(["10", "P", "W", "20", "21", "V", "U"])

        self.assertTrue(self.fx._ancestor(self._head("10"), self._head("20")))   # the facts
        self.assertFalse(self.fx._ancestor(self._head("10"), self._head("21")))
        self.assertEqual(self._merges("origin/main", "10", "21"), 1)
        self.assertIsNone(self._merges("origin/main", "P", "21"))
        rebuilt = self._rebuilt("21", on="10")
        self.assertIsNone(self._merges("origin/main", "10", "P", "W", rebuilt))
        self.assertIsNone(self._merges("origin/main", "10", "W", "20", rebuilt))
        self.assertEqual(self._merges("origin/main", "10", "P", "W", "20", rebuilt), 4)
        self.assertIsNone(self._merges(f"origin/fix/{rebuilt}", "P", "W", "20"))
        self._stopped(1, "21")
        self.assertIn("(conflicts in other.txt, shared.txt)", self.err)
        self.assertEqual(self.results["U"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    def test_a_clash_with_a_partner_its_prerequisite_was_built_on_still_stops(self):
        """Fails rule (e): 21 depends on 10, and 10 on A, but 21 is cut from the plain base,
        so it clashes with A (a.txt) and with 10's own change (shared.txt): both are named.
        10 is 21's own prerequisite, so it stays on the line — and 10 was built on A, so the
        line without A still brings A back, and the run stops, as for any bundle left on the
        line built on a partner. The trial alone would not stop it: rebuilt on 10, 21 holds
        A too. child-2 (#748) may refine this. W→V→U→X is unrelated; 21 is in wave 2, so the
        fold that meets it runs before X's wave."""
        self._on_base({"shared.txt": "x\n"})
        self._plan("A", {"a.txt": "a\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("10", {"shared.txt": "ten\n"}, depends_on="A")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("21", {"a.txt": "twenty-one\n", "shared.txt": "twenty-one\n"},
                   depends_on="10")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._plan("X", {"x.txt": "x\n"}, depends_on="U")
        self.cut_from["21"] = "origin/main"
        self._run(["A", "W", "10", "V", "21", "U", "X"])

        self.assertTrue(self.fx._ancestor(self._head("A"), self._head("10")))    # the facts
        self.assertFalse(self.fx._ancestor(self._head("A"), self._head("21")))
        self.assertEqual(self._merges("origin/main", "A", "21"), 1)
        rebuilt = self._rebuilt("21", on="10")
        self.assertTrue(self.fx._ancestor(self._head("A"), self._head(rebuilt)))
        self.assertIsNone(self._merges("origin/main", "W", "10", "V", rebuilt))
        self._stopped(2, "21")
        self.assertIn("(conflicts in a.txt, shared.txt)", self.err)
        self._complete("A", "W", "10", "V", "U")
        self.assertEqual(self.results["X"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)


if __name__ == "__main__":
    unittest.main()
