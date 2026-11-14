"""Merge mode merges a marked prerequisite before wave 0, and holds on anything else (#839).

A dependent whose prerequisite is COMPLETE, published by an earlier run and still unmerged
used to be built on a base without it (plain ``Depends on``, or the prerequisite named in the
command), or held with no step ever merging it (``Depends on (merged)``). A PR a merge-mode
run opened as a non-final wave now carries ``merge_eligible`` in its ``publish.json``, and a
later merge-mode run merges it through ``merge._merge_one``'s gates before wave 0; anything
else unmerged holds the dependent, with the reason and a way out that fits merge mode.

The way out has to work: ``merged.merged_into`` judges the publish record, so a record that
can never read merged on the dependent's base is not told to merge its PR by hand, and a
refusal nothing at the PR can clear is not blamed on the PR. The walk follows each edge of a
dependent on its own and never goes past a prerequisite it may not merge. It touches only a
PR that reads open, so a human can leave a marked PR alone by closing it. A prerequisite in
any state a bundle can be left in (no ``brief.md``, a brief with no target, a ``patch.diff``
that cannot be read) holds that dependent instead of ending the run or building without it.
The marker survives an iterate but is tied to the patch its PR was published from, so a
rebuilt attempt that never reached the PR holds the dependent instead of merging the old head.
The walk claims each prerequisite before it reads it (#565): one another live run holds is
neither judged nor merged, and holds its dependents.

Real git (the ``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py: a bare
``origin`` and a primary checkout), the real ``_merge_one`` and ``_runnable``, and the real
``worktree.ensure`` for what a dependent would be built on. ``gh`` is a stub in front of
``subprocess.run`` that really merges the PR's branch on ``origin``; the build and publish
leaves are stubs.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_merge_resume_stranded_prereq
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import (act, cli, drive_claim, driver, flow, leaves, merge, merged, publish,
                          signoff, state, worktree)

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-31"
STOP = "STOP: later waves are NOT run"
PLAIN = "Depends on"
ON_MERGED = "Depends on (merged)"
BASE_READ = "the read of a PR's head and base"
NOT_TEXT = b"\xff\xfe not text"
REAL_PUBLISH_BUNDLE = flow._publish_bundle      # `_run` stubs it; a few cases need the real one


def _patch_sha(d: Path) -> str:
    """What a marker records of the patch it was written for. Spelled out rather than read from
    the fix: it is also the format a later run has to read back off an earlier run's record."""
    return hashlib.sha256((d / "patch.diff").read_bytes()).hexdigest()


class MergeResume(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.cfg.wave_mode = "merge"
        self.cfg.merge_wait_secs = 0         # read the rollup once
        self.pr: dict[str, dict] = {}        # id → the host's PR: state, draft, rollup
        self.gh_calls: list[list[str]] = []
        self.events: list[str] = []          # gh ready / merge and builds, in order
        self.built: list[str] = []
        self.published: list[str] = []
        self.base_files: dict[str, set[str]] = {}    # what each build's worktree held
        self.marked_at_ready: dict[str, bool] = {}   # id → marker on disk at its first ready
        self.finish: dict[str, dict[str, str]] = {}  # id → what its build publishes (files)
        self.failing: set[str] = set()               # `gh pr` verbs that fail (or BASE_READ)
        self.queued: set[str] = set()                # ids whose merge the host only queues
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _opened(self, iid: str, *, edges: str = "", marked: bool = False,
                rollup: str = "pass") -> Path:
        """``iid`` signed off and published by an earlier run: a draft PR, branch on origin."""
        d = self.fx._publish(iid, {f"{iid}.txt": f"{iid}\n"})
        sb._brief(d, extra=edges)
        sb._accept(d)
        if marked:
            self._record(d, merge_eligible=True, merge_eligible_patch=_patch_sha(d))
        self.pr[iid] = {"state": "OPEN", "draft": True, "rollup": rollup}
        self.assertEqual(state.state(d), state.COMPLETE)
        return d

    def _record(self, d: Path, **changes: object) -> None:
        rec = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        rec.update(changes)
        (d / "publish.json").write_text(json.dumps(rec), encoding="utf-8")

    def _no_brief(self, d: Path) -> None:
        """A bundle past Do needs no brief for its state (``state.state``, #481)."""
        (d / "brief.md").unlink()
        self.assertEqual(state.state(d), state.COMPLETE)

    def _no_target(self, d: Path) -> None:
        """``d``'s brief can be read and names no target: no line would ever carry it."""
        (d / "brief.md").write_text(f"- **Slug:** {d.name}\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.COMPLETE)

    def _bad_patch(self, d: Path) -> None:
        """``d``'s ``patch.diff`` is not text, and nothing past Do reads it for its state."""
        (d / "patch.diff").write_bytes(NOT_TEXT)
        self.assertEqual(state.state(d), state.COMPLETE)

    def _iterated(self, d: Path) -> None:
        """The real iterate-to-Do of accepted ``d``: the attempt is archived and ``publish.json``
        stays, so ``d`` is driven again while its record still names the first attempt's PR."""
        shutil.copyfile(sb.TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="T", date=TODAY, delta="rebuild")
        with redirect_stdout(io.StringIO()):
            driver.advance(d, self.cfg)
        self.assertEqual(state.state(d), state.PLANNED)
        self.assertTrue((d / "publish.json").is_file())
        self.assertFalse((d / "patch.diff").exists())

    def _merged_by_hand(self, iid: str) -> None:
        """A human merges ``iid``'s PR on the host, into the base it is on."""
        self.fx._merge_pr(iid)
        self.pr[iid]["state"] = "MERGED"

    def _targets(self, d: Path, base: str) -> None:
        """``d``'s brief targets ``base`` instead of the fixture's ``main``."""
        text = (d / "brief.md").read_text(encoding="utf-8")
        (d / "brief.md").write_text(text.replace("org/repo @ main", f"org/repo @ {base}"),
                                    encoding="utf-8")

    def _planned(self, iid: str, edges: str = "") -> Path:
        d = self.cfg.bundle(iid)
        sb._brief(d, extra=edges)
        return d

    def _du(self, field: str = PLAIN, prereq: str = "P") -> None:
        self._planned("D", f"- **{field}:** {prereq}\n")
        self._planned("U")

    # -- stubs -----------------------------------------------------------------------------

    def _gh(self, args, *a, **kw):
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        self.gh_calls.append(list(args))
        verb, url = args[2], str(args[3])
        if verb in self.failing or (verb == "view" and "baseRefName" in args[-1]
                                    and BASE_READ in self.failing):
            return subprocess.CompletedProcess(args, 1, "", "refused")
        if verb == "graphql":       # `gh api graphql`: a blocked PR's review threads
            threads = {"nodes": [], "pageInfo": {"hasNextPage": False}}
            return subprocess.CompletedProcess(args, 0, json.dumps(
                {"data": {"resource": {"reviewThreads": threads}}}), "")
        if verb == "create":        # a real re-publish opens the PR again
            body = Path(args[args.index("--body-file") + 1])
            iid = body.parent.name.removeprefix("issue_")
            return subprocess.CompletedProcess(args, 0, f"https://example.test/pr/{iid}\n", "")
        iid = url.rsplit("/", 1)[-1]
        pr = self.pr[iid]
        out = ""
        # The host takes none of these on a PR that is not open, as a real one does not.
        if verb in ("ready", "update-branch", "merge") and pr["state"] != "OPEN":
            return subprocess.CompletedProcess(args, 1, "", f"the PR is {pr['state']}")
        if verb == "view":
            branch = f"fix/{iid}"
            head = self.fx._tip(branch) if self.fx._has(branch) else ""
            out = json.dumps({"state": pr["state"], "headRefOid": head,
                              "baseRefName": pr.get("host_base", "main"),
                              "isCrossRepository": pr.get("cross_repo", False),
                              "mergeStateStatus": pr.get("merge_state", "CLEAN"),
                              "reviewDecision": pr.get("decision", ""), "latestReviews": []})
        elif verb == "checks":
            moved = pr.pop("moved_at_checks", None)     # what moves while the checks are read
            if moved == "head":
                self.fx._push_from_human({f"{iid}-more.txt": "more\n"}, to=f"fix/{iid}",
                                         off=f"origin/fix/{iid}")
            elif moved == "base":
                self.fx._push_from_human({f"{iid}-base.txt": "base\n"}, to="main")
            elif moved == "retarget":
                pr["host_base"] = "elsewhere"
            out = json.dumps([{"name": "ci", "bucket": pr["rollup"]}])
        elif verb == "ready":
            if "--undo" in args:
                pr["draft"] = True
                self.events.append(f"undo issue_{iid}")
            else:
                pr["draft"] = False
                self.events.append(f"ready issue_{iid}")
                self.marked_at_ready.setdefault(iid, self._marker(iid))
        elif verb == "update-branch":
            if not pr.get("update_never_lands"):
                self.fx._update_branch(iid)
        elif verb == "merge":
            self.events.append(f"merge issue_{iid}")
            if iid not in self.queued:      # a merge queue says yes and merges later
                self.fx._merge_pr(iid)
                pr["state"] = "MERGED"
        return subprocess.CompletedProcess(args, 0, out, "")

    def _rec(self, d: Path) -> dict:
        return json.loads((d / "publish.json").read_text(encoding="utf-8"))

    def _marker(self, iid: str) -> bool:
        return self._rec(self.cfg.find_bundle(iid)).get("merge_eligible") is True

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        for d in wave:
            self.built.append(d.name)
            self.events.append(f"build {d.name}")
            # What Do would actually be cut from: the real worktree off the real base.
            wt = worktree.ensure(d, cfg)
            self.base_files[d.name] = {p.name for p in wt.iterdir()} if wt else set()
            iid = d.name.removeprefix("issue_")
            brief_text = (d / "brief.md").read_text(encoding="utf-8")
            if iid in self.finish:      # really published, as a later wave's merge needs
                self.fx._publish(iid, self.finish[iid])
                self.pr[iid] = {"state": "OPEN", "draft": True,
                                "rollup": self.pr.get(iid, {}).get("rollup", "pass")}
            else:
                (d / "patch.diff").write_text(sb._new_file_patch(f"{iid}.txt", iid),
                                              encoding="utf-8")
            (d / "brief.md").write_text(brief_text, encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, _cfg, d: Path, **_kw) -> bool:
        self.published.append(d.name)
        return True

    def _real_publish_bundle(self, publishes) -> list:
        """For ``_run``: the real ``flow._publish_bundle``, which reads off the record whether
        anything was pushed, over ``publishes`` in place of ``publish.publish``."""
        return [mock.patch.object(flow, "_publish_bundle", REAL_PUBLISH_BUNDLE),
                mock.patch.object(publish, "publish", publishes)]

    def _real_publish(self, d: Path) -> tuple[int, str]:
        """The real ``publish.publish`` of accepted ``d``, its texts in place: (rc, stderr)."""
        (d / publish.COMMIT_MSG).write_text("fix\n", encoding="utf-8")
        (d / publish.PR_BODY).write_text("Fixes #1\n", encoding="utf-8")
        err = io.StringIO()

        def quiet(args, *a, **kw):      # publish lets git talk to the terminal
            kw.setdefault("capture_output", True)
            return self._gh(args, *a, **kw)

        with mock.patch.object(subprocess, "run", quiet), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = publish.publish(self.cfg, d.name.removeprefix("issue_"), by="T", today=TODAY,
                                 texts_prevalidated=True)
        return rc, err.getvalue()

    def _driven(self, drive, stack=()):
        """What ``drive()`` returns, with the leaves and ``gh`` stubbed; stderr in ``self.err``."""
        err = io.StringIO()
        with ExitStack() as ctx:
            ctx.enter_context(mock.patch.object(subprocess, "run", self._gh))
            ctx.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            ctx.enter_context(mock.patch.object(flow, "_publish_bundle", self._publish_bundle))
            ctx.enter_context(mock.patch.object(publish, "draft_texts", lambda *a, **k: True))
            ctx.enter_context(mock.patch.object(leaves, "do_plan_batch", lambda *a, **k: None))
            for patcher in stack:
                ctx.enter_context(patcher)
            ctx.enter_context(redirect_stdout(io.StringIO()))
            ctx.enter_context(redirect_stderr(err))
            got = drive()
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)
        return got

    def _run(self, ids: list[str], *, do_publish: bool = True, stack=(), sweep: bool = False,
             claims: drive_claim.Run | None = None) -> None:
        how = {"do_publish": do_publish, "do_act": False, "by": "T", "today": TODAY,
               "claims": claims}
        self.results = self._driven((lambda: flow.flow_batch(self.cfg, **how)) if sweep else
                                    (lambda: flow.flow_ids(self.cfg, ids, **how)), stack)

    def _cli(self, ids: list[str], *, do_publish: bool = True, stack=()) -> int:
        """``pdca flow <ids>`` through the CLI's own entry point, which makes the run's claim
        scope and claims the ids it names. Its exit code; ``self.results`` as ``_run`` fills it."""
        args = SimpleNamespace(issue_ids=ids, from_csv=None, from_briefs=None,
                               no_publish=not do_publish, no_act=True, by="T", lanes=None,
                               max_passes=None)
        rc = self._driven(lambda: cli._flow(self.cfg, args), stack)
        self.results = {iid: state.state(self.cfg.bundle(iid)) for iid in ids}
        return rc

    # -- assertions ------------------------------------------------------------------------

    def _held_line(self, dependent: str = "D") -> str:
        """``dependent`` was not built and stays PLANNED, and ONE line says so, naming its
        base, in merge mode's terms: never a line of the run."""
        self.assertNotIn(f"issue_{dependent}", self.built, self.err)
        self.assertEqual(self.results[dependent], state.PLANNED)
        lines = [x for x in self.err.splitlines() if f"flow: issue_{dependent} held" in x]
        self.assertEqual(len(lines), 1, self.err)
        line = lines[0]
        self.assertIn("not merged into main (org/repo)", line)
        for wrong in ("line of this run", "carries them", "in the same `pdca flow <ids>`"):
            self.assertNotIn(wrong, line)
        return line

    def _no_merge_attempt(self) -> None:
        verbs = {c[2] for c in self.gh_calls}
        self.assertLessEqual(verbs, {"view"}, self.gh_calls)
        for call in self.gh_calls:
            self.assertIn("state", call[-1], call)
        self.assertEqual(self.events.count("merge issue_P"), 0)

    def _on_main(self, iid: str) -> bool:
        tip = self.fx._tip("main")
        return sb._run(self.fx.origin, "merge-base", "--is-ancestor",
                       self.fx._tip(f"fix/{iid}"), tip).returncode == 0

    # -- (1) the bug: built on a base without the prerequisite -----------------------------

    def test_1_unmarked_plain_prereq_holds_the_dependent(self):
        self._opened("P")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertIn("merge the PR by hand", line)
        self._no_merge_attempt()
        self.assertTrue(self.gh_calls)
        self.assertIn("issue_U", self.built)

    def test_1b_a_prereq_named_in_the_command_holds_the_dependent_too(self):
        self._opened("P")
        self._du()
        self._run(["P", "D", "U"])
        self.assertIn("issue_P", self._held_line())
        self._no_merge_attempt()
        self.assertNotIn("issue_P", self.built)

    def test_1c_merge_mode_builds_no_bundle_base(self):
        forbid = [mock.patch.object(flow, name, side_effect=AssertionError(name))
                  for name in ("_base_plans", "_bundle_bases")]
        self._opened("P")
        self._du()
        self._run(["D", "U"], stack=forbid)
        self._held_line()
        self.assertIn("issue_U", self.built)
        for iid in ("D", "U", "P"):
            self.assertFalse((self.cfg.bundle(iid) / "stack-base").exists(), iid)

    def test_1c_no_out_of_batch_prereq_builds_no_bundle_base_either(self):
        forbid = [mock.patch.object(flow, name, side_effect=AssertionError(name))
                  for name in ("_base_plans", "_bundle_bases")]
        self._planned("A")
        self._planned("B")
        self._run(["A", "B"], stack=forbid)
        self.assertEqual(sorted(self.built), ["issue_A", "issue_B"])
        for iid in ("A", "B"):
            self.assertFalse((self.cfg.bundle(iid) / "stack-base").exists(), iid)
        self.assertEqual(self.gh_calls, [])

    # -- (2) a marked prerequisite is merged, and the dependent builds on it ----------------

    def _merged_then_built(self, field: str) -> None:
        self._opened("P", marked=True)
        self._du(field)
        self.assertFalse(self._on_main("P"))
        self._run(["D", "U"])
        self.assertTrue(self._on_main("P"), self.err)
        self.assertLess(self.events.index("ready issue_P"), self.events.index("merge issue_P"))
        self.assertLess(self.events.index("merge issue_P"),
                        min(self.events.index("build issue_D"),
                            self.events.index("build issue_U")))
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)

    def test_2_marked_plain_prereq_is_merged_and_the_dependent_builds_on_it(self):
        self._merged_then_built(PLAIN)

    def test_2_marked_depends_on_merged_prereq_is_merged_and_the_dependent_builds_on_it(self):
        self._merged_then_built(ON_MERGED)

    def test_2_a_marked_named_prereq_is_merged_too(self):
        self._opened("P", marked=True)
        self._du()
        self._run(["P", "D", "U"])
        self.assertTrue(self._on_main("P"), self.err)
        self.assertIn("P.txt", self.base_files["issue_D"])

    def test_2_a_marked_chain_merges_deepest_first(self):
        self._opened("Q", marked=True)
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        merges = [e for e in self.events if e.startswith("merge ")]
        self.assertEqual(merges, ["merge issue_Q", "merge issue_P"], self.err)
        self.assertTrue(self.base_files["issue_D"] >= {"P.txt", "Q.txt"}, self.err)

    def test_2_a_marked_pr_on_another_base_is_retargeted_merged_and_releases(self):
        # Its record names a line, but it is still open: `_merge_one` holds it to its target
        # and rewrites the record, so `merged_into` then answers for the dependent's base.
        d = self._opened("P", marked=True)
        self._record(d, base="pdca-integration/main")
        self._du()
        self._run(["D", "U"])
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)
        self.assertEqual(json.loads((d / "publish.json").read_text(encoding="utf-8"))["base"],
                         "main")

    def test_2b_a_pr_merged_into_another_base_holds_the_dependent(self):
        d = self._opened("P", marked=True)
        self._record(d, base="pdca-integration/main")
        self.pr["P"]["state"] = "MERGED"
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_P", self._held_line())
        self.assertNotIn("let a merge-mode run merge it", self.err)
        self._no_merge_attempt()
        self.assertIn("issue_U", self.built)

    def _merge_one_says(self) -> tuple[list[int], list]:
        """A spy on the real ``_merge_one``, for a return code a test must not be released on."""
        rcs: list[int] = []
        real = merge._merge_one

        def spy(*a, **k):
            rcs.append(real(*a, **k))
            return rcs[-1]

        return rcs, [mock.patch.object(merge, "_merge_one", spy)]

    def test_2b_a_return_code_of_0_for_a_merge_into_another_base_does_not_release(self):
        # A human merges it where its record says after the walk read it open: `is_merged`
        # answers for a merge into any base, so `_merge_one` returns 0 without touching it.
        d = self._opened("P", marked=True)
        self._record(d, base="pdca-integration/main")
        self._du()
        rcs, spy = self._merge_one_says()
        real_gate = merged.is_merged

        def merged_meanwhile(cfg, iid):
            self.pr[iid]["state"] = "MERGED"
            return real_gate(cfg, iid)

        spy.append(mock.patch.object(merged, "is_merged", merged_meanwhile))
        self._run(["D", "U"], stack=spy)
        self.assertEqual(rcs, [0], self.err)     # `_merge_one` was asked, and said "merged"
        self.assertIn("issue_P", self._held_line())
        self.assertNotIn("ready issue_P", self.events)

    def test_2b_a_return_code_of_0_for_a_merge_only_queued_does_not_release(self):
        # The host accepts `gh pr merge` and has not merged yet: D would miss P on its base.
        self._opened("P", marked=True)
        self._du()
        self.queued.add("P")
        rcs, spy = self._merge_one_says()
        self._run(["D", "U"], stack=spy)
        self.assertEqual(rcs, [0], self.err)
        self.assertIn("merge issue_P", self.events)
        self.assertFalse(self._on_main("P"))
        self.assertIn("let a merge-mode run merge it", self._held_line())
        self.assertIn("issue_U", self.built)

    # -- (3) refusal, unmarked, chain, retry -------------------------------------------------

    def test_3_a_refused_prereq_holds_only_its_dependent(self):
        self._opened("P", marked=True, rollup="fail")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertIn("did not pass the merge gates", line)
        self.assertIn("`merge:` lines above", line)
        self.assertNotIn(STOP, self.err)
        self.assertIn("issue_D is held; resolve at the PR, then re-run", self.err)
        self.assertIn("a check is FAILING", self.err)
        self.assertIn("issue_U", self.built)
        self.assertIn("issue_U", self.published)
        self.assertEqual(self.results["U"], state.COMPLETE)
        self.assertEqual(self.events[:2], ["ready issue_P", "undo issue_P"])
        self.assertFalse(self._on_main("P"))
        self.assertEqual(cli._results_rc(self.results), 1)

    def test_3b_an_unmarked_draft_is_never_readied_or_merged(self):
        for field in (PLAIN, ON_MERGED):
            with self.subTest(field=field):
                self.gh_calls.clear()
                self.built.clear()
                if "P" not in self.pr:
                    self._opened("P")
                self._du(field)
                self._run(["D"])
                line = self._held_line()
                self.assertIn("issue_P", line)
                self.assertIn("merge the PR by hand", line)
                self._no_merge_attempt()
                self.assertTrue(self.pr["P"]["draft"])

    def test_3c_a_marked_prereq_of_an_unmerged_one_is_not_merged(self):
        self._opened("Q")
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertIn("issue_Q", line)
        self.assertIn("issue_Q: no merge-mode run may merge it (it is not marked), so merge the "
                      "PR by hand", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertFalse(self._on_main("P"))

    def test_3c_a_blocker_that_never_finished_is_not_told_to_publish(self):
        # The graph check reads only the drive set's edges, so this one can be anything.
        self._planned("Q")
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn(f"issue_Q: it is {state.PLANNED}, not COMPLETE; finish its cycle", line)
        self.assertNotIn("pdca publish", line)
        self.assertNotIn("merge issue_P", self.events)

    def test_3c_its_blocker_refused_by_the_gates_says_so_on_the_dependents_line(self):
        self._opened("Q", marked=True, rollup="fail")
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_Q: its PR did not pass the merge gates", line)
        self.assertNotIn("merge issue_P", self.events)
        self.assertNotIn("ready issue_P", self.events)
        self.assertIn("issue_U", self.built)

    def test_3c_a_blocker_that_can_never_read_merged_gets_a_way_out_that_works(self):
        q = self._opened("Q")
        self._record(q, mode="stacked")
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D"])
        line = self._held_line()
        self.assertIn("issue_Q: it is an `Onto branch` commit", line)
        self.assertIn("remove the edge to Q from issue_P's brief", line)
        self.assertNotIn("issue_Q: no merge-mode run", line)
        # Following it: P no longer waits for Q, so the next run merges P and D builds on it.
        sb._brief(self.cfg.bundle("P"))
        self._run(["D"])
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    # -- the walk merges only what can release the dependent --------------------------------

    def test_the_walk_stops_at_a_prereq_already_merged(self):
        # Nothing waits for Q: D's edge is to P, which is on the base. Merging Q unattended
        # would land a PR no bundle of this run asked for.
        self._opened("Q", marked=True)
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._merged_by_hand("P")
        self._du()
        self._run(["D", "U"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertNotIn("ready issue_Q", self.events)
        self.assertNotIn("merge issue_Q", self.events)
        self.assertEqual(self.pr["Q"]["state"], "OPEN")
        self.assertTrue(self.pr["Q"]["draft"])

    def test_the_walk_does_not_go_past_a_prereq_it_may_not_merge(self):
        # P stays unmerged whatever happens to Q, so D stays held: merging Q gains this run
        # nothing, and the human who merges P decides.
        self._opened("Q", marked=True)
        self._opened("P", edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P: no merge-mode run may merge it", line)
        self.assertNotIn("ready issue_Q", self.events)
        self.assertNotIn("merge issue_Q", self.events)
        self.assertIn("issue_U", self.built)

    def test_an_unmarked_prereq_costs_the_walk_no_gh_read(self):
        self._opened("P")
        self._du()
        self._run(["D", "U"])
        self._held_line()
        # `_runnable`'s state read, and the one its hold line makes for the way out.
        self.assertEqual(len(self.gh_calls), 2, self.gh_calls)

    def test_two_dependents_of_one_prereq_cost_the_walk_one_gh_read(self):
        self._opened("P", marked=True)
        self._planned("D", "- **Depends on:** P\n")
        self._planned("E", "- **Depends on:** P\n")
        self._run(["D", "E"])
        self.assertEqual(self.events.count("merge issue_P"), 1, self.err)
        self.assertIn("issue_D, issue_E wait for it", self.err)
        reads = [c for c in self.gh_calls if c[2] == "view" and c[-1] == "state,headRefOid"]
        self.assertEqual(len(reads), 3, reads)      # the walk's, and `_runnable`'s for each
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertEqual(self.results["E"], state.COMPLETE, self.err)

    def test_each_edge_of_a_dependent_is_judged_on_its_own(self):
        # P can merge though D stays held for R: any later run could merge P the same way,
        # and R may have merged by then.
        self._opened("P", marked=True)
        self._opened("R")
        self._planned("D", "- **Depends on:** P, R\n")
        self._planned("U")
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("prerequisite(s) issue_R not merged", line)
        self.assertIn("issue_R: no merge-mode run may merge it", line)
        self.assertNotIn("issue_P", line)
        self.assertTrue(self._on_main("P"), self.err)
        self.assertNotIn("ready issue_R", self.events)
        self.assertIn("issue_U", self.built)
        # … and the edge left clears the same way: merged by hand, D builds on both.
        self._merged_by_hand("R")
        self._run(["D"])
        self.assertTrue(self.base_files["issue_D"] >= {"P.txt", "R.txt"}, self.err)

    def test_a_prereq_merges_though_its_dependent_never_builds_for_another_reason(self):
        # The same rule when what stops D is not an edge the walk reads: R, of this run, is
        # built after P merged, and its own merge refused stops the run before D's wave.
        self._opened("P", marked=True)
        self._planned("R")
        self.finish["R"] = {"R.txt": "R\n"}
        self.pr["R"] = {"state": "OPEN", "draft": True, "rollup": "fail"}
        self._planned("D", "- **Depends on:** P, R\n")
        self._run(["D", "R"])
        self.assertTrue(self._on_main("P"), self.err)
        self.assertLess(self.events.index("merge issue_P"), self.events.index("build issue_R"))
        self.assertIn("flow: wave 0 did not merge; STOPPING", self.err)
        self.assertNotIn("issue_D", self.built)

    def test_a_cycle_among_prereqs_holds_and_merges_neither(self):
        # The graph check reads only the drive set's edges, so this can reach the walk.
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._opened("Q", marked=True, edges="- **Depends on:** P\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_Q", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertIn("issue_U", self.built)

    def test_a_note_is_for_the_base_its_merge_was_for(self):
        # E targets another base, so P's gates say nothing about what would release it.
        self._opened("P", marked=True, rollup="fail")
        self._du()
        e = self._planned("E", "- **Depends on:** P\n")
        self._targets(e, "rel")
        self._run(["D", "E", "U"])
        self.assertIn("did not pass the merge gates", self._held_line())
        lines = [x for x in self.err.splitlines() if "flow: issue_E held" in x]
        self.assertEqual(len(lines), 1, self.err)
        line = lines[0]
        self.assertIn("not merged into rel (org/repo)", line)
        self.assertNotIn("merge gates", line)
        self.assertIn("its PR is on record for main (org/repo)", line)
        self.assertIn("issue_U", self.built)

    def test_every_edge_kind_of_a_prereq_counts_below_it(self):
        # Below a prerequisite it takes, the walk and the merge rule read all three fields.
        for i, field in enumerate((PLAIN, ON_MERGED, "Stacks on")):
            with self.subTest(field, marked=True):
                self._opened(f"Q{i}", marked=True)
                self._opened(f"P{i}", marked=True, edges=f"- **{field}:** Q{i}\n")
                self._du(prereq=f"P{i}")
                self._run(["D"])
                self.assertLess(self.events.index(f"merge issue_Q{i}"),
                                self.events.index(f"merge issue_P{i}"), self.err)
                self._d_built_then_replanned()
            with self.subTest(field, marked=False):
                self._opened(f"S{i}")
                self._opened(f"R{i}", marked=True, edges=f"- **{field}:** S{i}\n")
                self._du(prereq=f"R{i}")
                self._run(["D"])
                line = self._held_line()
                self.assertIn(f"issue_S{i}", line)
                self.assertNotIn(f"merge issue_R{i}", self.events)
                self.assertNotIn(f"ready issue_R{i}", self.events)

    def _d_built_then_replanned(self) -> None:
        """D built: gone, for the next case to plan again."""
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        shutil.rmtree(self.cfg.bundle("D"))
        self.built.clear()

    def test_a_stacks_on_edge_of_the_dependent_starts_no_merge(self):
        # Only the edges `_runnable` holds on start the walk: a `Stacks on` edge waits for
        # COMPLETE, so merging for it would land a PR nothing here waits to see merged.
        self._opened("P", marked=True)
        self._du("Stacks on")
        self._run(["D"])
        self.assertIn("issue_D", self.built, self.err)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertFalse(self._on_main("P"))

    def test_a_marked_chain_merges_without_the_rollup_gate_too(self):
        # `merge_requires = "required"` fetches nothing between the merges, and `_merge_one`
        # fetches one checkout only once a run: D's base must have both all the same.
        self.cfg.merge_requires = "required"
        self._opened("Q", marked=True)
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        self.assertEqual([e for e in self.events if e.startswith("merge ")],
                         ["merge issue_Q", "merge issue_P"], self.err)
        self.assertTrue(self.base_files["issue_D"] >= {"P.txt", "Q.txt"}, self.err)

    def test_a_dependent_with_no_target_of_its_own_has_no_base_to_wait_for(self):
        # Publish skips such a bundle, so nothing it could wait for or merge has a base (#647).
        self._opened("P", marked=True)
        (self._planned("D") / "brief.md").write_text(
            "- **Slug:** d\n- **Depends on:** P\n", encoding="utf-8")
        self._run(["D"])
        self.assertIn("issue_D", self.built, self.err)
        self.assertEqual(self.gh_calls, [])
        self.assertFalse(self._on_main("P"))

    def test_a_marked_prereq_archived_to_completed_is_found_and_merged(self):
        d = self._opened("P", marked=True)
        (self.cfg.bundle_root / "completed").mkdir()
        d.rename(self.cfg.bundle_root / "completed" / d.name)
        self._du()
        self._run(["D", "U"])
        self.assertTrue(self._on_main("P"), self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_the_sweep_merges_a_marked_prereq_too(self):
        self._opened("P", marked=True)
        self._du()
        self._run([], sweep=True)
        self.assertTrue(self._on_main("P"), self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    # -- a prerequisite with no brief: hold that dependent, never crash the run ------------

    def _no_brief_held(self, ids: list[str], *, field: str = PLAIN, marked: bool = False) -> str:
        self._no_brief(self._opened("P", marked=marked))
        self._du(field)
        self._run(ids)
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertNotIn("ready issue_P", self.events)
        self.assertNotIn("merge issue_P", self.events)
        self.assertIn("issue_U", self.built)
        self.assertEqual(self.results["U"], state.COMPLETE)
        return line

    def test_a_prereq_with_no_brief_holds_only_its_dependent(self):
        self.assertIn("(it is not marked), so merge the PR by hand",
                      self._no_brief_held(["D", "U"]))

    def test_a_marked_prereq_with_no_brief_is_not_taken_on_a_merged_edge(self):
        # `_merge_one` would refuse it: with no brief it has no target to check its PR against.
        self.assertIn("no merge-mode run merges it for issue_D (it has no readable brief.md), so "
                      "merge the PR by hand",
                      self._no_brief_held(["D", "U"], field=ON_MERGED, marked=True))

    def test_a_named_prereq_with_no_brief_holds_too(self):
        self.assertIn("merge the PR by hand", self._no_brief_held(["P", "D", "U"], marked=True))

    def test_a_prereq_whose_brief_cannot_be_read_holds_the_same_way(self):
        d = self._opened("P", marked=True)
        (d / "brief.md").write_bytes(NOT_TEXT)
        self._du()
        self._run(["D", "U"])
        self.assertIn("(it has no readable brief.md)", self._held_line())
        self.assertNotIn("ready issue_P", self.events)
        self.assertIn("issue_U", self.built)

    def test_a_prereq_with_no_brief_and_no_pr_is_told_to_restore_it(self):
        d = self._opened("P")
        self._record(d, pr_url="")
        self._no_brief(d)
        self._du()
        self._run(["D", "U"])
        self.assertIn("no PR on record and no readable brief.md to publish one from; restore its "
                      "brief", self._held_line())
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_U", self.built)

    def test_a_merged_prereq_with_no_brief_releases_its_dependent(self):
        self._no_brief(self._opened("P"))
        self._merged_by_hand("P")
        self._du()
        self._run(["D", "U"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_prereq_of_a_prereq_with_no_brief_holds_too(self):
        self._no_brief(self._opened("Q"))
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_Q", line)
        self.assertNotIn("merge issue_P", self.events)
        self.assertIn("issue_U", self.built)

    def test_a_prereq_with_no_patch_and_no_brief_has_nothing_to_wait_for(self):
        p = self._planned("P")
        (p / state.CLOSE_MARKER).write_text("likely-close\n", encoding="utf-8")
        sb._accept(p)
        self._no_brief(p)
        self._du()
        self._run(["D", "U"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertEqual(self.gh_calls, [])

    # -- a prerequisite whose brief names no target: no line would carry it, and it still ----
    # -- ships something a dependent would miss ---------------------------------------------

    def _no_target_held(self, *, field: str = PLAIN, marked: bool = False,
                        ids: tuple[str, ...] = ("D", "U")) -> str:
        self._no_target(self._opened("P", marked=marked))
        self._du(field)
        self._run(list(ids))
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertFalse(self._on_main("P"))
        self._no_merge_attempt()
        self.assertIn("issue_U", self.built)
        self.assertEqual(self.results["U"], state.COMPLETE)
        return line

    def test_a_prereq_with_no_target_holds_on_a_plain_edge(self):
        self.assertIn("no merge-mode run may merge it (it is not marked), so merge the PR by hand",
                      self._no_target_held())
        # Following it works: its record, not its brief, says where the PR merged.
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_marked_prereq_with_no_target_is_not_merged_for_a_dependent(self):
        # `_merge_one` would refuse it: it has no target to check its PR against.
        self.assertIn("no merge-mode run merges it for issue_D (its brief does not target main), "
                      "so merge the PR by hand", self._no_target_held(marked=True))

    def test_a_prereq_with_no_target_holds_the_same_on_a_merged_edge(self):
        self._no_target_held(field=ON_MERGED)

    def test_a_named_prereq_with_no_target_holds_too(self):
        self._no_target_held(ids=("P", "D", "U"))

    def test_a_prereq_with_no_target_and_no_pr_is_told_to_land_the_work(self):
        d = self._opened("P")
        self._record(d, pr_url="")
        self._no_target(d)
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("no PR on record and its brief does not target main (org/repo)", line)
        self.assertIn("remove the edge to P from issue_D's brief", line)
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_U", self.built)

    def test_a_prereq_with_no_target_and_nothing_shipped_has_nothing_to_wait_for(self):
        p = self._planned("P")
        (p / "patch.diff").write_text("", encoding="utf-8")
        sb._accept(p)
        self._no_target(p)
        self._du()
        self._run(["D", "U"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertEqual(self.gh_calls, [])

    # -- a patch that cannot be read: hold that dependent, never crash the run ---------------

    def test_has_patch_tells_nothing_shipped_from_a_patch_it_cannot_read(self):
        d = self.cfg.bundle("X")
        d.mkdir(parents=True)
        self.assertIs(merged.has_patch(d), False)           # no patch.diff
        (d / "patch.diff").write_text(" \n", encoding="utf-8")
        self.assertIs(merged.has_patch(d), False)           # an empty one
        (d / "patch.diff").write_text("diff\n", encoding="utf-8")
        self.assertIs(merged.has_patch(d), True)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertIsNone(merged.has_patch(d))          # a read that fails
        self._bad_patch(self._opened("Y"))
        self.assertIsNone(merged.has_patch(self.cfg.bundle("Y")))   # not text

    def _bad_patch_held(self, *, field: str = PLAIN, marked: bool = True,
                        ids: tuple[str, ...] = ("D", "U")) -> str:
        self._bad_patch(self._opened("P", marked=marked))
        self._du(field)
        self._run(list(ids))
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertFalse(self._on_main("P"))
        self._no_merge_attempt()
        self.assertIn("issue_U", self.built)
        self.assertEqual(self.results["U"], state.COMPLETE)
        return line

    def test_a_prereq_whose_patch_cannot_be_read_holds_only_its_dependent(self):
        self.assertIn("no merge-mode run merges it for issue_D (its patch.diff cannot be read), "
                      "so merge the PR by hand", self._bad_patch_held())
        # Following it works: the PR's merge says what the file cannot.
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_patch_that_cannot_be_read_holds_the_same_on_a_merged_edge(self):
        self._bad_patch_held(field=ON_MERGED)

    def test_an_unmarked_prereq_whose_patch_cannot_be_read_holds_too(self):
        self.assertIn("(it is not marked), so merge the PR by hand",
                      self._bad_patch_held(marked=False))

    def test_a_named_prereq_whose_patch_cannot_be_read_holds_too(self):
        self._bad_patch_held(ids=("P", "D", "U"))

    def test_a_prereq_of_a_prereq_whose_patch_cannot_be_read_holds_too(self):
        self._bad_patch(self._opened("Q"))
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertIn("issue_Q", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertIn("issue_U", self.built)

    # -- any state a bundle can be left in: hold that dependent, never crash, never merge -----

    _BROKEN = {
        "no record": lambda d: (d / "publish.json").unlink(),
        "a record that is not JSON":
            lambda d: (d / "publish.json").write_text("{nope", encoding="utf-8"),
        "a record that is not text": lambda d: (d / "publish.json").write_bytes(NOT_TEXT),
        "a record that is not an object":
            lambda d: (d / "publish.json").write_text("[1]", encoding="utf-8"),
        "a marker that is not true":
            lambda d: (d / "publish.json").write_text(json.dumps(
                {"pr_url": f"https://example.test/pr/{d.name.removeprefix('issue_')}",
                 "repo": "org/repo", "base": "main", "merge_eligible": "true"}),
                encoding="utf-8"),
        "no brief": lambda d: (d / "brief.md").unlink(),
        "an empty brief": lambda d: (d / "brief.md").write_text("", encoding="utf-8"),
        "a brief that is not text": lambda d: (d / "brief.md").write_bytes(NOT_TEXT),
        "a target with no base":
            lambda d: (d / "brief.md").write_text("- **Repo + branch target:** org/repo\n",
                                                  encoding="utf-8"),
        "a patch that is not text": lambda d: (d / "patch.diff").write_bytes(NOT_TEXT),
        "a marker with no patch hash":
            lambda d: (d / "publish.json").write_text(json.dumps(
                {"pr_url": f"https://example.test/pr/{d.name.removeprefix('issue_')}",
                 "repo": "org/repo", "base": "main", "merge_eligible": True}),
                encoding="utf-8"),
    }

    def test_a_broken_prereq_holds_only_its_dependent_and_merges_nothing(self):
        for i, (name, breaks) in enumerate(self._BROKEN.items()):
            for field in (PLAIN, ON_MERGED):
                for chain in (False, True):
                    with self.subTest(name, field=field, chain=chain):
                        self._a_broken_prereq(f"{i}{field == PLAIN}{chain}", breaks, field, chain)

    def _a_broken_prereq(self, tag: str, breaks, field: str, chain: bool) -> None:
        low, top, other = f"Q{tag}".upper(), f"P{tag}".upper(), f"U{tag}".upper()
        if chain:
            broken = self._opened(low, marked=True)
            self._opened(top, marked=True, edges=f"- **Depends on:** {low}\n")
        else:
            broken = self._opened(top, marked=True)
        breaks(broken)
        self._planned("D", f"- **{field}:** {top}\n")
        self._planned(other)
        self._run(["D", other])
        line = self._held_line()
        self.assertIn(f"issue_{low if chain else top}", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [], self.err)
        self.assertIn(f"issue_{other}", self.built, self.err)
        self.assertEqual(self.results[other], state.COMPLETE)
        # Gone before the next case: `_merge_one` reads every sibling's record (the next test).
        shutil.rmtree(broken)

    def test_a_sibling_whose_record_is_not_an_object_holds_instead_of_crashing(self):
        # `publish._batch_branch_producer` raises on it, for any bundle `_merge_one` is asked
        # about, whatever that bundle's own record says. Named, and only the dependent waits.
        self._opened("P", marked=True)
        (self._opened("S") / "publish.json").write_text("[1]", encoding="utf-8")
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_P: its merge stopped on an error", self._held_line())
        self.assertIn("flow: issue_P — merge before wave 0 failed (AttributeError", self.err)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertIn("issue_U", self.built)

    # -- a closed PR: the documented way to leave a marked one alone -------------------------

    def _closed_held(self, field: str = PLAIN, *, marked: bool = True) -> str:
        self._opened("P", marked=marked)
        self.pr["P"]["state"] = "CLOSED"
        self._du(field)
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P: its PR is closed", line)
        self.assertIn("reopen it and merge it by hand", line)
        self.assertIn("remove the edge to P from issue_D's brief", line)
        self.assertNotIn("let a merge-mode run", line)
        self._no_merge_attempt()
        self.assertEqual(self.pr["P"]["state"], "CLOSED")
        self.assertIn("issue_U", self.built)
        return line

    def test_a_closed_marked_pr_is_left_alone_and_holds_its_dependent(self):
        self._closed_held()
        # Following it works: reopened and merged by hand, it releases D.
        self.pr["P"]["state"] = "OPEN"
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_closed_marked_pr_holds_the_same_on_a_merged_edge(self):
        self._closed_held(ON_MERGED)

    def test_a_closed_unmarked_pr_is_told_the_same(self):
        self._closed_held(marked=False)

    def test_a_closed_pr_reopened_is_merged_by_the_next_run(self):
        self._closed_held()
        self.pr["P"]["state"] = "OPEN"
        self._run(["D"])
        self.assertIn("merge issue_P", self.events)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_the_walk_does_not_go_past_a_closed_pr(self):
        # Merging Q would land a PR for a dependent that P still holds.
        self._opened("Q", marked=True)
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self.pr["P"]["state"] = "CLOSED"
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_P: its PR is closed", self._held_line())
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertTrue(self.pr["Q"]["draft"])

    def test_a_closed_prereq_of_a_prereq_is_named_on_the_dependents_line(self):
        self._opened("Q", marked=True)
        self.pr["Q"]["state"] = "CLOSED"
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_Q: its PR is closed", line)
        self.assertIn("remove the edge to Q from issue_P's brief", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertIn("issue_U", self.built)

    def test_the_walk_does_not_go_past_a_pr_merged_into_another_base(self):
        # P can never read merged on this base, so Q's merge could release nothing.
        self._opened("Q", marked=True)
        p = self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._record(p, base="pdca-integration/main")
        self.pr["P"]["state"] = "MERGED"
        self._du()
        self._run(["D", "U"])
        self.assertIn("its PR is on record for pdca-integration/main", self._held_line())
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertTrue(self.pr["Q"]["draft"])

    def test_a_pr_whose_state_cannot_be_read_is_left_alone(self):
        self._opened("P", marked=True)
        self._du()
        self.failing = {"view"}
        self._run(["D", "U"])
        self.assertIn("let a merge-mode run merge it", self._held_line())
        self.assertIn("merged: could not read PR state for P", self.err)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertIn("issue_U", self.built)

    def _not_merged_line(self) -> str:
        """The one `merge:` line saying a PR was refused, with nothing of a stopped run in it."""
        lines = [x for x in self.err.splitlines() if "was NOT merged" in x]
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("only PRs whose head is in the base repository", lines[0])
        self.assertNotIn(STOP, lines[0])
        return lines[0]

    def test_a_fork_pr_is_refused_before_the_ready_mark_and_holds_the_dependent(self):
        # Issue #806: the pre-wave walk runs `_merge_one`, so the host's answer stops it too,
        # in the walk's terms: the run goes on, and the dependent waits.
        self._opened("P", marked=True)
        self._du()
        self.pr["P"]["cross_repo"] = True
        self._run(["D", "U"])
        self._held_line()
        self.assertIn("issue_D is held; the PR is left draft or ready, as it was; merge the PR "
                      "by hand, then re-run.", self._not_merged_line())
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertTrue(self.pr["P"]["draft"])
        self.assertIn("issue_U", self.built)
        # The way out it names is one: merged by hand, the dependent goes on.
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_pr_whose_repo_cannot_be_read_holds_the_dependent_until_it_can(self):
        # Issue #806: an answer it cannot use refuses as a fork does, but nothing has shown this
        # PR is on a fork, so what it names is the read. Fixed, the same PR merges unattended.
        self._opened("P", marked=True)
        self._du()
        self.pr["P"]["cross_repo"] = None
        self._run(["D", "U"])
        self._held_line()
        line = self._not_merged_line()
        self.assertIn("could not be read", line)
        self.assertIn("issue_D is held; the PR is left draft or ready, as it was; fix what "
                      "stopped the read, then re-run.", line)
        self.assertNotIn("by hand", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertTrue(self.pr["P"]["draft"])
        self.pr["P"]["cross_repo"] = False
        self._run(["D"])
        self.assertIn("merge issue_P", self.events)
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)

    # -- the way out names what can actually release the dependent --------------------------

    def _a_merge_by_hand_would_not_release(self, iid: str = "P") -> None:
        """The PR reads MERGED, and the dependent is still held: merging by hand is no way out."""
        self.pr[iid]["state"] = "MERGED"
        self._run(["D"])
        self._held_line()

    def test_an_unmarked_prs_way_out_works(self):
        self._opened("P")
        self._du()
        self._run(["D"])
        self.assertIn("merge the PR by hand, then re-run", self._held_line())
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_an_onto_branch_record_is_told_to_drop_the_edge(self):
        for marked in (False, True):
            with self.subTest(marked=marked):
                iid = "PM" if marked else "PU"
                d = self._opened(iid, marked=marked)
                self._record(d, mode="stacked", base="origin/someone-elses-branch")
                self._du(prereq=iid)
                self._run(["D"])
                line = self._held_line()
                self.assertIn(f"issue_{iid}: it is an `Onto branch` commit on another PR, which "
                              "never counts as merged into main", line)
                self.assertIn("get its work onto main", line)
                self.assertIn(f"remove the edge to {iid} from issue_D's brief", line)
                self.assertNotIn("merge the PR by hand", line)
                self.assertNotIn("let a merge-mode run", line)
                self.assertNotIn(f"ready issue_{iid}", self.events)
                self._a_merge_by_hand_would_not_release(iid)

    def test_an_onto_branch_record_on_the_same_base_is_told_the_same(self):
        d = self._opened("P")
        self._record(d, mode="stacked")     # the record's base is no help: `merged_into` refuses
        self._du()
        self._run(["D"])
        self.assertNotIn("merge the PR by hand", self._held_line())
        self._a_merge_by_hand_would_not_release()
        # Following it: with the edge gone there is nothing to hold D.
        self._planned("D")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)

    def test_a_pr_merged_into_another_base_has_nothing_left_to_merge_by_hand(self):
        for marked in (False, True):
            with self.subTest(marked=marked):
                iid = "QM" if marked else "QU"
                d = self._opened(iid, marked=marked)
                self._record(d, base="pdca-integration/main")
                self.pr[iid]["state"] = "MERGED"
                self._du(prereq=iid)
                self._run(["D"])
                line = self._held_line()
                self.assertIn(f"issue_{iid}: its PR is on record for pdca-integration/main "
                              "(org/repo), so its merge does not count as one into main", line)
                self.assertIn(f"remove the edge to {iid} from issue_D's brief", line)
                self.assertNotIn("merge the PR by hand", line)
                self.assertNotIn(f"ready issue_{iid}", self.events)

    def test_a_pr_on_record_for_another_repo_is_never_merged_for_the_dependent(self):
        d = self._opened("P", marked=True)
        self._record(d, repo="org/other")
        self._du()
        self._run(["D"])
        line = self._held_line()
        self.assertIn("its PR is on record for main (org/other)", line)
        self.assertNotIn("merge the PR by hand", line)
        self.assertNotIn("ready issue_P", self.events)

    def test_no_pr_on_record(self):
        d = self._opened("P", marked=True)
        self._record(d, pr_url="")
        self._du()
        self._run(["D"])
        self.assertIn("it has no PR on record; publish it (`pdca publish P`), merge the PR by "
                      "hand, then re-run", self._held_line())
        self.assertEqual(self.gh_calls, [])

    def test_no_pr_on_record_and_a_brief_for_another_base(self):
        d = self._opened("P")
        self._record(d, pr_url="")
        self._targets(d, "rel")
        self._du()
        self._run(["D"])
        line = self._held_line()
        self.assertIn("it has no PR on record and its brief does not target main (org/repo)", line)
        self.assertIn("remove the edge to P from issue_D's brief", line)
        self.assertNotIn("pdca publish", line)

    def test_3d_a_refused_pr_is_readied_and_merged_by_the_next_run(self):
        self._opened("P", marked=True, rollup="fail")
        self._du()
        self._run(["D"])
        self._held_line()
        self.assertTrue(self.pr["P"]["draft"])          # back to draft, marker kept
        self.assertTrue(self._marker("P"))
        self.pr["P"]["rollup"] = "pass"
        self._run(["D"])
        self.assertEqual(self.events.count("ready issue_P"), 2)
        self.assertTrue(self._on_main("P"), self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_missing_gh_holds_the_dependent_without_a_traceback(self):
        self._opened("P", marked=True)
        self._du()

        def gone(args, *a, **k):
            if isinstance(args, list) and args[:1] == ["gh"]:
                raise FileNotFoundError(2, "No such file or directory", "gh")
            return self._real_run(args, *a, **k)

        self._gh = gone     # type: ignore[method-assign]
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertNotIn("merge gates", line)
        self.assertIn("merged: could not read PR state for P", self.err)
        self.assertNotIn("merge before wave 0 failed", self.err)
        self.assertIn("issue_U", self.built)

    def test_an_internal_error_is_not_blamed_on_the_pr(self):
        self._opened("P", marked=True)
        self._du()
        self._run(["D", "U"], stack=[mock.patch.object(
            merge, "_merge_one", side_effect=RuntimeError("boom"))])
        line = self._held_line()
        self.assertIn("issue_P: its merge stopped on an error, see its `flow:` line above", line)
        self.assertNotIn("merge gates", line)
        self.assertNotIn("resolve at the PR", line)
        self.assertIn("flow: issue_P — merge before wave 0 failed (RuntimeError: boom)", self.err)
        self.assertIn("issue_U", self.built)

    def test_a_malformed_setting_is_not_blamed_on_the_pr(self):
        # Nothing at the PR can clear this, so nothing here may say so.
        self.cfg.expected_checks_error = "[driver].expected_checks must be a list, got 3"
        self._opened("P", marked=True)
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("[driver].expected_checks is malformed", line)
        self.assertIn("fix the setting in pdca.toml, then re-run", line)
        self.assertNotIn("merge gates", line)
        self.assertNotIn("resolve", line)
        self.assertIn("must be a list, got 3. issue_D is held; fix the setting in pdca.toml, "
                      "then re-run.\n", self.err)
        self.assertNotIn("resolve at the PR", self.err)
        self.assertNotIn(STOP, self.err)
        self.assertNotIn("ready issue_P", self.events)
        self.assertIn("issue_U", self.built)

    def test_a_prereq_for_another_target_is_left_alone(self):
        d = self._opened("P", marked=True)
        self._targets(d, "rel")     # its record still says main, so a human's merge counts
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("no merge-mode run merges it for issue_D (its brief does not target main), "
                      "so merge the PR by hand, then re-run", line)
        self.assertNotIn("let a merge-mode run", line)
        self.assertNotIn("ready issue_P", self.events)
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)

    def test_a_pr_for_another_target_on_record_gets_no_advice_to_merge_it(self):
        d = self._opened("P", marked=True)
        self._targets(d, "rel")
        self._record(d, base="rel")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("its PR is on record for rel (org/repo)", line)
        self.assertNotIn("merge the PR by hand", line)
        self.assertNotIn("ready issue_P", self.events)

    # -- a rebuilt attempt: the marker is for the patch its PR was published from -------------

    def _rebuilt_patch(self, d: Path) -> None:
        """Accepted ``d`` holds a new patch; its record and its PR's branch are the old one's."""
        first = _patch_sha(d)
        (d / "patch.diff").write_text(sb._new_file_patch("P2.txt", "rebuilt"), encoding="utf-8")
        self.assertNotEqual(_patch_sha(d), first)
        self.assertEqual(state.state(d), state.COMPLETE)

    def _old_head_held(self) -> None:
        """D is held naming P, with the way out, and P's PR was asked for nothing but its state."""
        line = self._held_line()
        self.assertIn("issue_P: its PR was published for a different patch.diff", line)
        self.assertIn("close it, re-publish (`pdca publish P`) and merge the new PR by hand", line)
        self.assertIn("merge this PR by hand as it stands", line)
        self.assertNotIn("let a merge-mode run", line)
        self.assertNotIn("merge gates", line)
        self._no_merge_attempt()
        self.assertNotIn("ready issue_P", self.events)
        self.assertTrue(self.pr["P"]["draft"])
        self.assertFalse(self._on_main("P"))
        self.assertIn("issue_U", self.built, self.err)

    def test_a_marked_pr_published_for_another_patch_is_not_merged(self):
        # The finding, as its probe ran it. The record and its branch are the first attempt's,
        # and a green rollup judges that old head: only the hash stops this run merging it.
        d = self._opened("P", marked=True)
        self._rebuilt_patch(d)
        self._du()
        self._run(["D", "U"])
        self._old_head_held()
        self.assertEqual([c for c in self.gh_calls if c[2] in ("ready", "merge")], [])
        self.assertTrue(self._marker("P"))      # it survives; it just licenses nothing here

    def test_a_pr_for_another_patch_holds_the_same_on_a_merged_edge(self):
        self._rebuilt_patch(self._opened("P", marked=True))
        self._du(ON_MERGED)
        self._run(["D", "U"])
        self._old_head_held()

    def test_a_named_prereq_for_another_patch_holds_too(self):
        self._rebuilt_patch(self._opened("P", marked=True))
        self._du()
        self._run(["P", "D", "U"])
        self._old_head_held()

    def test_a_pr_for_another_patch_costs_the_walk_no_gh_read(self):
        self._rebuilt_patch(self._opened("P", marked=True))
        self._du()
        self._run(["D", "U"])
        self._held_line()
        # `_runnable`'s state read, and the one its hold line makes for the way out.
        self.assertEqual(len(self.gh_calls), 2, self.gh_calls)

    def test_the_walk_does_not_go_past_a_pr_for_another_patch(self):
        self._opened("Q", marked=True)
        self._rebuilt_patch(self._opened("P", marked=True, edges="- **Depends on:** Q\n"))
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_P: its PR was published for a different", self._held_line())
        self.assertNotIn("ready issue_Q", self.events)
        self.assertNotIn("merge issue_Q", self.events)

    def test_3c_a_blocker_for_another_patch_is_named_on_the_dependents_line(self):
        self._rebuilt_patch(self._opened("Q", marked=True))
        self._opened("P", marked=True, edges="- **Depends on:** Q\n")
        self._du()
        self._run(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertIn("issue_Q: its PR was published for a different patch.diff", line)
        self.assertIn("(`pdca publish Q`)", line)
        self.assertEqual([c for c in self.gh_calls if c[2] != "view"], [])
        self.assertFalse(self._on_main("P"))
        self.assertFalse(self._on_main("Q"))

    def test_a_rebuild_accepted_with_no_publish_holds_its_dependent(self):
        # `--no-publish`, on a real iterate: the sign-off accepts a patch no PR holds.
        d = self._opened("P", marked=True)
        first = _patch_sha(d)
        self._iterated(d)
        self._run(["P"], do_publish=False)
        self.assertEqual(self.results["P"], state.COMPLETE, self.err)
        self.assertNotEqual(_patch_sha(d), first)
        self.assertTrue(self._marker("P"))      # the marker survives an iterate
        self._du()
        self._run(["D", "U"])
        self._old_head_held()

    def test_a_rebuild_whose_publish_failed_before_its_push_holds_its_dependent(self):
        # The real iterate and the real publish, refused at `git apply`: nothing is pushed, and
        # the record stays the first attempt's, as `integrate.unpublished` warns.
        d = self._opened("P", marked=True)
        tip = self.fx._tip("fix/P")
        self._iterated(d)
        self._run(["P"], do_publish=False)
        (d / "patch.diff").write_text("diff --git a/nope b/nope\n--- a/nope\n+++ b/nope\n"
                                      "@@ -1 +1 @@\n-no\n+yes\n", encoding="utf-8")
        before = (d / "publish.json").read_text(encoding="utf-8")
        rc, err = self._real_publish(d)
        self.assertEqual(rc, 1, err)
        self.assertIn("publish: step failed", err)
        self.assertEqual(self.fx._tip("fix/P"), tip)
        self.assertEqual((d / "publish.json").read_text(encoding="utf-8"), before)
        self._du()
        self._run(["D", "U"])
        self._old_head_held()

    def _rebuilt_and_its_publish_failed(self, *, marked: bool) -> Path:
        """P's PR is an earlier run's. This run rebuilds P in a non-final wave and its publish
        pushes nothing, so the wave boundary attempts the old PR, on the old record. That is
        red, so nothing merges; then the old head goes green for whatever run comes next."""
        d = self._opened("P", marked=marked, rollup="fail")
        self._iterated(d)
        self._planned("E", "- **Depends on:** P\n")
        self._run(["P", "E"], stack=self._real_publish_bundle(lambda *a, **k: 1))
        self.assertIn("issue_P is COMPLETE but publish did not complete", self.err)
        self.assertIn("flow: wave 0 did not merge; STOPPING", self.err)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.pr["P"]["rollup"] = "pass"
        self.gh_calls.clear()
        self.events.clear()
        self.built.clear()
        return d

    def test_a_wave_does_not_mark_a_pr_whose_re_publish_failed(self):
        # Marked now, the hash would be of the new patch on a record whose PR holds the old
        # one, so it would match, and the next run would merge that old head.
        self._rebuilt_and_its_publish_failed(marked=False)
        self.assertFalse(self._marker("P"), self.err)
        self.assertNotIn("merge_eligible_patch", self._rec(self.cfg.find_bundle("P")))
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_P: no merge-mode run may merge it (it is not marked), so merge the "
                      "PR by hand", self._held_line())
        self._no_merge_attempt()
        self.assertFalse(self._on_main("P"))

    def test_a_wave_keeps_the_hash_of_a_marked_pr_whose_re_publish_failed(self):
        d = self._rebuilt_and_its_publish_failed(marked=True)
        rec = self._rec(d)
        self.assertIs(rec["merge_eligible"], True)
        self.assertNotEqual(rec["merge_eligible_patch"], _patch_sha(d))
        self._du()
        self._run(["D", "U"])
        self._old_head_held()

    def test_merge_wave_never_re_stamps_a_marked_record(self):
        # The same answer with no `pushed` to go on: only a fresh record gets a new hash.
        d = self._opened("P", marked=True, rollup="fail")
        first = self._rec(d)["merge_eligible_patch"]
        self._rebuilt_patch(d)
        with mock.patch.object(subprocess, "run", self._gh), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(merge.merge_wave(self.cfg, [d]), 1)
        self.assertEqual(self._rec(d)["merge_eligible_patch"], first)

    def test_a_wave_marks_a_pr_its_own_publish_pushed(self):
        # The real `_publish_bundle` over a publish that writes its record: the other direction
        # of the two cases above, so "pushed this run" cannot simply mean "nothing".
        self.pr["P"] = {"state": "OPEN", "draft": True, "rollup": "fail"}
        self._planned("P")
        self._planned("E", "- **Depends on:** P\n")

        def publishes(_cfg, iid, **_k):
            self.fx._publish(iid, {f"{iid}.txt": f"{iid}\n"})
            return 0

        self._run(["P", "E"], stack=self._real_publish_bundle(publishes))
        rec = self._rec(self.cfg.find_bundle("P"))
        self.assertIs(rec.get("merge_eligible"), True, self.err)
        self.assertEqual(rec["merge_eligible_patch"], _patch_sha(self.cfg.find_bundle("P")))

    def _marked_by_a_refused_wave(self) -> Path:
        """P, published and marked by a real run's wave boundary, which refused its red rollup:
        what a later run finds, written by the real writer. Its checks have since gone green."""
        self.pr["P"] = {"state": "OPEN", "draft": True, "rollup": "fail"}
        self.finish["P"] = {"P.txt": "P\n"}
        self._planned("P")
        self._planned("E", "- **Depends on:** P\n")
        self._run(["P", "E"])
        self.assertIn("flow: wave 0 did not merge; STOPPING", self.err)
        self.assertEqual(self.marked_at_ready.get("P"), True, self.err)
        d = self.cfg.find_bundle("P")
        self.assertEqual(self._rec(d)["merge_eligible_patch"], _patch_sha(d))
        self.pr["P"]["rollup"] = "pass"
        self.gh_calls.clear()
        self.events.clear()
        self.built.clear()
        return d

    def test_the_marker_a_wave_wrote_licenses_the_patch_it_was_written_for(self):
        # What the writer records is what the walk compares: a later run merges this PR.
        self._marked_by_a_refused_wave()
        self._du()
        self._run(["D", "U"])
        self.assertIn("merge issue_P", self.events, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)

    def test_the_marker_a_wave_wrote_licenses_no_other_patch(self):
        self._rebuilt_patch(self._marked_by_a_refused_wave())
        self._du()
        self._run(["D", "U"])
        self._old_head_held()

    def test_a_rebuild_a_wave_re_published_is_marked_for_its_new_patch(self):
        # The way back to unattended: a run that builds and really publishes it again marks the
        # new head, for the new patch.
        d = self._marked_by_a_refused_wave()
        first = self._rec(d)["merge_eligible_patch"]
        self._iterated(d)
        self.fx._delete("fix/P")    # the fixture's publish pushes without force, unlike the real
        self.finish["P"] = {"P2.txt": "rebuilt\n"}
        self.pr["P"]["rollup"] = "fail"
        self._run(["P", "E"])
        self.assertIn("flow: wave 0 did not merge; STOPPING", self.err)
        self.assertEqual(self._rec(d)["merge_eligible_patch"], _patch_sha(d))
        self.assertNotEqual(_patch_sha(d), first)
        self.pr["P"]["rollup"] = "pass"
        self._du()
        self._run(["D", "U"])
        self.assertIn("P2.txt", self.base_files["issue_D"], self.err)
        self.assertNotIn("P.txt", self.base_files["issue_D"], self.err)

    def test_merging_a_pr_for_another_patch_by_hand_releases_the_dependent(self):
        # The way out, as it stands: the human takes the old head knowingly.
        self._rebuilt_patch(self._opened("P", marked=True))
        self._du()
        self._run(["D", "U"])
        self._old_head_held()
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_closing_re_publishing_and_merging_by_hand_releases_the_dependent(self):
        # The way out, for the new patch. The real publish pushes to the same branch, as a real
        # bundle's first attempt would have. The new record has no marker, so a human merges.
        self.cfg.fix_branch_pattern = "fix/{id}"
        d = self._opened("P", marked=True)
        self._rebuilt_patch(d)
        self._du()
        self._run(["D"])
        self.assertIn("close it, re-publish (`pdca publish P`)", self._held_line())
        self.pr["P"]["state"] = "CLOSED"
        rc, err = self._real_publish(d)
        self.assertEqual(rc, 0, err)
        self.pr["P"] = {"state": "OPEN", "draft": True, "rollup": "pass"}
        self.assertNotIn("merge_eligible", self._rec(d))
        self.gh_calls.clear()
        self._run(["D"])
        self.assertIn("issue_P: no merge-mode run may merge it (it is not marked), so merge the "
                      "PR by hand", self._held_line())
        self._no_merge_attempt()
        self._merged_by_hand("P")
        self._run(["D"])
        self.assertEqual(self.results["D"], state.COMPLETE, self.err)
        self.assertIn("P2.txt", self.base_files["issue_D"], self.err)
        self.assertNotIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_marker_is_not_trusted_for_a_patch_it_cannot_read(self):
        # With no hash on record and none to compute, neither side has an answer: not a match.
        d = self._opened("P")
        self.assertEqual(merged.patch_hash(d), _patch_sha(d))
        self.assertTrue(merged.marked_for_this_patch(d, {"merge_eligible_patch": _patch_sha(d)}))
        self.assertFalse(merged.marked_for_this_patch(d, {"merge_eligible_patch": "other"}))
        self.assertFalse(merged.marked_for_this_patch(d, {}))
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertIsNone(merged.patch_hash(d))
            self.assertFalse(merged.marked_for_this_patch(d, {}))

    # -- (4) nothing to merge ---------------------------------------------------------------

    def test_4_a_close_prereq_releases_its_dependent_with_no_gh_call(self):
        p = self._planned("P")
        (p / "patch.diff").write_text("", encoding="utf-8")
        sb._accept(p)
        self.assertEqual(state.state(p), state.COMPLETE)
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_D", self.built, self.err)
        self.assertEqual(self.gh_calls, [])

    # -- (5) dry run and --no-publish -------------------------------------------------------

    def _nothing_happens(self) -> None:
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_D", self.built, self.err)
        self.assertFalse(self._on_main("P"))

    def test_5_no_publish_merges_and_holds_nothing(self):
        self._opened("P", marked=True)
        self._du()
        self._run(["D", "U"], do_publish=False)
        self._nothing_happens()

    def test_5_a_merged_edge_keeps_its_hold_without_publishing_and_merges_nothing(self):
        # `Depends on (merged)` already holds there, with one state read (#186): this bundle
        # only changes what the line says, since merge mode has no line either way.
        self._opened("P", marked=True)
        self._du(ON_MERGED)
        self._run(["D", "U"], do_publish=False)
        self.assertIn("let a merge-mode run merge it (it is marked)", self._held_line())
        self._no_merge_attempt()
        self.assertFalse(self._on_main("P"))
        self.assertIn("issue_U", self.built)

    def test_5_a_dry_run_merges_and_holds_nothing(self):
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
        self._opened("P", marked=True)
        self._du()
        self._run(["D", "U"])
        self._nothing_happens()

    # -- the marker -------------------------------------------------------------------------

    def test_merge_wave_marks_before_the_ready_mark_and_a_refusal_still_stops(self):
        self.pr["A"] = {"state": "OPEN", "draft": True, "rollup": "fail"}
        self.finish["A"] = {"A.txt": "A\n"}
        self._planned("A")
        self._planned("B", "- **Depends on:** A\n")
        self._run(["A", "B"])
        self.assertEqual(self.marked_at_ready.get("A"), True, self.err)
        self.assertTrue(self._marker("A"))              # kept after the refusal
        self.assertIn(STOP, self.err)                   # the wave boundary's text, unchanged
        self.assertIn("flow: wave 0 did not merge; STOPPING", self.err)
        self.assertNotIn("issue_B", self.built)

    def test_the_final_wave_is_not_marked(self):
        self.finish.update({"A": {"A.txt": "A\n"}, "B": {"B.txt": "B\n"}})
        self._planned("A")
        self._planned("B", "- **Depends on:** A\n")
        self._run(["A", "B"])
        self.assertTrue(self._marker("A"), self.err)
        self.assertTrue(self._on_main("A"), self.err)
        self.assertFalse(self._marker("B"), self.err)
        self.assertNotIn("merge_eligible_patch", self._rec(self.cfg.find_bundle("B")))
        self.assertTrue(self.pr["B"]["draft"])

    def test_a_failed_marker_write_stops_nothing_and_leaves_the_record_whole(self):
        d = self._opened("P")
        before = (d / "publish.json").read_text(encoding="utf-8")
        err = io.StringIO()
        real_write = Path.write_text

        def dies_part_way(path, text, *a, **k):
            if "publish.json" in path.name:
                real_write(path, text[:10], *a, **k)
                raise OSError("No space left on device")
            return real_write(path, text, *a, **k)

        with mock.patch.object(subprocess, "run", self._gh), \
                mock.patch.object(Path, "write_text", dies_part_way), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(merge.merge_wave(self.cfg, [d]), 0)
        self.assertIn("could not mark issue_P", err.getvalue())
        self.assertEqual((d / "publish.json").read_text(encoding="utf-8"), before)
        self.assertEqual(list(d.glob(".*partial")), [])
        self.assertTrue(self._on_main("P"))

    def test_merge_one_alone_does_not_mark(self):
        d = self._opened("P")
        with mock.patch.object(subprocess, "run", self._gh), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(merge._merge_one(self.cfg, d, dry_run=False, method="merge",
                                              fetched=set()), 0)
        self.assertFalse(self._marker("P"))

    def test_a_re_publish_writes_a_record_without_the_marker(self):
        # The real publish, as a rebuilt attempt gets it. What the marker licensed is gone
        # with the head it was written for, so the new PR holds D until it is merged by hand.
        d = self._opened("P", marked=True)
        (d / "patch.diff").write_text(sb._new_file_patch("P2.txt", "rebuilt"), encoding="utf-8")
        rc, err = self._real_publish(d)
        self.assertEqual(rc, 0, err)
        rec = self._rec(d)
        self.assertEqual(rec["pr_url"], "https://example.test/pr/P")
        self.assertNotIn("merge_eligible", rec)
        self.assertNotIn("merge_eligible_patch", rec)
        self.gh_calls.clear()
        self._du()
        self._run(["D", "U"])
        self.assertIn("issue_P: no merge-mode run may merge it (it is not marked), so merge the "
                      "PR by hand", self._held_line())
        self._no_merge_attempt()

    # -- every refusal: who is held and what clears it on this path, the wave boundary's
    # text on the other. The wave boundary's cases run through `merge_wave`, the caller that
    # was there before the fix: they pass with it reverted, so the text is proven unchanged.

    _REFUSALS = {
        "ready-mark": ({"fail": {"ready"}},
                       "could not be marked ready to merge. ",
                       "STOP: later waves are NOT run; resolve at the PR, then re-run.\n",
                       "resolve at the PR, then re-run."),
        "failed merge": ({"fail": {"merge"}},
                         "that requires up-to-date branches. ",
                         "STOP: later waves are NOT run; resolve at the PR, then re-run.\n",
                         "resolve at the PR, then re-run."),
        "rollup": ({"rollup": "fail"},
                   "a check is FAILING — ci (fail). ",
                   "The host's required-checks config is not enough: this wave's base must be "
                   "green before the next wave builds on it. STOP: later waves are NOT run; "
                   "re-run once the checks are green (the run resumes idempotently), or set "
                   "[driver] merge_requires = \"required\" to merge on the host's required "
                   "checks alone, or raise [driver] merge_wait_secs if the checks just take "
                   "longer than that to report.\n",
                   "resolve at the PR, then re-run once the checks are green, or set [driver] "
                   "merge_requires = \"required\" to merge on the host's required checks alone, "
                   "or raise [driver] merge_wait_secs if the checks just take longer than that "
                   "to report."),
        "malformed setting": ({"settings": {"expected_checks_error": "bad setting"}},
                              "bad setting. ",
                              "STOP: later waves are NOT run; fix the setting in pdca.toml, "
                              "then re-run (the run resumes idempotently).\n",
                              "fix the setting in pdca.toml, then re-run."),
        "target base": ({"fail": {BASE_READ}},
                        "its base could not be checked against its target base",
                        "STOP: later waves are NOT run, and the PR is left draft or ready, as "
                        "it was. Re-run once that is resolved (the run resumes idempotently).\n",
                        "the PR is left draft or ready, as it was; re-run once that is "
                        "resolved."),
        "failed fetch": ({"settings": {"base_remote": "no-such-remote"}},
                         "git failed: `git fetch no-such-remote`",
                         "STOP: later waves are NOT run; resolve at the PR, then re-run (the "
                         "run resumes idempotently).\n",
                         "resolve that, then re-run."),
        "review remarks": ({"fail": {"merge"}, "merge_state": "BLOCKED",
                            "decision": "CHANGES_REQUESTED"},
                           "the host blocks its merge",
                           "STOP: later waves are NOT run; resolve at the PR, then re-run (the "
                           "run resumes idempotently).\n",
                           "resolve at the PR, then re-run."),
        "failed update": ({"fail": {"update-branch"}, "behind": True},
                          "it is behind its base and could not be brought up to date",
                          "STOP: later waves are NOT run; resolve at the PR, then re-run (the "
                          "run resumes idempotently).\n",
                          "resolve at the PR, then re-run."),
        "update never landed": ({"behind": True, "update_never_lands": True},
                                "its update had not landed after 0s",
                                "STOP: later waves are NOT run; resolve at the PR, then re-run "
                                "(the run resumes idempotently).\n",
                                "resolve that, then re-run."),
        "head moved": ({"moved_at_checks": "head"},
                       "while its checks were being read, so the green rollup may not be that "
                       "head's",
                       "STOP: later waves are NOT run; resolve at the PR, then re-run (the run "
                       "resumes idempotently).\n",
                       "resolve at the PR, then re-run."),
        "retargeted on the host": ({"moved_at_checks": "retarget"},
                                   "its base on the host is now `elsewhere`",
                                   "STOP: later waves are NOT run; resolve at the PR, then re-run "
                                   "(the run resumes idempotently).\n",
                                   "resolve that, then re-run."),
        "base moved": ({"moved_at_checks": "base"},
                       "its base moved while its checks were being read",
                       "STOP: later waves are NOT run; resolve at the PR, then re-run (the run "
                       "resumes idempotently).\n",
                       "resolve at the PR, then re-run."),
    }

    def _refused(self, drive, *, fail: set[str] | None = None, behind: bool = False,
                 settings: dict[str, object] | None = None, **pr) -> str:
        """A marked PR of its own, refused the way the arguments say, through ``drive(id)``;
        what the refusal printed. Each call has a bundle and a branch of its own."""
        iid = f"R{len(self.pr)}"
        self._opened(iid, marked=True)
        self.pr[iid].update(pr)
        if behind:
            self.fx._push_from_human({f"{iid}-behind.txt": "behind\n"}, to="main")
        self.failing = fail or set()
        with ExitStack() as ctx:
            for name, value in (settings or {}).items():
                ctx.enter_context(mock.patch.object(self.cfg, name, value))
            out = drive(iid)
        self.failing = set()
        return out

    def _through_the_wave_boundary(self, iid: str) -> str:
        err = io.StringIO()
        with mock.patch.object(subprocess, "run", self._gh), redirect_stdout(io.StringIO()), \
                redirect_stderr(err):
            rc = merge.merge_wave(self.cfg, [self.cfg.bundle(iid)], method="merge")
        self.assertEqual(rc, 1, err.getvalue())
        return err.getvalue()

    def _through_the_flow(self, iid: str) -> str:
        self._planned(f"D{iid}", f"- **{PLAIN}:** {iid}\n")
        self._run([f"D{iid}"])
        self.assertIn(f"issue_{iid}: ", self._held_line(f"D{iid}"))
        return self.err

    def test_refusals_say_who_is_held_and_what_clears_it_before_wave_0(self):
        for name, (kw, why, _wave, todo) in self._REFUSALS.items():
            with self.subTest(name):
                out = self._refused(self._through_the_flow, **kw)
                dep = f"issue_DR{len(self.pr) - 1}"
                self.assertIn(why, out)
                self.assertIn(f"{dep} is held; {todo}\n", out)
                self.assertNotIn("STOP", out)
                self.assertNotIn("later waves", out)

    def test_refusals_keep_the_wave_text_at_the_wave_boundary(self):
        for name, (kw, why, wave, _todo) in self._REFUSALS.items():
            with self.subTest(name):
                out = self._refused(self._through_the_wave_boundary, **kw)
                self.assertIn(why, out)
                self.assertIn(wave, out)

    def test_refusals_the_walk_never_reaches_keep_the_wave_text_too(self):
        no_pr = self._opened("A")
        self._record(no_pr, pr_url="")
        stacked = self._opened("B")
        self._record(stacked, mode="stacked")
        self.assertIn("merge: issue_A is COMPLETE but has no recorded PR — cannot merge a wave "
                      "whose member wasn't published. STOP.\n",
                      self._through_the_wave_boundary("A"))
        self.assertIn("merge mode does not ready or merge a PR this run did not open. STOP: "
                      "later waves are NOT run. Run this batch in the default [driver] "
                      "wave_mode = \"stack\", or take this bundle out of this merge-mode batch, "
                      "then re-run. The PR itself stays the human's to ready or merge, but "
                      "doing so does not let this merge-mode run continue: its record is "
                      "refused the same way merged or not.\n",
                      self._through_the_wave_boundary("B"))

    def test_refusals_the_walk_never_reaches_never_say_stop(self):
        no_pr = self._opened("A")
        self._record(no_pr, pr_url="")
        stacked = self._opened("B")
        self._record(stacked, mode="stacked")
        no_repo = self._opened("C")
        self._record(no_repo, repo="")
        for d, todo in ((no_pr, "publish it, then re-run."),
                        (stacked, "remove the edge to it from the dependent's brief, then "
                                  "re-run."),
                        (no_repo, "resolve that, then re-run.")):
            err = io.StringIO()
            with self.subTest(d.name), mock.patch.object(subprocess, "run", self._gh), \
                    redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = merge._merge_one(self.cfg, d, dry_run=False, method="merge", fetched=set(),
                                      held="issue_D is held")
                self.assertEqual(rc, 1)
                self.assertIn("issue_D is held; ", err.getvalue())
                self.assertIn(todo, err.getvalue())
                self.assertNotIn("STOP", err.getvalue())

    def test_the_merged_module_is_still_what_releases(self):
        # `_runnable` reads `merged_into`; a stubbed "no" holds even a just-merged prereq, and
        # the way out for a marked PR no note was left on is the one the brief names.
        self._opened("P", marked=True)
        self._du()
        no = mock.patch.object(merged, "merged_into", lambda *a, **k: False)
        self._run(["D", "U"], stack=[no])
        self.assertIn("issue_P: let a merge-mode run merge it (it is marked), or merge the PR by "
                      "hand, then re-run.", self._held_line())

    # -- the claim: one live run on a prerequisite before wave 0 (#565) --------------------

    def _another_run_holds(self, *iids: str) -> drive_claim.Run:
        """Another live run's claim on each of ``iids``: the real lock on its own handle, which
        refuses this run as another process does (`drive_claim.py`, "The RUN owns its claims";
        `test_another_process_holding_a_prereq_is_named_with_its_pid` uses a real process)."""
        other = drive_claim.Run(self.cfg)
        self.addCleanup(other.close)
        for iid in iids:
            self.assertIsNone(other.take(self.cfg.find_bundle(iid)))
        return other

    def _claim_is_free(self, iid: str) -> bool:
        """Could another run claim ``iid`` right now? Asked with the same lock, then let go."""
        probe = drive_claim.Run(self.cfg)
        try:
            return probe.take(self.cfg.find_bundle(iid)) is None
        finally:
            probe.close()

    def _claim_held_line(self, dependent: str = "D", prereq: str = "issue_P",
                         base: str = "main") -> str:
        """``dependent`` was not built and ONE line says who holds ``prereq`` and the way out."""
        self.assertNotIn(f"issue_{dependent}", self.built, self.err)
        self.assertEqual(self.results[dependent], state.PLANNED)
        lines = [x for x in self.err.splitlines()
                 if f"flow: issue_{dependent} held" in x and prereq in x]
        self.assertEqual(len(lines), 1, self.err)
        line = lines[0]
        self.assertIn(f"{prereq} not known to be merged into {base} (org/repo)", line)
        self.assertIn(f"{prereq}: it is held by another live `flow` run", line)
        self.assertIn("did not judge or merge it", line)
        self.assertIn("let that run finish (or stop it), then re-run", line)
        for wrong in ("line of this run", "carries them", "by hand", "merge gates"):
            self.assertNotIn(wrong, line)
        return line

    def _another_run_holds_p(self, field: str) -> None:
        # The race the claim closes: everything else about P says "merge it".
        d = self._opened("P", marked=True)
        self._du(field)
        record = (d / "publish.json").read_bytes()
        self._another_run_holds("P")
        rc = self._cli(["D", "U"])
        self._claim_held_line()
        self.assertEqual(self.gh_calls, [])     # never judged, let alone readied or merged
        self.assertEqual(self.events.count("ready issue_P"), 0)
        self.assertEqual(self.events.count("merge issue_P"), 0)
        self.assertEqual((d / "publish.json").read_bytes(), record)
        self.assertFalse(self._on_main("P"))
        self.assertIn("issue_U", self.built)
        self.assertEqual(rc, 1)

    def test_a_prereq_another_run_holds_is_not_touched_and_holds_its_dependent(self):
        self._another_run_holds_p(PLAIN)

    def test_a_prereq_another_run_holds_holds_the_same_on_a_merged_edge(self):
        self._another_run_holds_p(ON_MERGED)

    def test_another_process_holding_a_prereq_is_named_with_its_pid(self):
        # A claim could be per-process, so one case has the competing driver be a real one.
        d = self._opened("P", marked=True)
        self._du()
        code = ("import pathlib, sys, types\n"
                "from pdca_harness import drive_claim\n"
                "cfg = types.SimpleNamespace(process_dir=pathlib.Path(sys.argv[1]))\n"
                "run = drive_claim.Run(cfg)\n"
                "assert run.take(pathlib.Path(sys.argv[2])) is None\n"
                "print('held', flush=True)\n"
                "sys.stdin.read()\n")
        env = {**os.environ, "PYTHONPATH": str(Path(drive_claim.__file__).resolve().parents[1])}
        child = subprocess.Popen([sys.executable, "-c", code, str(self.cfg.process_dir), str(d)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, timeout=30)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "held")
        rc = self._cli(["D", "U"])
        self.assertIn(f"held by another live `flow` run (pid {child.pid})",
                      self._claim_held_line())
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_U", self.built)
        self.assertEqual(rc, 1)

    def test_the_way_out_of_a_claim_hold_works(self):
        self._opened("P", marked=True)
        self._du()
        other = self._another_run_holds("P")
        self._cli(["D", "U"])
        self._claim_held_line()
        other.close()
        self.assertEqual(self._cli(["D"]), 0, self.err)
        self.assertTrue(self._on_main("P"), self.err)
        self.assertLess(self.events.index("merge issue_P"), self.events.index("build issue_D"))
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_merged_prereq_another_run_holds_holds_too(self):
        # Not judged means not judged: this run reads nothing of a bundle another run holds.
        self._opened("P", marked=True)
        self._du()
        self._merged_by_hand("P")
        other = self._another_run_holds("P")
        self._cli(["D", "U"])
        self._claim_held_line()
        self.assertEqual(self.gh_calls, [])
        other.close()
        self.assertEqual(self._cli(["D"]), 0, self.err)
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)
        self.assertEqual(self.events.count("merge issue_P"), 0)

    def test_a_close_prereq_another_run_holds_holds_too(self):
        p = self._planned("P")
        (p / "patch.diff").write_text("", encoding="utf-8")
        sb._accept(p)
        self._du()
        self._another_run_holds("P")
        self._cli(["D", "U"])
        self._claim_held_line()
        self.assertEqual(self.gh_calls, [])

    def test_a_prereq_this_run_cannot_claim_holds_the_same(self):
        # Fail closed (`drive_claim.py`): a claim that cannot be recorded is no claim.
        self._opened("P", marked=True)
        self._du()
        real_lock = act._lock_exclusive

        def no_lock_for_p(fh, *, wait: bool = True) -> None:
            if "issue_P" in str(fh.name):
                raise OSError(errno.ENOLCK, "No locks available")
            real_lock(fh, wait=wait)

        locks = mock.patch.object(act, "_lock_exclusive", no_lock_for_p)
        rc = self._cli(["D", "U"], stack=[locks])
        line = next(x for x in self.err.splitlines() if "flow: issue_D held" in x)
        self.assertIn("issue_P not known to be merged into main (org/repo)", line)
        self.assertIn("could not record its claim on it", line)
        self.assertIn("No locks available", line)
        self.assertIn("a writable directory on a filesystem that supports file locks", line)
        self.assertNotIn("issue_D", self.built)
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_U", self.built)
        self.assertEqual(rc, 1)

    def test_the_sweep_holds_a_dependent_of_a_prereq_another_run_holds(self):
        self._opened("P", marked=True)
        self._du()
        self._another_run_holds("P")
        with drive_claim.run(self.cfg) as claims:
            self._run([], sweep=True, claims=claims)
        self._claim_held_line()
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_U", self.built)

    def test_a_prereq_of_a_prereq_another_run_holds_stops_the_merge_above_it(self):
        self._opened("Q", marked=True)
        self._opened("P", edges=f"- **{PLAIN}:** Q\n", marked=True)
        self._du()
        self._another_run_holds("Q")
        self._cli(["D", "U"])
        line = self._held_line()
        self.assertIn("issue_P", line)
        self.assertIn("its own prerequisite(s) issue_Q are not known to be merged into main", line)
        self.assertIn("issue_Q: it is held by another live `flow` run", line)
        self.assertEqual([e for e in self.events if not e.startswith("build")], [])
        # P was judged, under its own claim; Q was not read at all.
        self.assertTrue(self.gh_calls)
        for call in self.gh_calls:
            self.assertEqual(call[:4], ["gh", "pr", "view", "https://example.test/pr/P"], call)
        self.assertIn("issue_U", self.built)

    def test_a_merged_prereq_keeps_the_walk_off_one_another_run_holds_below_it(self):
        # The walk stops at a merged prerequisite, so what is below it holds nobody.
        self._opened("Q", marked=True)
        self._opened("P", edges=f"- **{PLAIN}:** Q\n", marked=True)
        self._du()
        self._merged_by_hand("P")
        self._another_run_holds("Q")
        self.assertEqual(self._cli(["D"]), 0, self.err)
        self.assertIn("issue_D", self.built)
        self.assertEqual([e for e in self.events if not e.startswith("build")], [])

    def test_each_edge_is_judged_on_its_own_when_another_run_holds_one(self):
        self._opened("P", marked=True)
        self._opened("R", marked=True)
        self._planned("D", f"- **{PLAIN}:** P, R\n")
        self._planned("U")
        self._another_run_holds("R")
        self._cli(["D", "U"])
        self._claim_held_line(prereq="issue_R")
        self.assertTrue(self._on_main("P"), self.err)
        self.assertFalse(self._on_main("R"))
        self.assertEqual(self.events.count("merge issue_R"), 0)

    def test_a_dependent_held_for_a_claim_and_a_merge_gets_a_line_for_each(self):
        self._opened("P")
        self._opened("R", marked=True)
        self._planned("D", f"- **{PLAIN}:** P, R\n")
        self._another_run_holds("R")
        self._cli(["D"])
        self._claim_held_line(prereq="issue_R")
        lines = [x for x in self.err.splitlines()
                 if "flow: issue_D held" in x and "issue_P not merged into main (org/repo)" in x]
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("merge the PR by hand", lines[0])

    def test_two_dependents_on_two_bases_each_get_their_own_line(self):
        self._opened("P", marked=True)
        self._du()
        e = self._planned("E", f"- **{PLAIN}:** P\n")
        self._targets(e, "rel")
        self._another_run_holds("P")
        self._cli(["D", "E", "U"])
        self._claim_held_line()
        self._claim_held_line("E", base="rel")
        self.assertEqual(self.gh_calls, [])
        self.assertIn("issue_U", self.built)

    def test_a_dependent_of_a_dependent_held_for_a_claim_is_not_built_either(self):
        self._opened("P", marked=True)
        self._du()
        self._planned("E", f"- **{PLAIN}:** D\n")
        self._another_run_holds("P")
        self.assertEqual(self._cli(["D", "E"]), 1, self.err)
        self._claim_held_line()
        self.assertEqual(self.built, [])

    def test_the_walk_holds_a_claim_while_it_works_and_lets_go_when_it_ends(self):
        self._opened("P", marked=True)
        self._du()
        free_at: dict[str, bool] = {}
        real_gh, real_wave = self._gh, self._drive_wave

        def gh(args, *a, **kw):
            if isinstance(args, list) and args[:2] == ["gh", "pr"] and args[3].endswith("/P"):
                free_at.setdefault(args[2], self._claim_is_free("P"))
            return real_gh(args, *a, **kw)

        def wave(cfg, bundles, **kw):
            for d in bundles:
                free_at.setdefault(d.name, self._claim_is_free("P"))
            return real_wave(cfg, bundles, **kw)

        self._gh, self._drive_wave = gh, wave
        self.assertEqual(self._cli(["D"]), 0, self.err)
        self.assertLessEqual({"view", "ready", "checks", "merge"}, set(free_at), free_at)
        for verb in ("view", "ready", "checks", "merge"):
            self.assertFalse(free_at[verb], verb)   # another run is refused all the while
        self.assertTrue(free_at["issue_D"], self.err)   # … and not for the rest of this run

    def test_two_runs_reaching_one_marked_pr_never_both_touch_it(self):
        # The race itself: a second run starts while the first is part-way through P's merge.
        self._opened("P", marked=True)
        self._du()
        self._planned("E", f"- **{PLAIN}:** P\n")
        second: list[tuple[int, str]] = []
        real_gh = self._gh

        def gh(args, *a, **kw):
            if isinstance(args, list) and args[:3] == ["gh", "pr", "ready"] and not second:
                second.append((0, ""))
                before = len(self.gh_calls)
                rc = self._cli(["E"])
                second[0] = (rc, self.err)
                self.assertEqual(self.gh_calls[before:], [], self.err)
            return real_gh(args, *a, **kw)

        self._gh = gh
        self.assertEqual(self._cli(["D"]), 0, self.err)
        rc, err = second[0]
        self.assertEqual(rc, 1, err)
        self.assertIn("flow: issue_E held — prerequisite(s) issue_P not known to be merged", err)
        self.assertIn("held by another live `flow` run", err)
        self.assertEqual(self.events.count("ready issue_P"), 1)
        self.assertEqual(self.events.count("merge issue_P"), 1)
        self.assertEqual(self.events.count("undo issue_P"), 0)
        self.assertEqual(self.built, ["issue_D"])
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)
        # Its way out: E builds on the merged base once it is re-run.
        self.assertEqual(self._cli(["E"]), 0, self.err)
        self.assertIn("P.txt", self.base_files["issue_E"], self.err)
        self.assertEqual(self.events.count("merge issue_P"), 1)

    def test_the_walk_claims_a_prereq_it_will_not_merge_and_lets_that_go_too(self):
        self._opened("P")
        self._du()
        free_at: dict[str, bool] = {}
        real_wave = self._drive_wave

        def wave(cfg, bundles, **kw):
            free_at["P"] = self._claim_is_free("P")
            return real_wave(cfg, bundles, **kw)

        self._drive_wave = wave
        self._cli(["D", "U"])
        self.assertTrue(free_at["P"], self.err)     # U's build, with D held

    def test_the_walk_never_lets_go_of_what_the_run_drives(self):
        # D's prerequisite is in the run, so the walk reaches a bundle the run holds already.
        self.finish["A"] = {"A.txt": "A\n"}
        self._planned("A")
        self._planned("D", f"- **{PLAIN}:** A\n")
        free_at: dict[str, tuple[bool, bool]] = {}
        real_wave = self._drive_wave

        def wave(cfg, bundles, **kw):
            for d in bundles:
                free_at[d.name] = (self._claim_is_free("A"), self._claim_is_free("D"))
            return real_wave(cfg, bundles, **kw)

        self._drive_wave = wave
        self.assertEqual(self._cli(["A", "D"]), 0, self.err)
        self.assertEqual(free_at, {"issue_A": (False, False), "issue_D": (False, False)})
        self.assertTrue(self._claim_is_free("A"))   # the scope let go when the run ended

    def test_a_finished_named_prereq_is_claimed_again_for_its_merge(self):
        # The CLI claims a named id and `flow_ids` lets a finished one go; the walk takes it back.
        self._opened("P", marked=True)
        self._du()
        free_at: dict[str, bool] = {}
        real_gh = self._gh

        def gh(args, *a, **kw):
            if isinstance(args, list) and args[:3] == ["gh", "pr", "merge"]:
                free_at["merge"] = self._claim_is_free("P")
            return real_gh(args, *a, **kw)

        self._gh = gh
        self.assertEqual(self._cli(["P", "D"]), 0, self.err)
        self.assertEqual(free_at, {"merge": False})
        self.assertIn("P.txt", self.base_files["issue_D"], self.err)

    def test_a_named_prereq_another_run_holds_refuses_the_run_before_the_walk(self):
        self._opened("P", marked=True)
        self._du()
        self._another_run_holds("P")
        self.assertEqual(self._cli(["P", "D"]), 1)
        self.assertIn("refusing to start a second driver", self.err)
        self.assertEqual(self.built, [])
        self.assertEqual(self.gh_calls, [])

    def test_a_run_that_publishes_nothing_is_not_held_by_a_claim(self):
        # (5) again: no merge, no hold and no `gh` call, so nothing here to serialize.
        for tag, stub in (("no-publish", False), ("dry-run", True)):
            with self.subTest(tag):
                if stub:
                    self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
                iid = f"P{int(stub)}"
                self._opened(iid, marked=True)
                self._planned(f"D{int(stub)}", f"- **{PLAIN}:** {iid}\n")
                other = self._another_run_holds(iid)
                rc = self._cli([f"D{int(stub)}"], do_publish=stub)
                self.assertEqual(rc, 0, self.err)
                self.assertIn(f"issue_D{int(stub)}", self.built, self.err)
                self.assertEqual(self.gh_calls, [])
                other.close()


if __name__ == "__main__":
    unittest.main()
