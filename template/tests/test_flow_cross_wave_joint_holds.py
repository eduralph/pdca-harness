"""A fold clash that line bundles make only together holds the bundle and names them (#748).

#747 holds a bundle whose clash at the line fold one bundle already on the line explains
alone (``integrate._held_by_line``). A clash that no single bundle explains — the branch
merges cleanly after either of two line bundles, but not after both — still stopped the
run: ``STOPPING``, and bundles with nothing to do with it left PLANNED. Now the fold names
each bundle in a minimal set the branch's own change does not merge after, of one bundle
or two (``integrate._together``), and holds the branch when the line without them takes
it. A bundle in no such set is never named; a clash only three of them make together is
not attributed and still stops the run.

The joint clash is a rename: P renames ``shared.txt`` to ``moved.txt``, R rewrites most of
``shared.txt``, and the held branch changes its last line. After P alone, git follows the
rename and carries the edit over; after R alone the edits are on different lines. After
both, ``moved.txt`` is too unlike the base's ``shared.txt`` for git to see a rename, so the
branch's edit meets a deleted file: a conflict.

The trials that look for those sets try the branch's own change only
(``integrate._own_change``): for a branch built on a bundle on the line, what it holds of
that bundle is merged first, so it is on both sides of the trial. Case (6) pins it. Tried
from the base, a branch built on a bundle that renames a file, which then rewrites most of
the renamed file, deletes the old file as git sees it; a line bundle that edits the old
file then clashes with it, and would be named for a clash the branch's own change does not
have.

These cases drive ``flow._drive_and_act`` through real folds and real base builds on the
``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare ``origin`` and a
primary checkout), loaded by path exactly as tests/test_flow_same_wave_conflict.py does,
with the stubs of tests/test_flow_cross_wave_fold_holds.py: the build and sign-off leaves
are stubbed, ``flow._publish_bundle`` cuts and pushes each PR branch from the recorded
stack-base tip, and ``gh`` is answered OPEN. The publisher is NOT a stub (a stub turns
every fold into a dry-run). The exit code is the one ``cli._flow`` gives the same run:
``cli._results_rc`` over the results and holds.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run, and the hold cases fail on their assertions:
they end in ``wave 1 did not integrate … STOPPING`` with the unrelated chain never built.
Each ``_merges`` call states a fact of the fixture, tried by plain git in the scratch
clone after the run; what the run did is read from its results, holds and stderr.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_cross_wave_joint_holds
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import subprocess
import unittest
from collections.abc import Iterable
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
    """A ten-line file for the base, each line long enough that rewriting six or seven of
    them takes the file below git's rename-similarity threshold (:func:`_rewrite`)."""
    return "".join(f"line {i} of the {what} file, as the base has it\n" for i in range(1, 11))


TEN = _ten("shared")
TWO = _ten("second")


def _edit(text: str, **lines: str) -> str:
    """``text`` with line ``n`` (``l<n>=...``) replaced."""
    out = text.splitlines(keepends=True)
    for key, new in lines.items():
        out[int(key.removeprefix("l")) - 1] = new + "\n"
    return "".join(out)


def _rewrite(text: str, who: str, lines: Iterable[int] = range(1, 8)) -> str:
    """``text`` (from :func:`_ten`) with ``lines`` (by default its first seven) rewritten by
    ``who``."""
    return _edit(text, **{f"l{i}": f"{who} rewrote line {i} entirely: something else altogether"
                          for i in lines})


class CrossWaveJointHolds(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        # id → path → its text on the bundle's branch (None: the branch deletes it)
        self.changes: dict[str, dict[str, str | None]] = {}
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

    def _plan(self, iid: str, files: dict[str, str | None], *, depends_on: str = "") -> None:
        sb._brief(self.cfg.bundle(iid),
                  extra=f"- **Depends on:** {depends_on}\n" if depends_on else "")
        self.changes[iid] = files

    def _push_branch(self, iid: str, cut_from: str) -> None:
        """What `publish` does for an accepted bundle: cut ``fix/<iid>`` off ``cut_from``,
        make its change (deleting a file where it says None), commit it signed off, push
        it, and record it in publish.json and patch.diff."""
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
        branch, else the base), push it, record it. The run's brief is kept as it was."""
        iid = d.name.removeprefix("issue_")
        tip, base = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self._push_branch(iid, tip or (f"origin/{base}" if base else "origin/main"))
        (d / "brief.md").write_text(brief, encoding="utf-8")
        return True

    def _run(self, ids: list[str]) -> None:
        """One stack-mode run asked for ``ids``, driving all of them."""
        self.line = integrate.integration_branch(self.cfg, "main", ids)
        self.holds: dict[str, str] = {}
        self._err = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(self._err))
            self.results = flow._drive_and_act(
                self.cfg, [self.cfg.bundle(i) for i in ids], do_publish=True, do_act=False,
                by="T", today=TODAY, batch=ids, holds=self.holds)
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

    def _joint_held(self, iid: str, partners: list[str], files: str, *,
                    unrelated: tuple[str, ...]) -> None:
        """The run held ``issue_<iid>`` naming exactly ``partners`` (ids, in merge order) as
        already on the line from an earlier wave, proposed `Conflicts with` exactly them,
        and named none of ``unrelated``; W→V→U went on; the run exits 3."""
        self._not_stopped()
        names = ", ".join(f"issue_{p}'s" for p in partners)
        line = self._held(iid, f"its change conflicts with {names}, already on the line from "
                               f"an earlier wave (conflicts in {files})")
        end = self._report(iid, f"conflicts with {names} ({files}). Proposed, for you to "
                                f"confirm: declare `Conflicts with: {', '.join(partners)}` in "
                                f"issue_{iid}'s brief, then rebuild issue_{iid} on a base that "
                                f"holds {', '.join(f'issue_{p}' for p in partners)}.")
        for other in unrelated:
            self.assertNotIn(f"issue_{other}", line + end)
        self._complete("W", "V", "U")
        self.assertEqual(self.holds, {iid: "conflict"})
        self.assertEqual(self.rc, 3, self.err)

    # -- (1) a clash two line bundles make only together ---------------------------------------

    def test_a_clash_two_line_bundles_make_only_together_holds_naming_both(self):
        # P and R (wave 0) are on the line when H (wave 1, built on a base holding only K) is
        # folded. H merges after either alone, not after both. Before: STOPPING, U PLANNED.
        self._on_base({"shared.txt": TEN})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R")})
        self._plan("K", {"k.txt": "k\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it")}, depends_on="K")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["P", "R", "K", "W", "H", "V", "U"])

        self.assertIsNone(self._merges("origin/main", "K", "P", "H"))     # the fixture's facts
        self.assertIsNone(self._merges("origin/main", "K", "R", "H"))
        self.assertEqual(self._merges("origin/main", "K", "P", "R", "H"), 3)
        self._joint_held("H", ["P", "R"], "shared.txt", unrelated=("K", "W", "V"))
        self._complete("P", "R", "K", "H")

    # -- (2) a line bundle in no conflicting set is never named -------------------------------

    def test_a_line_bundle_the_joint_clash_does_not_need_is_not_named(self):
        # Case (1) plus X, on the line from wave 0, which touches files H does not.
        self._on_base({"shared.txt": TEN, "x.txt": "x\n"})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R")})
        self._plan("X", {"x.txt": "X changed it\n", "x2.txt": "x\n"})
        self._plan("K", {"k.txt": "k\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it")}, depends_on="K")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["P", "R", "X", "K", "W", "H", "V", "U"])

        self.assertIsNone(self._merges("origin/main", "K", "X", "H"))     # the fixture's facts
        self.assertIsNone(self._merges("origin/main", "K", "P", "X", "H"))
        self.assertIsNone(self._merges("origin/main", "K", "R", "X", "H"))
        self.assertEqual(self._merges("origin/main", "K", "P", "R", "X", "H"), 4)
        self._joint_held("H", ["P", "R"], "shared.txt", unrelated=("X", "K", "W", "V"))
        self.assertNotIn("Conflicts with: P, R, X", self.err)
        self._complete("X")

    # -- (3) a direct clash and a joint one in the same file ----------------------------------

    def test_a_direct_and_a_joint_clash_in_one_file_name_all_three(self):
        # A edits line 10 of shared.txt, as H does: a clash with A alone. P and R make the
        # other one together, in the same file. All three are named, and H rebuilt on a base
        # that holds them, as proposed, merges onto the line the run pushed.
        self._on_base({"shared.txt": TEN})
        self._plan("A", {"shared.txt": _edit(TEN, l10="line 10, as A has it")})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R")})
        self._plan("K", {"k.txt": "k\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it")}, depends_on="K")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["A", "P", "R", "K", "W", "H", "V", "U"])

        self.assertEqual(self._merges("origin/main", "K", "A", "H"), 2)   # the fixture's facts
        self.assertIsNone(self._merges("origin/main", "K", "P", "H"))
        self.assertIsNone(self._merges("origin/main", "K", "R", "H"))
        self.assertEqual(self._merges("origin/main", "K", "P", "R", "H"), 3)
        self._joint_held("H", ["A", "P", "R"], "shared.txt", unrelated=("K", "W", "V"))

        # The proposed rebuild: H's change made again on a base that holds A, P and R (its
        # owner takes its own line 10 over A's, in the file P moved), tried onto the line.
        human = self.fx.human
        sb._git(human, "fetch", "-q", "origin")
        sb._git(human, "checkout", "-q", "-B", "rebuilt", "origin/fix/K")
        for p in ("A", "P", "R"):
            sb._git(human, "merge", "-q", "--no-ff", "--no-edit", f"origin/fix/{p}")
        moved = (human / "moved.txt").read_text(encoding="utf-8")
        (human / "moved.txt").write_text(_edit(moved, l10="line 10, as H has it"),
                                         encoding="utf-8")
        sb._git(human, "commit", "-q", "-am", "H rebuilt")
        sb._git(human, "push", "-q", "origin", "HEAD:refs/heads/fix/H-rebuilt")
        sb._git(human, "checkout", "-q", "--detach", "origin/main")
        self.assertIsNone(self._merges(f"origin/{self.line}", "H-rebuilt"))
        self.assertEqual(self._merges(f"origin/{self.line}", "H"), 0)

    # -- (4) several joint clashes at once ------------------------------------------------------

    def test_two_joint_clashes_in_different_files_name_both_pairs_and_nothing_else(self):
        # P and R make one together in shared.txt, M and N another in two.txt; X is unrelated.
        # The wave merges in name order, so the partners are named M, N, P, R.
        self._on_base({"shared.txt": TEN, "two.txt": TWO})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R")})
        self._plan("M", {"two.txt": None, "moved2.txt": TWO})
        self._plan("N", {"two.txt": _rewrite(TWO, "N")})
        self._plan("X", {"x.txt": "x\n"})
        self._plan("K", {"k.txt": "k\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it"),
                         "two.txt": _edit(TWO, l10="line 10, as H has it")}, depends_on="K")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["P", "R", "M", "N", "X", "K", "W", "H", "V", "U"])

        self.assertIsNone(self._merges("origin/main", "K", "P", "N", "H"))   # the facts
        self.assertIsNone(self._merges("origin/main", "K", "M", "R", "H"))
        self.assertEqual(self._merges("origin/main", "K", "P", "R", "H"), 3)
        self.assertEqual(self._merges("origin/main", "K", "M", "N", "H"), 3)
        self._joint_held("H", ["M", "N", "P", "R"], "shared.txt, two.txt",
                         unrelated=("X", "K", "W", "V"))

    # -- (5) a clash only three line bundles make together still stops ------------------------

    def test_a_clash_only_three_line_bundles_make_together_still_stops(self):
        # P renames shared.txt; R and S each rewrite three of its lines, apart (line 4 lies
        # between them, so R and S merge with each other). After P and either
        # one, git still follows the rename; after all three it does not. No set of one or
        # two explains the clash, so it is not attributed: the run stops as before.
        self._on_base({"shared.txt": TEN})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R", range(1, 4))})
        self._plan("S", {"shared.txt": _rewrite(TEN, "S", range(5, 8))})
        self._plan("K", {"k.txt": "k\n"})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it")}, depends_on="K")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["P", "R", "S", "K", "W", "H", "V", "U"])

        for pair in (("P", "R"), ("P", "S"), ("R", "S")):                 # the fixture's facts
            self.assertIsNone(self._merges("origin/main", "K", *pair, "H"), pair)
        self.assertEqual(self._merges("origin/main", "K", "P", "R", "S", "H"), 4)
        self.assertIn("wave 1 did not integrate (issue_H's branch", self.err)
        self.assertIn("STOPPING", self.err)
        self.assertNotIn("issue_H held out of the integration fold", self.err)
        self.assertNotIn("issue_H was held", self.err)
        self.assertEqual(self.results["U"], state.PLANNED, self.err)
        self.assertEqual(self.holds, {})
        self.assertEqual(self.rc, 1, self.err)

    # -- (6) a branch built on a line bundle is tried on its own change only ------------------

    def test_a_branch_built_on_a_line_bundle_is_tried_on_its_own_change_only(self):
        # H is built on L (it depends on L), which renames two.txt to moved2.txt; H rewrites
        # lines 1-7 of moved2.txt and edits shared.txt's last line. X, on the line, edits
        # two.txt's last line. All of H's branch, tried from the base, is too unlike two.txt
        # for git to see the rename, so X's edit meets a deleted file. H's own change, tried
        # on L's head, where H started, has no clash with X: X's edit lands on line 10 of
        # moved2.txt, clear of H's lines. So X is not named. P and R make the clash in
        # shared.txt together, and are.
        self._on_base({"shared.txt": TEN, "two.txt": TWO})
        self._plan("L", {"two.txt": None, "moved2.txt": TWO})
        self._plan("P", {"shared.txt": None, "moved.txt": TEN})
        self._plan("R", {"shared.txt": _rewrite(TEN, "R")})
        self._plan("X", {"two.txt": _edit(TWO, l10="line 10, as X has it")})
        self._plan("W", {"w.txt": "w\n"})
        self._plan("H", {"shared.txt": _edit(TEN, l10="line 10, as H has it"),
                         "moved2.txt": _rewrite(TWO, "H")}, depends_on="L")
        self._plan("V", {"v.txt": "v\n"}, depends_on="W")
        self._plan("U", {"u.txt": "u\n"}, depends_on="V")
        self._run(["L", "P", "R", "X", "W", "H", "V", "U"])

        self.assertTrue(self.fx._ancestor(self._head("L"), self._head("H")))  # the facts
        self.assertEqual(self._merges("origin/main", "X", "H"), 1)      # all of H's branch
        self.assertIsNone(self._merges("origin/main", "L", "X", "H"))   # H's own change
        self.assertIsNone(self._merges("origin/main", "L", "P", "H"))
        self.assertIsNone(self._merges("origin/main", "L", "R", "H"))
        self.assertEqual(self._merges("origin/main", "L", "P", "R", "H"), 3)
        self.assertIsNone(self._merges("origin/main", "L", "X", "W", "H"))
        self._joint_held("H", ["P", "R"], "shared.txt", unrelated=("L", "X", "W", "V"))
        self._complete("L", "P", "R", "X", "H")


if __name__ == "__main__":
    unittest.main()
