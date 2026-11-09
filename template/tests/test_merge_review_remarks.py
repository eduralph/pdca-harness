"""Merge mode names a block by review remarks (issue #838).

A non-final wave's PR can read green on its check rollup and still be refused by the host
because review threads are unresolved or a review requested changes. `gh pr merge` then
fails, and before #838 `_merge_one` printed only its generic refusal ("a conflict, no merge
rights …") and wrote nothing to the bundle. Now, when the merge fails, it reads the PR's
`mergeStateStatus` (`gh pr view`, polled while `UNKNOWN` on what the update and rollup waits
left of the one `merge_wait_secs` budget) and, for `BLOCKED`, its unresolved review threads
(`gh api graphql` on the host in the PR's URL, every page of them) and reviews: it names the
block with each thread's path:line, writes `merge-remarks.json` into the bundle, and returns
the PR to draft. A `BLOCKED` with no thread and no CHANGES_REQUESTED review keeps the generic
refusal and writes no file. Threads that could not be read beside such a review are `null` in
the file: unknown, never none. The file appears in one step, so a write that dies part-way
leaves none rather than a torn one. It describes the attempt that wrote it: the next attempt
to merge the bundle removes it first, and an iterate archives it with that attempt.

`BLOCKED` does not say which of the host's rules blocks the merge and the driver does not read
those rules, so a PR short of an approval that also has an open thread is still named as blocked
by the thread. The stop therefore shows the PR's `reviewDecision`, and the file records that and
the failed merge's own error, for a reader to check the named block against.

`gh` and `git` are stubbed in front of `subprocess.run`, as `test_merge.py` does — no
network. Run from template/:
    PYTHONPATH=src python -m unittest tests.test_merge_review_remarks
"""

from __future__ import annotations

import errno
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import driver, merge, merged, state
from pdca_harness.config import Config, LeafConfig

PR = "https://github.com/org/repo/pull/7"
HEAD = "1" * 40
STALE = "0" * 40
BASE_TIP = "b" * 40
REMARKS = "merge-remarks.json"
MERGE_ERROR = ("GraphQL: Pull request is not mergeable: the base branch policy prohibits the "
               "merge. (mergePullRequest)")


def _cfg(root: Path, **overrides: object) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={"org/repo": str(root / "repo")},
        **overrides)


def _res(code: int = 0, out: str = "", err: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def _thread(tid: str, path: str, line: int | None, body: str, who: str, *,
            original: int | None = None, resolved: bool = False,
            outdated: bool | None = None) -> dict:
    return {"id": tid, "isResolved": resolved,
            "isOutdated": line is None if outdated is None else outdated, "path": path,
            "line": line, "originalLine": original if original is not None else line,
            "comments": {"nodes": [{"body": body, "url": f"{PR}#{tid}",
                                    "author": {"login": who}}]}}


def _page(nodes: list, more: bool, end: str | None) -> str:
    """One `reviewThreads` page as `gh api graphql` prints it."""
    return json.dumps({"data": {"resource": {"reviewThreads": {
        "pageInfo": {"hasNextPage": more, "endCursor": end}, "nodes": nodes}}}})


class _Gh:
    """`subprocess.run` stub: the PR is on `main`, its rollup reads `checks` (green by
    default); `gh pr merge` fails as a host does for a blocked PR unless `merge_ok`, printing
    `merge_err` on stderr and `merge_out` on stdout. The PR is up to date with `main` unless
    `behind`: then its head is `STALE` until `gh pr update-branch`, whose update shows from
    the second head read after it. `states` is the sequence of `mergeStateStatus` reads while
    the PR is marked ready (the last repeats; a draft reads `DRAFT`, as on the host),
    `decision` the `reviewDecision`, `reviews` the `latestReviews`; `state_read` replaces every
    such read with that one result. `ready` is whether it is marked ready now. The GraphQL
    thread nodes are `threads`, or `pages` of them served the way the host pages
    `reviewThreads`: the page after cursor `c<k>` is `pages[k]`. `stuck` makes every page
    claim a next page under the same cursor, `graphql_ok` is how many requests are answered
    before `gh api graphql` fails, and `answer` replaces what it prints (an exception is
    raised instead). `gh api` asks `gh`'s default host unless told which, where a PR on another
    host does not resolve, so a GraphQL request that does not name `host` with `--hostname`
    fails the test on the spot: an AssertionError, which no handler in production swallows.
    Every command is logged in `calls`, the cursor of each GraphQL request in `cursors`."""

    def __init__(self, *, states=("BLOCKED",), threads=(), pages=None, reviews=(),
                 decision: object = "", checks: str = "pass", merge_ok: bool = False,
                 merge_err: str = MERGE_ERROR, merge_out: str = "",
                 state_read: SimpleNamespace | None = None,
                 stuck: bool = False, behind: bool = False, graphql_ok: int | None = None,
                 answer: str | Exception | None = None, host: str = "github.com") -> None:
        self.states = list(states)
        self.pages = [list(p) for p in pages] if pages is not None else [list(threads)]
        self.reviews = list(reviews)
        self.decision = decision
        self.checks = checks
        self.merge_ok = merge_ok
        self.merge_err = merge_err
        self.merge_out = merge_out
        self.state_read = state_read
        self.stuck = stuck
        self.head = STALE if behind else HEAD
        self.updating = False
        self.ready = False
        self.graphql_ok = graphql_ok
        self.answer = answer
        self.host = host
        self.calls: list[list[str]] = []
        self.cursors: list[str | None] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:1] == ["git"]:
            if "rev-parse" in cmd:
                return _res(out=BASE_TIP + "\n")
            if "merge-base" in cmd and cmd[-1] == STALE:
                return _res(1)                  # that head lacks the base's tip: behind
            return _res()                       # fetch / cat-file / merge-base: up to date
        if cmd[:3] == ["gh", "api", "graphql"]:
            asked = cmd[cmd.index("--hostname") + 1] if "--hostname" in cmd else None
            if asked != self.host:
                raise AssertionError(f"gh api graphql asked --hostname {asked}, not {self.host}")
            cursor = next((a.split("=", 1)[1] for a in cmd if a.startswith("cursor=")), None)
            self.cursors.append(cursor)
            if self.graphql_ok is not None and len(self.cursors) > self.graphql_ok:
                return _res(1, err="HTTP 502: Bad Gateway")
            if isinstance(self.answer, Exception):
                raise self.answer
            if self.answer is not None:
                return _res(out=self.answer)
            if self.stuck:
                if len(self.cursors) > 10:      # never hang the suite on a missing guard
                    return _res(1, err="stub: too many pages")
                return _res(out=_page(self.pages[0], True, "c1"))
            k = 0 if cursor is None else int(cursor[1:])
            return _res(out=_page(self.pages[k], k + 1 < len(self.pages), f"c{k + 1}"))
        verb = cmd[2]
        if verb == "view":
            fields = cmd[cmd.index("--json") + 1]
            if "mergeStateStatus" in fields:
                if self.state_read is not None:
                    return self.state_read
                st = "DRAFT"
                if self.ready:
                    st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                return _res(out=json.dumps({"mergeStateStatus": st,
                                            "reviewDecision": self.decision,
                                            "latestReviews": self.reviews}))
            head = self.head
            if self.updating:                   # the update lands after this read
                self.head, self.updating = HEAD, False
            return _res(out=json.dumps({"headRefOid": head, "baseRefName": "main",
                                        "isCrossRepository": False}))
        if verb == "checks":
            return _res(0 if self.checks == "pass" else 1,
                        out=json.dumps([{"name": "ci", "bucket": self.checks}]))
        if verb == "merge":
            if self.merge_ok:
                return _res()
            return _res(1, out=self.merge_out, err=self.merge_err)
        if verb == "update-branch":
            self.updating = True
        if verb == "ready":
            self.ready = "--undo" not in cmd
        return _res()


class MergeReviewRemarks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.sleeps: list[int] = []
        sleeper = mock.patch.object(merge, "_sleep", side_effect=self.sleeps.append)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, cfg: Config, iid: str = "838", *, pr: str = PR) -> Path:
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Repo + branch target:** org/repo @ main\n",
                                    encoding="utf-8")
        (d / "patch.diff").write_text("diff\n", encoding="utf-8")
        (d / "publish.json").write_text(json.dumps({"pr_url": pr, "repo": "org/repo",
                                                    "base": "main"}), encoding="utf-8")
        return d

    def _run(self, gh: _Gh, d: Path | None = None, *, already_merged: bool = False,
             dry_run: bool = False, **cfg_over: object) -> tuple[int, str, Path]:
        cfg = _cfg(self.tmp, **cfg_over)
        d = d or self._bundle(cfg)
        with mock.patch.object(merge.subprocess, "run", side_effect=gh), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merged, "is_merged", return_value=already_merged), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(cfg, [d], dry_run=dry_run, method="merge")
        return rc, err.getvalue(), d

    def _assert_undone(self, gh: _Gh, pr: str = PR) -> None:
        self.assertIn(["gh", "pr", "ready", pr, "--undo"], gh.calls)
        self.assertFalse(gh.ready)                          # it is not left marked ready

    @staticmethod
    def _state_reads(gh: _Gh) -> int:
        return sum(c[:3] == ["gh", "pr", "view"] and "mergeStateStatus" in c[-1]
                   for c in gh.calls)

    def _assert_generic_refusal(self, err: str, d: Path) -> None:
        self.assertIn("a conflict, no merge rights", err)
        self.assertNotIn("unresolved review thread", err)
        self.assertNotIn("CHANGES_REQUESTED", err)
        self.assertNotIn("reviewDecision", err)
        self.assertFalse((d / REMARKS).exists())

    def _remarks(self, d: Path) -> dict:
        self.assertTrue((d / REMARKS).is_file(), f"no {REMARKS} in the bundle")
        return json.loads((d / REMARKS).read_text(encoding="utf-8"))

    def _two_threads(self) -> list[dict]:
        return [_thread("T1", "src/a.py", 12, "Use a context manager here.", "alice"),
                _thread("T2", "src/b.py", 40, "Off by one?", "copilot"),
                _thread("T3", "src/c.py", 3, "Done already.", "bob", resolved=True)]

    def test_blocked_by_threads_is_named_and_written(self) -> None:
        gh = _Gh(threads=self._two_threads())
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("blocked by 2 unresolved review thread(s)", err)
        self.assertIn("src/a.py:12", err)
        self.assertIn("src/b.py:40", err)
        self.assertNotIn("src/c.py", err)                  # a resolved thread is no remark
        self.assertNotIn("a conflict, no merge rights", err)
        f = d / REMARKS
        self.assertTrue(f.is_file())
        # The file and nothing else: no temp left beside it.
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["brief.md", REMARKS, "patch.diff", "publish.json"])
        threads = json.loads(f.read_text(encoding="utf-8"))["threads"]
        self.assertEqual(len(threads), 2)
        for t in threads:
            for key in ("id", "path", "line", "body", "author", "url"):
                self.assertIn(key, t)
        self.assertEqual(threads[0], {**threads[0], "id": "T1", "path": "src/a.py",
                                      "line": 12, "body": "Use a context manager here.",
                                      "author": "alice", "url": f"{PR}#T1"})
        self._assert_undone(gh)

    def test_threads_past_the_first_page_are_read(self) -> None:
        # The host serves `reviewThreads` at most 100 a page. A PR whose first 100 threads
        # are resolved is still blocked by the unresolved ones on the pages after them.
        done = [_thread(f"R{i}", "src/done.py", i + 1, "ok", "bob", resolved=True)
                for i in range(100)]
        gh = _Gh(pages=[done,
                        [_thread("T101", "src/late.py", 9, "Still open.", "alice"),
                         *done[:99]],
                        [_thread("T201", "src/later.py", 4, "And this.", "copilot")]])
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertEqual(gh.cursors, [None, "c1", "c2"])    # each page after the last one
        self.assertIn("blocked by 2 unresolved review thread(s)", err)
        self.assertIn("src/late.py:9", err)
        self.assertIn("src/later.py:4", err)
        self.assertNotIn("a conflict, no merge rights", err)
        threads = json.loads((d / REMARKS).read_text(encoding="utf-8"))["threads"]
        self.assertEqual([t["id"] for t in threads], ["T101", "T201"])
        self._assert_undone(gh)

    def test_a_page_cursor_that_repeats_ends_the_read(self) -> None:
        # A host that claims another page under a cursor it already gave cannot be read to
        # the end: the read stops there, and the refusal stays the generic one.
        gh = _Gh(pages=[[_thread("T1", "src/a.py", 12, "Hm.", "alice")]], stuck=True)
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertEqual(gh.cursors, [None, "c1"])
        self.assertIn("a conflict, no merge rights", err)
        self.assertNotIn("unresolved review thread", err)
        self.assertFalse((d / REMARKS).exists())
        self._assert_undone(gh)

    def test_threads_are_read_on_the_host_in_the_pr_url(self) -> None:
        # `gh pr view <url>` and `gh pr merge <url>` follow the URL to its host, and the threads
        # must come from the same one, on every page. The host is what `gh` itself takes from
        # such a URL: lowercase, with no port.
        urls = {"an Enterprise host": "https://ghe.example.com/org/repo/pull/7",
                "with a port": "https://ghe.example.com:8443/org/repo/pull/7",
                "in capitals": "https://GHE.Example.COM/org/repo/pull/7"}
        for i, (name, url) in enumerate(urls.items()):
            with self.subTest(name):
                gh = _Gh(host="ghe.example.com",
                         pages=[[_thread("T1", "src/a.py", 12, "Hm.", "alice")],
                                [_thread("T2", "src/b.py", 40, "Hm.", "alice")]])
                d = self._bundle(_cfg(self.tmp), f"838{i}", pr=url)
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertEqual(gh.cursors, [None, "c1"])
                self.assertIn("blocked by 2 unresolved review thread(s)", err)
                self.assertIn("src/a.py:12", err)
                self.assertIn("src/b.py:40", err)
                self.assertEqual(self._remarks(d)["pr_url"], url)
                self._assert_undone(gh, url)

    def test_a_pr_url_with_no_host_to_ask_is_not_read(self) -> None:
        # There is no host to send the query to, so the threads are unknown, as for any other
        # read that fails. This runs while the PR is marked ready, so a URL the parser rejects
        # must not raise either.
        urls = {"a shorthand": "org/repo#7", "a bare number": "7",
                "no authority": "https:///org/repo/pull/7",
                "one the parser rejects": "https://[ghe.example.com/org/repo/pull/7"}
        for i, (name, url) in enumerate(urls.items()):
            with self.subTest(name):
                gh = _Gh(threads=self._two_threads())
                d = self._bundle(_cfg(self.tmp), f"838{i}", pr=url)
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertEqual(gh.cursors, [])
                self.assertIn("review threads could not be read", err)
                self._assert_generic_refusal(err, d)
                self._assert_undone(gh, url)

    def test_a_later_attempt_removes_the_file_it_no_longer_describes(self) -> None:
        # The file names the remarks the attempt that wrote it stopped on. Once they are
        # resolved, the next attempt to merge the bundle removes it, whatever that attempt
        # then ends on, so nothing acts on remarks that no longer block anything.
        later = {
            "merges": (dict(gh=_Gh(states=("CLEAN",), merge_ok=True)), 0),
            "generic refusal": (dict(gh=_Gh(threads=[_thread("T1", "src/a.py", 12, "ok",
                                                              "alice", resolved=True)],
                                            decision="REVIEW_REQUIRED")), 1),
            "refused before the merge": (dict(gh=_Gh(checks="fail")), 1),
            "already merged": (dict(gh=_Gh(), already_merged=True), 0),
        }
        for i, (name, (run, want)) in enumerate(later.items()):
            with self.subTest(name):
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, _, _ = self._run(_Gh(threads=self._two_threads()), d)
                self.assertNotEqual(rc, 0)
                self.assertTrue((d / REMARKS).is_file())    # the first attempt wrote it
                rc, err, _ = self._run(d=d, **run)
                self.assertEqual(rc, want)
                self.assertNotIn("unresolved review thread", err)
                self.assertFalse((d / REMARKS).exists())

    def test_a_refusal_on_the_publish_record_removes_the_file_too(self) -> None:
        # These stops come before any `gh` call and have nothing to do with the remarks an
        # earlier attempt named, so the file must not sit beside them either.
        records = {"no recorded PR": {"repo": "org/repo", "base": "main"},
                   "a stacked record": {"pr_url": PR, "repo": "org/repo", "base": "main",
                                        "mode": "stacked"}}
        for i, (name, record) in enumerate(records.items()):
            with self.subTest(name):
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, _, _ = self._run(_Gh(threads=self._two_threads()), d)
                self.assertNotEqual(rc, 0)
                self.assertTrue((d / REMARKS).is_file())    # the first attempt wrote it
                (d / "publish.json").write_text(json.dumps(record), encoding="utf-8")
                gh = _Gh()
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertEqual(gh.calls, [])
                self.assertNotIn("unresolved review thread", err)
                self.assertFalse((d / REMARKS).exists())

    def test_an_iterate_archives_the_file_with_its_attempt(self) -> None:
        # The file describes the attempt whose PR stopped: a rebuild must not sit beside remarks
        # its new patch may already answer, and the archive keeps them with that attempt.
        rc, _, d = self._run(_Gh(threads=self._two_threads()))
        self.assertNotEqual(rc, 0)
        before = self._remarks(d)
        driver._archive_iteration(d, 1, include_brief=False)
        self.assertFalse((d / REMARKS).exists())
        self.assertEqual(self._remarks(d / "iteration-v1"), before)

    def test_a_dry_run_leaves_the_file_alone(self) -> None:
        # A dry run attempts nothing, so it has nothing to say about the remarks.
        rc, _, d = self._run(_Gh(threads=self._two_threads()))
        self.assertNotEqual(rc, 0)
        self.assertTrue((d / REMARKS).is_file())            # the first attempt wrote it
        before = (d / REMARKS).read_text(encoding="utf-8")
        gh = _Gh(merge_ok=True)
        rc, _, _ = self._run(gh, d, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(gh.calls, [])
        self.assertEqual((d / REMARKS).read_text(encoding="utf-8"), before)

    def test_required_mode_detects_it_at_the_failed_merge(self) -> None:
        # Leg (i): under "required" the rollup gate is skipped, so the block only shows
        # as the failed `gh pr merge` — detection runs there too.
        gh = _Gh(threads=self._two_threads())
        rc, err, d = self._run(gh, merge_requires="required")
        self.assertNotEqual(rc, 0)
        self.assertFalse(any(c[:3] == ["gh", "pr", "checks"] for c in gh.calls))
        self.assertIn("blocked by 2 unresolved review thread(s)", err)
        self.assertIn("src/a.py:12", err)
        self.assertIn("src/b.py:40", err)
        self.assertEqual(len(json.loads((d / REMARKS).read_text())["threads"]), 2)
        self._assert_undone(gh)

    def test_review_required_with_an_open_thread_shows_the_real_signals(self) -> None:
        # `BLOCKED` does not say which of the host's rules blocks the merge, and here a missing
        # approval may be what does, with the open thread beside it blocking nothing. The
        # detection and its wording stay, under either merge_requires value, and the decision
        # and the host's own error are there to check them against.
        for i, requires in enumerate(("all", "required")):
            with self.subTest(merge_requires=requires):
                gh = _Gh(threads=[_thread("T1", "src/a.py", 12, "Nit.", "alice")],
                         decision="REVIEW_REQUIRED")
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d, merge_requires=requires)
                self.assertNotEqual(rc, 0)
                self.assertIn("blocked by 1 unresolved review thread(s)", err)
                self.assertIn("src/a.py:12", err)
                self.assertIn("reviewDecision: REVIEW_REQUIRED", err)
                self.assertIn(MERGE_ERROR, err)
                self.assertNotIn("a conflict, no merge rights", err)
                remarks = self._remarks(d)
                self.assertEqual(remarks["reviewDecision"], "REVIEW_REQUIRED")
                self.assertEqual(remarks["merge_error"], MERGE_ERROR)
                self.assertEqual(remarks["mergeStateStatus"], "BLOCKED")
                self.assertEqual([t["id"] for t in remarks["threads"]], ["T1"])
                self._assert_undone(gh)

    def test_every_remarks_stop_shows_the_review_decision(self) -> None:
        # Whatever is named as the block, the decision is shown and recorded: it is what shows
        # a CHANGES_REQUESTED review that does not count (the decision still reads APPROVED).
        # "" in the file and "none" in the message are the one answer for no decision.
        open_one = [_thread("T1", "src/a.py", 12, "Hm.", "alice")]
        dave = [{"author": {"login": "dave"}, "state": "CHANGES_REQUESTED"}]
        cases = {
            "a thread, approved": (dict(threads=open_one, decision="APPROVED"),
                                   "APPROVED", "APPROVED"),
            "a review that does not count": (dict(reviews=dave, decision="APPROVED"),
                                             "APPROVED", "APPROVED"),
            "changes requested": (dict(reviews=dave, decision="CHANGES_REQUESTED"),
                                  "CHANGES_REQUESTED", "CHANGES_REQUESTED"),
            "threads unread beside a review": (dict(reviews=dave, decision="REVIEW_REQUIRED",
                                                    graphql_ok=0),
                                               "REVIEW_REQUIRED", "REVIEW_REQUIRED"),
            "no decision": (dict(threads=open_one, decision=""), "none", ""),
            "a null decision": (dict(threads=open_one, decision=None), "none", ""),
            "a decision that is not text": (dict(threads=open_one, decision=7), "none", ""),
        }
        for i, (name, (over, shown, recorded)) in enumerate(cases.items()):
            with self.subTest(name):
                gh = _Gh(**over)
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertIn("it is blocked by", err)
                self.assertIn(f"reviewDecision: {shown}.", err)
                self.assertEqual(self._remarks(d)["reviewDecision"], recorded)
                self.assertEqual(self._remarks(d)["merge_error"], MERGE_ERROR)
                self._assert_undone(gh)

    def test_the_file_records_the_merge_error_as_it_was_printed(self) -> None:
        # The host's own words, where `gh` put them, in full: they may name the real block.
        words = ("X Pull request org/repo#7 is not mergeable: the base branch policy prohibits "
                 "the merge.\nTo have the pull request merged after all the requirements have "
                 "been met, add the `--auto` flag.")
        cases = {"on stderr, over several lines": (dict(merge_err=f"  {words}\n"), words),
                 "on stdout alone": (dict(merge_err="", merge_out=f"{words}\n"), words),
                 "stderr first": (dict(merge_err=words, merge_out="ignored"), words),
                 "nothing at all": (dict(merge_err=""), "")}
        for i, (name, (over, want)) in enumerate(cases.items()):
            with self.subTest(name):
                gh = _Gh(threads=self._two_threads(), **over)
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertIn(want, err)
                self.assertEqual(self._remarks(d)["merge_error"], want)
                self._assert_undone(gh)

    def test_blocked_without_remarks_keeps_generic_refusal(self) -> None:
        # Leg (ii): REVIEW_REQUIRED / a ruleset — BLOCKED, but no thread and no review
        # requesting changes.
        gh = _Gh(threads=[_thread("T3", "src/c.py", 3, "ok", "bob", resolved=True)],
                 decision="REVIEW_REQUIRED")
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("a conflict, no merge rights", err)
        self.assertNotIn("0 unresolved", err)
        self.assertNotIn("review thread", err)
        self.assertNotIn("not checked", err)                # it was checked: nothing to name
        self._assert_generic_refusal(err, d)
        self._assert_undone(gh)

    def test_a_merge_state_that_cannot_be_read_is_reported_as_not_checked(self) -> None:
        # "Not blocked by remarks" and "could not check" must not read the same. Nothing is
        # known to name, so the refusal stays generic and the threads are not asked for.
        cases = {"the read fails": _res(1, err="HTTP 502: Bad Gateway"),
                 "the answer is not JSON": _res(out="<html>502</html>"),
                 "the answer is not an object": _res(out="[]"),
                 "the answer has no state": _res(out="{}")}
        for i, (name, read) in enumerate(cases.items()):
            with self.subTest(name):
                gh = _Gh(threads=self._two_threads(), state_read=read)
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertIn(f"the merge state of {PR} is unreadable (`gh pr view`), so it "
                              "was not checked for review remarks", err)
                self.assertEqual(gh.cursors, [])
                self._assert_generic_refusal(err, d)
                self._assert_undone(gh)

    def test_a_state_the_host_reports_is_not_reported_as_not_checked(self) -> None:
        # Any state but BLOCKED, read, says the block is not a remarks block: nothing more to
        # report, however many threads are open.
        for i, status in enumerate(("DIRTY", "BEHIND", "UNSTABLE", "CLEAN", "HAS_HOOKS")):
            with self.subTest(status):
                gh = _Gh(states=(status,), threads=self._two_threads(),
                         decision="CHANGES_REQUESTED")
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertNotIn("not checked", err)
                self.assertEqual(gh.cursors, [])
                self._assert_generic_refusal(err, d)
                self._assert_undone(gh)

    def test_unknown_is_polled_then_read(self) -> None:
        # Leg (iii): UNKNOWN straight after the ready-mark is polled, then the BLOCKED
        # read that follows is acted on.
        gh = _Gh(states=("UNKNOWN", "UNKNOWN", "BLOCKED"), threads=self._two_threads())
        rc, err, d = self._run(gh, merge_wait_secs=60)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.sleeps, [15, 5, 5])           # the green's confirm, then the poll
        self.assertIn("blocked by 2 unresolved review thread(s)", err)
        self.assertTrue((d / REMARKS).is_file())

    def test_unknown_to_the_end_is_not_a_remarks_block(self) -> None:
        # Leg (iii): an UNKNOWN that never resolves is polled within merge_wait_secs and
        # then treated as not blocked by remarks.
        gh = _Gh(states=("UNKNOWN",), threads=self._two_threads())
        rc, err, d = self._run(gh, merge_wait_secs=20, merge_requires="required")
        self.assertNotEqual(rc, 0)
        views = [c for c in gh.calls if c[:3] == ["gh", "pr", "view"]
                 and "mergeStateStatus" in c[-1]]
        self.assertGreater(len(views), 1)                   # it was polled, not read once
        self.assertEqual(self.sleeps, [5, 5, 5, 5])         # no rollup wait: all 20 s for it
        self.assertIn("a conflict, no merge rights", err)
        self.assertIn(f"the merge state of {PR} is still UNKNOWN, so it was not checked for "
                      "review remarks", err)
        self.assertNotIn("review thread", err)
        self.assertFalse((d / REMARKS).exists())
        self._assert_undone(gh)

    def test_the_poll_gets_only_what_the_rollup_wait_left(self) -> None:
        # Leg (iii), "the same budget": confirming the green costs 15 s of the 20, so the
        # poll has 5 s, not a fresh 20. The PR stays marked ready 20 s on these waits, not 35.
        gh = _Gh(states=("UNKNOWN",), threads=self._two_threads())
        rc, err, d = self._run(gh, merge_wait_secs=20)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.sleeps, [15, 5])
        self.assertEqual(self._state_reads(gh), 2)
        self._assert_generic_refusal(err, d)
        self._assert_undone(gh)

    def test_the_poll_finds_a_block_on_what_is_left(self) -> None:
        # What the rollup wait left is still enough to see UNKNOWN turn into BLOCKED.
        gh = _Gh(states=("UNKNOWN", "BLOCKED"), threads=self._two_threads())
        rc, err, d = self._run(gh, merge_wait_secs=20)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.sleeps, [15, 5])
        self.assertIn("blocked by 2 unresolved review thread(s)", err)
        self.assertTrue((d / REMARKS).is_file())
        self._assert_undone(gh)

    def test_an_update_wait_is_charged_to_the_same_budget(self) -> None:
        # All three waits share the one bound: 5 s for the update to land and 15 s to confirm
        # the green use all 20, so the merge state is read once, with no sleep of its own.
        gh = _Gh(states=("UNKNOWN",), threads=self._two_threads(), behind=True)
        rc, err, d = self._run(gh, merge_wait_secs=20)
        self.assertNotEqual(rc, 0)
        self.assertIn(["gh", "pr", "update-branch", PR], gh.calls)
        self.assertEqual(self.sleeps, [5, 15])
        self.assertEqual(self._state_reads(gh), 1)
        self._assert_generic_refusal(err, d)
        self._assert_undone(gh)

    def test_with_no_budget_left_the_state_is_read_once(self) -> None:
        # The bound is never overrun, even by one poll: a spent budget, or none at all, still
        # reads the state once, and an UNKNOWN there is not blocked by remarks.
        cases = {"spent on the rollup wait": (dict(merge_wait_secs=15), [15]),
                 "no wait, rollup read": (dict(merge_wait_secs=0), []),
                 "no wait, no rollup read": (dict(merge_wait_secs=0,
                                                  merge_requires="required"), [])}
        for i, (name, (over, sleeps)) in enumerate(cases.items()):
            with self.subTest(name):
                self.sleeps.clear()
                gh = _Gh(states=("UNKNOWN",), threads=self._two_threads())
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d, **over)
                self.assertNotEqual(rc, 0)
                self.assertEqual(self.sleeps, sleeps)
                self.assertEqual(self._state_reads(gh), 1)
                self._assert_generic_refusal(err, d)
                self._assert_undone(gh)

    def test_outdated_thread_reports_original_line(self) -> None:
        # Leg (iv): an outdated thread has `line: null`; its originalLine is reported. A
        # thread on a whole file has no line at all and is reported by its path alone.
        gh = _Gh(threads=[_thread("T9", "src/old.py", None, "Stale?", "carol", original=77),
                          _thread("T8", "src/whole.py", None, "Split this file.", "dave",
                                  outdated=False)])
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("blocked by 2 unresolved review thread(s)", err)
        self.assertIn("src/old.py:77 (outdated)", err)
        self.assertIn("src/whole.py by dave", err)
        self.assertNotIn(":None", err)
        threads = json.loads((d / REMARKS).read_text())["threads"]
        self.assertEqual([(t["line"], t["outdated"]) for t in threads],
                         [(77, True), (None, False)])

    def test_changes_requested_without_threads_is_named(self) -> None:
        # Leg (v): a CHANGES_REQUESTED review with no threads.
        gh = _Gh(threads=[], decision="CHANGES_REQUESTED",
                 reviews=[{"author": {"login": "dave"}, "state": "CHANGES_REQUESTED"},
                          {"author": {"login": "erin"}, "state": "APPROVED"}])
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("CHANGES_REQUESTED", err)
        self.assertIn("dave", err)
        self.assertNotIn("erin", err)
        self.assertNotIn("0 unresolved", err)
        self.assertNotIn("a conflict, no merge rights", err)
        self.assertNotIn("could not be read", err)
        self.assertIn("reviewDecision: CHANGES_REQUESTED", err)
        remarks = json.loads((d / REMARKS).read_text(encoding="utf-8"))
        self.assertEqual(remarks["threads"], [])            # read, and there are none
        self.assertEqual(remarks["changes_requested_by"], ["dave"])
        self.assertEqual(remarks["reviewDecision"], "CHANGES_REQUESTED")
        self._assert_undone(gh)

    def test_a_decision_with_no_listed_review_names_no_one(self) -> None:
        # The decision alone says changes were requested: that is a block to name, with no
        # login to put in the file, and "?" is for the message only.
        gh = _Gh(decision="CHANGES_REQUESTED",
                 reviews=[{"author": {"login": "erin"}, "state": "APPROVED"}])
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("a CHANGES_REQUESTED review by ?", err)
        self.assertNotIn("erin", err)
        remarks = self._remarks(d)
        self.assertEqual(remarks["changes_requested_by"], [])
        self.assertEqual(remarks["reviewDecision"], "CHANGES_REQUESTED")
        self._assert_undone(gh)

    def test_threads_that_cannot_be_read_are_unknown_not_none(self) -> None:
        # A review requesting changes names the block on its own, but the threads were not
        # read: the file and the message must both say so, whatever went wrong with the read
        # and whatever an earlier page held, or whatever acts on the file takes it for none.
        open_one = _thread("T1", "src/a.py", 12, "Hm.", "alice")
        cases = {
            "the first request fails": dict(graphql_ok=0),
            "a later page fails": dict(pages=[[open_one], [open_one]], graphql_ok=1),
            "the cursor repeats": dict(pages=[[open_one]], stuck=True),
            "the answer is not JSON": dict(answer="<html>502</html>"),
            "the PR is not found": dict(answer=json.dumps({"data": {"resource": None}})),
            "gh is gone": dict(answer=OSError("gh: not found")),
        }
        for i, (name, over) in enumerate(cases.items()):
            with self.subTest(name):
                gh = _Gh(decision="CHANGES_REQUESTED", **over,
                         reviews=[{"author": {"login": "dave"}, "state": "CHANGES_REQUESTED"}])
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertIn("a CHANGES_REQUESTED review by dave", err)
                self.assertIn("review threads could not be read", err)
                self.assertIn("reviewDecision: CHANGES_REQUESTED", err)
                self.assertNotIn("unresolved review thread", err)
                self.assertNotIn("a conflict, no merge rights", err)
                remarks = json.loads((d / REMARKS).read_text(encoding="utf-8"))
                self.assertIsNone(remarks["threads"])
                self.assertEqual(remarks["changes_requested_by"], ["dave"])
                self.assertEqual(remarks["reviewDecision"], "CHANGES_REQUESTED")
                self.assertEqual(remarks["merge_error"], MERGE_ERROR)
                self._assert_undone(gh)

    def test_threads_that_cannot_be_read_with_no_review_stay_generic(self) -> None:
        # Nothing known to name, so no file and the generic refusal, and it says what it
        # could not read rather than leave a reader to assume there were no threads.
        gh = _Gh(graphql_ok=0)
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("review threads could not be read", err)
        self._assert_generic_refusal(err, d)
        self._assert_undone(gh)

    def test_an_unexpected_answer_still_returns_the_pr_to_draft(self) -> None:
        # These reads run while the PR is marked ready: an answer of an unexpected shape is a
        # failed read, never a crash that would skip the undo.
        t = _thread("T1", "src/a.py", 12, "Hm.", "alice")
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}}
        cases = {
            "a thread that is not an object": dict(threads=["junk"]),
            "comments that are not an object": dict(threads=[{**t, "comments": "junk"}]),
            "a comment that is not an object": dict(threads=[{**t, "comments": {"nodes": ["x"]}}]),
            "nodes that are not a list": dict(answer=json.dumps(
                {"data": {"resource": {"reviewThreads": {**page, "nodes": 7}}}})),
            "pageInfo that is not an object": dict(answer=json.dumps(
                {"data": {"resource": {"reviewThreads": {"pageInfo": "x", "nodes": []}}}})),
        }
        for i, (name, over) in enumerate(cases.items()):
            with self.subTest(name):
                gh = _Gh(**over)
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertIn("review threads could not be read", err)
                self._assert_generic_refusal(err, d)
                self._assert_undone(gh)

    def test_an_author_that_is_not_an_object_is_no_author(self) -> None:
        # The remark itself is still there to name; only who wrote it is unknown. The file says
        # that the same way for a thread and a review ("": never a login), and "?" is for the
        # message only.
        gh = _Gh(threads=[{**_thread("T1", "src/a.py", 12, "Hm.", "alice"),
                           "comments": {"nodes": [{"body": "Hm.", "url": "u", "author": "x"}]}}],
                 reviews=[{"author": "dave", "state": "CHANGES_REQUESTED"},
                          {"author": {"login": "erin"}, "state": "CHANGES_REQUESTED"}])
        rc, err, d = self._run(gh)
        self.assertNotEqual(rc, 0)
        self.assertIn("blocked by 1 unresolved review thread(s) — src/a.py:12 by ?", err)
        self.assertIn("a CHANGES_REQUESTED review by ?, erin", err)
        remarks = json.loads((d / REMARKS).read_text(encoding="utf-8"))
        self.assertEqual(remarks["threads"][0]["author"], "")
        self.assertEqual(remarks["changes_requested_by"], ["", "erin"])
        self._assert_undone(gh)

    def test_a_write_that_fails_leaves_no_file_and_still_returns_the_pr_to_draft(self) -> None:
        # Writing the file is the one other thing between the failed merge and the undo. A
        # write truncates first, so one that dies part-way (a full disk, a quota) has already
        # put part of the file on disk. That part must not be what a reader finds beside a
        # message saying the file could not be written, under its name or any other.
        real_write = Path.write_text
        partial: list[str] = []

        def dies_part_way(path: Path, text: str, **kw: object) -> int:
            real_write(path, text[:len(text) // 2], **kw)
            # Only the remarks file: `merge_wave` also marks `publish.json` the same way
            # (#839), and that write failing here must leave the record whole as well.
            if "merge-remarks" in path.name:
                partial.append(path.read_text(encoding="utf-8"))
            raise OSError(errno.ENOSPC, "No space left on device")

        faults = {
            "the write dies part-way": ("No space left on device", mock.patch.object(
                Path, "write_text", autospec=True, side_effect=dies_part_way)),
            "the file cannot be put in place": ("Permission denied", mock.patch(
                "os.replace", side_effect=OSError(errno.EACCES, "Permission denied"))),
        }
        for i, (name, (reason, fault)) in enumerate(faults.items()):
            with self.subTest(name):
                gh = _Gh(threads=self._two_threads())
                d = self._bundle(_cfg(self.tmp), f"838{i}")
                before = sorted(p.name for p in d.iterdir())
                with fault:
                    rc, err, _ = self._run(gh, d)
                self.assertNotEqual(rc, 0)
                self.assertIn("blocked by 2 unresolved review thread(s)", err)
                self.assertIn("src/a.py:12", err)
                self.assertIn("reviewDecision: none", err)
                self.assertIn(MERGE_ERROR, err)             # what the file would have kept
                self.assertIn("could not be written", err)
                self.assertIn(reason, err)
                self.assertNotIn("are written to", err)
                self.assertEqual(sorted(p.name for p in d.iterdir()), before)
                self._assert_undone(gh)
        # The first fault did fire, on a real part of the file.
        self.assertEqual(len(partial), 1)
        self.assertIn('"pr_url"', partial[0])
        self.assertRaises(ValueError, json.loads, partial[0])


if __name__ == "__main__":
    unittest.main()
