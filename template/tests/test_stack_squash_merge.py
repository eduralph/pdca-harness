"""Stack mode needs merge commits, and says so where it can help (#626).

Stack mode is not changed to support a squash or rebase merge. It warns up front, and it
recognises one by name when it happens:

- publish warns about a repo without merge commits for a bundle other bundles build on, not
  only for a PR cut from a stack base, and an ``Onto branch`` commit counts too;
- the fold, meeting a gone branch whose PR's merge commit is on the base but whose head is on
  neither the base nor the line, says it was a squash or rebase merge and what to do, for any
  kind of publish record;
- the re-issue carry names the same shape, on a line already on origin too, and a finished
  dependent stays held, with advice that points the right way. The way out it gives is
  followed here too, and so is what the run does not check about it. Where that way out drops
  an edge, those lines and the fold's stop also name the line to delete, the re-issued batch's;
- a dependent left naming such a prerequisite in a run without it, where its record never
  counts as merged, is told to drop the edge, not to wait for a merge or re-add it, and to
  delete a line of the run that lacks the merge, which nothing checks once the edge is gone,
  or one this run never got to check. It is told the PR merged, never that the base has its
  work: a revert there is not seen.

Real git: the ``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare
``origin``, a primary checkout and a scratch clone that plays the maintainer), loaded by path
as tests/test_flow_merged_prereq_on_line.py does. ``gh`` is answered by a stub in front of
``subprocess.run``, which follows ``merged.merge_and_head``'s documented contract.

Only modules are imported (never a symbol the change adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_stack_squash_merge
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import flow, integrate, publish, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-10-14"
TARGET = sb.TARGET
ABSENT = sb.ABSENT
ELSEWHERE = "merged somewhere other than"
OTHER_BASE = "may have merged into a base other than"
NO_MERGE_COMMITS = "does not allow merge commits"
EDGE_HINT = "remove the `Depends on:"
NAMED = "squash- or rebase-merged"
DROP = "dropping issue_P from the brief of any bundle of the re-issued batch that names it"
DELETE = "deleting any line on origin that batch would continue"
READD = "in the same `pdca flow <ids>` command"
WAIT = "until their PR merges into main"
# A legacy `Stacks on:` PR's record: it targets its parent's branch, not the base.
LEGACY = {"mode": "stacked-pr", "base": "fix/parent"}
# A record for a repository whose name has changed since: only `repo` differs.
RENAMED = {"repo": "old-org/old-name"}


def _onto_record(iid: str) -> dict[str, str]:
    """An ``Onto branch`` record (``publish._publish_stacked``): a commit on another PR."""
    return {"mode": "stacked", "base": f"origin/fix/{iid}"}


class _Git(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.prs: dict[str, str] = {}         # PR state by id (absent: OPEN)
        self.pr_heads: dict[str, str] = {}    # PR head by id, kept once its branch is gone
        self.merge_oid: dict[str, str] = {}   # id → what `gh` reports as its merge commit
        self.gh_fail_merge_commit = False     # `gh pr view … mergeCommit` exits 1
        self.reads_before_gh_fails: int | None = None   # … only after this many answered
        self.merge_commit_reads: list[str] = []
        self.merge_commit_allowed: bool | None = False   # None: `gh repo view` exits 1
        self.repo_views = 0
        self._real_run = subprocess.run

    def _gh_stub(self, args, *a, **kw):
        """``gh pr view <url> --json <fields>``, ``gh pr list --head <branch>`` (one open PR
        on it) and ``gh repo view``, answered from this test's fields. Everything else runs
        for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        if args[1] == "repo":
            self.repo_views += 1
            if self.merge_commit_allowed is None:
                return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
            return subprocess.CompletedProcess(args, 0, json.dumps(
                {"mergeCommitAllowed": self.merge_commit_allowed,
                 "squashMergeAllowed": True}), "")
        if args[2] == "list":
            branch = args[args.index("--head") + 1]
            return subprocess.CompletedProcess(args, 0, json.dumps(
                [{"url": f"https://example.test/pr/{branch}", "headRefName": branch,
                  "headRepositoryOwner": {"login": publish._fork_owner(self.fx.primary)}}]), "")
        iid = str(args[3]).rsplit("/", 1)[-1]
        if "mergeCommit" in str(args[-1]):
            self.merge_commit_reads.append(iid)
            if self.gh_fail_merge_commit or (
                    self.reads_before_gh_fails is not None
                    and len(self.merge_commit_reads) > self.reads_before_gh_fails):
                return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
        head = self.pr_heads.get(iid) or (
            self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else "")
        oid = self.merge_oid.get(iid)
        out = json.dumps({"state": self.prs.get(iid, "OPEN"), "headRefOid": head,
                          "mergeCommit": {"oid": oid} if oid else None})
        return subprocess.CompletedProcess(args, 0, out, "")

    def _squash(self, iid: str, *, into: str = "main") -> str:
        """The maintainer squash-merges ``iid``'s PR into ``into``: the branch gets one new
        commit, and the PR's head is never on it. Returns that commit."""
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", into, f"origin/{into}")
        sb._git(h, "merge", "-q", "--squash", f"origin/fix/{iid}")
        sb._git(h, "commit", "-q", "-s", "-m", f"fix {iid} (#{iid})")
        sb._git(h, "push", "-q", "origin", into)
        return self._merged_new_commits(iid, into)

    def _rebase(self, iid: str) -> str:
        """The maintainer rebase-merges ``iid``'s PR into a main that has moved on: main gets
        a copy of its commit under a new id, the last copy being what ``gh`` reports as the
        merge commit, and the PR's head is never on it. Returns that commit."""
        self.fx._push_from_human({"other.txt": "other\n"}, to="main")
        h = self.fx.human
        sb._git(h, "fetch", "-q", "origin")
        sb._git(h, "checkout", "-q", "-B", "rebased", f"origin/fix/{iid}")
        sb._git(h, "rebase", "-q", "origin/main")
        sb._git(h, "push", "-q", "origin", "rebased:main")
        return self._merged_new_commits(iid, "main")

    def _merged_new_commits(self, iid: str, into: str) -> str:
        """``into``'s tip is what ``iid``'s PR merged as, and the PR's head is not on it."""
        oid = self.fx._tip(into)
        self.prs[iid], self.merge_oid[iid] = "MERGED", oid
        self.pr_heads[iid] = self.fx._tip(f"fix/{iid}")
        self.assertFalse(self.fx._ancestor(self.pr_heads[iid], into))
        return oid

    def _record(self, d: Path, **fields: str) -> None:
        """Change bundle ``d``'s publish record: what the other kinds of publish write."""
        rec = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        rec.update(fields)
        (d / "publish.json").write_text(json.dumps(rec), encoding="utf-8")


class PublishWarning(_Git):
    """(a), (d), (d2), (e): a real ``publish.publish``; no PR opened, ``gh repo view`` stubbed."""

    def _publish(self, d: Path, *, dry_run: bool = False) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = publish.publish(self.cfg, d.name.removeprefix("issue_"), open_pr=False,
                                 by="T", today=TODAY, dry_run=dry_run)
        self.assertEqual(rc, 0, err.getvalue())
        return rc, err.getvalue()

    def _dependent(self, field: str, value: str = "P") -> None:
        sb._brief(self.cfg.bundle("D"), extra=f"- **{field}:** {value}\n")

    def _onto(self) -> Path:
        """Accepted P, whose brief adds its commit to the existing PR on ``feat``."""
        self.fx._push_from_human({"feat.txt": "feat\n"}, to="feat")
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        sb._brief(p, extra="- **Onto branch:** origin/feat\n")
        return p

    def _assert_stacked(self, p: Path) -> None:
        """The publish took the ``Onto branch`` path: P's commit is on that PR's branch."""
        rec = json.loads((p / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["mode"], "stacked")
        self.assertEqual(self.fx._show("feat", "p.txt"), "p\n")

    def test_a_wave_0_pr_another_bundle_depends_on_warns(self):
        # (a) P has no stack base. Before: only a PR with one warned.
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._dependent("Depends on")
        _rc, err = self._publish(p)
        self.assertEqual(publish.read_stack_base(p), "")
        self.assertIn(NO_MERGE_COMMITS, err)
        self.assertEqual(self.repo_views, 1)

    def test_every_declared_edge_counts_however_the_id_is_written(self):
        # `waves.declared_deps`: all three fields, and the forms a brief may write an id in.
        for field, value in (("Depends on (merged)", "P"), ("Stacks on", "P"),
                             ("Depends on", "#P"), ("Depends on", "issue_P"),
                             ("Depends on", "A1, P")):
            with self.subTest(field=field, value=value):
                p = self.fx._bundle("P", {"p.txt": "p\n"})
                self._dependent(field, value)
                _rc, err = self._publish(p)
                self.assertIn(NO_MERGE_COMMITS, err)

    def test_a_wave_0_pr_nothing_depends_on_does_not_warn(self):
        # (d) No new noise, and no `gh` call for it: another bundle names some other id.
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._dependent("Depends on", "P2")
        _rc, err = self._publish(p)
        self.assertNotIn(NO_MERGE_COMMITS, err)
        self.assertEqual(self.repo_views, 0)

    def test_a_brief_naming_itself_is_no_dependent(self):
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        sb._brief(p, extra="- **Depends on:** P\n")
        _rc, err = self._publish(p)
        self.assertNotIn(NO_MERGE_COMMITS, err)

    def _unreadable_brief(self) -> None:
        d = self.cfg.bundle("D")
        d.mkdir(parents=True)
        (d / "brief.md").write_bytes(b"\xff\xfe not utf-8")

    def test_an_unreadable_brief_is_skipped(self):
        # Best-effort: another bundle's brief that cannot be read never stops a publish.
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._unreadable_brief()
        _rc, err = self._publish(p)
        self.assertNotIn(NO_MERGE_COMMITS, err)

    def test_an_unreadable_brief_does_not_hide_a_readable_one(self):
        # The scan goes on past it: E sorts after D, and E names P.
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._unreadable_brief()
        sb._brief(self.cfg.bundle("E"), extra="- **Depends on:** P\n")
        _rc, err = self._publish(p)
        self.assertIn(NO_MERGE_COMMITS, err)

    def test_a_brief_that_cannot_be_opened_does_not_hide_a_readable_one(self):
        # An `OSError`, where the two above are a decoding error.
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        (self.cfg.bundle("D") / "brief.md").mkdir(parents=True)
        sb._brief(self.cfg.bundle("E"), extra="- **Depends on:** P\n")
        _rc, err = self._publish(p)
        self.assertIn(NO_MERGE_COMMITS, err)

    def test_an_onto_branch_commit_others_build_on_warns(self):
        # Its commit lands on a PR that can be squashed, and that publish takes its own path.
        p = self._onto()
        self._dependent("Depends on")
        _rc, err = self._publish(p)
        self._assert_stacked(p)
        self.assertIn(NO_MERGE_COMMITS, err)
        self.assertEqual(self.repo_views, 1)

    def test_an_onto_branch_commit_nothing_depends_on_does_not_warn(self):
        # (d) for that path.
        p = self._onto()
        self._dependent("Depends on", "P2")
        _rc, err = self._publish(p)
        self._assert_stacked(p)
        self.assertNotIn(NO_MERGE_COMMITS, err)
        self.assertEqual(self.repo_views, 0)

    def test_an_onto_branch_commit_in_merge_mode_does_not_warn(self):
        # (e) for that path.
        self.cfg.wave_mode = "merge"
        p = self._onto()
        self._dependent("Depends on")
        _rc, err = self._publish(p)
        self._assert_stacked(p)
        self.assertNotIn(NO_MERGE_COMMITS, err)
        self.assertEqual(self.repo_views, 0)

    def test_a_dry_run_asks_no_host(self):
        # A dry-run prints the plan and calls nothing.
        self._dependent("Depends on")
        self._publish(self.fx._bundle("P", {"p.txt": "p\n"}), dry_run=True)
        self.assertEqual(self.repo_views, 0)

    def test_an_onto_branch_dry_run_asks_no_host(self):
        self._dependent("Depends on")
        self._publish(self._onto(), dry_run=True)
        self.assertEqual(self.repo_views, 0)

    def test_a_pr_cut_from_a_stack_base_still_warns_with_no_dependent(self):
        # (d2) As before (#123): nothing names W, but its PR is cut from a stack base.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        with redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [a])
        w = self.fx._bundle("W", {"w.txt": "w\n"})
        publish.write_stack_base(w, sb.LINE)
        _rc, err = self._publish(w)
        self.assertIn(NO_MERGE_COMMITS, err)

    def test_merge_mode_does_not_warn(self):
        # (e) Merge mode picks its own method.
        self.cfg.wave_mode = "merge"
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._dependent("Depends on")
        _rc, err = self._publish(p)
        self.assertNotIn(NO_MERGE_COMMITS, err)
        self.assertEqual(self.repo_views, 0)

    def test_a_repo_that_allows_merge_commits_does_not_warn(self):
        self.merge_commit_allowed = True
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._dependent("Depends on")
        _rc, err = self._publish(p)
        self.assertEqual(self.repo_views, 1)
        self.assertNotIn(NO_MERGE_COMMITS, err)

    def test_a_gh_failure_is_silent(self):
        self.merge_commit_allowed = None
        p = self.fx._bundle("P", {"p.txt": "p\n"})
        self._dependent("Depends on")
        _rc, err = self._publish(p)
        self.assertEqual(self.repo_views, 1)
        self.assertNotIn(NO_MERGE_COMMITS, err)


class FoldOfAGoneBranch(_Git):
    """(b), (c3): ``integrate.fold`` over a bundle whose branch is gone."""

    def setUp(self) -> None:
        super().setUp()
        z = self.fx._publish("Z", {"z.txt": "z\n"})
        with redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [z], folded_this_run={})
        self.z, self.t = z, self.fx._tip(sb.LINE)

    def _stopped(self, a: Path) -> str:
        """The fold stops on ``a``, names it and pushes nothing. Returns the message."""
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [self.z, a], folded_this_run={TARGET: self.t})
        self.assertEqual(self.fx._tip(sb.LINE), self.t)     # nothing pushed
        msg = str(ctx.exception)
        self.assertIn(a.name, msg)
        return msg

    def test_a_squash_merge_is_named_with_its_merge_commit_and_the_step(self):
        # (b) Before: the generic "merged somewhere other than" message.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        oid = self._squash("A")
        self.fx._delete("fix/A")
        msg = self._stopped(a)
        self.assertRegex(msg, "squash|rebase")
        self.assertIn(oid, msg)
        self.assertNotIn(ELSEWHERE, msg)
        self.assertIn("Update branch", msg)
        self.assertIn("origin/main", msg)
        self.assertIn(f"re-issue without {a.name}", msg)

    def test_a_squash_merge_whose_head_the_clone_never_had_is_named_too(self):
        # A deleted branch's head may be in no clone at all: still on neither.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        oid = self._squash("A")
        self.fx._delete("fix/A")
        self.pr_heads["A"] = ABSENT
        msg = self._stopped(a)
        self.assertIn(oid, msg)
        self.assertNotIn(ELSEWHERE, msg)

    def test_a_merge_commit_the_base_lacks_keeps_the_generic_message(self):
        # (b) The PR merged into another branch: `release` has its merge commit, main not.
        sb._git(self.fx.origin, "branch", "release", "main")
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self._squash("A", into="release")
        self.fx._delete("fix/A")
        self.assertIn(f"{ELSEWHERE} origin/main", self._stopped(a))

    def test_a_merge_commit_the_clone_does_not_have_keeps_the_generic_message(self):
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self._squash("A")
        self.merge_oid["A"] = ABSENT
        self.fx._delete("fix/A")
        self.assertIn(f"{ELSEWHERE} origin/main", self._stopped(a))

    def test_an_empty_merge_commit_keeps_the_generic_message(self):
        # (c3) `gh` reports none: fail closed.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self._squash("A")
        del self.merge_oid["A"]
        self.fx._delete("fix/A")
        self.assertIn(f"{ELSEWHERE} origin/main", self._stopped(a))

    def test_an_unreadable_merge_commit_keeps_the_generic_message(self):
        # (c3) `gh` fails on the second read: fail closed.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self._squash("A")
        self.fx._delete("fix/A")
        self.gh_fail_merge_commit = True
        self.assertIn(f"{ELSEWHERE} origin/main", self._stopped(a))

    def test_a_merge_with_a_merge_commit_never_reads_it(self):
        # The base has the head: the line takes the base in, as before, with no extra read.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self.pr_heads["A"], self.prs["A"] = self.fx._tip("fix/A"), "MERGED"
        self.fx._merge_pr("A")
        self.merge_oid["A"] = self.fx._tip("main")
        self.fx._delete("fix/A")
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [self.z, a], folded_this_run={TARGET: self.t})
        self.assertEqual(self.merge_commit_reads, [])
        self.assertTrue(self.fx._ancestor(self.merge_oid["A"], sb.LINE))

    def test_a_squash_merge_whose_head_is_on_the_line_is_skipped(self):
        # The line has the head, so it has all of the PR's work: no read, nothing to name.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        with redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [self.z, a], folded_this_run={TARGET: self.t})
        t = self.fx._tip(sb.LINE)
        self._squash("A")
        self.fx._delete("fix/A")
        err = io.StringIO()
        with mock.patch.object(subprocess, "run", self._gh_stub), redirect_stderr(err):
            integrate.fold(self.cfg, [self.z, a], folded_this_run={TARGET: t})
        self.assertIn("already carries that commit", err.getvalue())
        self.assertEqual(self.fx._tip(sb.LINE), t)
        self.assertEqual(self.merge_commit_reads, [])

    # -- an `Onto branch` record, or a legacy `Stacks on:` PR ----------------------------------

    def _gone_record(self, iid: str, record: dict[str, str]) -> Path:
        """``iid`` published, squash-merged, its branch deleted, its publish record then
        ``record``. Returns the bundle."""
        a = self.fx._publish(iid, {f"{iid.lower()}.txt": f"{iid}\n"})
        self._squash(iid)
        self.fx._delete(f"fix/{iid}")
        self._record(a, **record)
        return a

    def test_those_records_keep_their_message_when_the_merge_commit_cannot_be_read(self):
        # (c3) Fail closed, empty and unreadable alike, once the read was tried. A merge commit
        # the base lacks is below (`_assert_neither_names_it`), with the carry's reading of the
        # same bundle.
        a = self._gone_record("A", _onto_record("A"))
        del self.merge_oid["A"]
        self.assertIn(f"{OTHER_BASE} origin/main", self._stopped(a))
        self.assertIn("A", self.merge_commit_reads)
        b = self._gone_record("B", LEGACY)
        self.gh_fail_merge_commit = True
        self.assertIn(f"{OTHER_BASE} origin/main", self._stopped(b))
        self.assertIn("B", self.merge_commit_reads)

    def test_a_record_for_another_repo_changes_only_the_drop_step(self):
        # The fold's own rule stands. With the head on the base it takes the base in, as before,
        # and a PR merged into another branch keeps the plain message, not the other-base one.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self.pr_heads["A"], self.prs["A"] = self.fx._tip("fix/A"), "MERGED"
        self.fx._merge_pr("A")
        self.merge_oid["A"] = self.fx._tip("main")
        self.fx._delete("fix/A")
        self._record(a, **RENAMED)
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [self.z, a], folded_this_run={TARGET: self.t})
        self.assertTrue(self.fx._ancestor(self.merge_oid["A"], sb.LINE))
        t = self.fx._tip(sb.LINE)
        sb._git(self.fx.origin, "branch", "release", "main")
        b = self.fx._publish("B", {"b.txt": "b\n"})
        self._squash("B", into="release")
        self.fx._delete("fix/B")
        self._record(b, **RENAMED)
        self.t = t
        said = self._stopped(b)
        self.assertIn(f"{ELSEWHERE} origin/main", said)
        self.assertNotIn(OTHER_BASE, said)

    def _stopped_with_batch(self, a: Path, batch: list[str]) -> str:
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [a], folded_this_run={}, batch=batch)
        return str(ctx.exception)

    def test_the_stop_names_the_line_of_the_batch_without_that_bundle(self):
        a = self._gone_record("A", _onto_record("A"))
        msg = self._stopped_with_batch(a, ["A", "Z", "D"])
        self.assertIn(DELETE, msg)
        self.assertIn("(for this batch without issue_A: "
                      f"{integrate.integration_branch(self.cfg, 'main', ['Z', 'D'])})", msg)
        # Not this run's own line: a run without A does not use it.
        self.assertNotIn(integrate.integration_branch(self.cfg, "main", ["A", "Z", "D"]) + ")", msg)

    def test_with_no_batch_or_none_left_the_step_stands_without_a_name(self):
        # A direct caller has no batch, and a batch of A alone has nothing to re-issue: no line
        # to name, and the step still says which one to look for.
        a = self._gone_record("A", _onto_record("A"))
        for said in (self._stopped(a), self._stopped_with_batch(a, ["A"]),
                     self._stopped_with_batch(a, ["issue_A"])):
            self.assertIn(DELETE + ": with the edge gone", said)

    def test_a_record_that_counts_is_asked_for_no_delete_with_a_batch_either(self):
        # D's edge to it stays, and makes the re-run check its line.
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self._squash("A")
        self.fx._delete("fix/A")
        msg = self._stopped_with_batch(a, ["A", "Z"])
        self.assertIn("re-issue without issue_A", msg)
        self.assertNotIn(DELETE, msg)

    def test_a_bundles_own_base_stops_with_the_same_step(self):
        # `build_bundle_base` merges each member as the fold does, for the same batch.
        m = self._gone_record("A", _onto_record("A"))
        d = self.cfg.bundle("D")
        sb._brief(d, extra="- **Depends on:** A\n")
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.build_bundle_base(self.cfg, d, [m], batch=["A", "D"], carried={},
                                            excluded={})
        msg = str(ctx.exception)
        self.assertIn(DELETE, msg)
        self.assertIn("(for this batch without issue_A: "
                      f"{integrate.integration_branch(self.cfg, 'main', ['D'])})", msg)

    def test_those_records_read_no_merge_commit_with_the_head_on_the_base(self):
        # A merge with a merge commit: nothing to tell apart, so no extra read, as before.
        for iid, record in (("A", _onto_record("A")), ("B", LEGACY)):
            a = self.fx._publish(iid, {f"{iid.lower()}.txt": f"{iid}\n"})
            self.pr_heads[iid], self.prs[iid] = self.fx._tip(f"fix/{iid}"), "MERGED"
            self.fx._merge_pr(iid)
            self.merge_oid[iid] = self.fx._tip("main")
            self.fx._delete(f"fix/{iid}")
            self._record(a, **record)
            self.assertIn(f"{OTHER_BASE} origin/main", self._stopped(a))
            self.assertEqual(self.merge_commit_reads, [])


class ReissueCarry(_Git):
    """(c), (c2), (c3): ``flow._drive_and_act`` with the leaves stubbed, a re-issued run. The
    publisher is NOT a stub (a stub turns every fold into a dry-run, which holds nothing)."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.seen: dict[str, tuple[str, str]] = {}
        self.not_built: set[str] = set()      # bundles a Do of this test leaves unbuilt
        self.after_publish = None             # called with a bundle once it is published

    def _finished(self, iid: str, on: str = "") -> Path:
        """``iid`` finished in an earlier run; ``on``: built on that id's branch, and its
        plain ``Depends on`` names it."""
        d = self.fx._publish(iid, {f"{iid.lower()}.txt": f"{iid}\n"},
                             cut_from=f"origin/fix/{on}" if on else "origin/main")
        sb._brief(d, extra=f"- **Depends on:** {on}\n" if on else "")  # _publish rewrites it
        sb._accept(d)
        self.assertEqual(state.state(d), state.COMPLETE)
        return d

    def _planned(self, iid: str, depends_on: str) -> Path:
        d = self.cfg.bundle(iid)
        sb._brief(d, extra=f"- **Depends on:** {depends_on}\n")
        return d

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        for d in wave:
            if d.name in self.not_built:
                continue
            self.seen[d.name] = (publish.read_stack_base(d), publish.read_stack_base_tip(d))
            (d / "patch.diff").write_text(sb._new_file_patch(f"{d.name}.txt", d.name),
                                          encoding="utf-8")
            sb._accept(d)
        return 1

    def _publish_bundle(self, cfg, d: Path, **_kw) -> bool:
        tip, line = publish.read_stack_base_tip(d), publish.read_stack_base(d)
        self.fx._publish(d.name.removeprefix("issue_"), {f"{d.name}.txt": d.name + "\n"},
                         cut_from=tip or (f"origin/{line}" if line else "origin/main"))
        if self.after_publish:
            self.after_publish(d)
        return True

    def _run(self, drive: list[Path], batch: list[str]) -> None:
        self.line = integrate.integration_branch(self.cfg, "main", batch)
        self.seen = {}
        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  self._publish_bundle))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            self.results = flow._drive_and_act(self.cfg, drive, do_publish=True, do_act=False,
                                               by="T", today=TODAY, batch=batch)
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)

    def _not_carried(self, iid: str) -> str:
        """The ONE stderr line saying finished ``iid`` is not carried."""
        lines = [x for x in self.err.splitlines()
                 if x.startswith(f"flow: issue_{iid} (finished in an earlier run) is not")]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    def _assert_never_built(self, iid: str) -> None:
        self.assertNotIn(f"issue_{iid}", self.seen, self.err)
        self.assertEqual(self.results[iid], state.PLANNED)

    def _assert_named_squash(self, line: str, oid: str = "") -> None:
        self.assertRegex(line, NAMED)
        self.assertIn("origin/main", line)
        self.assertNotIn(EDGE_HINT, line)
        if oid:
            self.assertIn(oid[:12], line)

    def _earlier_run_folds(self, batch: list[str], *bundles: Path) -> str:
        """An earlier run of ``batch`` folded ``bundles`` onto its line. Returns the line."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, list(bundles), folded_this_run={}, batch=batch)
        return integrate.integration_branch(self.cfg, "main", batch)

    def _line_holds_an_older_head(self, batch: list[str]) -> tuple[str, str, str]:
        """P (with Q built on it, and R, unrelated) went onto ``batch``'s line, a fixup then
        moved P's PR to a new head, and P was squash-merged: origin's line holds P's old head,
        which nothing clean accounts for. Returns the line, that old head and the merge
        commit."""
        p = self._finished("P")
        self._finished("Q", on="P")
        self._finished("R")
        line = self._earlier_run_folds(batch, p)
        old = self.fx._tip("fix/P")
        self.fx._push_from_human({"p2.txt": "after review\n"}, to="fix/P", off="origin/fix/P")
        oid = self._squash("P")
        self.assertTrue(self.fx._ancestor(old, line))
        self.assertFalse(self.fx._ancestor(self.pr_heads["P"], line))
        return line, old, oid

    def _assert_names_the_line_to_come(self, said: str, line: str, *without: str) -> None:
        """``said`` gives the delete step with the drop, and names the line of the batch left
        once ``without`` are out of it: ``line``, and why."""
        self.assertIn(DELETE, said)
        self.assertIn(f"(for this batch without {', '.join(without)}: {line})", said)
        self.assertIn("with the edge gone nothing checks that line for the merge", said)

    def _assert_q_held_for_p(self, *, drop: bool = False) -> str:
        q = self._not_carried("Q")
        self.assertIn(f"issue_P was {NAMED}", q)
        self.assertIn("Update branch", q)
        self.assertIn("update issue_Q's branch from origin/main", q)
        self.assertIn("rebuild issue_Q from origin/main", q)
        self.assertIn("re-issue without issue_P", q)
        self.assertNotIn("get issue_P carried", q)
        self.assertNotIn(EDGE_HINT, q)
        (self.assertIn if drop else self.assertNotIn)(DROP, q)
        (self.assertIn if drop else self.assertNotIn)(DELETE, q)
        return q

    def test_a_squash_merged_prerequisite_is_named_and_its_dependent_stays_held(self):
        # (c) Before: P got the `edge` hint, Q the `built_on` hint "get issue_P carried".
        self._finished("P")
        self._finished("Q", on="P")
        oid = self._squash("P")
        d = self._planned("D", "Q")
        self._run([d], ["P", "Q", "D"])
        self._assert_named_squash(self._not_carried("P"), oid)
        self.assertIn("issue_D", self._assert_q_held_for_p())   # Q holds D: Q is not carried
        self.assertNotIn("carried issue_Q", self.err)
        self.assertFalse(self.fx._has(self.line))           # no line holds anything
        self._assert_never_built("D")

    def test_a_line_on_origin_does_not_hide_the_squash_merge(self):
        # Before: the untrusted line replaced every reason, so P's and Q's lines said only to
        # delete it. R has nothing else to say, so it keeps that.
        batch = ["P", "Q", "R", "D"]
        line, old, oid = self._line_holds_an_older_head(batch)
        tip = self.fx._tip(line)
        d = self._planned("D", "Q")
        self._run([d], batch)
        p = self._not_carried("P")
        self._assert_named_squash(p, oid)
        q = self._assert_q_held_for_p()
        for held in (p, q):
            self.assertNotIn(f"holds {old[:12]}", held)
            self.assertNotIn("on origin", held)
        r = self._not_carried("R")
        self.assertIn(f"holds {old[:12]}", r)
        self.assertIn(f"delete {line} on origin", r)
        self.assertEqual(self.fx._tip(line), tip)           # carried nothing onto it
        self._assert_never_built("D")

    def test_a_line_on_origin_still_replaces_any_other_reason(self):
        # Only a squash-merged prerequisite and the ones held for it keep their own: its way
        # out is a run without it. R's PR is closed, and R's way on starts with that line.
        batch = ["P", "Q", "R", "D"]
        line, old, _oid = self._line_holds_an_older_head(batch)
        self.prs["R"] = "CLOSED"
        self._run([self._planned("D", "Q")], batch)
        r = self._not_carried("R")
        self.assertIn(f"holds {old[:12]}", r)
        self.assertIn(f"delete {line} on origin", r)
        self.assertNotIn("CLOSED", r)

    def test_a_line_on_origin_does_not_hide_it_for_an_onto_branch_record_either(self):
        batch = ["P", "Q", "R", "D"]
        _line, _old, oid = self._line_holds_an_older_head(batch)
        self._record(self.cfg.bundle("P"), **_onto_record("P"))
        self._run([self._planned("D", "Q")], batch)
        p = self._not_carried("P")
        self._assert_named_squash(p, oid)
        self.assertIn(DROP, p)
        q = self._assert_q_held_for_p(drop=True)
        # The line named is the re-issue's, not this run's: that one holds the older head, but
        # a run without P does not use it.
        after = integrate.integration_branch(self.cfg, "main", ["Q", "R", "D"])
        for held in (p, q):
            self._assert_names_the_line_to_come(held, after, "issue_P")
            self.assertNotIn(f"delete {_line}", held)

    def test_the_way_out_works_past_a_line_on_origin(self):
        # P's and Q's lines say nothing of the old line, and need not: a run without P has a
        # line of its own, so nobody has to delete that one first.
        batch = ["P", "Q", "R", "D"]
        line, _old, oid = self._line_holds_an_older_head(batch)
        tip = self.fx._tip(line)
        d = self._planned("D", "Q")
        self._run([d], batch)
        self._assert_never_built("D")
        self.fx._update_branch("Q")
        self._run([d], ["Q", "R", "D"])
        self.assertNotEqual(self.line, line)
        self.assertIn("carried issue_Q, issue_R", self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))
        self.assertEqual(self.fx._tip(line), tip)           # the old line is left as it was

    def test_a_failed_read_of_the_line_does_not_hide_the_squash_merge(self):
        # A git failure while listing the line's commits holds everything, as before, but
        # what was already read about P still stands.
        batch = ["P", "Q", "R", "D"]
        _line, _old, oid = self._line_holds_an_older_head(batch)
        d = self._planned("D", "Q")
        with mock.patch.object(integrate, "foreign_commit",
                               side_effect=integrate.IntegrationError("rev-list failed")):
            self._run([d], batch)
        self._assert_named_squash(self._not_carried("P"), oid)
        self._assert_q_held_for_p()
        self.assertIn("rev-list failed", self._not_carried("R"))
        self._assert_never_built("D")

    def _head_on_the_line_is_carried(self, *, gone: bool) -> None:
        """The line half of "on neither": an earlier run's line has P's head, which is all of
        P's work, so a squash merge since changes nothing: no read, nothing named, P and Q
        carried and D built."""
        batch = ["P", "Q", "D"]
        p = self._finished("P")
        self._finished("Q", on="P")
        self._earlier_run_folds(batch, p)
        head = self.fx._tip("fix/P")
        self._squash("P")
        if gone:
            self.fx._delete("fix/P")
        self._run([self._planned("D", "Q")], batch)
        self.assertIn("carried issue_P, issue_Q", self.err)
        self.assertNotIn(NAMED, self.err)
        self.assertEqual(self.merge_commit_reads, [])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(head, self.seen["issue_D"][1]))
        # The docs say this of the shape too: Q's PR keeps showing P's change.
        self.assertEqual(self.fx._diff_files("main", "fix/Q"), ["p.txt", "q.txt"])

    def test_a_head_already_on_the_line_is_carried_with_no_read(self):
        self._head_on_the_line_is_carried(gone=False)

    def test_a_head_already_on_the_line_is_carried_with_its_branch_deleted_too(self):
        self._head_on_the_line_is_carried(gone=True)

    def test_an_untrusted_line_names_no_merge_for_a_head_already_on_it(self):
        # That head makes P clean, so nothing is read or named here either, and P's line is
        # only the untrusted line's: only a prerequisite named as merged keeps its own reason.
        batch = ["P", "Q", "D"]
        p = self._finished("P")
        self._finished("Q", on="P")
        line = self._earlier_run_folds(batch, p)
        other = self.fx._push_from_human({"other.txt": "other\n"}, to=line, off=f"origin/{line}")
        self._squash("P")
        self._run([self._planned("D", "Q")], batch)
        said = self._not_carried("P")
        self.assertIn(f"holds {other[:12]}", said)
        self.assertIn(f"delete {line} on origin", said)
        self.assertNotIn(NAMED, self.err)
        self.assertEqual(self.merge_commit_reads, [])
        self._assert_never_built("D")

    def test_a_merged_pr_with_no_head_reported_keeps_the_open_hint(self):
        # With no head, "on neither" cannot be known: no read, and the same line as before.
        self._finished("P")
        self._squash("P")
        self.fx._delete("fix/P")
        self.pr_heads["P"] = ""
        d = self._planned("D", "P")
        self._run([d], ["P", "D"])
        line = self._not_carried("P")
        self.assertIn("(none reported)", line)
        self.assertIn("get issue_P's PR open", line)
        self.assertNotIn(NAMED, line)
        self.assertEqual(self.merge_commit_reads, [])

    # -- both sites read one bundle alike ------------------------------------------------------

    def _both_sites(self, record: dict[str, str], *, into: str = "main",
                    rebase: bool = False) -> tuple[str, str, str]:
        """P finished and squash-merged into ``into`` (or ``rebase``-merged into main), its
        branch deleted, its publish record then ``record``. Returns P's line from a re-issue
        (whose D is never built), the fold's stop for the same bundle, and the merge commit."""
        p = self._finished("P")
        oid = self._rebase("P") if rebase else self._squash("P", into=into)
        self.fx._delete("fix/P")
        self._record(p, **record)
        self._run([self._planned("D", "P")], ["P", "D"])
        self._assert_never_built("D")
        with mock.patch.object(subprocess, "run", self._gh_stub), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [p], folded_this_run={})
        return self._not_carried("P"), str(ctx.exception), oid

    def _assert_both_name_it(self, record: dict[str, str], *, drop: bool,
                             rebase: bool = False) -> None:
        carry, fold, oid = self._both_sites(record, rebase=rebase)
        self._assert_named_squash(carry, oid)
        self.assertIn(NAMED, fold)
        self.assertIn(oid, fold)
        for said in (carry, fold):                  # the same step, in the same words
            self.assertIn("update each branch built on issue_P from origin/main", said)
            self.assertIn("rebuild it from origin/main", said)
            self.assertIn("Update branch", said)
            self.assertIn("re-issue without issue_P", said)
            self.assertNotIn("restore the branch", said)
            self.assertNotIn(OTHER_BASE, said)
            # Only a record that never counts needs the edge dropped too: see the way out below.
            (self.assertIn if drop else self.assertNotIn)(DROP, said)
            (self.assertIn if drop else self.assertNotIn)(DELETE, said)

    def test_both_sites_name_the_merge_of_a_new_pr(self):
        self._assert_both_name_it({}, drop=False)

    def test_both_sites_name_the_merge_of_a_pr_cut_from_a_stack_base(self):
        # It targets the base, so a merge counts: the mode alone asks for no drop.
        self._assert_both_name_it({"mode": "stacked-pr"}, drop=False)

    def test_both_sites_name_a_rebase_merge(self):
        # Real git: main gets a copy of P's commit, whose id is what `gh` reports. The live
        # host's answer for a rebase merge is a stub's here, as for a squash (#625).
        self._assert_both_name_it({}, drop=False, rebase=True)

    def test_both_sites_name_the_merge_of_an_onto_branch_record(self):
        # Before: the carry said "update each branch" and the fold "restore the branch".
        self._assert_both_name_it(_onto_record("P"), drop=True)

    def test_both_sites_name_the_merge_of_a_legacy_stacks_on_pr(self):
        self._assert_both_name_it(LEGACY, drop=True)

    def test_both_sites_ask_for_the_drop_on_a_record_for_another_repo(self):
        # `merged.merged_into` asks for the record's repo too: without the drop a D naming P
        # would stay held after the way out.
        self._assert_both_name_it(RENAMED, drop=True)

    def _assert_neither_names_it(self, record: dict[str, str]) -> None:
        sb._git(self.fx.origin, "branch", "release", "main")
        carry, fold, _oid = self._both_sites(record, into="release")
        self.assertIn("remove the `Depends on: P` edge", carry)
        self.assertIn(f"{OTHER_BASE} origin/main", fold)
        for said in (carry, fold):
            self.assertNotIn(NAMED, said)

    def test_neither_site_names_a_merge_commit_the_base_lacks_on_an_onto_branch_record(self):
        self._assert_neither_names_it(_onto_record("P"))

    def test_neither_site_names_a_merge_commit_the_base_lacks_on_a_legacy_stacks_on_pr(self):
        self._assert_neither_names_it(LEGACY)

    def test_a_chain_on_a_squash_merged_prerequisite_names_it_all_the_way(self):
        # R builds on Q builds on P: the way out names P, not "get issue_Q carried".
        self._finished("P")
        self._finished("Q", on="P")
        self._finished("R", on="Q")
        self._squash("P")
        d = self._planned("D", "R")
        self._run([d], ["P", "Q", "R", "D"])
        r = self._not_carried("R")
        self.assertIn("issue_P was squash- or rebase-merged", r)
        self.assertIn("update issue_R's branch from origin/main", r)
        self.assertNotIn("get issue_Q carried", r)
        self.assertIn("re-issue without issue_P", r)
        self._assert_never_built("D")

    def test_a_chain_on_a_record_that_never_counts_asks_for_the_drop_all_the_way(self):
        # R builds on Q builds on P. Either might be the line someone acts on.
        p = self._finished("P")
        self._finished("Q", on="P")
        self._finished("R", on="Q")
        self._squash("P")
        self._record(p, **_onto_record("P"))
        self._run([self._planned("D", "R")], ["P", "Q", "R", "D"])
        self._assert_q_held_for_p(drop=True)
        r = self._not_carried("R")
        self.assertIn("update issue_R's branch from origin/main", r)
        self.assertIn(DROP, r)
        self._assert_never_built("D")

    def test_a_chain_through_a_finished_id_with_no_patch_names_it_too(self):
        # Q's `Depends on` reaches P only through B, which no fold carries (#728).
        self._finished("P")
        b = self.cfg.bundle("B")
        sb._brief(b, extra="- **Depends on:** P\n")
        (b / state.CLOSE_MARKER).write_text("close\n", encoding="utf-8")
        sb._accept(b)
        self.assertEqual(state.state(b), state.COMPLETE)
        q = self.fx._publish("Q", {"q.txt": "Q\n"}, cut_from="origin/fix/P")
        sb._brief(q, extra="- **Depends on:** B\n")
        sb._accept(q)
        self._squash("P")
        d = self._planned("D", "Q")
        self._run([d], ["P", "B", "Q", "D"])
        self.assertIn("through issue_B (finished with no patch)", self._assert_q_held_for_p())
        self._assert_never_built("D")

    def test_a_dependent_already_rebuilt_and_merged_is_told_nothing_false_of_its_branch(self):
        # Q was rebuilt from main once P merged and merged itself, with a merge commit, and the
        # same batch is re-issued against that advice. The hold goes by the edge, so Q stays held
        # and the way out is still a run without P, but its branch holds no commit main lacks.
        self._finished("P")
        self._squash("P")
        q = self.fx._publish("Q", {"q.txt": "Q\n"})
        sb._brief(q, extra="- **Depends on:** P\n")
        sb._accept(q)
        self.pr_heads["Q"], self.prs["Q"] = self.fx._tip("fix/Q"), "MERGED"
        self.fx._merge_pr("Q")
        self.fx._delete("fix/Q")
        self.assertTrue(self.fx._ancestor(self.pr_heads["Q"], "main"))
        self._run([self._planned("D", "Q")], ["P", "Q", "D"])
        said = self._assert_q_held_for_p()
        self.assertIn("if you have not", said)
        self.assertNotIn("issue_Q's branch holds", said)
        self._assert_never_built("D")

    def test_a_squash_merge_with_its_branch_deleted_is_named_too(self):
        # The usual end of a squash merge: the head is on no ref any more, and may be in no
        # clone. Before it read "not in <repo>", with the `open` hint.
        p = self._finished("P")
        self._finished("Q", on="P")
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self.pr_heads["P"] = ABSENT
        self.assertTrue(integrate._has_patch(p))
        d = self._planned("D", "P")
        self._run([d], ["P", "Q", "D"])
        line = self._not_carried("P")
        self._assert_named_squash(line, oid)
        self.assertIn("issue_D", line)
        self.assertNotIn("get issue_P's PR open", line)
        self._assert_never_built("D")

    def test_a_merge_commit_the_base_lacks_keeps_the_edge_hint(self):
        # (c2) The PR merged into another branch: as before.
        sb._git(self.fx.origin, "branch", "release", "main")
        self._finished("P")
        self._squash("P", into="release")
        d = self._planned("D", "P")
        self._run([d], ["P", "D"])
        line = self._not_carried("P")
        self.assertIn("which neither origin/main nor the line has", line)
        self.assertIn("remove the `Depends on: P` edge", line)
        self.assertNotRegex(line, "squash|rebase")
        self._assert_never_built("D")

    def test_an_unreadable_merge_commit_keeps_the_edge_hint(self):
        # (c3) `gh` fails on the second read: fail closed.
        self._finished("P")
        self._squash("P")
        self.gh_fail_merge_commit = True
        d = self._planned("D", "P")
        self._run([d], ["P", "D"])
        self.assertIn("P", self.merge_commit_reads)
        self.assertIn("remove the `Depends on: P` edge", self._not_carried("P"))
        self._assert_never_built("D")

    def test_an_empty_merge_commit_keeps_the_edge_hint(self):
        # (c3) `gh` reports no merge commit: fail closed.
        self._finished("P")
        self._squash("P")
        del self.merge_oid["P"]
        d = self._planned("D", "P")
        self._run([d], ["P", "D"])
        self.assertIn("remove the `Depends on: P` edge", self._not_carried("P"))
        self._assert_never_built("D")

    def test_a_built_on_hold_for_any_other_cause_is_unchanged(self):
        # #652: a closed P keeps the `built_on` hint and no read of its merge commit.
        self._finished("P")
        self._finished("Q", on="P")
        self.prs["P"] = "CLOSED"
        d = self._planned("D", "Q")
        self._run([d], ["P", "Q", "D"])
        self.assertIn("get issue_P carried, or re-drive issue_Q on a base without it",
                      self._not_carried("Q"))
        self.assertEqual(self.merge_commit_reads, [])

    def test_an_open_or_merge_committed_prerequisite_never_reads_the_merge_commit(self):
        # The extra `gh` call is made only for a MERGED P whose head is on neither.
        self._finished("O")
        self._finished("M")
        self.pr_heads["M"], self.prs["M"] = self.fx._tip("fix/M"), "MERGED"
        self.fx._merge_pr("M")
        d = self._planned("D", "O, M")
        self._run([d], ["O", "M", "D"])
        self.assertIn("carried issue_O, issue_M", self.err)
        self.assertEqual(self.merge_commit_reads, [])
        self.assertEqual(self.results["D"], state.COMPLETE)

    def test_the_way_out_the_hold_gives_works(self):
        # The hold gives two steps. Update Q from the base: Q's PR showed P's change, and now
        # shows only its own. Re-issue without P: a run asked for Q and D carries Q, and D is
        # built on a line holding Q's branch and P's work under the squash commit. D's brief
        # still names P, which no longer holds it: P is out of the batch and merged into main.
        self._finished("P")
        self._finished("Q", on="P")
        oid = self._squash("P")
        d = self._planned("D", "Q, P")
        self._run([d], ["P", "Q", "D"])
        self._assert_never_built("D")
        self.assertIn("re-issue without issue_P", self._not_carried("Q"))
        self.assertEqual(self.fx._diff_files("main", "fix/Q"), ["p.txt", "q.txt"])
        self.fx._update_branch("Q")                  # GitHub's "Update branch"
        self.assertTrue(self.fx._ancestor(oid, "fix/Q"))
        self.assertEqual(self.fx._diff_files("main", "fix/Q"), ["q.txt"])
        self._run([d], ["Q", "D"])
        self.assertIn("carried issue_Q", self.err)
        self.assertIn("issue_D", self.seen, self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)
        tip = self.seen["issue_D"][1]
        self.assertTrue(tip, self.err)
        self.assertTrue(self.fx._ancestor(oid, tip))
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/Q"), tip))

    def _assert_the_way_out_drops_the_edge(self, record: dict[str, str]) -> None:
        # That record never counts as merged into the base (`merged.merged_into`, #647), so a
        # D naming P is still held in a run without P. P's line and Q's both say so up front,
        # and D's says to drop the edge, where a plain hold would send you back to re-add P.
        # Once the edge is gone, D is built.
        p = self._finished("P")
        self._finished("Q", on="P")
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **record)
        d = self._planned("D", "Q, P")
        self._run([d], ["P", "Q", "D"])
        self.assertIn(DROP, self._not_carried("P"))
        self._assert_q_held_for_p(drop=True)
        self.fx._update_branch("Q")
        self._run([d], ["Q", "D"])
        self.assertIn("carried issue_Q", self.err)
        self._assert_never_built("D")
        self._assert_told_to_drop(oid)
        self._planned("D", "Q")
        self._run([d], ["Q", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_the_way_out_of_an_onto_branch_record_drops_the_edge_too(self):
        self._assert_the_way_out_drops_the_edge(_onto_record("P"))

    def test_the_way_out_of_a_legacy_stacks_on_pr_drops_the_edge_too(self):
        self._assert_the_way_out_drops_the_edge(LEGACY)

    def test_the_way_out_of_a_record_for_another_repo_drops_the_edge_too(self):
        self._assert_the_way_out_drops_the_edge(RENAMED)

    # -- a dependent left naming it, in a run without it ---------------------------------------

    def _left_naming_p(self, record: dict[str, str], *, into: str = "main",
                       field: str = "Depends on") -> tuple[Path, str]:
        """P finished and squash-merged into ``into``, its branch deleted, its record then
        ``record``; D, not built, still names P in ``field``. Returns D and the merge commit."""
        p = self._finished("P")
        oid = self._squash("P", into=into)
        self.fx._delete("fix/P")
        self._record(p, **record)
        d = self.cfg.bundle("D")
        sb._brief(d, extra=f"- **{field}:** P\n")
        return d, oid

    def _judge(self, d: Path, *, hold_unmerged: bool = True, **kw) -> list[Path]:
        """``flow._runnable`` on ``d`` alone, as a run not asked for P calls it."""
        err = io.StringIO()
        with mock.patch.object(subprocess, "run", self._gh_stub), redirect_stderr(err):
            runnable = flow._runnable(self.cfg, [d], {d.name}, hold_unmerged=hold_unmerged,
                                      **kw)
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)
        return runnable

    def _d_held(self) -> str:
        """The ONE stderr line saying D is held."""
        lines = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    def _assert_told_to_drop(self, oid: str, *, delete: str = "") -> str:
        """D's line names the merge and gives the drop, and offers no way that cannot work.
        ``delete``: the line it must also say to delete, else it asks to delete none. It says
        the PR merged into the base, never that the base has its work. Returns the line."""
        said = self._d_held()
        self.assertIn(f"{NAMED} into main", said)
        self.assertIn(oid[:12], said)
        self.assertIn("drop issue_P from issue_D's brief", said)
        self.assertNotIn(READD, said)
        self.assertNotIn(WAIT, said)
        self.assertNotRegex(said, r"main has|has the PR's work|has its work|has that work")
        if delete:
            self.assertIn(f"delete {delete} on origin", said)
        else:
            self.assertNotIn("on origin", said)
        return said

    def _assert_plain_hold(self, *, carry_advice: bool = True) -> None:
        """D's line is the one a prerequisite not merged into D's base always got (#647)."""
        said = self._d_held()
        self.assertIn(WAIT, said)
        (self.assertIn if carry_advice else self.assertNotIn)(READD, said)
        self.assertNotRegex(said, "squash|rebase")
        self.assertNotIn("drop issue_P", said)

    def _assert_left_naming_it_is_told_to_drop(self, record: dict[str, str]) -> None:
        # Before: "name issue_P in the same `pdca flow <ids>` command … or wait until their PR
        # merges into main", which brings back P's hold and never comes true.
        d, oid = self._left_naming_p(record)
        self.assertEqual(self._judge(d), [])
        self._assert_told_to_drop(oid)

    def test_a_dependent_naming_an_onto_branch_record_is_told_to_drop_the_edge(self):
        self._assert_left_naming_it_is_told_to_drop(_onto_record("P"))

    def test_a_dependent_naming_a_legacy_stacks_on_pr_is_told_to_drop_the_edge(self):
        self._assert_left_naming_it_is_told_to_drop(LEGACY)

    def test_a_dependent_naming_a_record_for_another_repo_is_told_to_drop_the_edge(self):
        self._assert_left_naming_it_is_told_to_drop(RENAMED)

    def test_a_dependent_naming_a_stacked_record_on_its_base_is_told_to_drop_the_edge(self):
        # The mode alone: its repo and base read right, and `merged_into` still never counts it.
        self._assert_left_naming_it_is_told_to_drop({"mode": "stacked"})

    def test_a_depends_on_merged_edge_is_told_to_drop_it_too(self):
        # `--no-publish` as well: that edge is gated either way, and no merge will count.
        d, oid = self._left_naming_p(_onto_record("P"), field="Depends on (merged)")
        for hold_unmerged in (True, False):
            self.assertEqual(self._judge(d, hold_unmerged=hold_unmerged), [])
            self._assert_told_to_drop(oid)

    def test_a_dependent_on_a_record_that_counts_is_not_held_and_nothing_extra_is_read(self):
        # A squash merge of a PR against the base counts, as before.
        d, _oid = self._left_naming_p({})
        self.assertEqual(self._judge(d), [d])
        self.assertEqual(self.merge_commit_reads, [])
        self.assertNotIn(NAMED, self.err)

    def test_a_merge_commit_another_branch_has_keeps_the_plain_hold(self):
        # The PR merged into `release`, not into D's base: main does not have the work.
        sb._git(self.fx.origin, "branch", "release", "main")
        d, _oid = self._left_naming_p(_onto_record("P"), into="release")
        self.assertEqual(self._judge(d), [])
        self.assertIn("P", self.merge_commit_reads)
        self._assert_plain_hold()

    def test_an_empty_merge_commit_keeps_the_plain_hold(self):
        # (c3) for this line too: fail closed.
        d, _oid = self._left_naming_p(_onto_record("P"))
        del self.merge_oid["P"]
        self.assertEqual(self._judge(d), [])
        self._assert_plain_hold()

    def test_an_unreadable_merge_commit_keeps_the_plain_hold(self):
        d, _oid = self._left_naming_p(_onto_record("P"))
        self.gh_fail_merge_commit = True
        self.assertEqual(self._judge(d), [])
        self.assertIn("P", self.merge_commit_reads)
        self._assert_plain_hold()

    def test_no_head_reported_keeps_the_plain_hold(self):
        # With no head, the base lacking it cannot be known.
        d, _oid = self._left_naming_p(_onto_record("P"))
        self.pr_heads["P"] = ""
        self.assertEqual(self._judge(d), [])
        self._assert_plain_hold()

    def test_a_merge_with_a_merge_commit_keeps_the_plain_hold(self):
        # The base has the head too: nothing was squashed or rebased.
        p = self._finished("P")
        self.pr_heads["P"], self.prs["P"] = self.fx._tip("fix/P"), "MERGED"
        self.fx._merge_pr("P")
        self.merge_oid["P"] = self.fx._tip("main")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "P")
        self.assertEqual(self._judge(d), [])
        self.assertIn("P", self.merge_commit_reads)
        self._assert_plain_hold()

    def test_a_pr_not_merged_keeps_the_plain_hold(self):
        # On either kind of record, and only the one that never counts costs a read.
        p = self._finished("P")
        d = self._planned("D", "P")
        self.assertEqual(self._judge(d), [])
        self._assert_plain_hold()
        self.assertEqual(self.merge_commit_reads, [])
        self._record(p, **_onto_record("P"))
        self.assertEqual(self._judge(d), [])
        self._assert_plain_hold()
        self.assertEqual(self.merge_commit_reads, ["P"])

    def test_a_checkout_that_cannot_be_fetched_keeps_the_plain_hold(self):
        d, _oid = self._left_naming_p(_onto_record("P"))
        with mock.patch.object(integrate, "carry_view",
                               side_effect=integrate.IntegrationError("no fetch")):
            self.assertEqual(self._judge(d), [])
        self._assert_plain_hold()
        self.assertEqual(self.merge_commit_reads, [])

    def test_a_git_failure_keeps_the_plain_hold(self):
        d, _oid = self._left_naming_p(_onto_record("P"))
        with mock.patch.object(integrate, "_carries",
                               side_effect=integrate.IntegrationError("git failed")):
            self.assertEqual(self._judge(d), [])
        self._assert_plain_hold()

    def test_merge_mode_keeps_the_plain_hold_and_reads_nothing_extra(self):
        # Merge mode picks its own merge method: only a `Depends on (merged)` edge is gated.
        self.cfg.wave_mode = "merge"
        d, _oid = self._left_naming_p(_onto_record("P"), field="Depends on (merged)")
        self.assertEqual(self._judge(d, hold_unmerged=False), [])
        self._assert_plain_hold(carry_advice=False)
        self.assertEqual(self.merge_commit_reads, [])

    def test_a_dry_run_keeps_the_plain_hold_and_asks_for_nothing_more(self):
        # Only a `Depends on (merged)` edge is gated there, on the state `merged_into` reads.
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
        d, _oid = self._left_naming_p(_onto_record("P"), field="Depends on (merged)")
        with mock.patch.object(integrate, "carry_view") as fetch:
            self.assertEqual(self._judge(d, hold_unmerged=False), [])
        fetch.assert_not_called()
        self._assert_plain_hold(carry_advice=False)
        self.assertEqual(self.merge_commit_reads, [])

    # -- ... when the run's line is older than the merge ---------------------------------------

    def _line_older_than_the_merge(self, record: dict[str, str], *,
                                   batch: tuple[str, ...] = ("R", "D")) -> tuple[str, str]:
        """An earlier run of ``batch`` folded R onto its line. P was then squash-merged, its
        branch deleted and its record then ``record``, so the line has neither P's merge commit
        nor P's head. Returns the line and the merge commit."""
        r, p = self._finished("R"), self._finished("P")
        line = self._earlier_run_folds(list(batch), r)
        head = self.fx._tip("fix/P")
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **record)
        self.assertFalse(self.fx._ancestor(oid, line))
        self.assertFalse(self.fx._ancestor(head, line))
        return line, oid

    def _assert_older_line_is_named(self, record: dict[str, str]) -> None:
        # Before: "drop issue_P from issue_D's brief, then re-run", and nothing else: the re-run
        # continues the line, with no edge left to make it check for the merge.
        line, oid = self._line_older_than_the_merge(record)
        tip = self.fx._tip(line)
        self._run([self._planned("D", "R, P")], ["R", "D"])
        self.assertEqual(self.line, line)
        self.assertIn("carried issue_R", self.err)
        self._assert_never_built("D")
        said = self._assert_told_to_drop(oid, delete=line)
        self.assertIn(f"{line} at {tip[:12]}, does not have the merge of issue_P,", said)
        self.assertIn("once this run ends", said)
        self.assertIn("starts from main, which has the merge commit", said)

    def test_an_older_line_is_named_for_an_onto_branch_record(self):
        self._assert_older_line_is_named(_onto_record("P"))

    def test_an_older_line_is_named_for_a_legacy_stacks_on_pr(self):
        self._assert_older_line_is_named(LEGACY)

    def test_an_older_line_is_named_for_a_record_for_another_repo(self):
        self._assert_older_line_is_named(RENAMED)

    def test_an_older_line_is_named_for_a_depends_on_merged_edge(self):
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        d = self.cfg.bundle("D")
        sb._brief(d, extra="- **Depends on:** R\n- **Depends on (merged):** P\n")
        self._run([d], ["R", "D"])
        self._assert_never_built("D")
        self._assert_told_to_drop(oid, delete=line)

    def test_the_way_out_past_an_older_line_builds_on_the_merge(self):
        # The line's two steps followed: D is built on a line that has the merge commit.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        d = self._planned("D", "R, P")
        self._run([d], ["R", "D"])
        self._assert_never_built("D")
        self._assert_told_to_drop(oid, delete=line)
        self._planned("D", "R")
        self.fx._delete(line)
        self._run([d], ["R", "D"])
        self.assertEqual(self.line, line)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"][0], line)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_the_drop_alone_leaves_d_on_the_older_line(self):
        # Why the line asks for both: with the edge gone nothing checks the line, so a run that
        # still finds it builds D on a tip without the merge, and says nothing. Pinned so that
        # the docs' sentence on it stays true.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        d = self._planned("D", "R, P")
        self._run([d], ["R", "D"])
        tip = self.fx._tip(line)
        self._planned("D", "R")
        self._run([d], ["R", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"], (line, tip))
        self.assertFalse(self.fx._ancestor(oid, tip))
        self.assertNotIn(NAMED, self.err)

    def test_a_drop_made_before_the_re_run_is_told_to_delete_the_line_too(self):
        # P's own line asks for the drop up front. Followed as written, no run sees D naming P,
        # so D's own line-check never runs, and P's line has to give that step itself. Before:
        # it said nothing of the line, and the re-run below built D on a tip without the merge,
        # with no word of it.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        d = self._planned("D", "R, P")
        self._run([d], ["P", "R", "D"])
        p = self._not_carried("P")
        self.assertIn(DROP, p)
        self._assert_names_the_line_to_come(p, line, "issue_P")
        self._assert_never_built("D")
        self.assertNotEqual(self.line, line)
        self._planned("D", "R")
        self.fx._delete(line)
        self._run([d], ["R", "D"])
        self.assertEqual(self.line, line)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_the_advice_followed_from_the_first_run_on_builds_on_the_merge(self):
        # Each step as the printed lines give it. A run of R and D, with P's PR open, folds R onto
        # its line and says to name P. P is then squash-merged. The run naming P says to drop it
        # and delete that first line. Before: it said only to drop it, and the last run built D
        # on the first line's tip, without the merge, with no word of it.
        self._finished("R")
        p = self._finished("P")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "R, P")
        self._run([d], ["R", "D"])
        line = self.line
        self.assertIn("carried issue_R", self.err)
        self.assertIn(READD, self._d_held())
        self._assert_never_built("D")
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self.assertFalse(self.fx._ancestor(oid, line))
        self._run([d], ["P", "R", "D"])
        said = self._not_carried("P")
        self.assertIn(DROP, said)
        self._assert_names_the_line_to_come(said, line, "issue_P")
        self._assert_never_built("D")
        self._planned("D", "R")
        self.fx._delete(line)
        self._run([d], ["R", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"][0], line)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))
        self.assertEqual(self.fx._show(self.seen["issue_D"][1], "p.txt"), "P\n")

    def test_a_line_held_for_it_is_told_to_delete_the_line_too(self):
        # Q's line asks for the same drop, and someone may act on that line alone.
        p, r = self._finished("P"), self._finished("R")
        self._finished("Q", on="P")
        line = self._earlier_run_folds(["Q", "R", "D"], r)
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **_onto_record("P"))
        self.assertFalse(self.fx._ancestor(oid, line))
        d = self._planned("D", "Q, R, P")
        self._run([d], ["P", "Q", "R", "D"])
        self._assert_names_the_line_to_come(self._assert_q_held_for_p(drop=True), line, "issue_P")
        self._assert_never_built("D")
        self.fx._update_branch("Q")
        self._planned("D", "Q, R")
        self.fx._delete(line)
        self._run([d], ["Q", "R", "D"])
        self.assertEqual(self.line, line)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_the_folds_stop_in_a_run_is_told_to_delete_the_line_too(self):
        # A's PR is squash-merged and its branch deleted once it is published, so the wave's own
        # fold stops on it: the same step, with the line of the batch without A.
        a, b = self.cfg.bundle("A"), self._planned("B", "A")
        sb._brief(a)

        def merged_at_once(d: Path) -> None:
            self._squash("A")
            self.fx._delete("fix/A")
            self._record(d, **_onto_record("A"))

        self.after_publish = merged_at_once
        self._run([a, b], ["A", "B"])
        stop = next(x for x in self.err.splitlines() if "did not integrate" in x)
        self.assertIn(NAMED, stop)
        self.assertIn("dropping issue_A from the brief", stop)
        self._assert_names_the_line_to_come(
            stop, integrate.integration_branch(self.cfg, "main", ["B"]), "issue_A")
        self.assertNotIn("issue_B", self.seen)

    def test_the_line_named_is_the_one_a_re_issue_continues_however_ids_are_written(self):
        line, _oid = self._line_older_than_the_merge(_onto_record("P"))
        self._run([self._planned("D", "R, P")], ["issue_P", "issue_R", "D"])
        self._assert_names_the_line_to_come(self._not_carried("P"), line, "issue_P")

    def _two_merged(self, record_o: dict[str, str]) -> tuple[str, str, str]:
        """R on the line of a run of R and D, and P and O squash-merged since, P's record never
        counting and O's then ``record_o``. A re-issue of them all names both. Returns that
        line and P's and O's lines."""
        p, o, r = self._finished("P"), self._finished("O"), self._finished("R")
        line = self._earlier_run_folds(["R", "D"], r)
        self._squash("P")
        self._squash("O")
        self._record(p, **_onto_record("P"))
        self._record(o, **record_o)
        self._run([self._planned("D", "R, P, O")], ["P", "O", "R", "D"])
        self._assert_never_built("D")
        return line, self._not_carried("P"), self._not_carried("O")

    def test_two_merges_name_the_line_of_the_batch_without_either(self):
        # Each line asks to re-issue without its own, so the batch to come has neither.
        line, p, o = self._two_merged(_onto_record("O"))
        for said in (p, o):
            self._assert_names_the_line_to_come(said, line, "issue_P", "issue_O")

    def test_a_merge_that_counts_leaves_that_batch_too_but_asks_for_no_delete(self):
        # O's line asks to re-issue without it as well, so P's names the line without both. O's
        # own has no drop in it: D's edge to it stays, and makes the re-run check its line.
        line, p, o = self._two_merged({})
        self._assert_names_the_line_to_come(p, line, "issue_P", "issue_O")
        self.assertIn("re-issue without issue_O", o)
        self.assertNotIn(DELETE, o)

    # -- ... and when this run never got to judge a line -------------------------------------

    def _assert_told_line_not_checked(self, oid: str, line: str) -> str:
        said = self._assert_told_to_drop(oid, delete=line)
        self.assertIn(f"delete {line} on origin if it is there", said)
        self.assertIn("this run did not check that line", said)
        self.assertIn("once this run ends", said)
        self.assertIn("starts from main, which has the merge commit", said)
        return said

    def test_a_carry_that_held_its_prerequisite_leaves_the_line_not_checked(self):
        # R's PR is closed, so the carry holds R (its head no longer accounts for the line) and
        # gives the run no line. Before: only "drop issue_P from issue_D's brief, then re-run",
        # with the older line still on origin. Both steps followed: D is built on a line that
        # has the merge commit.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        self.prs["R"] = "CLOSED"
        d = self._planned("D", "R, P")
        self._run([d], ["R", "D"])
        self._assert_never_built("D")
        self.assertTrue(self.fx._has(line))
        self._assert_told_line_not_checked(oid, line)
        self.prs["R"] = "OPEN"
        self._planned("D", "R")
        self.fx._delete(line)
        self._run([d], ["R", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_the_drop_alone_past_a_line_not_checked_leaves_d_on_it(self):
        # Why that line asks for both: the older line is continued once R is back, and nothing
        # says so. Pinned so that the docs' sentence on it stays true.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        self.prs["R"] = "CLOSED"
        d = self._planned("D", "R, P")
        self._run([d], ["R", "D"])
        self.prs["R"] = "OPEN"
        self._planned("D", "R")
        self._run([d], ["R", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"][0], line)
        self.assertFalse(self.fx._ancestor(oid, self.seen["issue_D"][1]))
        self.assertNotIn(NAMED, self.err)

    def test_a_fold_that_failed_leaves_the_line_not_checked(self):
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        with mock.patch.object(integrate, "fold",
                               side_effect=integrate.IntegrationError("the fold failed")):
            self._run([self._planned("D", "R, P")], ["R", "D"])
        self.assertIn("the fold failed", self._not_carried("R"))
        self._assert_never_built("D")
        self._assert_told_line_not_checked(oid, line)

    def test_a_red_re_gate_leaves_the_line_not_checked(self):
        # The fold pushed, so the line moved, but the run does not use it.
        self.cfg.regate_between_waves = True
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        with mock.patch.object(flow.gates, "run_integration",
                               return_value={"overall": "fail"}):
            self._run([self._planned("D", "R, P")], ["R", "D"])
        self.assertIn("re-gate", self._not_carried("R"))
        self._assert_never_built("D")
        self._assert_told_line_not_checked(oid, line)

    def test_an_untrusted_line_is_named_as_not_checked(self):
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        other = self.fx._push_from_human({"other.txt": "other\n"}, to=line, off=f"origin/{line}")
        self._run([self._planned("D", "R, P")], ["R", "D"])
        self.assertIn(f"holds {other[:12]}", self._not_carried("R"))
        self._assert_never_built("D")
        self._assert_told_line_not_checked(oid, line)

    def test_a_prerequisite_left_unbuilt_keeps_d_off_the_line_only_this_run(self):
        # The carry worked, but E, which D names, was not built, so D is not cut from the line
        # this run, and the line is not judged for it. The next run builds E there, with D.
        batch = ["R", "E", "D"]
        line, oid = self._line_older_than_the_merge(_onto_record("P"), batch=tuple(batch))
        e, d = self._planned("E", "R"), self._planned("D", "E, P")
        self.not_built = {"issue_E"}
        self._run([e, d], batch)
        self.assertIn("carried issue_R", self.err)
        self.assertEqual(self.results["D"], state.PLANNED)
        self._assert_told_line_not_checked(oid, line)
        self.not_built = set()
        self._planned("D", "E")
        self._run([e, d], batch)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"][0], line)
        self.assertFalse(self.fx._ancestor(oid, self.seen["issue_D"][1]))   # hence the step

    def _conflicts_with_r(self) -> Path:
        """D names P only, and `Conflicts with` R, finished and named in the run: a seam that
        keeps D on the run's line, with no other edge to hold it (#911)."""
        d = self.cfg.bundle("D")
        sb._brief(d, extra="- **Depends on:** P\n- **Conflicts with:** R\n")
        return d

    def test_a_seam_with_no_other_hold_is_told_the_line_was_not_checked(self):
        # D is on the line by its `Conflicts with` edge to R, and held for nothing but P. The
        # fold fails, so the run records no line for main and judges none for D. Before: only
        # "drop issue_P from issue_D's brief, then re-run", and that re-run continued the older
        # line, so D was COMPLETE on a tip without P's merge commit, with no word of it. Every
        # step D's line prints is followed, and no other: D is built on a tip that has the
        # merge.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        d = self._conflicts_with_r()
        with mock.patch.object(integrate, "fold",
                               side_effect=integrate.IntegrationError("the fold failed")):
            self._run([d], ["R", "D"])
        self.assertEqual(self.line, line)
        self._assert_never_built("D")
        self.assertTrue(self.fx._has(line))
        said = self._d_held()
        self.assertIn("drop issue_P from issue_D's brief", said)
        sb._brief(d, extra="- **Conflicts with:** R\n")     # drop P from D's brief
        if f"delete {line} on origin" in said and self.fx._has(line):
            self.fx._delete(line)                            # delete the line it names
        self._run([d], ["R", "D"])                           # re-run R, D
        self.assertEqual(self.results["D"], state.COMPLETE)
        # D's published branch is what it was built on plus its own commit.
        self.assertTrue(self.fx._ancestor(oid, "fix/D"), "D built without P's merge commit")
        self.err = said
        self._assert_told_line_not_checked(oid, line)

    def test_a_seam_whose_line_a_re_run_continues_is_built_on_the_merge(self):
        # As above, with E in the batch naming R in its plain `Depends on`, so the re-run's
        # carry does continue the older line, and D's seam keeps D on it. Before: the drop step
        # alone, and once it was followed D was COMPLETE on that line's tip, without P's merge
        # commit, with no word of it. Every step D's line prints is followed, and no other.
        batch = ["R", "E", "D"]
        line, oid = self._line_older_than_the_merge(_onto_record("P"), batch=tuple(batch))
        e, d = self._planned("E", "R"), self._conflicts_with_r()
        with mock.patch.object(integrate, "fold",
                               side_effect=integrate.IntegrationError("the fold failed")):
            self._run([e, d], batch)
        self.assertEqual(self.line, line)
        self.assertIn("the fold failed", self._not_carried("R"))
        self._assert_never_built("D")
        said = self._d_held()
        self.assertIn("drop issue_P from issue_D's brief", said)
        sb._brief(d, extra="- **Conflicts with:** R\n")     # drop P from D's brief
        if f"delete {line} on origin" in said and self.fx._has(line):
            self.fx._delete(line)                            # delete the line it names
        self._run([e, d], batch)                             # re-run R, E, D
        self.assertIn("carried issue_R", self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"][0], line)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]),
                        "D built on a line without P's merge commit")
        self.err = said
        self._assert_told_line_not_checked(oid, line)

    def test_a_prerequisite_to_name_in_the_re_run_brings_that_lines_step(self):
        # A run of O and D left O on its line. A run of D alone is told to name O and to drop P:
        # that re-run continues the older line, which this run's line is not.
        o, p = self._finished("O"), self._finished("P")
        line = self._earlier_run_folds(["O", "D"], o)
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "O, P")
        self._run([d], ["D"])
        self.assertNotEqual(self.line, line)
        self._assert_never_built("D")
        held = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(held), 2, self.err)
        told = next(x for x in held if "issue_P (merge commit" in x)
        self.assertIn(f"delete {line} on origin if it is there", told)
        self.assertIn("that line, the line of a re-run naming issue_O too, for that merge", told)
        self.assertNotIn("as well", told)           # that is the one line here, said once
        self.assertIn(READD, next(x for x in held if x is not told))
        self._planned("D", "O")
        self.fx._delete(line)
        self._run([d], ["O", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_the_drop_alone_past_that_line_leaves_d_on_it(self):
        # Why it asks for the delete there too: the run naming O continues the older line, and
        # nothing says so. Pinned so that the docs' sentence on it stays true.
        o, p = self._finished("O"), self._finished("P")
        line = self._earlier_run_folds(["O", "D"], o)
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "O, P")
        self._run([d], ["D"])
        self._planned("D", "O")
        self._run([d], ["O", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"][0], line)
        self.assertFalse(self.fx._ancestor(oid, self.seen["issue_D"][1]))
        self.assertNotIn(NAMED, self.err)

    def test_a_prerequisite_to_name_past_an_older_line_brings_both_lines_steps(self):
        # D's line says to delete this run's older line and to name O, and naming O moves the
        # re-run to another older line. Before: only the first was named, so with every step
        # followed D was built on the second, without the merge.
        r, o, p = self._finished("R"), self._finished("O"), self._finished("P")
        other = self._earlier_run_folds(["R", "O", "D"], r, o)
        line = self._earlier_run_folds(["R", "D"], r)
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "R, O, P")
        self._run([d], ["R", "D"])
        self.assertEqual(self.line, line)
        self._assert_never_built("D")
        held = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(held), 2, self.err)
        told = next(x for x in held if "issue_P (merge commit" in x)
        self.assertIn(f"delete {line} on origin, then re-run", told)
        self.assertIn(f"Naming issue_O too gives the re-run another line: delete {other} on origin "
                      "as well if it is there.", told)
        self._planned("D", "R, O")
        self.fx._delete(line, other)
        self._run([d], ["R", "O", "D"])
        self.assertEqual(self.line, other)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(oid, self.seen["issue_D"][1]))

    def test_a_bundle_outside_the_re_issued_batch_keeps_its_edge_and_is_told_when_it_runs(self):
        # P's line asks for the drop only in the batch it re-issues, and names that batch's line.
        # E is in another batch, with an older line of its own: it keeps its edge, and that edge
        # is what makes its run check its line.
        line, oid = self._line_older_than_the_merge(_onto_record("P"), batch=("R", "E"))
        d, e = self._planned("D", "R, P"), self._planned("E", "R, P")
        self._run([d], ["P", "R", "D"])
        p = self._not_carried("P")
        self.assertIn(DROP, p)
        self.assertNotIn(line, p)
        self._run([e], ["R", "E"])
        self.assertEqual(self.line, line)
        self._assert_never_built("E")
        told = [x for x in self.err.splitlines() if x.startswith("flow: issue_E held")]
        self.assertEqual(len(told), 1, self.err)
        self.assertIn(oid[:12], told[0])
        self.assertIn("drop issue_P from issue_E's brief", told[0])
        self.assertIn(f"delete {line} on origin", told[0])

    def test_a_bundle_held_for_nothing_else_is_not_told_the_line_was_not_checked(self):
        # Something else is held, but D does not name it: once the edge is gone D is built from
        # the base.
        d, oid = self._left_naming_p(_onto_record("P"))
        self.assertEqual(self._judge(d, held={"issue_R"}), [])
        self._assert_told_to_drop(oid)

    def test_a_line_judged_to_have_the_merge_is_not_named_as_not_checked(self):
        # D is held for R too, but a line was judged for it: nothing to delete.
        d, oid = self._left_naming_p(_onto_record("P"))
        self._finished("R")
        self._planned("D", "R, P")
        tip = self.fx._push_from_human({"line.txt": "line\n"}, to="the-line", off="origin/main")
        self.assertTrue(self.fx._ancestor(oid, tip))
        self.assertEqual(self._judge(d, held={"issue_R"}, requested={"issue_R", "issue_D"},
                                     lines={TARGET: ("the-line", tip)}), [])
        self.assertIn("not ready (R)", self.err)
        self._assert_told_to_drop(oid)

    def test_a_line_that_has_the_head_is_not_named(self):
        # An earlier run folded Q, built on P: that line has P's head, so all of P's work,
        # though not the merge commit. No need to delete it, and once the edge is gone D is
        # built on P's work.
        p = self._finished("P")
        q = self._finished("Q", on="P")
        line = self._earlier_run_folds(["Q", "D"], q)
        head = self.fx._tip("fix/P")
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **_onto_record("P"))
        self.assertFalse(self.fx._ancestor(oid, line))
        self.assertTrue(self.fx._ancestor(head, line))
        d = self._planned("D", "Q, P")
        self._run([d], ["Q", "D"])
        self._assert_never_built("D")
        self.assertIn("carried issue_Q", self.err)
        self._assert_told_to_drop(oid)
        self._planned("D", "Q")
        self._run([d], ["Q", "D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertTrue(self.fx._ancestor(head, self.seen["issue_D"][1]))

    def test_a_line_that_has_the_merge_commit_is_not_named(self):
        # A line started after the merge: the way out needs nothing done to it.
        r, p = self._finished("R"), self._finished("P")
        oid = self._squash("P")
        self.fx._delete("fix/P")
        self._record(p, **_onto_record("P"))
        line = self._earlier_run_folds(["R", "D"], r)
        self.assertTrue(self.fx._ancestor(oid, line))
        self._run([self._planned("D", "R, P")], ["R", "D"])
        self._assert_never_built("D")
        self._assert_told_to_drop(oid)

    def test_a_dependent_built_from_the_base_has_no_line_to_delete(self):
        # The run has that older line, for E, but no edge ties D to it (#727): D is built from
        # the base as fetched, which has the merge commit, and the line is never read for it.
        batch = ["R", "E", "D"]
        line, oid = self._line_older_than_the_merge(_onto_record("P"), batch=tuple(batch))
        d, e = self._planned("D", "P"), self._planned("E", "R")
        self._run([d, e], batch)
        self.assertEqual(self.seen["issue_E"][0], line)
        self._assert_never_built("D")
        self.assertEqual(self.merge_commit_reads, ["P"])
        self._assert_told_to_drop(oid)
        sb._brief(d)
        self._run([d], batch)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"], ("", ""))

    def test_a_line_that_cannot_be_checked_is_named_as_that(self):
        # Fail closed, as for a record that counts: the second read of the merge commit fails.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        self.reads_before_gh_fails = 1
        self._run([self._planned("D", "R, P")], ["R", "D"])
        self._assert_never_built("D")
        self.assertEqual(self.merge_commit_reads, ["P", "P"])
        said = self._assert_told_to_drop(oid, delete=line)
        self.assertIn("is not known to have the merge of issue_P (its PR's merge commit could "
                      "not be read)", said)

    def test_of_two_to_drop_only_the_one_the_line_lacks_is_named_as_lacking(self):
        # O merged before the line was cut from main, P after: both go, and only P is missing.
        o, p = self._finished("O"), self._finished("P")
        o_oid = self._squash("O")
        self._record(o, **_onto_record("O"))
        tip = self.fx._push_from_human({"line.txt": "line\n"}, to="the-line", off="origin/main")
        p_oid = self._squash("P")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "O, P")
        self.assertEqual(self._judge(d, lines={TARGET: ("the-line", tip)}), [])
        said = self._d_held()
        self.assertIn(o_oid[:12], said)
        self.assertIn(p_oid[:12], said)
        self.assertIn("drop issue_O, issue_P from issue_D's brief and", said)
        self.assertIn(f"the-line at {tip[:12]}, does not have the merge of issue_P,", said)

    def test_a_target_with_no_line_has_none_to_delete_and_one_with_no_tip_is_not_checked(self):
        # Judged as before, as for a record that counts, and so is a line of another target:
        # D is on no line this run.
        d, oid = self._left_naming_p(_onto_record("P"))
        for lines in ({}, {("org/other", "main"): (sb.LINE, ABSENT)}):
            with self.subTest(lines=lines):
                self.merge_commit_reads.clear()
                self.assertEqual(self._judge(d, lines=lines), [])
                self._assert_told_to_drop(oid)
                self.assertEqual(self.merge_commit_reads, ["P"])
        # A line with no recorded tip, every bundle on it (`on_line` None): D is on a line the
        # run did not check (#911). Before: the drop step alone, as for no line at all.
        self.merge_commit_reads.clear()
        self.assertEqual(self._judge(d, lines={TARGET: (sb.LINE, None)}), [])
        self._assert_told_line_not_checked(
            oid, integrate.integration_branch(self.cfg, "main", [d.name]))
        self.assertEqual(self.merge_commit_reads, ["P"])

    def test_a_bundle_not_on_the_line_is_not_told_to_delete_it(self):
        d, oid = self._left_naming_p(_onto_record("P"))
        tip = self.fx._push_from_human({"line.txt": "line\n"}, to="the-line", off="origin/main~1")
        self.assertFalse(self.fx._ancestor(oid, tip))
        self.assertEqual(self._judge(d, lines={TARGET: ("the-line", tip)}, on_line=set()), [])
        self._assert_told_to_drop(oid)
        self.assertEqual(self.merge_commit_reads, ["P"])

    def test_the_line_is_read_once_for_two_dependents(self):
        # As for a record that counts (`lacks`): each dependent reads the merge commit for its
        # own hold, and the first of them once more for the line.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        d, e = self._planned("D", "P"), self._planned("E", "P")
        err = io.StringIO()
        with mock.patch.object(subprocess, "run", self._gh_stub), redirect_stderr(err):
            runnable = flow._runnable(self.cfg, [d, e], {d.name, e.name}, hold_unmerged=True,
                                      lines={TARGET: (line, self.fx._tip(line))})
        self.assertEqual(runnable, [])
        self.assertEqual(self.merge_commit_reads, ["P", "P", "P"])
        self.assertEqual(err.getvalue().count(f"delete {line} on origin"), 2)
        self.assertIn(oid[:12], err.getvalue())

    # -- ... and when the base reverted it since ------------------------------------------------

    def test_a_reverted_merge_is_not_said_to_be_on_the_base(self):
        # `_squash_merged` shows the merge commit is an ancestor of the base, which a revert
        # does not change. Before: "so main has the PR's work", of a main without it.
        d, oid = self._left_naming_p(_onto_record("P"))
        self.assertEqual(self.fx._show("main", "p.txt"), "P\n")
        self.fx._revert_on_main(oid)
        self.assertNotEqual(sb._run(self.fx.origin, "cat-file", "-e", "main:p.txt").returncode, 0)
        self.assertEqual(self._judge(d), [])
        said = self._assert_told_to_drop(oid)
        self.assertIn("their PR was squash- or rebase-merged into main", said)

    def test_a_reverted_merge_is_not_said_to_be_on_the_base_past_an_older_line(self):
        # The same words where the line asks for a delete too: it speaks of the merge commit.
        line, oid = self._line_older_than_the_merge(_onto_record("P"))
        self.fx._revert_on_main(oid)
        self._run([self._planned("D", "R, P")], ["R", "D"])
        self._assert_never_built("D")
        said = self._assert_told_to_drop(oid, delete=line)
        self.assertIn("which has the merge commit", said)

    def test_the_drop_after_a_revert_builds_without_the_change(self):
        # What the line's plainer words leave to you: the check cannot see the revert, so the
        # drop builds D from a main without P's change. Pinned so that the docs' sentence on it
        # stays true; a run that looked for the revert would change this case.
        d, oid = self._left_naming_p(_onto_record("P"))
        self.fx._revert_on_main(oid)
        self.assertEqual(self._judge(d), [])
        sb._brief(d)
        self._run([d], ["D"])
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertEqual(self.seen["issue_D"], ("", ""))
        self.assertNotIn(NAMED, self.err)

    def test_a_dependent_held_for_two_things_gets_a_line_for_each(self):
        # One prerequisite to drop, one to wait for: neither hides the other.
        p = self._finished("P")
        self._finished("O")
        oid = self._squash("P")
        self._record(p, **_onto_record("P"))
        d = self._planned("D", "P, O")
        self.assertEqual(self._judge(d), [])
        lines = [x for x in self.err.splitlines() if x.startswith("flow: issue_D held")]
        self.assertEqual(len(lines), 2, self.err)
        told = next(x for x in lines if "issue_P" in x)
        plain = next(x for x in lines if "issue_O" in x)
        self.assertIn(oid[:12], told)
        self.assertIn("drop issue_P from issue_D's brief", told)
        self.assertNotIn(WAIT, told)
        self.assertNotIn(READD, told)
        self.assertIn(WAIT, plain)
        self.assertNotIn("issue_P", plain)

    def test_the_run_does_not_check_the_update(self):
        # Of those two steps only the second releases Q: P leaving the batch ends the hold, so
        # a Q nobody updated is carried too, and its PR keeps showing P's change. Pinned so
        # that the docs' sentence on it stays true; a run that enforced the update would
        # change this case.
        self._finished("P")
        self._finished("Q", on="P")
        oid = self._squash("P")
        d = self._planned("D", "Q")
        self._run([d], ["Q", "D"])
        self.assertIn("carried issue_Q", self.err)
        self.assertEqual(self.results["D"], state.COMPLETE)
        self.assertFalse(self.fx._ancestor(oid, "fix/Q"))
        self.assertEqual(self.fx._diff_files("main", "fix/Q"), ["p.txt", "q.txt"])


if __name__ == "__main__":
    unittest.main()
