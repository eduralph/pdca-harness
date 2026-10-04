"""Offline slice for opt-in auto-merge mode (`merge.merge_wave`, #wave-model).

Proves the fail-closed contract: a published, COMPLETE bundle's PR is `gh pr merge`d and
the base re-fetched; a close/no-fix bundle and an already-merged PR are skipped; a
COMPLETE bundle with no recorded PR, or a `gh pr merge` failure, returns non-zero so the
caller STOPs. Dry-run shells nothing. `gh` and state are mocked — no network. Run from the
project root:
    PYTHONPATH=src python -m unittest discover -s tests

Issue #413 extends that fail-closed contract from "merged" to "merged GREEN": `gh pr merge`
only refuses on checks the HOST marks required in branch protection, so `_merge_one` reads
the PR's own FULL check rollup (`gh pr checks`) after the ready-mark and immediately before
the merge, and refuses on any failing, pending or missing check — whatever branch
protection is (or isn't) configured to require. `[driver].merge_requires = "required"` opts
back into the host-config-only behaviour.

Issue #462 extends it once more: a non-final wave's PR is only seconds old, so that same
rollup read is routinely `pending`/`empty` NOT because anything is wrong but because the
checks have not reported yet. `_merge_one` now waits (`_wait_for_green`, bounded by
`[driver].merge_wait_secs`, driven through the patchable `merge._sleep` so these tests cost
no wall-clock) before treating an unresolved rollup as a refusal, and undoes the ready-mark
(`gh pr ready --undo`) on every path where it declines to merge a PR it already readied.

Issue #531 ties the green to the base and the head that merge: after the ready-mark
`_merge_one` decides in plain git whether the PR's head lacks its base's tip (`gh pr view`
for the head, `git fetch`, `git merge-base --is-ancestor`), brings a PR that is behind up to
date (`gh pr update-branch`, polled until it lands), waits for that head's rollup, reads the
head and base again, and merges pinned to the head (`--match-head-commit`). The `MergeWave`
stubs answer that read as "up to date"; `MergeAgainstCurrentBase` drives it against a
stateful fake host (`_Host`).
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import merge, state
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, **overrides: object) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={"org/repo": str(root / "repo")},
        **overrides)


def _rollup(*checks: tuple[str, str], code: int = 0) -> SimpleNamespace:
    """A `gh pr checks --json name,bucket` result: `(name, bucket)` pairs plus the exit
    code gh would pair with them (0 all passed, 1 something failed, 8 something pending —
    gh prints the JSON either way, so the buckets are what decides)."""
    return SimpleNamespace(
        returncode=code, stderr="",
        stdout=json.dumps([{"name": n, "bucket": b} for n, b in checks]))


HEAD = "1" * 40       # the PR head the stubs' `gh pr view` reports (issue #531)
BASE_TIP = "b" * 40   # the base tip the stubs' `git rev-parse` resolves


def _up_to_date(cmd: list[str]) -> SimpleNamespace | None:
    """Issue #531's base read, answered for a PR that is NOT behind its base: `gh pr view`
    reports head `HEAD` on base `main`, `git rev-parse` resolves the base tip, and the
    fetches and `git merge-base --is-ancestor` fall through to the callers' default exit 0
    (up to date). ``None`` for every other command."""
    if cmd[:3] == ["gh", "pr", "view"]:
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"headRefOid": HEAD, "baseRefName": "main"}))
    if cmd[:1] == ["git"] and "rev-parse" in cmd:
        return SimpleNamespace(returncode=0, stderr="", stdout=BASE_TIP + "\n")
    return None


def _gh(**by_verb: SimpleNamespace):
    """Build a `subprocess.run` stub: every `gh`/`git` call succeeds, except the verbs
    named here (`checks=`, `ready=`, `merge=`) which return the given result. The default
    rollup is green and the PR up to date with its base, so tests that are not about #413
    or #531 reach `gh pr merge` exactly as they did before them."""
    default_checks = _rollup(("ci", "pass"))

    def run(cmd, **kw):
        if cmd[:2] == ["gh", "pr"] and cmd[2] in by_verb:
            return by_verb[cmd[2]]
        if cmd[:3] == ["gh", "pr", "checks"]:
            return default_checks
        return _up_to_date(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


class MergeWave(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        # issue #582: every green rollup is now re-read once more, one poll interval later,
        # before it is believed — so any test that reaches a green rollup would sleep a
        # real 15 s. Patch the wait's sleep for the whole class; a test that patches it
        # again in its own `with` (to inspect the calls) just nests over this one.
        sleeper = mock.patch.object(merge, "_sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, pr_url: str | None = "https://gh/pr/1",
                patch: str | None = "diff\n", repo: str = "org/repo") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        if patch is not None:
            (d / "patch.diff").write_text(patch, encoding="utf-8")
        if pr_url is not None:
            (d / "publish.json").write_text(
                json.dumps({"pr_url": pr_url, "repo": repo}), encoding="utf-8")
        return d

    def test_dry_run_shells_nothing(self) -> None:
        b = self._bundle("M1")
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                redirect_stdout(io.StringIO()) as out:
            rc = merge.merge_wave(self.cfg, [b], dry_run=True, method="merge")
        self.assertEqual(rc, 0)
        run.assert_not_called()                       # no gh in a dry-run
        self.assertIn("gh pr merge", out.getvalue())

    def test_merges_then_fetches_base(self) -> None:
        b = self._bundle("M2")
        runs: list[list[str]] = []
        gh = _gh()

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return gh(cmd, **kw)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [b], method="squash")
        self.assertEqual(rc, 0)
        merge_cmd = ["gh", "pr", "merge", "https://gh/pr/1", "--squash",
                     "--match-head-commit", HEAD]
        self.assertIn(merge_cmd, runs)
        # base refreshed AFTER the merge (issue #531 fetches before it too, for its read)
        self.assertTrue(any("fetch" in c for c in runs[runs.index(merge_cmd) + 1:]))

    def test_close_no_fix_skipped(self) -> None:
        b = self._bundle("M3", patch=None)             # no patch — nothing to merge
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE):
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_no_pr_url_fails_closed(self) -> None:
        b = self._bundle("M4", pr_url=None)            # COMPLETE + patch but never published
        with mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertIn("no recorded PR", err.getvalue())

    def test_merge_failure_stops(self) -> None:
        b = self._bundle("M5")
        # ready + the check rollup succeed; the merge itself fails (a conflict, no rights).
        fail_merge = _gh(merge=SimpleNamespace(returncode=1, stdout="",
                                               stderr="not mergeable"))
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return fail_merge(cmd, **kw)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertIn("did not merge", err.getvalue())
        # issue #462 (iii): a failing `gh pr merge` declines AFTER the ready-mark too, so it
        # must be undone the same as a rollup refusal.
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)

    def test_readies_before_merging(self) -> None:
        # #279: the publisher opens every PR --draft, but `gh pr merge` refuses a draft, so a
        # non-final wave's PR must be readied first. `gh pr ready` must precede `gh pr merge`.
        b = self._bundle("M7")
        runs: list[list[str]] = []
        gh_ok = _gh()

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return gh_ok(cmd, **kw)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [b], method="merge")
        self.assertEqual(rc, 0)
        gh = [c for c in runs if c[:2] == ["gh", "pr"]]
        self.assertEqual(gh[0], ["gh", "pr", "ready", "https://gh/pr/1"])
        self.assertEqual(gh[-1], ["gh", "pr", "merge", "https://gh/pr/1", "--merge",
                                  "--match-head-commit", HEAD])

    def test_ready_failure_stops_before_merge(self) -> None:
        # If a PR can't be readied it can't be merged — fail-closed, and never attempt merge.
        b = self._bundle("M8")
        runs: list[list[str]] = []

        def fail_ready(cmd, **kw):
            runs.append(cmd)
            rc = 1 if cmd[:3] == ["gh", "pr", "ready"] else 0
            return SimpleNamespace(returncode=rc, stdout="", stderr="cannot ready")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fail_ready), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertIn("could not be marked ready", err.getvalue())
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in runs))

    def test_dry_run_readies_nothing(self) -> None:
        # A dry-run must shell nothing — not even the new ready step.
        b = self._bundle("M9")
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                redirect_stdout(io.StringIO()):
            merge.merge_wave(self.cfg, [b], dry_run=True)
        run.assert_not_called()

    def test_already_merged_skipped(self) -> None:
        b = self._bundle("M6")
        with mock.patch("pdca_harness.merge.subprocess.run") as run, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=True):
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 0)
        run.assert_not_called()                        # idempotent — no second merge

    def test_first_failure_stops_the_wave(self) -> None:
        # The second bundle has no PR → the wave STOPs there; order is name-sorted by caller.
        ok = self._bundle("MA")
        bad = self._bundle("MB", pr_url=None)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=_gh()), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stderr(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [ok, bad])
        self.assertEqual(rc, 1)

    # ---- issue #413: merged means merged GREEN, not merely merged --------------------

    def _drive(self, iid: str, *, cfg: Config | None = None,
               **by_verb: SimpleNamespace) -> tuple[int, list[list[str]], str]:
        """Run one bundle through `merge_wave` against a stubbed `gh`. Returns the exit
        code, every command shelled, and stderr — so a test can assert BOTH the refusal
        and that `gh pr merge` was never reached. `_sleep` is patched to a no-op so a
        pending/empty rollup's wait (issue #462) costs no wall-clock here."""
        b = self._bundle(iid)
        calls: list[list[str]] = []
        gh = _gh(**by_verb)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return gh(cmd, **kw)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge, "_sleep", create=True), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(cfg or self.cfg, [b])
        return rc, calls, err.getvalue()

    def _merged(self, calls: list[list[str]]) -> bool:
        return any(c[:3] == ["gh", "pr", "merge"] for c in calls)

    def test_failing_check_refuses_and_never_merges(self) -> None:
        # The defect: a red job the HOST does not mark required in branch protection. `gh
        # pr merge` would happily succeed (the stub returns 0 for it) — the rollup read is
        # the only thing that can stop this.
        rc, calls, err = self._drive(
            "MC", checks=_rollup(("build", "pass"), ("lint", "fail"), code=1))
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("FAILING", err)
        self.assertIn("lint (fail)", err)              # names the offending check
        # issue #462 (iii): a red rollup declines AFTER the ready-mark, so it must be undone.
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)

    def test_pending_check_refuses(self) -> None:
        # issue #462: "nothing was wrong, the evidence had simply not arrived" must not be a
        # terminal verdict. `_merge_one` waits (bounded, re-reading the rollup) before it
        # gives up — and STILL refuses, cleanly, once the bound is exhausted and the checks
        # genuinely never reported.
        rc, calls, err = self._drive("MD", checks=_rollup(("ci", "pending"), code=8))
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("not finished", err)
        self.assertIn(f"within {self.cfg.merge_wait_secs}s", err)   # distinguishes the two
        self.assertNotIn("FAILING", err)                            # from "a check is red"
        # The wait actually happened — the rollup was re-read, not just checked once.
        checks_calls = [c for c in calls if c[:3] == ["gh", "pr", "checks"]]
        self.assertGreater(len(checks_calls), 1)
        # issue #462 (iii): declined after the ready-mark ⇒ the ready-mark is undone, so the
        # stopped wave leaves no PR advertising a readiness no human granted.
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)

    def test_pending_then_green_merges(self) -> None:
        # issue #462 (i): the wait pays off — a rollup that resolves green after the checks
        # report merges, and the ready-mark is left alone (nothing to undo).
        b = self._bundle("MD2")
        calls: list[list[str]] = []
        reads = {"n": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "checks"]:
                reads["n"] += 1
                return (_rollup(("ci", "pending"), code=8) if reads["n"] < 3
                        else _rollup(("ci", "pass")))
            return _up_to_date(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge, "_sleep", create=True) as sleep, \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [b], method="merge")
        self.assertEqual(rc, 0)
        # pending, pending, green, then green again — issue #582: the first green is
        # re-read once more, one poll interval later, before it is believed.
        self.assertEqual(reads["n"], 4)
        self.assertTrue(sleep.called)                 # the wait actually slept in between
        self.assertIn(["gh", "pr", "merge", "https://gh/pr/1", "--merge",
                       "--match-head-commit", HEAD], calls)
        self.assertNotIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)

    def test_wait_bound_zero_performs_no_wait(self) -> None:
        # 0 means "do not wait" — the original immediate-refusal behaviour, for a host
        # whose checks are known to report before the wave boundary ever fires.
        cfg = _cfg(self.tmp, merge_wait_secs=0)
        rc, calls, err = self._drive("MD3", cfg=cfg,
                                     checks=_rollup(("ci", "pending"), code=8))
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("within 0s", err)
        checks_calls = [c for c in calls if c[:3] == ["gh", "pr", "checks"]]
        self.assertEqual(len(checks_calls), 1)         # exactly one read — no re-poll
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)

    def test_all_green_readies_then_checks_then_merges(self) -> None:
        rc, calls, _ = self._drive("ME")
        self.assertEqual(rc, 0)
        gh = [c[:3] for c in calls if c[:2] == ["gh", "pr"]]
        # Rollup read AFTER ready; issue #582: read twice — the green is confirmed once.
        # Issue #531: the head is read before and after the rollup wait; no update for a
        # PR that is not behind its base.
        self.assertEqual(gh, [["gh", "pr", "ready"], ["gh", "pr", "view"],
                              ["gh", "pr", "checks"], ["gh", "pr", "checks"],
                              ["gh", "pr", "view"], ["gh", "pr", "merge"]])

    def test_empty_rollup_refuses_under_the_default(self) -> None:
        # Absence of evidence is not green: nothing reported ⇒ nothing verified.
        rc, calls, err = self._drive("MF", checks=_rollup())
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("EMPTY", err)

    def test_rollup_gh_could_not_read_refuses(self) -> None:
        # gh's own shape for "no checks reported" (and for auth/network/too-old-gh): a
        # non-zero exit with no JSON at all. Fail-closed — never merge on a rollup we
        # could not read.
        rc, calls, err = self._drive("MF2", checks=SimpleNamespace(
            returncode=1, stdout="", stderr="no checks reported on the 'fix/x' branch"))
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("no checks reported", err)

    def test_skipped_and_neutral_checks_do_not_block(self) -> None:
        # Completed non-failures: a skipped path filter must not deadlock the wave.
        rc, calls, _ = self._drive(
            "MG", checks=_rollup(("ci", "pass"), ("docs", "skipping")))
        self.assertEqual(rc, 0)
        self.assertTrue(self._merged(calls))

    def test_unknown_bucket_is_treated_as_failing(self) -> None:
        # A bucket this harness has never heard of is not evidence of green.
        rc, calls, err = self._drive("MG2", checks=_rollup(("ci", "quantum"), code=1))
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("FAILING", err)

    def test_check_triggered_by_the_ready_mark_is_caught(self) -> None:
        # `gh pr ready` can itself trigger `ready_for_review` CI: green BEFORE the ready
        # mark, pending after it. A rollup read only pre-ready would have merged this —
        # this is what pins the read to AFTER ready, immediately before the merge.
        b = self._bundle("MH")
        readied = False
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            nonlocal readied
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "ready"]:
                readied = True
            elif cmd[:3] == ["gh", "pr", "checks"]:
                return (_rollup(("e2e", "pending"), code=8) if readied
                        else _rollup(("e2e", "pass")))
            return _up_to_date(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge, "_sleep", create=True), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(self.cfg, [b])
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("not finished", err.getvalue())
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)

    def test_a_red_wave_member_stops_the_wave_before_later_bundles(self) -> None:
        # Wave-level consequence of the gate: the red PR is not merged AND the next
        # bundle's PR is never touched, so no later wave can build on the half-merged set.
        red = self._bundle("MJ1")
        nxt = self._bundle("MJ2", pr_url="https://gh/pr/2")
        calls: list[list[str]] = []
        gh = _gh(checks=_rollup(("ci", "fail"), code=1))

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return gh(cmd, **kw)

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = merge.merge_wave(self.cfg, [red, nxt])
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertFalse(any("https://gh/pr/2" in c for c in calls))

    def test_merge_requires_required_restores_host_config_semantics(self) -> None:
        # The opt-in escape hatch: branch protection alone decides again, so the rollup is
        # not even read — and a red non-required check merges, exactly as before #413.
        cfg = _cfg(self.tmp, merge_requires="required")
        rc, calls, _ = self._drive("MI", cfg=cfg,
                                   checks=_rollup(("ci", "fail"), code=1))
        self.assertEqual(rc, 0)
        self.assertFalse(any(c[:3] == ["gh", "pr", "checks"] for c in calls))
        self.assertTrue(self._merged(calls))

    # ---- issue #582: a green rollup is believed only once it has held -----------------

    def _drive_reads(self, iid: str, reads: list[SimpleNamespace], *,
                     cfg: Config | None = None,
                     timeline: list | None = None) -> tuple[int, list[list[str]], str, list]:
        """Run one bundle through `merge_wave` where `gh pr checks` returns ``reads`` in
        order (the last one repeating). Returns the exit code, every command shelled,
        stderr, and the arguments `merge._sleep` was called with. ``timeline``, if given,
        receives every rollup read and sleep in the order they happened —
        ``("read", <the rollup returned>)`` / ``("sleep", secs)`` — and ``("merge",)``
        when `gh pr merge` runs."""
        b = self._bundle(iid)
        calls: list[list[str]] = []
        events = timeline if timeline is not None else []
        n = {"read": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "checks"]:
                n["read"] += 1
                read = reads[min(n["read"], len(reads)) - 1]
                events.append(("read", read))
                return read
            if cmd[:3] == ["gh", "pr", "merge"]:
                events.append(("merge",))
            return _up_to_date(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=fake_run), \
                mock.patch.object(merge, "_sleep",
                                  side_effect=lambda secs: events.append(("sleep", secs))), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(cfg or self.cfg, [b])
        slept = [e[1] for e in events if e[0] == "sleep"]
        return rc, calls, err.getvalue(), slept

    @staticmethod
    def _checks(calls: list[list[str]]) -> list[list[str]]:
        return [c for c in calls if c[:3] == ["gh", "pr", "checks"]]

    def test_partial_green_then_failing_does_not_merge(self) -> None:
        # The defect: a fast check (dco) has passed while a slow one (e2e) has not yet
        # registered, so the first read is a partial `green`. Before #582 that merged at
        # once; the confirm read sees e2e fail and refuses, exactly as a red rollup does.
        rc, calls, err, slept = self._drive_reads("MK1", [
            _rollup(("dco", "pass")),
            _rollup(("dco", "pass"), ("e2e", "fail"), code=1)])
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("FAILING", err)
        self.assertIn("e2e (fail)", err)               # names the failing check
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)
        self.assertEqual(len(self._checks(calls)), 2)  # failing returns at once
        self.assertLessEqual(sum(slept), self.cfg.merge_wait_secs)

    def test_partial_green_then_pending_merges_only_after_the_fourth_read(self) -> None:
        # (i)+(ii): the confirm read is pending, so the wait resumes; the rollup then goes
        # green and holds — merge only after that 4th read, never on the first green.
        rc, calls, _, slept = self._drive_reads("MK2", [
            _rollup(("dco", "pass")),
            _rollup(("dco", "pass"), ("e2e", "pending"), code=8),
            _rollup(("dco", "pass"), ("e2e", "pass")),
            _rollup(("dco", "pass"), ("e2e", "pass"))])
        self.assertEqual(rc, 0)
        self.assertTrue(self._merged(calls))
        gh = [c[:3] for c in calls if c[:2] == ["gh", "pr"]]
        self.assertEqual(gh, [["gh", "pr", "ready"], ["gh", "pr", "view"]]
                         + [["gh", "pr", "checks"]] * 4
                         + [["gh", "pr", "view"], ["gh", "pr", "merge"]])  # after read 4
        self.assertNotIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)
        self.assertLessEqual(sum(slept), self.cfg.merge_wait_secs)

    def test_confirm_read_unreadable_returns_at_once(self) -> None:
        # (ii): an unreadable confirm read refuses with no further reads.
        rc, calls, err, _ = self._drive_reads("MK3", [
            _rollup(("dco", "pass")),
            SimpleNamespace(returncode=4, stdout="", stderr="HTTP 502")])
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("could not be read", err)
        self.assertEqual(len(self._checks(calls)), 2)

    def test_green_with_no_budget_left_to_confirm_is_not_believed(self) -> None:
        # (iii): with a 15 s budget, pending → (15 s) → green leaves nothing to confirm the
        # green with, so it is refused as pending — saying why — and the bound holds.
        cfg = _cfg(self.tmp, merge_wait_secs=15)
        rc, calls, err, slept = self._drive_reads("MK4", [
            _rollup(("dco", "pass"), ("e2e", "pending"), code=8),
            _rollup(("dco", "pass"), ("e2e", "pass"))], cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("not finished within 15s", err)
        self.assertIn("confirm", err)
        self.assertIn("green first seen with 0s of wait budget left, too little to confirm "
                      "it 15s later (2 checks)", err)
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)
        self.assertEqual(len(self._checks(calls)), 2)
        self.assertLessEqual(sum(slept), 15)

    def test_wait_never_exceeds_the_bound_while_green_flickers(self) -> None:
        # (iii): green and pending alternating — every confirm fails, the loop keeps
        # waiting, and the total slept time stays within merge_wait_secs.
        cfg = _cfg(self.tmp, merge_wait_secs=100)
        flicker = [_rollup(("dco", "pass")),
                   _rollup(("dco", "pass"), ("e2e", "pending"), code=8)] * 50
        rc, calls, err, slept = self._drive_reads("MK5", flicker, cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("not finished within 100s", err)
        self.assertTrue(slept)
        self.assertLessEqual(sum(slept), 100)

    def test_wait_bound_zero_returns_a_single_green_read_as_is(self) -> None:
        # (iv): merge_wait_secs = 0 is unchanged — one read, no sleep, verdict as-is.
        cfg = _cfg(self.tmp, merge_wait_secs=0)
        rc, calls, _, slept = self._drive_reads("MK6", [_rollup(("dco", "pass"))], cfg=cfg)
        self.assertEqual(rc, 0)
        self.assertTrue(self._merged(calls))
        self.assertEqual(len(self._checks(calls)), 1)
        self.assertEqual(slept, [])

    def test_budget_under_one_poll_interval_never_confirms_a_green(self) -> None:
        # The confirm read comes one FULL poll interval (15 s) after the first green, or not
        # at all. With merge_wait_secs = 1 that interval never fits, so green, green is
        # refused as unconfirmed — not "confirmed" by a read squeezed in 1 s later.
        cfg = _cfg(self.tmp, merge_wait_secs=1)
        green = _rollup(("dco", "pass"), ("e2e", "pass"))
        rc, calls, err, slept = self._drive_reads("MK7", [green, green], cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("not finished within 1s", err)
        self.assertIn("confirm", err)
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)
        self.assertEqual(len(self._checks(calls)), 1)
        self.assertEqual(slept, [])

    def test_green_with_under_a_poll_interval_of_budget_left_is_not_believed(self) -> None:
        # With merge_wait_secs = 20, pending → (15 s) → green leaves 5 s: less than the full
        # poll interval a confirm read needs, so the green is refused as unconfirmed rather
        # than "confirmed" by a read 5 s later.
        cfg = _cfg(self.tmp, merge_wait_secs=20)
        green = _rollup(("dco", "pass"), ("e2e", "pass"))
        rc, calls, err, slept = self._drive_reads("MK8", [
            _rollup(("dco", "pass"), ("e2e", "pending"), code=8), green, green], cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertFalse(self._merged(calls))
        self.assertIn("not finished within 20s", err)
        self.assertIn("green first seen with 5s of wait budget left, too little to confirm "
                      "it 15s later (2 checks)", err)
        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"], calls)
        self.assertEqual(len(self._checks(calls)), 2)
        self.assertEqual(slept, [15])

    def test_a_merge_always_follows_two_greens_one_poll_interval_apart(self) -> None:
        # The invariant, swept over budgets on both sides of each 15 s boundary: whenever
        # merge_wave merges, the last two rollup reads before it were both green with
        # exactly one poll interval (15 s) slept between them, and the total slept never
        # exceeds merge_wait_secs. Each rollup sequence merges exactly when the budget has
        # room for that confirm, so the sweep also pins WHEN a merge happens.
        green = _rollup(("dco", "pass"), ("e2e", "pass"))
        pending = _rollup(("dco", "pass"), ("e2e", "pending"), code=8)
        cases = {                     # rollup reads in order, smallest budget that merges
            "green": ([green], 15),
            "pending-green": ([pending, green], 30),
            "empty-green": ([_rollup(), green], 30),
            "pending3-green": ([pending] * 3 + [green], 60),
            "green-empty-green": ([green, _rollup(), green], 45),   # confirm read is EMPTY
            "flicker": ([green, pending] * 40, None),          # never holds — never merges
        }
        for budget in (1, 14, 15, 16, 20, 29, 30, 31, 44, 45, 46, 59, 60, 61, 300):
            for name, (reads, merges_from) in cases.items():
                with self.subTest(budget=budget, rollups=name):
                    timeline: list = []
                    rc, calls, _, slept = self._drive_reads(
                        f"MS-{budget}-{name}", reads,
                        cfg=_cfg(self.tmp, merge_wait_secs=budget), timeline=timeline)
                    self.assertLessEqual(sum(slept), budget)
                    expect = merges_from is not None and budget >= merges_from
                    self.assertEqual(self._merged(calls), expect)
                    if not expect:
                        self.assertEqual(rc, 1)
                        self.assertIn(["gh", "pr", "ready", "https://gh/pr/1", "--undo"],
                                      calls)
                        continue
                    self.assertEqual(rc, 0)
                    before = timeline[:timeline.index(("merge",))]
                    at = [i for i, e in enumerate(before) if e[0] == "read"]
                    self.assertGreaterEqual(len(at), 2, "merged on a single rollup read")
                    self.assertIs(before[at[-2]][1], green)
                    self.assertIs(before[at[-1]][1], green)
                    self.assertEqual(
                        sum(e[1] for e in before[at[-2]:at[-1]] if e[0] == "sleep"), 15)

    def test_merge_requires_comes_from_the_driver_table(self) -> None:
        # Through the REAL config loader, not a hand-built Config: `[driver]
        # merge_requires` in a rendered pdca.toml has to actually reach `_merge_one`.
        root = self.tmp / "instance"
        root.mkdir()
        toml = root / "pdca.toml"
        base = '[paths]\nbundle_root = "results"\n'

        toml.write_text(base + '\n[driver]\nmerge_requires = "required"\n', encoding="utf-8")
        self.assertEqual(Config.load(root).merge_requires, "required")

        toml.write_text(base, encoding="utf-8")                    # unset ⇒ the default
        self.assertEqual(Config.load(root).merge_requires, "all")

        toml.write_text(base + '\n[driver]\nmerge_requires = "whatever"\n', encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            cfg = Config.load(root)
        self.assertEqual(cfg.merge_requires, "all")   # an unknown value fails CLOSED
        self.assertIn("merge_requires", err.getvalue())

    def test_merge_wait_secs_comes_from_the_driver_table(self) -> None:
        # issue #462: through the REAL config loader — `[driver] merge_wait_secs` in a
        # rendered pdca.toml has to actually reach `_merge_one` (the same plumbing as
        # merge_requires: dataclass field, load(), constructor kwarg).
        root = self.tmp / "instance2"
        root.mkdir()
        toml = root / "pdca.toml"
        base = '[paths]\nbundle_root = "results"\n'

        toml.write_text(base + '\n[driver]\nmerge_wait_secs = 30\n', encoding="utf-8")
        self.assertEqual(Config.load(root).merge_wait_secs, 30)

        toml.write_text(base, encoding="utf-8")                    # unset ⇒ the default
        self.assertEqual(Config.load(root).merge_wait_secs, 300)

        toml.write_text(base + '\n[driver]\nmerge_wait_secs = "soon"\n', encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            cfg = Config.load(root)
        self.assertEqual(cfg.merge_wait_secs, 300)     # an unparseable value fails CLOSED
        self.assertIn("merge_wait_secs", err.getvalue())

        toml.write_text(base + '\n[driver]\nmerge_wait_secs = -5\n', encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            cfg = Config.load(root)
        self.assertEqual(cfg.merge_wait_secs, 300)     # negative also fails CLOSED
        self.assertIn("merge_wait_secs", err.getvalue())


# ---- issue #531: merge only a verified head, one that contains the base it merges into --

PR_A = "https://github.com/org/repo/pull/1"
PR_B = "https://github.com/org/repo/pull/2"


def _res(code: int = 0, out: str = "", err: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class _Host:
    """A stateful fake of GitHub and the local checkout behind `gh`/`git`, keyed by PR URL
    (issue #531).

    Commits form a small graph (`parents`). On the host, `main` starts as one commit and
    every PR's head is a commit branched off it. The checkout has only what it fetched:
    `git fetch <base_remote>` copies `main`'s history and moves `<base_remote>/main` to it,
    `git fetch origin` copies the PR heads' (one remote does both on an own-repo checkout),
    and `git rev-parse` / `git merge-base --is-ancestor` answer from that copy — exit 128
    for a commit it lacks, as git does. `gh pr merge` merges whatever head it is given, like
    a host whose branch protection does not require up-to-date branches (where a stale PR
    lands silently), unless it is pinned (`--match-head-commit`) to a SHA that is no longer
    the head, or `strict` is set and the head lacks `main`'s tip. `gh pr update-branch`
    makes a merge commit of the head and `main`, which lands after `lag[url]` further
    `gh pr view` reads of that PR (0: at once; None: never). `_merge_one`'s sleeps are
    logged in `calls` as `["sleep", secs]`; every `gh pr view` / `gh pr checks` read is
    logged in `views` / `checks` with the head it reported or described."""

    def __init__(self, *urls: str, strict: bool = False, base_remote: str = "origin") -> None:
        self.parents: dict[str, tuple[str, ...]] = {}
        self.main = self._commit()
        self.head = {u: self._commit(self.main) for u in urls}
        self.local = set(self.parents)            # publish pushed the heads from this clone
        self.base_remote = base_remote
        self.tracking = {base_remote: self.main}  # `<base_remote>/main` in the checkout
        self.strict = strict
        self.calls: list[list] = []
        self.views: list[tuple[str, str]] = []            # (url, head it reported)
        self.checks: list[tuple[str, str]] = []           # (url, head the rollup described)
        self.merged: list[tuple[str, str | None]] = []    # (url, SHA the merge was pinned to)
        self.landed: dict[str, str] = {}                  # url -> its merge commit on main
        self.updated: dict[str, str] = {}                 # url -> head its update produced
        self.lag: dict[str, int | None] = {}
        self.red_after_update: set[str] = set()           # urls whose updated head is red
        self.update_fails: set[str] = set()
        self.push_after_checks: dict[str, int] = {}       # url -> pushed to after read N
        self.move_main_after_checks: dict[str, int] = {}  # url -> main moves after read N
        self.push_at_merge: set[str] = set()              # a push races the merge itself
        self.fetch_fails = False
        self.git_breaks = False
        self._pending: dict[str, list] = {}               # url -> [reads left, new head]
        self._red: set[str] = set()

    def _commit(self, *parents: str) -> str:
        sha = f"{len(self.parents) + 1:040x}"
        self.parents[sha] = parents
        return sha

    def history(self, sha: str) -> set[str]:
        todo, seen = [sha], set()
        while todo:
            c = todo.pop()
            if c not in seen:
                seen.add(c)
                todo.extend(self.parents[c])
        return seen

    def contains(self, head: str, tip: str) -> bool:
        return tip in self.history(head)

    def land_on_main(self) -> str:
        """A commit lands on `main` from outside the run."""
        self.main = self._commit(self.main)
        return self.main

    def sleep(self, secs: int) -> None:
        self.calls.append(["sleep", secs])

    def __call__(self, cmd: list[str], **kw) -> SimpleNamespace:
        self.calls.append(list(cmd))
        if cmd[0] == "git":
            return self._git(cmd[3:])                    # drop `git -C <checkout>`
        verb, url = cmd[2], cmd[3]
        if verb == "view":
            self._land_update(url)
            self.views.append((url, self.head[url]))
            return _res(out=json.dumps({"headRefOid": self.head[url], "baseRefName": "main"}))
        if verb == "update-branch":
            return self._update(url)
        if verb == "checks":
            return self._checks(url)
        if verb == "merge":
            return self._merge(url, cmd)
        return _res()                                    # ready / ready --undo

    def _git(self, args: list[str]) -> SimpleNamespace:
        if args[0] == "fetch":
            if self.fetch_fails:
                return _res(128, err="fatal: unable to access 'https://github.com/org/repo/': "
                                     "Could not resolve host: github.com")
            if args[1] == self.base_remote:
                self.local |= self.history(self.main)
                self.tracking[args[1]] = self.main
            if args[1] == "origin":
                for head in self.head.values():
                    self.local |= self.history(head)
            return _res()
        if args[0] == "rev-parse":
            remote, _, branch = args[-1].removesuffix("^{commit}").partition("/")
            sha = self.tracking.get(remote) if branch == "main" else None
            return _res(out=sha + "\n") if sha else _res(1)
        if args[:2] == ["merge-base", "--is-ancestor"]:
            tip, head = args[2:4]
            if self.git_breaks:
                return _res(128, err=f"fatal: bad object {head}")
            missing = [c for c in (tip, head) if c not in self.local]
            if missing:
                return _res(128, err=f"fatal: Not a valid commit name {missing[0]}")
            return _res(0 if self.contains(head, tip) else 1)
        raise AssertionError(f"unexpected git call: {args}")

    def _land_update(self, url: str) -> None:
        pending = self._pending.get(url)
        if pending is None or pending[0] is None:
            return
        if pending[0]:
            pending[0] -= 1
        else:
            self.head[url] = pending[1]
            del self._pending[url]

    def _update(self, url: str) -> SimpleNamespace:
        if url in self.update_fails:
            return _res(1, err="GraphQL: merge conflict between base and head "
                               "(updatePullRequestBranch)")
        new = self._commit(self.head[url], self.main)    # a merge commit, never a rebase
        self.updated[url] = new
        if url in self.red_after_update:
            self._red.add(new)
        lag = self.lag.get(url, 0)
        if lag == 0:
            self.head[url] = new
        else:
            self._pending[url] = [lag, new]
        return _res()

    def _checks(self, url: str) -> SimpleNamespace:
        head = self.head[url]
        self.checks.append((url, head))
        n = sum(1 for u, _ in self.checks if u == url)
        if self.push_after_checks.get(url) == n:
            self.head[url] = self._commit(head)          # someone pushes to the PR
        if self.move_main_after_checks.get(url) == n:
            self.land_on_main()
        return _rollup(("ci", "fail"), code=1) if head in self._red else _rollup(("ci", "pass"))

    def _merge(self, url: str, cmd: list[str]) -> SimpleNamespace:
        if url in self.push_at_merge:
            self.head[url] = self._commit(self.head[url])   # pushed after the last read
        head = self.head[url]
        pin = cmd[cmd.index("--match-head-commit") + 1] if "--match-head-commit" in cmd else None
        if pin is not None and pin != head:
            return _res(1, err="GraphQL: Head branch was modified. Review and try the merge "
                               "again. (mergePullRequest)")
        if self.strict and not self.contains(head, self.main):
            return _res(1, err="GraphQL: Head branch is out of date. Review and try the "
                               "merge again. (mergePullRequest)")
        self.main = self._commit(self.main, head)
        self.merged.append((url, pin))
        self.landed[url] = self.main
        return _res()


class MergeAgainstCurrentBase(unittest.TestCase):
    """Issue #531: a green rollup describes the head it ran on, built on the base that head
    contains. A wave's members merge back-to-back, so the second member's green predates the
    first member's merge, and merging it lands a combination nothing verified. Every test
    drives the production `merge.merge_wave` against `_Host`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        self._n = 0

    def _run(self, host: _Host, urls: list[str], cfg: Config | None = None,
             method: str = "merge") -> tuple[int, str]:
        cfg = cfg or self.cfg
        bundles = []
        for url in urls:                                 # distinct PR URL per bundle
            self._n += 1
            d = cfg.bundle(f"S{self._n}")
            d.mkdir(parents=True)
            (d / "patch.diff").write_text("diff\n", encoding="utf-8")
            (d / "publish.json").write_text(
                json.dumps({"pr_url": url, "repo": "org/repo"}), encoding="utf-8")
            bundles.append(d)
        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=host), \
                mock.patch.object(merge, "_sleep", side_effect=host.sleep), \
                mock.patch.object(merge.state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merge.merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(cfg, bundles, method=method)
        return rc, err.getvalue()

    @staticmethod
    def _at(host: _Host, cmd: list[str]) -> int:
        """Index in ``host.calls`` of the first call that starts with ``cmd``."""
        return next(i for i, c in enumerate(host.calls) if c[:len(cmd)] == cmd)

    def test_second_member_is_updated_and_reverified_before_it_merges(self) -> None:
        # (a): A and B are each green against the original base. Once A merges, B is
        # behind: its rollup never saw A. B must be updated (a merge commit, never a
        # rebase), its NEW head's rollup read green, and only then merged, pinned to it.
        host = _Host(PR_A, PR_B)
        a_head, b_reviewed = host.head[PR_A], host.head[PR_B]
        rc, err = self._run(host, [PR_A, PR_B])
        self.assertEqual(rc, 0, err)
        self.assertIn(["gh", "pr", "update-branch", PR_B], host.calls,
                      "B was merged without being brought up to date with A's merge")
        b_updated = host.updated[PR_B]
        # The update is a merge commit of the reviewed head with the base after A merged.
        self.assertEqual(host.parents[b_updated], (b_reviewed, host.landed[PR_A]))
        self.assertEqual(host.merged, [(PR_A, a_head), (PR_B, b_updated)])
        update = self._at(host, ["gh", "pr", "update-branch", PR_B])
        merge_b = self._at(host, ["gh", "pr", "merge", PR_B])
        self.assertLess(self._at(host, ["gh", "pr", "merge", PR_A]), update)
        reads_b = [i for i, c in enumerate(host.calls) if c[:4] == ["gh", "pr", "checks", PR_B]]
        self.assertTrue(reads_b)
        self.assertTrue(all(update < i < merge_b for i in reads_b))
        self.assertEqual({h for u, h in host.checks if u == PR_B}, {b_updated})
        self.assertEqual(host.calls[merge_b],
                         ["gh", "pr", "merge", PR_B, "--merge", "--match-head-commit", b_updated])
        # A was up to date: not updated. Nothing was ever rebased.
        self.assertNotIn(["gh", "pr", "update-branch", PR_A], host.calls)
        self.assertFalse(any("--rebase" in c for c in host.calls))

    def test_update_is_a_merge_commit_whatever_the_merge_method(self) -> None:
        # merge_method governs only the final merge; the update never rebases the commits
        # sign-off reviewed.
        for method in ("merge", "squash", "rebase"):
            with self.subTest(merge_method=method):
                host = _Host(PR_A, PR_B)
                rc, err = self._run(host, [PR_A, PR_B], method=method)
                self.assertEqual(rc, 0, err)
                updates = [c for c in host.calls if c[:3] == ["gh", "pr", "update-branch"]]
                self.assertEqual(updates, [["gh", "pr", "update-branch", PR_B]])
                self.assertIn(["gh", "pr", "merge", PR_B, f"--{method}",
                               "--match-head-commit", host.updated[PR_B]], host.calls)

    def test_head_changed_after_the_green_read_is_not_merged(self) -> None:
        # (b): someone pushes to the PR right after the read that confirmed its green. That
        # green described the OLD head; the new one was never verified.
        host = _Host(PR_A)
        host.push_after_checks[PR_A] = 2
        rc, err = self._run(host, [PR_A])
        self.assertEqual(rc, 1)
        self.assertEqual(host.merged, [])
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in host.calls))
        self.assertIn("head changed", err)
        self.assertIn(["gh", "pr", "ready", PR_A, "--undo"], host.calls)

    def test_pinned_merge_refused_when_the_head_moves_at_merge_time(self) -> None:
        # (b): the push races the merge itself, after the driver's last read. The pin makes
        # the host refuse it instead of merging a head nobody verified.
        host = _Host(PR_A)
        host.push_at_merge.add(PR_A)
        verified = host.head[PR_A]
        rc, err = self._run(host, [PR_A])
        self.assertEqual(rc, 1)
        self.assertEqual(host.merged, [])
        self.assertEqual([c for c in host.calls if c[:3] == ["gh", "pr", "merge"]],
                         [["gh", "pr", "merge", PR_A, "--merge", "--match-head-commit", verified]])
        self.assertIn("did not merge", err)
        self.assertIn(["gh", "pr", "ready", PR_A, "--undo"], host.calls)

    def test_merge_requires_required_call_log_is_unchanged(self) -> None:
        # (c): "trust the host's protection" — no base read, no update, no rollup read, no
        # pin: exactly the call log this setting produced before #531, stale merge included.
        host = _Host(PR_A, PR_B)
        cfg = _cfg(self.tmp, merge_requires="required")
        rc, err = self._run(host, [PR_A, PR_B], cfg)
        self.assertEqual(rc, 0, err)
        repo = str(merge.publish._checkout_path(cfg, "org/repo"))
        self.assertEqual(host.calls, [
            ["gh", "pr", "ready", PR_A],
            ["gh", "pr", "merge", PR_A, "--merge"],
            ["git", "-C", repo, "fetch", "origin"],
            ["gh", "pr", "ready", PR_B],
            ["gh", "pr", "merge", PR_B, "--merge"],
        ])

    def test_update_that_lands_late_still_merges_pinned_to_the_updated_head(self) -> None:
        # GitHub may apply `gh pr update-branch` after the command returns: the two reads
        # straight after it still show B's old head. The driver keeps polling, within
        # merge_wait_secs, rather than refusing.
        host = _Host(PR_A, PR_B)
        host.lag[PR_B] = 2
        b_reviewed = host.head[PR_B]
        rc, err = self._run(host, [PR_A, PR_B])
        self.assertEqual(rc, 0, err)
        self.assertIn(PR_B, host.updated)
        b_updated = host.updated[PR_B]
        # Read before the update, twice while it had not landed, once when it had, and
        # once more after the rollup wait.
        self.assertEqual([h for u, h in host.views if u == PR_B],
                         [b_reviewed, b_reviewed, b_reviewed, b_updated, b_updated])
        update = self._at(host, ["gh", "pr", "update-branch", PR_B])
        first_read = self._at(host, ["gh", "pr", "checks", PR_B])
        self.assertTrue(any(c[0] == "sleep" for c in host.calls[update:first_read]))
        self.assertEqual({h for u, h in host.checks if u == PR_B}, {b_updated})
        self.assertEqual(host.merged[-1], (PR_B, b_updated))

    def test_update_that_never_lands_refuses_readiness_undone(self) -> None:
        host = _Host(PR_A, PR_B)
        host.lag[PR_B] = None
        rc, err = self._run(host, [PR_A, PR_B])
        self.assertEqual(rc, 1)
        self.assertEqual([u for u, _ in host.merged], [PR_A])   # B is NOT merged stale
        self.assertIn("had not landed", err)
        self.assertIn(["gh", "pr", "ready", PR_B, "--undo"], host.calls)
        self.assertNotIn(PR_B, [u for u, _ in host.checks])     # no rollup read of it
        update = self._at(host, ["gh", "pr", "update-branch", PR_B])
        polled = [c[1] for c in host.calls[update:] if c[0] == "sleep"]
        self.assertTrue(polled)                                 # it did wait for it...
        self.assertLessEqual(sum(polled), self.cfg.merge_wait_secs)   # ...within the bound

    def test_updated_head_that_is_not_green_is_not_merged(self) -> None:
        # The combination is red although A and B were each green alone.
        host = _Host(PR_A, PR_B)
        host.red_after_update.add(PR_B)
        rc, err = self._run(host, [PR_A, PR_B])
        self.assertEqual(rc, 1)
        self.assertEqual([u for u, _ in host.merged], [PR_A])
        self.assertIn("FAILING", err)
        self.assertIn(PR_B, host.updated)
        self.assertIn((PR_B, host.updated[PR_B]), host.checks)  # the red read was of it
        self.assertIn(["gh", "pr", "ready", PR_B, "--undo"], host.calls)

    def test_update_that_fails_stops_the_wave_readiness_undone(self) -> None:
        host = _Host(PR_A, PR_B)
        host.update_fails.add(PR_B)
        rc, err = self._run(host, [PR_A, PR_B])
        self.assertEqual(rc, 1)
        self.assertEqual([u for u, _ in host.merged], [PR_A])
        self.assertIn("could not be brought up to date", err)
        self.assertIn(["gh", "pr", "ready", PR_B, "--undo"], host.calls)

    def test_first_member_behind_from_outside_the_run_is_updated(self) -> None:
        # The base can move between waves or from outside the run: the first PR merged is
        # held to the same rule.
        host = _Host(PR_A)
        outside = host.land_on_main()
        rc, err = self._run(host, [PR_A])
        self.assertEqual(rc, 0, err)
        self.assertIn(PR_A, host.updated)
        self.assertTrue(host.contains(host.updated[PR_A], outside))
        self.assertEqual(host.merged, [(PR_A, host.updated[PR_A])])

    def test_strict_host_completes_a_multi_member_wave(self) -> None:
        # On a host that requires up-to-date branches the second merge used to be refused,
        # stopping the wave after its first merge.
        host = _Host(PR_A, PR_B, strict=True)
        rc, err = self._run(host, [PR_A, PR_B])
        self.assertEqual(rc, 0, err)
        self.assertEqual([u for u, _ in host.merged], [PR_A, PR_B])

    def test_base_that_moves_while_checks_are_read_is_not_merged(self) -> None:
        # The last read before `gh pr merge` must find the head up to date.
        host = _Host(PR_A)
        host.move_main_after_checks[PR_A] = 2
        rc, err = self._run(host, [PR_A])
        self.assertEqual(rc, 1)
        self.assertEqual(host.merged, [])
        self.assertIn("base moved", err)
        self.assertIn(["gh", "pr", "ready", PR_A, "--undo"], host.calls)

    def test_failed_fetch_refuses_saying_git_failed(self) -> None:
        host = _Host(PR_A)
        host.fetch_fails = True
        rc, err = self._run(host, [PR_A])
        self.assertEqual(rc, 1)
        self.assertEqual(host.merged, [])
        self.assertIn("git failed: `git fetch origin`", err)
        self.assertIn(["gh", "pr", "ready", PR_A, "--undo"], host.calls)

    def test_merge_base_failure_refuses_saying_git_failed(self) -> None:
        # Exit 0 is up to date and exit 1 is behind; anything else is git failing.
        host = _Host(PR_A)
        host.git_breaks = True
        rc, err = self._run(host, [PR_A])
        self.assertEqual(rc, 1)
        self.assertEqual(host.merged, [])
        self.assertIn("git failed: `git merge-base --is-ancestor", err)
        self.assertIn("exited 128", err)
        self.assertIn(["gh", "pr", "ready", PR_A, "--undo"], host.calls)

    def test_fork_checkout_reads_the_base_and_the_head_from_their_remotes(self) -> None:
        # base_remote holds the base; the PR branch (and the update's merge commit) lives on
        # origin, where publish pushed it. Both are fetched.
        cfg = _cfg(self.tmp)
        cfg.base_remote = "upstream"
        host = _Host(PR_A, PR_B, base_remote="upstream")
        rc, err = self._run(host, [PR_A, PR_B], cfg)
        self.assertEqual(rc, 0, err)
        self.assertIn(PR_B, host.updated)
        self.assertEqual(host.merged[-1], (PR_B, host.updated[PR_B]))
        self.assertEqual({c[4] for c in host.calls if c[:1] == ["git"] and c[3] == "fetch"},
                         {"upstream", "origin"})

    def test_update_poll_and_rollup_wait_share_one_budget(self) -> None:
        # "Charged to merge_wait_secs": waiting for the update to land and waiting for the
        # updated head's rollup share ONE bound, so time a slow update took leaves less to
        # confirm a green with (issue #582), never a fresh budget on top of it.
        for budget in (0, 5, 10, 14, 15, 20, 25, 29, 30, 31, 300):
            for lag in (0, 1, 2, 3, None):
                with self.subTest(budget=budget, lag=lag):
                    host = _Host(PR_A)
                    host.land_on_main()                      # behind from the start
                    host.lag[PR_A] = lag
                    rc, err = self._run(host, [PR_A], _cfg(self.tmp, merge_wait_secs=budget))
                    self.assertIn(["gh", "pr", "update-branch", PR_A], host.calls)
                    sleeps = [c[1] for c in host.calls if c[0] == "sleep"]
                    self.assertLessEqual(sum(sleeps), budget)
                    if lag is None:
                        self.assertEqual(host.merged, [])
                    if budget == 300 and lag is not None:
                        self.assertEqual(host.merged, [(PR_A, host.updated[PR_A])])
                    if not host.merged:
                        self.assertEqual(rc, 1)
                        self.assertIn(["gh", "pr", "ready", PR_A, "--undo"], host.calls)
                        continue
                    self.assertEqual(rc, 0, err)
                    self.assertEqual(host.merged, [(PR_A, host.updated[PR_A])])
                    first_read = self._at(host, ["gh", "pr", "checks", PR_A])
                    polled = sum(c[1] for c in host.calls[:first_read] if c[0] == "sleep")
                    # Merged only with a full poll interval (15 s) left after the update to
                    # confirm the green — or with the wait off (0): one read, as before.
                    self.assertTrue(budget == 0 or budget - polled >= 15,
                                    f"merged with {budget - polled}s left after polling")


if __name__ == "__main__":
    unittest.main()
