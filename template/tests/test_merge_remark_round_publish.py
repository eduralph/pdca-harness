"""Only review bots open a remark round, and an accepted round is pushed onto its PR (#922).

#921 reopened a merge-mode PR blocked by review threads as a remark round, but for the
threads of repo collaborators, and an accepted round then stopped with "round publish not
available yet — apply patch.diff by hand". Now a round opens only when every open thread on
the PR is a review bot's — a login ``[driver].review_bots`` lists (case and a trailing
``[bot]`` aside) whom the host reports as a ``Bot`` — and no change request stands but a
bot's (read from ``latestOpinionatedReviews``); anything else holds the PR, and its
dependents, for the human on one line. An accepted round's ``patch.diff`` is committed onto
the round's ``pinned_head`` in a throwaway worktree, checked against the tree computed
before the commit, and pushed onto the PR's branch with a plain push; the operator's
checkout is never touched; the round records the commit (``pushed``, ``round_commits``) and
stays live.

Real git: the ``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare
``origin``, a primary checkout, a scratch clone for everyone else), loaded by path as
tests/test_merge_remark_round_open.py loads it. The run goes through ``flow.flow_ids`` with
the real driver, worktrees, publish (both its first, ordinary publish and the round's) and
merge step. The builder, reviewer, sign-off and publisher leaves are stubbed (the builder
edits the real Do worktree and writes its ``git diff``; the publisher writes the two texts),
and ``gh`` is a stub in front of ``subprocess.run`` that reports P's PR blocked by bot
threads. The publisher is not in stub mode, which would make every publish a dry run. A
wrapper around ``publish.publish`` dirties the operator's checkout and snapshots it just
before the round's publish, once ``remark_round.live(P)`` holds.

Only modules are imported (never a symbol the change adds), so with the production change
reverted these cases still load and run, and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_merge_remark_round_publish
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import (cli, config, flow, leaves, merge, merged, publish, remark_round,
                          state, worktree)

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-11-22"
P, Q = "50", "60"
PR = f"https://example.test/pr/{P}"
BRANCH = f"fix/{P}"
ROUND = "remark-round.json"
RESPONSES = "remark-responses.json"
BOTS = ("codex-bot", "lint-bot")
A = "".join(f"a line {i}\n" for i in range(1, 21))
B = "".join(f"b line {i}\n" for i in range(1, 11))
C = "".join(f"c line {i}\n" for i in range(1, 11))
T3_BODY = "SECRET-T3-BODY: a person asks for a different design"
MERGE_ERROR = ("GraphQL: Pull request is not mergeable: the base branch policy prohibits the "
               "merge. (mergePullRequest)")
FIRST_MSG = f"Fix the thing (first attempt)\n\nFixes #{P}\n"
ROUND_MSG = f"Fix the thing (remark round 1)\n\nFixes #{P}\n"
STOPPED = f"publish: issue_{P} — the round publish stopped at"
# What may run against the operator's checkout itself during a round's publish.
ON_CHECKOUT = {"remote", "fetch", "rev-parse", "cat-file", "read-tree", "apply", "write-tree",
               "worktree"}
GITLINK = ("diff --git a/sub b/sub\nnew file mode 160000\nindex 0000000..1234567\n"
           "--- /dev/null\n+++ b/sub\n@@ -0,0 +1 @@\n"
           "+Subproject commit 1234567890123456789012345678901234567890\n")
# A hunk that, appended to the round's patch.diff, makes it add one more file.
EXTRA_FILE = ("diff --git a/zz.py b/zz.py\nnew file mode 100644\n--- /dev/null\n+++ b/zz.py\n"
              "@@ -0,0 +1 @@\n+only in the edited patch.diff\n")
# The same, for a file whose one line is Latin-1, not UTF-8: still a patch `git apply` takes.
LATIN1_FILE = (b"diff --git a/latin.txt b/latin.txt\nnew file mode 100644\n--- /dev/null\n"
               b"+++ b/latin.txt\n@@ -0,0 +1 @@\n+caf\xe9 au lait\n")
# A [gates] host_ci command that records the tree it ran in (from a throwaway index).
HOST_CI = r'''
import json, os, subprocess, sys
wt = os.environ.get("PDCA_WORKTREE") or os.getcwd()
env = {**os.environ, "GIT_INDEX_FILE": os.path.join(sys.argv[2], f"index-{os.getpid()}")}
for args in (["read-tree", "HEAD"], ["add", "-A"]):
    subprocess.run(["git", "-C", wt, *args], env=env, check=True)
tree = subprocess.run(["git", "-C", wt, "write-tree"], env=env, check=True,
                      capture_output=True, text=True).stdout.strip()
os.unlink(env["GIT_INDEX_FILE"])
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"wt": wt, "tree": tree}) + "\n")
'''


def _node(tid: str | None, path: str, line: int, body: str, who: str | None,
          kind: str = "Bot") -> dict:
    """One unresolved `reviewThreads` node as the host returns it. ``who`` None is a deleted
    account (GraphQL gives `author: null`); an app's comment has `authorAssociation: NONE`."""
    author = None if who is None else {"__typename": kind, "login": who}
    first = {"body": body, "url": f"{PR}#{tid}", "authorAssociation": "NONE", "author": author}
    return {"id": tid, "isResolved": False, "isOutdated": False, "path": path, "line": line,
            "originalLine": line, "comments": {"nodes": [first]}}


def _asks(who: str, kind: str, st: str = "CHANGES_REQUESTED") -> dict:
    """One `latestOpinionatedReviews` node."""
    return {"state": st, "author": {"__typename": kind, "login": who}}


T1 = _node("T1", "a.py", 12, "a.py:12 reads past the end of the list", "lint-bot")
T2 = _node("T2", "b.py", 3, "b.py:3 should keep the old default", "codex-bot")
T3 = _node("T3", "c.py", 7, T3_BODY, "alice", "User")


def _edit(text: str, lines: dict[int, str]) -> str:
    out = text.splitlines(keepends=True)
    for n, new in lines.items():
        out[n - 1] = new + "\n"
    return "".join(out)


class RoundPublish(unittest.TestCase):

    def setUp(self) -> None:
        self._real_run = subprocess.run
        self._real_publish = publish.publish

    # -- the one fixture builder ------------------------------------------------------------

    def _fresh(self, *, threads=(T1, T2), bots=BOTS) -> None:
        """A new world: origin, the operator's checkout with a.py / b.py / c.py on main, the
        config of the criterion, P's PR blocked by ``threads``, and the leaves' plans."""
        self.fx = sb.StackFoldGit()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        cfg = self.cfg = self.fx.cfg
        cfg.publisher = dataclasses.replace(cfg.publisher, mode="command")
        cfg.wave_mode = "merge"
        cfg.merge_wait_secs = 0
        cfg.max_passes = 3
        cfg.remark_rounds = 1
        cfg.review_bots = list(bots)
        cfg.fix_branch_pattern = "fix/{id}"
        for name, text in {"a.py": A, "b.py": B, "c.py": C}.items():
            (self.fx.primary / name).write_text(text, encoding="utf-8")
        sb._git(self.fx.primary, "add", "--all")
        sb._git(self.fx.primary, "commit", "-q", "-m", "base files")
        sb._git(self.fx.primary, "push", "-q", "origin", "main")
        self.base = sb._git(self.fx.primary, "rev-parse", "HEAD").strip()
        self.owner = self.fx.origin.parent.name     # the owner `publish` reads off origin's URL
        # The host.
        self.threads = list(threads)
        self.threads_fail = False
        self.reviews: list[dict] = []                # latestReviews: #838's read, unchanged
        self.opinionated: list[dict] = []            # latestOpinionatedReviews: the round's
        self.opinionated_fails = False
        self.opinionated_more = False
        self.decision = ""                           # reviewDecision
        self.open_pr: str | None = PR                # `gh pr list --head fix/50 --state open`
        self.calls: list[list[str]] = []             # every gh and git call, in order
        self.refused_at: int | None = None           # len(calls) once P's merge was refused
        self.head_at_refusal = ""
        # The leaves.
        self.first_edits = {P: {"a.py": {12: "a line 12 - first fix"},
                                "b.py": {3: "b line 3 - first fix"}},
                            Q: {"c.py": {4: "c line 4 - Q"}}}
        self.round_edits = {"a.py": {12: "a line 12 - round fix"}}
        self.round_new = {"d.py": "d line 1\n"}       # the round adds a file too
        self.responses = {
            "T1": {"addressed": True, "reply": "Fixed: a.py:12 now stops at the end."},
            "T2": {"addressed": False, "reply": "Kept: b.py:3 keeps the new default."}}
        self.decisions: dict[str, list[str | None]] = {}
        self.builds: list[dict] = []
        self.drafts: list[tuple[str, bool]] = []     # (bundle, in a live round) per draft
        self.on_round_draft = None                   # run by the publisher leaf in a round
        # Around the round's publish (the wrapper, `_publish`).
        self.before_round_publish = None             # one shot, before the snapshot
        self.dirt: str | None = None                 # None | "basic" | "full"
        self.round_publishes: list[dict] = []

    # -- stubs ------------------------------------------------------------------------------

    def _sub(self, args, *a, **kw):
        """`gh` answered as the host, `git` run for real; both recorded."""
        if not (isinstance(args, list) and args[:1] in (["gh"], ["git"])):
            return self._real_run(args, *a, **kw)
        self.calls.append([str(x) for x in args])
        if args[0] == "git":
            return self._real_run(args, *a, **kw)
        return self._gh([str(x) for x in args])

    def _gh(self, args: list[str]) -> subprocess.CompletedProcess:
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")    # noqa: E731
        no = lambda err: subprocess.CompletedProcess(args, 1, "", err)       # noqa: E731
        if args[1:3] == ["api", "graphql"]:
            query = next((x for x in args if x.startswith("query=")), "")
            if "latestOpinionatedReviews" in query:
                if self.opinionated_fails:
                    return no("HTTP 502: Bad Gateway")
                return ok(json.dumps({"data": {"resource": {"latestOpinionatedReviews": {
                    "pageInfo": {"hasNextPage": self.opinionated_more},
                    "nodes": self.opinionated}}}}))
            if self.threads_fail:
                return no("HTTP 502: Bad Gateway")
            return ok(json.dumps({"data": {"resource": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": self.threads}}}}))
        if args[1] == "issue":                       # the tracker: every issue is open
            fields = args[args.index("--json") + 1] if "--json" in args else ""
            return ok(json.dumps({"comments": []} if fields == "comments" else
                                 {"state": "OPEN", "stateReason": "", "closedAt": ""}))
        verb = args[2]
        if verb == "list":
            head = args[args.index("--head") + 1]
            return ok(json.dumps([] if self.open_pr is None else [{
                "url": self.open_pr, "number": int(P), "headRefName": head,
                "headRepositoryOwner": {"login": self.owner}}]))
        if verb == "create":
            return ok(f"{PR}\n")
        if verb == "view":
            iid = args[3].rsplit("/", 1)[-1]
            head = self.fx._tip(f"fix/{iid}") if self.fx._has(f"fix/{iid}") else ""
            return ok(json.dumps({
                "state": "OPEN", "headRefOid": head, "baseRefName": "main",
                "mergeStateStatus": "BLOCKED", "reviewDecision": self.decision,
                "latestReviews": self.reviews}))
        if verb == "checks":
            return ok(json.dumps([{"name": "ci", "bucket": "pass"}]))
        if verb == "merge":
            if self.refused_at is None:
                self.refused_at = len(self.calls)
                self.head_at_refusal = self.fx._tip(BRANCH)
            return no(MERGE_ERROR)
        if verb == "update-branch":
            self.fx._update_branch(args[3].rsplit("/", 1)[-1])
        if verb == "repo":
            return ok("{}")
        return ok()

    def _build(self, d: Path, cfg) -> None:
        """Do: edit the real worktree (P's first cycle on the base, its round on the PR head)
        and write its diff; a round also answers its threads."""
        iid = d.name.removeprefix("issue_")
        in_round = remark_round.live(d)
        wt = worktree.ensure(d, cfg)
        self.assertIsNotNone(wt)
        self.builds.append({"bundle": d.name, "round": in_round,
                            "head": sb._git(wt, "rev-parse", "HEAD").strip()})
        edits, new = ((self.round_edits, self.round_new) if in_round
                      else (self.first_edits[iid], {}))
        for name, lines in edits.items():
            (wt / name).write_text(_edit((wt / name).read_text(encoding="utf-8"), lines),
                                   encoding="utf-8")
        for name, text in new.items():
            (wt / name).write_text(text, encoding="utf-8")
        sb._git(wt, "add", "--all")
        (d / "patch.diff").write_text(sb._git(wt, "diff", "--cached", "HEAD"), encoding="utf-8")
        (d / "build-notes.md").write_text("test builder\n", encoding="utf-8")
        if in_round:
            (d / RESPONSES).write_text(json.dumps(self.responses), encoding="utf-8")

    def _signoff(self, cfg, bundles: list[Path]) -> None:
        """Each bundle's next queued decision (accept by default; None decides nothing)."""
        for d in bundles:
            queue = self.decisions.get(d.name.removeprefix("issue_"))
            decision = queue.pop(0) if queue else "accept"
            if decision is None:
                continue
            summary = d / "SUMMARY.md"
            summary.write_text(summary.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                               encoding="utf-8")
            (d / leaves.SIGNOFF_DECISION).write_text(decision + "\n", encoding="utf-8")

    def _draft(self, d: Path, cfg) -> None:
        """The publisher leaf: the two texts, saying which attempt they are for."""
        live = remark_round.live(d)
        msg = ROUND_MSG if live else FIRST_MSG
        (d / publish.COMMIT_MSG).write_text(msg, encoding="utf-8")
        (d / publish.PR_BODY).write_text(f"**User impact:** {msg}", encoding="utf-8")
        self.drafts.append((d.name, live))
        if live and self.on_round_draft:
            self.on_round_draft(d)

    def _publish(self, cfg, issue_id, **kw):
        """`publish.publish`, with the operator's checkout dirtied and snapshotted just before
        a live round's publish (P's first, ordinary publish passes straight through)."""
        d = cfg.bundle(issue_id)
        if not remark_round.live(d):
            return self._real_publish(cfg, issue_id, **kw)
        if self.dirt and not self.round_publishes:
            self._dirty(full=self.dirt == "full")       # before any hook a case installs
        hook, self.before_round_publish = self.before_round_publish, None
        if hook:
            hook(d)
        call = {"mark": len(self.calls), "before": self._checkout_state()}
        call["rc"] = self._real_publish(cfg, issue_id, **kw)
        call.update(end=len(self.calls), after=self._checkout_state())
        self.round_publishes.append(call)
        return call["rc"]

    def _stubs(self, stack: ExitStack) -> None:
        stack.enter_context(mock.patch.object(subprocess, "run", self._sub))
        stack.enter_context(mock.patch.object(leaves, "do_build", self._build))
        stack.enter_context(mock.patch.object(leaves, "run_review", leaves._stub_review))
        stack.enter_context(mock.patch.object(leaves, "run_signoff_batch", self._signoff))
        stack.enter_context(mock.patch.object(leaves, "run_publish", self._draft))
        stack.enter_context(mock.patch.object(publish, "publish", self._publish))
        stack.enter_context(mock.patch.object(merge, "_sleep", lambda _s: None))

    # -- running ----------------------------------------------------------------------------

    def _scenario(self) -> Path:
        """P (wave 0) and Q, which depends on it (wave 1), driven by one merge-mode run."""
        sb._brief(self.cfg.bundle(P))
        sb._brief(self.cfg.bundle(Q), extra=f"- **Depends on:** {P}\n")
        self._run()
        return self.cfg.bundle(P)

    def _run(self, ids=(P, Q)) -> None:
        self.mark = len(self.calls)
        self.builds_before = len(self.builds)
        err = io.StringIO()
        self.holds: dict[str, str] = {}
        try:
            with ExitStack() as stack:
                self._stubs(stack)
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(redirect_stderr(err))
                self.results = flow.flow_ids(self.cfg, list(ids), do_publish=True,
                                             do_act=False, by="T", today=TODAY,
                                             holds=self.holds)
        finally:
            self.err = err.getvalue()
        self.rc = cli._results_rc(self.results, holds=self.holds)
        self.assertNotIn("Traceback", self.err)

    def _cli(self, *argv: str) -> tuple[int, str, list[list[str]]]:
        """`pdca <argv>` on this world: its exit code, its output, the calls it made."""
        mark = len(self.calls)
        out = io.StringIO()
        with ExitStack() as stack:
            self._stubs(stack)
            stack.enter_context(mock.patch.object(config.Config, "load",
                                                  lambda *_a, **_k: self.cfg))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(out))
            rc = cli.main(list(argv))
        self.assertNotIn("Traceback", out.getvalue())
        return rc, out.getvalue(), self.calls[mark:]

    # -- the operator's checkout --------------------------------------------------------------

    def _dirty(self, *, full: bool) -> None:
        """The operator's edits: staged, unstaged, untracked; ``full`` also an ignored file, an
        untracked file where the round's patch adds one, a local ``fix/50`` checked out at
        another commit than the PR head (as `gh pr checkout` leaves it), and
        ``status.showUntrackedFiles=no``."""
        p = self.fx.primary
        run = lambda *a: self.assertEqual(self._git(p, *a).returncode, 0)   # noqa: E731
        if full:
            run("checkout", "-q", "-B", BRANCH, "main")
            run("config", "status.showUntrackedFiles", "no")
            with (p / ".git" / "info" / "exclude").open("a", encoding="utf-8") as fh:
                fh.write("*.log\n")
            (p / "build.log").write_text("ignored\n", encoding="utf-8")
            (p / "d.py").write_text("the operator's own d.py\n", encoding="utf-8")
        (p / "base.txt").write_text("base\nstaged edit\n", encoding="utf-8")
        run("add", "base.txt")
        (p / "c.py").write_text(C + "unstaged edit\n", encoding="utf-8")
        (p / "notes.txt").write_text("untracked\n", encoding="utf-8")

    def _checkout_state(self) -> dict:
        """What the round's publish must leave as it was in the operator's checkout: HEAD,
        every local branch, the index, the status (ignored and untracked files included), the
        stash, the worktrees, and every byte of every file in it."""
        p = self.fx.primary
        say = lambda *a: self._git(p, *a).stdout                            # noqa: E731
        files = {str(f.relative_to(p)): f.read_bytes() for f in sorted(p.rglob("*"))
                 if f.is_file() and ".git" not in f.relative_to(p).parts}
        return {"HEAD": say("rev-parse", "HEAD") + say("symbolic-ref", "-q", "HEAD"),
                "heads": say("for-each-ref", "refs/heads/"), "index": say("write-tree"),
                "status": say("status", "--porcelain=v2", "--untracked-files=all", "--ignored"),
                "stash": say("stash", "list"), "worktrees": say("worktree", "list", "--porcelain"),
                "files": files}

    def _leftover_worktrees(self) -> list[Path]:
        """Throwaway worktrees left beside the operator's checkout."""
        p = self.fx.primary
        return sorted(p.parent.glob(f"{p.name}{worktree._OVF_SUFFIX}*"))

    def _checkout_untouched(self) -> None:
        """Criterion (4): every round publish of this world left the operator's checkout as it
        found it, ran nothing against it but what may run there, and left no worktree."""
        self.assertTrue(self.round_publishes, self.err)
        self.assertEqual(self._leftover_worktrees(), [])
        primary = self.fx.primary.resolve()
        for call in self.round_publishes:
            self.assertEqual(call["after"], call["before"])
            on_it = [c for c in self.calls[call["mark"]:call["end"]]
                     if c[:2] == ["git", "-C"] and Path(c[2]).resolve() == primary]
            self.assertTrue(on_it)
            for c in on_it:
                self.assertIn(c[3], ON_CHECKOUT, c)
                if c[3] == "apply":
                    self.assertIn("--cached", c)

    # -- reading ----------------------------------------------------------------------------

    def _git(self, repo: Path, *args: str, env=None) -> subprocess.CompletedProcess:
        return self._real_run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              env=env)

    def _at_origin(self, *args: str) -> str:
        r = self._git(self.fx.origin, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def _tree(self, base: str, *patches: Path) -> str:
        """The tree of ``base`` with ``patches`` applied in turn, from a throwaway index in the
        scratch clone (nothing of the run's own)."""
        h = self.fx.human
        self.assertEqual(self._git(h, "fetch", "-q", "origin").returncode, 0)
        env = {**os.environ, "GIT_INDEX_FILE": str(self.fx.tmp / f"index-{len(self.calls)}")}
        steps = [("read-tree", base), *(("apply", "--cached", str(p)) for p in patches),
                 ("write-tree",)]
        for args in steps:
            r = self._git(h, *args, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def _json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _pushes(calls: list[list[str]]) -> list[list[str]]:
        return [c for c in calls if c[:1] == ["git"] and "push" in c]

    def _window(self, n: int = 0) -> list[list[str]]:
        call = self.round_publishes[n]
        return self.calls[call["mark"]:call["end"]]

    def _stopped_at(self) -> str:
        """The one line the round's publish stopped with."""
        lines = [x for x in self.err.splitlines() if x.startswith(STOPPED)]
        self.assertEqual(len(lines), 1, self.err)
        return lines[0]

    def _stop_line(self) -> str:
        """The flow's one line for the wave that stopped on P's round."""
        lines = [x for x in self.err.splitlines() if f"stopped on issue_{P}'s remark round" in x]
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("STOPPING", lines[0])
        return lines[0]

    def _nothing_recorded(self, d: Path, pinned: str) -> None:
        """A round publish that failed: the PR branch where the round found it, no `pushed`
        entry, no `round_commits`, and the round still live and accepted."""
        self.assertEqual(self.fx._tip(BRANCH), pinned)
        rec = remark_round.read(d) or {}
        pushed = rec.get("pushed")
        entry = pushed.get("1") if isinstance(pushed, dict) else None
        self.assertIn(entry, (None, "", 7), rec)     # none, or the malformed one (h) wrote
        pub = publish._publish_record(d)
        self.assertNotIn("round_commits", pub if isinstance(pub, dict) else {})
        self.assertNotEqual(self.rc, 0)
        stop = self._stop_line()
        self.assertIn("its round publish did not complete", stop)
        self.assertNotIn("pushes it", stop)              # not the round's line after a failure

    def _q_not_built(self) -> None:
        self.assertEqual(self.results.get(Q), state.PLANNED, self.err)
        self.assertEqual([b for b in self.builds if b["bundle"] == f"issue_{Q}"], [])

    def _held_for_the_human(self, d: Path, names: list[str]) -> str:
        """No round: nothing archived or built, nothing pushed, Q not built, the run stopped
        as #838 leaves it, and one line naming ``names`` as the human's to answer."""
        self.assertIsNotNone(self.refused_at, self.err)
        self.assertFalse((d / "iteration-v1").exists(), self.err)
        self.assertFalse((d / ROUND).exists(), self.err)
        self.assertEqual([b for b in self.builds if b["round"]], [], self.err)
        self.assertEqual(self._pushes(self.calls[self.refused_at:]), [])
        self.assertEqual(self.round_publishes, [])
        self._q_not_built()
        self.assertIn("did not merge; STOPPING", self.err)
        self.assertNotEqual(self.rc, 0)
        lines = [x for x in self.err.splitlines() if "held for the human" in x]
        self.assertEqual(len(lines), 1, self.err)
        for name in names:
            self.assertIn(name, lines[0])
        self.assertIn("the human's to answer", lines[0])
        return lines[0]

    def _asked_for_change_requests(self, calls: list[list[str]]) -> list[list[str]]:
        return [c for c in calls if c[:3] == ["gh", "api", "graphql"]
                and any("latestOpinionatedReviews" in x for x in c)]

    # -- the criterion ------------------------------------------------------------------------

    def test_criterion_an_accepted_bot_round_is_one_plain_commit_on_its_pr(self):
        self._fresh()
        self.dirt = "basic"
        d = self._scenario()
        rec = self._json(d / ROUND)
        pinned = rec["pinned_head"]
        self.assertEqual(pinned, self.head_at_refusal, self.err)
        self.assertEqual(len(self.round_publishes), 1, self.err)
        tip = self.fx._tip(BRANCH)

        # (1) one new commit on the PR branch, its parent the pinned head, pushed plainly.
        self.assertEqual(self._at_origin("rev-list", f"{pinned}..{tip}").split(), [tip])
        self.assertEqual(self._at_origin("rev-list", "--parents", "-n", "1", tip).split(),
                         [tip, pinned])
        window = self._window()
        self.assertEqual(len(self._pushes(window)), 1, window)
        for c in self._pushes(self.calls[self.refused_at:]):
            self.assertFalse([x for x in c if x.startswith(("--force", "-f"))], c)
        self.assertEqual([c for c in window if c[:3] in (["gh", "pr", "create"],
                                                         ["gh", "pr", "edit"],
                                                         ["gh", "pr", "ready"])], [])
        self.assertTrue(self._at_origin("log", "-1", "--format=%B", tip).startswith(ROUND_MSG))
        self.assertEqual((d / publish.COMMIT_MSG).read_text(encoding="utf-8"), ROUND_MSG)
        self.assertEqual((d / "iteration-v1" / publish.COMMIT_MSG).read_text(encoding="utf-8"),
                         FIRST_MSG)
        self.assertEqual(self._at_origin("rev-parse", f"{tip}^{{tree}}"),
                         self._tree(self.base, d / "iteration-v1" / "patch.diff",
                                    d / "patch.diff"))

        # (2) the records: the commit in both, the threads still open, the marker on it.
        pub = self._json(d / "publish.json")
        self.assertEqual((pub["mode"], pub["pr_url"], pub["branch"]), ("new-pr", PR, BRANCH))
        self.assertEqual(pub.get("round_commits"), [tip])
        self.assertTrue(merged.marked_for_this_patch(d, pub), pub)
        rec = self._json(d / ROUND)
        self.assertEqual(rec.get("pushed"), {"1": tip})
        self.assertEqual({t: e["status"] for t, e in rec["threads"].items()},
                         {"T1": "open", "T2": "open"})
        (d / RESPONSES).unlink()
        problems = remark_round.reply_problems(d)
        for tid in ("T1", "T2"):
            self.assertTrue([x for x in problems if f"thread {tid} " in x], problems)
        self.assertIn(tip, remark_round.line(d))

        # (3) the run stops non-zero naming the commit and the PR; Q is not built.
        self.assertNotEqual(self.rc, 0)
        stop = self._stop_line()
        self.assertIn(tip, stop)
        self.assertIn(PR, stop)
        self._q_not_built()
        self.assertEqual([c for c in self.calls if c[:3] == ["gh", "api", "graphql"]
                          and any("mutation" in x for x in c)], [])
        self.assertEqual([c for c in self.calls[self.refused_at:]
                          if c[:3] == ["gh", "pr", "merge"]], [])

        # (4) the operator's checkout is as the round's publish found it.
        self._checkout_untouched()

    # -- the legs -----------------------------------------------------------------------------

    def test_a_a_persons_thread_beside_the_bots_holds_the_pr_for_the_human(self):
        self._fresh(threads=(T1, T2, T3))
        d = self._scenario()
        line = self._held_for_the_human(d, ["T3 c.py:7 by alice"])
        self.assertNotIn("T1", line)
        self.assertNotIn(T3_BODY, (d / "brief.md").read_text(encoding="utf-8"))
        self.assertNotIn(T3_BODY, self.err)

    def test_b_any_thread_not_a_listed_bots_holds_on_its_own(self):
        for what, thread in {
                "a person": T3,
                "a deleted account": _node("T4", "c.py", 2, "gone", None),
                "an app the list does not name": _node("T5", "c.py", 3, "lint", "some-app[bot]"),
                "a person with a listed bot's login": _node("T6", "b.py", 4, "me", "codex-bot",
                                                            "User")}.items():
            with self.subTest(what):
                self._fresh(threads=(thread,))
                d = self._scenario()
                self._held_for_the_human(d, [f"{thread['id']} {thread['path']}"])

    def _load_bots(self, raw: str | None) -> tuple[tuple, list[str]]:
        """``[driver].review_bots = <raw>`` (unset for None) read by the real config loader:
        the value it reads, and its ``config:`` warnings about it."""
        root = self.fx.tmp / "cfg"
        root.mkdir()
        (root / "pdca.toml").write_text(
            "[driver]\n" + (f"review_bots = {raw}\n" if raw else ""), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            loaded = config.Config.load(root)
        return (tuple(getattr(loaded, "review_bots", ("unread",))),
                [x for x in err.getvalue().splitlines()
                 if x.startswith("config:") and "review_bots" in x])

    def test_c_a_listed_bot_matches_whatever_its_case_bot_suffix_or_padding(self):
        for raw, who in (('["Codex-Bot[bot]"]', "codex-bot"), ('["codex-bot"]', "Codex-Bot[bot]"),
                         ('[" Codex-Bot[bot] "]', "codex-bot")):
            with self.subTest(review_bots=raw, author=who):
                self._fresh(threads=(_node("T2", "b.py", 3, "b.py:3 keeps the default", who),))
                self.cfg.review_bots, warned = self._load_bots(raw)
                self.assertEqual(warned, [])
                self.decisions[P] = ["accept"] + [None] * 5     # stop at the round's sign-off
                d = self._scenario()
                self.assertTrue((d / ROUND).is_file(), self.err)
                self.assertEqual(list(self._json(d / ROUND)["threads"]), ["T2"])
                self.assertNotIn("held for the human", self.err)

    def test_d_no_usable_review_bot_list_opens_no_round(self):
        warns = {"unset": None, "empty": "[]", "a string": '"codex-bot"',
                 "a non-string entry": '["codex-bot", 3]',
                 "an entry that is only [bot]": '["codex-bot", "[bot]"]',
                 "a blank entry": '["codex-bot", "  "]',
                 "an entry with a space inside": '["codex-bot", "lint bot"]'}
        for what, raw in warns.items():
            with self.subTest(what):
                self._fresh()
                loaded, warned = self._load_bots(raw)
                self.assertEqual(len(warned), 0 if what in ("unset", "empty") else 1, warned)
                self.assertEqual(loaded, ())
                self.cfg.review_bots = loaded
                d = self._scenario()
                self._held_for_the_human(d, ["T1 a.py:12 by lint-bot", "T2 b.py:3 by codex-bot"])

    def test_e_a_branch_moved_or_deleted_stops_the_publish_and_records_nothing(self):
        self._fresh()
        self.dirt = "basic"
        self.before_round_publish = lambda d: self.fx._push_from_human(
            {"z.txt": "someone else\n"}, to=BRANCH, off=f"origin/{BRANCH}")
        d = self._scenario()
        moved = self.fx._tip(BRANCH)
        pinned = self._json(d / ROUND)["pinned_head"]
        self.assertNotEqual(moved, pinned)
        line = self._stopped_at()
        self.assertIn("the push", line)
        self.assertIn("[rejected]", line)
        self.assertIn(f"`pdca publish {P}`", line)
        self._nothing_recorded(d, moved)
        self._checkout_untouched()

        self._fresh()
        self.before_round_publish = lambda d: self.fx._delete(BRANCH)
        d = self._scenario()
        line = self._stopped_at()
        self.assertIn("the fetch", line)
        self.assertIn(f"git fetch origin {BRANCH}", line)
        self.assertFalse(self.fx._has(BRANCH))
        self.assertEqual(self._pushes(self._window()), [])
        self.assertNotIn("pushed", self._json(d / ROUND))
        self.assertNotIn("round_commits", self._json(d / "publish.json"))

        # A publish that raises: the stop line says so, and to read `pushed` before a retry.
        self._fresh()

        def boom(d: Path) -> None:
            raise RuntimeError("stub: the publisher leaf crashed")

        self.on_round_draft = boom
        d = self._scenario()
        stop = self._stop_line()
        self.assertIn("its round publish raised (RuntimeError", stop)
        self.assertIn(f"{ROUND}'s `pushed` before running `pdca publish {P}`", stop)
        self.assertEqual(self._pushes(self.calls[self.refused_at:]), [])
        self.assertNotIn("pushed", self._json(d / ROUND))
        self.assertEqual(self._leftover_worktrees(), [])

    def test_f_a_round_that_changed_nothing_records_no_change_and_pushes_nothing(self):
        self._fresh()
        self.round_edits, self.round_new = {}, {}
        self.responses = {"T1": {"addressed": False, "reply": "Kept: the loop stops at 20."},
                          "T2": {"addressed": False, "reply": "Kept: the new default stays."}}
        d = self._scenario()
        pinned = self._json(d / ROUND)["pinned_head"]
        self.assertEqual((d / "patch.diff").read_text(encoding="utf-8"), "")
        self.assertEqual(self._json(d / ROUND).get("pushed"), {"1": "no-change"}, self.err)
        self.assertEqual(self.fx._tip(BRANCH), pinned)
        self.assertEqual(self._pushes(self._window()), [])
        self.assertNotIn("round_commits", self._json(d / "publish.json"))
        self.assertIn("accepted with no code change", remark_round.line(d))
        self.assertIn("accepted with no code change", self._stop_line())
        rc, said, calls = self._cli("publish", P)
        self.assertEqual(rc, 0, said)
        self.assertEqual(self._pushes(calls), [])
        self.assertEqual(self.fx._tip(BRANCH), pinned)

    def _rejecting_hook(self, d: Path) -> None:
        hook = self.fx.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'rejected by the test hook' >&2\nexit 1\n",
                        encoding="utf-8")
        hook.chmod(0o755)

    def test_g_a_rejected_push_is_retried_by_publish_once_and_then_done(self):
        self._fresh()
        self.dirt = "basic"
        self.before_round_publish = self._rejecting_hook
        d = self._scenario()
        pinned = self._json(d / ROUND)["pinned_head"]
        line = self._stopped_at()
        self.assertIn("the push", line)
        self.assertIn("rejected by the test hook", line)
        self.assertIn(f"`pdca publish {P}`", line)
        self._nothing_recorded(d, pinned)
        self._checkout_untouched()

        (self.fx.origin / "hooks" / "pre-receive").unlink()
        rc, said, calls = self._cli("publish", P)
        self.assertEqual(rc, 0, said)
        self.assertEqual(len(self._pushes(calls)), 1, said)
        tip = self.fx._tip(BRANCH)
        self.assertEqual(self._at_origin("rev-list", "--parents", "-n", "1", tip).split(),
                         [tip, pinned])
        self.assertEqual(self._json(d / ROUND).get("pushed"), {"1": tip})
        self.assertEqual(self._json(d / "publish.json").get("round_commits"), [tip])
        self.assertTrue(self._at_origin("log", "-1", "--format=%B", tip).startswith(ROUND_MSG))
        rc, said, calls = self._cli("publish", P)
        self.assertEqual(rc, 0, said)
        self.assertEqual(self._pushes(calls), [])
        self.assertIn(tip, said)

        # A later run on the pushed, live round: refused at its merge before wave 0, with the
        # cap line read from the round's record; no hold, nothing asked of the host or pushed.
        self._run()
        refusal = [x for x in self.err.splitlines()
                   if x.startswith(f"!!! merge: issue_{P} was NOT merged")]
        self.assertEqual(len(refusal), 1, self.err)
        self.assertIn(tip, refusal[0])
        self.assertIn("held until the round ends", refusal[0])
        self._rounds_exhausted()
        self.assertEqual(self._pushes(self.calls[self.mark:]), [])
        self.assertEqual(self.fx._tip(BRANCH), tip)
        self._q_not_built()

        with self.subTest("a round accepted by the later run: the wave's stop says it too"):
            self._fresh()
            self.decisions[P] = ["accept"] + [None] * 5     # the round waits for its sign-off
            d = self._scenario()
            self.assertTrue(remark_round.live(d), self.err)
            self.assertNotEqual(state.state(d), state.COMPLETE)
            self.decisions[P] = ["accept"]
            self._run()
            self.assertEqual(state.state(d), state.COMPLETE, self.err)
            self._rounds_exhausted()
            self.assertIn("wave 0 did not merge; STOPPING", self.err)
            self.assertEqual(self._pushes(self.calls[self.mark:]), [])
            self.assertNotIn("pushed", self._json(d / ROUND))
            self._q_not_built()

    def _rounds_exhausted(self) -> None:
        """The run just made said once that P's rounds are used up, naming its PR and the
        threads its round still has open, with no hold line and no review read."""
        lines = [x for x in self.err.splitlines() if "remark rounds exhausted" in x]
        self.assertEqual(len(lines), 1, self.err)
        for part in (PR, "1 of [driver].remark_rounds = 1 used", "T1 a.py:12 by lint-bot",
                     "T2 b.py:3 by codex-bot"):
            self.assertIn(part, lines[0])
        self.assertNotIn("held for the human", self.err)
        self.assertEqual(self._asked_for_change_requests(self.calls[self.mark:]), [])

    def test_h_records_that_cannot_be_published_are_refused_before_any_push(self):
        def rewrite(name: str, change):
            def hook(d: Path) -> None:
                data = json.loads((d / name).read_text(encoding="utf-8"))
                change(data)
                (d / name).write_text(json.dumps(data), encoding="utf-8")
            return hook

        def write(name: str, text: str):
            return lambda d: (d / name).write_text(text, encoding="utf-8")

        t4 = {"id": "T4-round", "tier": "T4", "label": "round texts", "scope": "bundle",
              "gating": True, "cmd": f"{sys.executable} -c \"import os, sys; p = os.path.join("
                                     "os.environ['PDCA_BUNDLE'], 'commit-msg.txt'); sys.exit("
                                     "1 if os.path.exists(p) and 'remark round' in open(p)."
                                     "read() else 0)\""}
        records = "its records"
        refused = {
            "publish.json unreadable": (write("publish.json", "{not json"), records,
                                        "publish.json"),
            "remark-round.json unreadable": (write(ROUND, "[1, 2"), records, ROUND),
            "pushed is a list": (rewrite(ROUND, lambda r: r.update(pushed=[])), records,
                                 "pushed"),
            'pushed["1"] is ""': (rewrite(ROUND, lambda r: r.update(pushed={"1": ""})),
                                  records, "pushed"),
            'pushed["1"] is 7': (rewrite(ROUND, lambda r: r.update(pushed={"1": 7})), records,
                                 "pushed"),
            "rounds is 0": (rewrite(ROUND, lambda r: r.update(rounds=0)), records, "rounds"),
            "another pr_url": (rewrite(ROUND, lambda r: r.update(pr_url=PR + "0")), records,
                               "pr_url"),
            "another branch": (rewrite(ROUND, lambda r: r.update(branch="fix/other")),
                               records, "branch"),
            "no contribution": (rewrite(ROUND, lambda r: r.update(contribution=[])), records,
                                "contribution"),
            'contribution ["."]': (rewrite(ROUND, lambda r: r.update(contribution=["."])),
                                   records, "contribution"),
            "a gitlink change": (write("patch.diff", GITLINK), "the submodule check",
                                 "submodule"),
            "a T4 row failing the round's texts": (None, "the T4 gate", "T4")}
        for what, (hook, step, named) in refused.items():
            with self.subTest(what):
                self._fresh()
                if hook is None:
                    self.cfg.gates_checks = [t4]
                self.before_round_publish = hook
                d = self._scenario()
                self.assertEqual(len(self.round_publishes), 1, self.err)
                self.assertNotEqual(self.round_publishes[0]["rc"], 0)
                line = self._stopped_at()
                self.assertIn(f"stopped at {step}: ", line)
                self.assertIn(named, line)
                self.assertEqual(self._pushes(self._window()), [])
                self._nothing_recorded(d, self.head_at_refusal)
                drafted = [x for x in self.drafts if x[1]]
                if hook is None:        # T4 judges the round's own texts, drafted just before
                    self.assertEqual(len(drafted), 1, self.drafts)
                else:                   # stopped before the texts: the first publish's stay
                    self.assertEqual(drafted, [])
                    self.assertEqual((d / publish.COMMIT_MSG).read_text(encoding="utf-8"),
                                     FIRST_MSG)
                    self.assertFalse((d / "iteration-v1" / publish.COMMIT_MSG).exists())
                if what.startswith("pushed"):
                    self.assertIn("entry cannot be read", remark_round.line(d))

        with self.subTest("publish.json with no merge marker"):
            self._fresh()
            self.before_round_publish = rewrite(
                "publish.json", lambda r: [r.pop(k, None) for k in ("merge_eligible",
                                                                    merged.MARKED_PATCH)])
            d = self._scenario()
            pub = self._json(d / "publish.json")
            self.assertEqual(pub.get("round_commits"), [self.fx._tip(BRANCH)], self.err)
            self.assertNotIn(merged.MARKED_PATCH, pub)
            self.assertNotIn("merge_eligible", pub)

    def test_i_a_dry_run_or_a_stub_publisher_prints_the_plan_and_changes_nothing(self):
        self._fresh()
        self.before_round_publish = self._rejecting_hook
        d = self._scenario()
        self.assertTrue((self.fx.origin / "hooks" / "pre-receive").exists())
        kept = {name: (d / name).read_bytes() for name in ("publish.json", ROUND)}
        tip = self.fx._tip(BRANCH)
        rc, said, calls = self._cli("publish", P, "--dry-run")
        self.assertEqual(rc, 0, said)
        self.assertIn(f"publish --dry-run — issue_{P} → remark round 1", said)
        self.assertIn(f"HEAD:refs/heads/{BRANCH}", said)
        self.assertEqual(self._pushes(calls), [])
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
        rc, said, calls = self._cli("signoff", P, "--accept", "--by", "T")
        self.assertEqual(rc, 0, said)
        self.assertIn(f"publish --dry-run — issue_{P} → remark round 1", said)
        self.assertEqual(self._pushes(calls), [])
        self.assertEqual(self.fx._tip(BRANCH), tip)
        self.assertEqual({name: (d / name).read_bytes() for name in kept}, kept)

    def test_k_a_change_request_from_anyone_but_a_bot_holds_and_a_bots_does_not(self):
        idless = _node(None, "c.py", 9, "no id", "lint-bot")
        held = {
            "a person's change request": (dict(opinionated=[_asks("alice", "User")]),
                                          (T1, T2), ["changes requested by alice"]),
            "a listed login that is a User": (dict(opinionated=[_asks("codex-bot", "User")]),
                                              (T1, T2), ["changes requested by codex-bot"]),
            "reviewDecision with none standing": (dict(decision="CHANGES_REQUESTED"),
                                                  (T1, T2), ["changes requested by ?"]),
            "the review read fails": (dict(opinionated_fails=True), (T1, T2),
                                      ["changes requested by ?"]),
            "the review read has a next page": (dict(opinionated_more=True), (T1, T2),
                                                ["changes requested by ?"]),
            # Reviews that cannot be read are unknown, never "none stands".
            "the review read's nodes are not a list": (dict(opinionated={}), (T1, T2),
                                                       ["changes requested by ?"]),
            "a review with no state": (dict(opinionated=[
                {"author": {"__typename": "User", "login": "alice"}}]), (T1, T2),
                ["changes requested by ?"]),
            "a review with no author": (dict(opinionated=[{"state": "APPROVED"}]), (T1, T2),
                                        ["changes requested by ?"]),
            "a thread with no id beside the bots'": ({}, (T1, T2, idless), ["? c.py:9"]),
            "only threads with no id": ({}, (idless,), ["? c.py:9 by lint-bot"]),
            "threads unknown beside a bot's change request": (
                dict(threads_fail=True, opinionated=[_asks("codex-bot", "Bot")],
                     reviews=[{"state": "CHANGES_REQUESTED", "author": {"login": "codex-bot"}}]),
                (T1, T2), ["threads could not be read: ?"])}
        for what, (host, threads, names) in held.items():
            with self.subTest(what):
                self._fresh(threads=threads)
                for k, v in host.items():
                    setattr(self, k, v)
                d = self._scenario()
                self._held_for_the_human(d, names)

        with self.subTest("a listed bot's change request"):
            self._fresh()
            self.opinionated = [_asks("codex-bot", "Bot")]
            self.decisions[P] = ["accept"] + [None] * 5
            d = self._scenario()
            self.assertTrue((d / ROUND).is_file(), self.err)
            self.assertNotIn("held for the human", self.err)

        with self.subTest("rounds off, a person's thread open"):
            self._fresh(threads=(T1, T2, T3))
            self.cfg.remark_rounds = 0
            d = self._scenario()
            self.assertFalse((d / ROUND).exists())
            self.assertIn("did not merge; STOPPING", self.err)
            self.assertNotIn("held for the human", self.err)
            self.assertEqual(self._asked_for_change_requests(self.calls), [])

    def test_l_an_open_pr_that_is_not_the_rounds_stops_before_the_texts(self):
        for what, found in (("no open PR", None), ("another PR", PR + "1")):
            with self.subTest(what):
                self._fresh()
                self.open_pr = found
                d = self._scenario()
                self.assertIn("the open-PR lookup", self._stopped_at())
                self.assertEqual([x for x in self.drafts if x[1]], [])
                self.assertEqual((d / publish.COMMIT_MSG).read_text(encoding="utf-8"),
                                 FIRST_MSG)
                self.assertFalse((d / "iteration-v1" / publish.COMMIT_MSG).exists())
                self._nothing_recorded(d, self.head_at_refusal)

    def _hook(self, body: str, name: str = "pre-commit"):
        def install(d: Path) -> None:
            hook = self.fx.primary / ".git" / "hooks" / name
            hook.write_text("#!/bin/sh\n" + body.replace("{patch}", str(d / "patch.diff")),
                            encoding="utf-8")
            hook.chmod(0o755)
        return install

    @staticmethod
    def _append_to_patch(d: Path) -> None:
        with (d / "patch.diff").open("a", encoding="utf-8") as fh:
            fh.write("# edited while the texts were drafted\n")

    def test_m_a_commit_that_is_not_the_snapshot_is_never_pushed(self):
        rewrite = "echo '# a hook was here' >> a.py && git add a.py\n"
        tree = "the tree check"
        for what, hook, draft, step, named in (
                ("a hook rewrites and stages a.py", self._hook(rewrite), None, tree,
                 "the commit's tree is not pinned_head + patch.diff"),
                ("a hook rewrites a.py and patch.diff to match",
                 self._hook(rewrite + "git diff --cached HEAD > '{patch}'\n"), None, tree,
                 "the commit's tree is not pinned_head + patch.diff"),
                ("a hook rejects the commit", self._hook("echo 'a hook says no' >&2; exit 1\n"),
                 None, "the commit", "a hook says no"),
                ("a hook adds a commit of its own",
                 self._hook('[ -n "$HOOKED" ] || HOOKED=1 git commit -q --allow-empty -m "a '
                            "hook's own\"\n", "post-commit"), None, tree, "pinned_head"),
                ("the publisher leaf edits patch.diff", None, self._append_to_patch, tree,
                 "patch.diff in the bundle")):
            with self.subTest(what):
                self._fresh()
                self.dirt = "basic"
                self.before_round_publish = hook
                self.on_round_draft = draft
                d = self._scenario()
                line = self._stopped_at()
                self.assertIn(f"stopped at {step}: ", line)
                self.assertIn(named, line)
                self.assertEqual(self._pushes(self._window()), [])
                self._nothing_recorded(d, self.head_at_refusal)
                self._checkout_untouched()

    def test_n_a_change_request_behind_a_later_comment_still_holds(self):
        self._fresh()
        self.reviews = [{"state": "COMMENTED", "author": {"login": "alice"}}]
        self.opinionated = [_asks("alice", "User")]
        d = self._scenario()
        self._held_for_the_human(d, ["changes requested by alice"])

    def test_o_the_operators_checkout_is_never_touched(self):
        moved = lambda d: self.fx._push_from_human(                         # noqa: E731
            {"z.txt": "someone else\n"}, to=BRANCH, off=f"origin/{BRANCH}")
        # git keeps a worktree whose post-checkout hook fails, and exits non-zero.
        checkout_fails = self._hook("echo 'post-checkout says no' >&2; exit 3\n",
                                    "post-checkout")
        for what, before, step in (
                ("a pushed round", None, None), ("a branch moved (e)", moved, "the push"),
                ("a push the host rejects (g)", self._rejecting_hook, "the push"),
                ("a hook that rewrites a.py (m)",
                 self._hook("echo x >> a.py && git add a.py\n"), "the tree check"),
                ("a hook that fails the commit (m)", self._hook("exit 1\n"), "the commit"),
                ("a post-checkout hook that fails", checkout_fails, "the throwaway worktree"),
                ("a post-checkout hook that fails, host CI declared", checkout_fails,
                 "the host CI gate")):
            with self.subTest(what):
                self._fresh()
                self.dirt = "full"
                if "host CI" in what:
                    self.cfg.host_ci_checks = config._normalize_host_ci(
                        [f"{shlex.quote(sys.executable)} -c pass"])
                self.before_round_publish = before
                d = self._scenario()
                self.assertEqual(self.round_publishes[0]["rc"] == 0, before is None, self.err)
                self._checkout_untouched()
                if step:
                    line = self._stopped_at()
                    self.assertIn(f"stopped at {step}: ", line)
                    if step == "the throwaway worktree":    # the hook's own words, not git's
                        self.assertIn("post-checkout says no", line)
                    self._nothing_recorded(d, self.fx._tip(BRANCH) if before is moved
                                           else self._json(d / ROUND)["pinned_head"])
                status = self.round_publishes[0]["before"]["status"]
                for kept in ("build.log", "d.py", "notes.txt", "base.txt", "c.py"):
                    self.assertIn(kept, status)
                self.assertIn(f"refs/heads/{BRANCH}",
                              self.round_publishes[0]["before"]["HEAD"])

    def test_p_host_ci_certifies_the_pinned_head_plus_the_patch(self):
        stamped = "a post-checkout hook edits a file outside the patch"
        latin1 = "a patch with a Latin-1 line"
        for case in ("branch moved", "branch not moved", "patch.diff edited and put back",
                     stamped, latin1):
            with self.subTest(case):
                self._fresh()
                moved = case == "branch moved"
                script, log = self.fx.tmp / "host_ci.py", self.fx.tmp / "host-ci.jsonl"
                script.write_text(HOST_CI, encoding="utf-8")
                rows = [" ".join(shlex.quote(str(x))
                                 for x in (sys.executable, script, log, self.fx.tmp))]
                if case == stamped:
                    # A hook that succeeds: every worktree checked out from here gets a c.py
                    # that is not the PR head's, which the patch does not touch.
                    stamp = self._hook("echo '# stamped at checkout' >> c.py\n", "post-checkout")

                    def before(d: Path) -> None:
                        self.ci_runs = (len(log.read_text(encoding="utf-8").splitlines())
                                        if log.exists() else 0)
                        stamp(d)
                    self.before_round_publish = before
                if case == latin1:
                    self.before_round_publish = lambda d: (d / "patch.diff").write_bytes(
                        (d / "patch.diff").read_bytes() + LATIN1_FILE)
                if case == "patch.diff edited and put back":
                    # The publisher leaf makes patch.diff add a file while it drafts the texts;
                    # a second CI command puts the file back as it was once CI has run, so the
                    # check before the push finds it unchanged.
                    kept = self.fx.tmp / "patch.kept"
                    patch = self.cfg.bundle(P) / "patch.diff"
                    self.on_round_draft = lambda d: (
                        kept.write_bytes(patch.read_bytes()),
                        patch.write_text(patch.read_text(encoding="utf-8") + EXTRA_FILE,
                                         encoding="utf-8"))
                    rows.append(f"{shlex.quote(sys.executable)} -c " + shlex.quote(
                        f"import os, shutil; os.path.exists({str(kept)!r}) and "
                        f"shutil.copyfile({str(kept)!r}, {str(patch)!r})"))
                self.cfg.host_ci_checks = config._normalize_host_ci(rows)
                if moved:
                    self.before_round_publish = lambda d: self.fx._push_from_human(
                        {"z.txt": "someone else\n"}, to=BRANCH, off=f"origin/{BRANCH}")
                d = self._scenario()
                pinned = self._json(d / ROUND)["pinned_head"]
                runs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
                if case == stamped:
                    # Refused before any command ran on the stamped tree; nothing pushed.
                    self.assertEqual(len(runs), self.ci_runs, runs)
                    self.assertIn("stopped at the host CI gate: ", self._stopped_at())
                    self.assertIn("a checkout hook changed it", self.err)
                    self._nothing_recorded(d, pinned)
                    self._checkout_untouched()
                    continue
                if case == latin1:
                    # Read as the bytes it is: no crash, and no worktree left behind.
                    self.assertNotIn("round publish raised", self.err)
                    self.assertEqual(self._leftover_worktrees(), [])
                    self._checkout_untouched()
                    self.assertIn(b"caf\xe9", (d / "patch.diff").read_bytes())
                at_publish = [r["tree"] for r in runs if worktree._OVF_SUFFIX in r["wt"]]
                self.assertTrue(at_publish, runs)
                self.assertEqual(at_publish[-1], self._tree(pinned, d / "patch.diff"))
                if moved:
                    self.assertIn("the push", self._stopped_at())
                    self._nothing_recorded(d, self.fx._tip(BRANCH))
                else:
                    tip = self.fx._tip(BRANCH)
                    self.assertEqual(self._json(d / ROUND).get("pushed"), {"1": tip}, self.err)
                    self.assertEqual(at_publish[-1],
                                     self._at_origin("rev-parse", f"{tip}^{{tree}}"))

    def test_record_write_failing_after_the_push_lists_every_hand_repair(self):
        # Pushed, then a record write fails: the commit is on the PR, so the line says not to
        # re-run publish and lists what to record by hand, with the values: both records and
        # the merge marker (the PR is marked here), less what was written already.
        for name in (ROUND, "publish.json"):
            with self.subTest(f"writing {name} fails"):
                self._fresh()
                # A directory where the write's temp file goes: the write fails, nothing else.
                self.before_round_publish = (
                    lambda d, name=name: (d / f".{name}.partial").mkdir())
                d = self._scenario()
                pinned = self._json(d / ROUND)["pinned_head"]
                tip = self.fx._tip(BRANCH)
                self.assertEqual(self._at_origin("rev-list", "--parents", "-n", "1",
                                                 tip).split(), [tip, pinned], self.err)
                self.assertNotEqual(self.round_publishes[0]["rc"], 0)
                lines = [x for x in self.err.splitlines() if f"but writing {name} failed" in x]
                self.assertEqual(len(lines), 1, self.err)
                marker = hashlib.sha256((d / "patch.diff").read_bytes()).hexdigest()
                for part in (tip, PR, f"do NOT re-run `pdca publish {P}`",
                             f'append "{tip}" to `round_commits`',
                             f'set `{merged.MARKED_PATCH}` to "{marker}"'):
                    self.assertIn(part, lines[0])
                self.assertEqual(f'set `pushed["1"]` to "{tip}"' in lines[0], name == ROUND)
                self.assertEqual(self._json(d / ROUND).get("pushed"),
                                 None if name == ROUND else {"1": tip})
                self.assertNotIn("round_commits", self._json(d / "publish.json"))
                stop = self._stop_line()
                self.assertIn("its round publish did not complete", stop)
                self.assertNotIn("pushes it", stop)


if __name__ == "__main__":
    unittest.main()
