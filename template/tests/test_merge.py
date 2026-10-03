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


def _gh(**by_verb: SimpleNamespace):
    """Build a `subprocess.run` stub: every `gh`/`git` call succeeds, except the verbs
    named here (`checks=`, `ready=`, `merge=`) which return the given result. The default
    rollup is green, so tests that are not about #413 reach `gh pr merge` exactly as they
    did before it."""
    default_checks = _rollup(("ci", "pass"))

    def run(cmd, **kw):
        if cmd[:2] == ["gh", "pr"]:
            return by_verb.get(cmd[2], default_checks if cmd[2] == "checks"
                               else SimpleNamespace(returncode=0, stdout="", stderr=""))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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
        self.assertIn(["gh", "pr", "merge", "https://gh/pr/1", "--squash"], runs)
        self.assertTrue(any("fetch" in c for c in runs))   # base refreshed after merge

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
        self.assertEqual(gh[-1], ["gh", "pr", "merge", "https://gh/pr/1", "--merge"])

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
        self.assertNotIn(["gh", "pr", "merge", "https://gh/pr/1", "--merge"], runs)

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
            return SimpleNamespace(returncode=0, stdout="", stderr="")

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
        self.assertIn(["gh", "pr", "merge", "https://gh/pr/1", "--merge"], calls)
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
        self.assertEqual(gh, [["gh", "pr", "ready"], ["gh", "pr", "checks"],
                              ["gh", "pr", "checks"], ["gh", "pr", "merge"]])

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
            return SimpleNamespace(returncode=0, stdout="", stderr="")

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
            return SimpleNamespace(returncode=0, stdout="", stderr="")

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
        self.assertEqual(gh, [["gh", "pr", "ready"]] + [["gh", "pr", "checks"]] * 4
                         + [["gh", "pr", "merge"]])     # merge strictly after read 4
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


if __name__ == "__main__":
    unittest.main()
