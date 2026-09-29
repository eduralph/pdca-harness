"""The Do builder retries like every other leaf, and a failed Do says what it left (#537).

The reviewer and both advisory leaves run under `_invoke_leaf_resilient` (#138): a
transient death — the child exits non-zero before emitting any work, or on its own report of
a transient API error (#539) — is retried, bounded, with backoff. The builder called plain
`_invoke`, so no builder failure was ever retried, on the leaf where an attempt costs the
most; and a Do whose attempts ran out printed an argv and an exit status, while what the
operator should do next depends on what is left in the bundle — a bundle holding patch.diff
reads BUILT, and a plain re-run runs Check on it.

Every test here drives the real `leaves.do_build` with a stub "leaf" that is a Python
interpreter (the harness from test_leaf_resilience.py, copied, not imported). The stub
counts its own invocations into $CNT, keeps the prompt each one was sent on stdin, and
keeps a copy of the bundle's build.error.log as each one found it. Only pre-existing API is
imported, so with the production change reverted every test still loads and fails on its
assertions. The backoff is recorded, not slept: `leaves.time` is replaced by a clock whose
`sleep` only notes the delay, which also lets the tests check the shipped schedule.

Offline: no model, no network. Run from the template root:
    PYTHONPATH=src python -m unittest tests.test_builder_retry -v
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pdca_harness import leaves, state, worktree
from pdca_harness.config import Config, LeafConfig

# --- the stub-leaf harness, copied from test_leaf_resilience.py:26-35 -------------------
# A claude-family leaf so the stream path engages (the only path that yields the "did a
# session start" signal). argv is a Python interpreter running an inline script; `_invoke`
# appends the stream flags (ignored) and feeds the prompt on stdin. Every variant first
# runs `_RECORD`: count this invocation into $CNT, keep the prompt it was sent in
# $PROMPTS.<n>, and copy the bundle's build.error.log (if any) to $SEEN.<n>.
_RECORD = (
    "import os,sys,shutil; open(os.environ['CNT'],'a').write('x'); "
    "n=len(open(os.environ['CNT']).read()); "
    "open(os.environ['PROMPTS']+'.%d'%n,'w').write(sys.stdin.read()); "
    "log=os.path.join(os.environ['BUNDLE'],'build.error.log'); "
    "os.path.exists(log) and shutil.copyfile(log, os.environ['SEEN']+'.%d'%n); "
)
_TRANSIENT = _RECORD + (  # dies at invocation: only stderr, no stdout → no session started
    "sys.stderr.write('overloaded_error 529\\n'); sys.exit(1)"
)
_SUBSTANTIVE = _RECORD + (  # ran (emitted a stream event on stdout) then failed
    "print('{\"type\": \"assistant\"}'); sys.stderr.write('boom\\n'); sys.exit(1)"
)
# Transient on the first invocation, healthy on the second: the retry absorbs it.
_RECOVERS = _RECORD + (
    "n==1 and (sys.stderr.write('overloaded_error 529\\n'), sys.exit(1)); "
    "print('{\"type\": \"result\"}')"
)
# Transient on the first invocation ("first-death"), then a builder that did work — wrote
# a partial patch.diff and build-notes.md — and failed on the merits ("second-death").
_TRANSIENT_THEN_SUBSTANTIVE = _RECORD + (
    "n==1 and (sys.stderr.write('first-death\\n'), sys.exit(1)); "
    "b=os.environ['BUNDLE']; "
    "open(os.path.join(b,'patch.diff'),'w').write('--- a/x\\n+++ b/x\\n'); "
    "open(os.path.join(b,'build-notes.md'),'w').write('half done\\n'); "
    "print('{\"type\": \"assistant\"}'); sys.stderr.write('second-death\\n'); sys.exit(1)"
)
# Does work — a partial patch.diff — then fails on the merits on its only attempt.
_LEAVES_A_PATCH = _RECORD + (
    "open(os.path.join(os.environ['BUNDLE'],'patch.diff'),'w').write('--- a/x\\n'); "
    "print('{\"type\": \"assistant\"}'); sys.stderr.write('boom\\n'); sys.exit(1)"
)


def _leaf(script: str) -> LeafConfig:
    return LeafConfig(mode="command", family="claude",
                      argv=[sys.executable, "-c", script], interactive=False)


def _cfg(root: Path, builder: LeafConfig) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=builder,
        reviewer=LeafConfig(mode="stub", family="codex"),
        worktree=False,          # edit in place — keep the slice free of git
    )


class _Clock:
    """`time` as `leaves` sees it, minus the wait: each backoff is recorded, not slept."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __getattr__(self, name: str):
        return getattr(time, name)


class _BuilderCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cnt = self.tmp / "count.txt"
        self.prompts = self.tmp / "prompt"
        self.seen = self.tmp / "seen"
        self.clock = _Clock()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, script: str) -> None:
        self.cfg = _cfg(self.tmp, _leaf(script))
        self.d = self.cfg.bundle("RETRY")
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(
            "- **Slug:** builder-retry\n- **Test file:** tests/test_x.py\n", encoding="utf-8")
        self.log = self.d / leaves.BUILD_ERROR_LOG

    def _do(self, script: str, *, fails: bool = True) -> str:
        """Run Do once with ``script`` as the builder; return what it printed to stderr."""
        self._bundle(script)
        env = {"CNT": str(self.cnt), "PROMPTS": str(self.prompts), "SEEN": str(self.seen),
               "BUNDLE": str(self.d)}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(leaves, "time", self.clock), \
                redirect_stderr(err):
            if fails:
                with self.assertRaises(leaves.LeafError):  # still re-raised for _isolate
                    leaves.do_build(self.d, self.cfg)
            else:
                try:
                    leaves.do_build(self.d, self.cfg)
                except leaves.LeafError as exc:
                    self.fail(f"Do failed on a death a retry should have absorbed: {exc}")
        return err.getvalue()

    def _runs(self) -> int:
        return len(self.cnt.read_text()) if self.cnt.exists() else 0

    def _prompt(self, n: int) -> str:
        path = Path(f"{self.prompts}.{n}")
        self.assertTrue(path.exists(), f"the builder was never spawned a {n}. time")
        return path.read_text(encoding="utf-8")

    def _seen(self, n: int) -> str | None:
        path = Path(f"{self.seen}.{n}")
        return path.read_text(encoding="utf-8") if path.exists() else None


class TheBuilderRetriesUnderTheSharedContract(_BuilderCase):
    """Criterion (i): a transient builder death is retried — bounded, with the shipped
    backoff — and a substantive one is not; the final failure is still re-raised."""

    def test_a_transient_builder_death_is_retried_with_the_shipped_backoff(self) -> None:
        self._do(_TRANSIENT)
        self.assertEqual(self._runs(), 3, "a transient builder death must be retried up to "
                                          "the shipped attempt budget")
        self.assertEqual(self.clock.slept, [4.0, 8.0], "the shipped backoff, unchanged")
        text = self.log.read_text(encoding="utf-8")
        for n in (1, 2, 3):
            self.assertIn(f"attempt {n}", text)
        self.assertIn("overloaded_error 529", text)

    def test_a_transient_death_the_retry_absorbs_is_a_successful_build(self) -> None:
        out = self._do(_RECOVERS, fails=False)
        self.assertEqual(self._runs(), 2, "the retry is what absorbed the dropped attempt")
        self.assertFalse(self.log.exists(), "a Do that succeeded left an error log behind")
        self.assertNotIn("Do failed", out)

    def test_a_substantive_builder_failure_is_still_not_retried(self) -> None:
        self._do(_SUBSTANTIVE)
        self.assertEqual(self._runs(), 1, "a builder that failed on the merits is not retried")
        self.assertEqual(self.clock.slept, [])
        self.assertIn("boom", self.log.read_text(encoding="utf-8"))


class ARetriedBuilderIsToldItsPredecessorDied(_BuilderCase):
    """Criterion (ii): attempt 1's prompt is today's, byte for byte; a retry is told the
    attempt before it died mid-flight and that what it finds is residue, not a finished
    build — and the record it is pointed at is really there when it starts."""

    def test_attempt_one_is_unchanged_and_each_retry_carries_the_notice(self) -> None:
        self._do(_TRANSIENT)
        first = self._prompt(1)
        self.assertEqual(first, leaves._build_prompt(self.d, self.cfg),
                         "attempt 1 must be sent exactly the prompt every Do is sent today")
        self.assertNotIn("RETRY NOTICE", first)
        for n in (2, 3):
            retry = self._prompt(n)
            self.assertTrue(retry.startswith(first), "the task itself must not change")
            notice = retry[len(first):]
            self.assertIn("RETRY NOTICE", notice)
            self.assertIn(f"attempt {n}", notice)
            self.assertIn("MID-FLIGHT", notice)
            for artifact in ("patch.diff", "build-notes.md", "test file"):
                self.assertIn(artifact, notice)
            self.assertIn("RESIDUE", notice)
            self.assertIn("Never treat any of it as evidence the work is done", notice)
            self.assertIn(str(self.log), notice)
        self.assertIn("previous 2 attempts", self._prompt(3))

    def test_the_record_a_retry_is_pointed_at_is_on_disk_when_it_starts(self) -> None:
        self._do(_TRANSIENT)
        self.assertIsNone(self._seen(1), "attempt 1 must find no stale record")
        record = self._seen(2)
        self.assertIsNotNone(record, "attempt 2 was told of a record that was not there")
        self.assertIn("attempt 1", record)
        self.assertIn("overloaded_error 529", record)


class EveryFailedDoSaysWhatItLeft(_BuilderCase):
    """Criterion (iii): every failed Do — transient, substantive or setup — names the
    residue on disk and the next action true for it; only a transient failure gets the
    transient sentence, and it names the attempts actually spent."""

    def test_a_patch_left_behind_reads_built_and_a_rerun_runs_check(self) -> None:
        out = self._do(_LEAVES_A_PATCH)
        self.assertEqual(self._runs(), 1)
        self.assertTrue((self.d / "patch.diff").exists(), "nothing may delete the residue")
        self.assertEqual(state.state(self.d), state.BUILT)
        self.assertIn("left in the bundle: patch.diff", out)
        self.assertIn(f"now reads {state.BUILT}", out)
        self.assertIn("pdca run RETRY", out)
        self.assertIn("runs CHECK on that unfinished patch.diff, not Do", out)
        self.assertNotIn("— transient:", out)

    def test_a_transient_failure_names_the_attempts_it_actually_spent(self) -> None:
        out = self._do(_TRANSIENT)
        self.assertIn("— transient:", out)
        self.assertIn(f"not absorbed after {self._runs()} attempt(s)", out)
        self.assertIn("no patch.diff, build-notes.md or brief-named test file", out)
        self.assertIn(f"still reads {state.PLANNED}", out)
        self.assertIn("starts Do again", out)
        self.assertNotIn(state.BUILT, out)

    def test_a_substantive_failure_after_a_transient_one_gets_no_transient_sentence(
            self) -> None:
        out = self._do(_TRANSIENT_THEN_SUBSTANTIVE)
        self.assertEqual(self._runs(), 2)
        self.assertNotIn("— transient:", out)
        self.assertIn("left in the bundle: patch.diff, build-notes.md", out)
        self.assertIn(f"now reads {state.BUILT}", out)

    def test_a_do_that_died_in_setup_reports_too(self) -> None:
        self._bundle(_TRANSIENT)
        self.cfg.worktree = True
        boom = worktree.WorktreeError("RETRY: base ref 'origin/main' does not resolve")
        err = io.StringIO()
        with mock.patch.object(leaves.worktree, "ensure", side_effect=boom), \
                mock.patch.object(leaves, "time", self.clock), redirect_stderr(err):
            with self.assertRaises(worktree.WorktreeError):
                leaves.do_build(self.d, self.cfg)
        out = err.getvalue()
        self.assertEqual(self._runs(), 0)  # the leaf never launched
        self.assertIn("does not resolve", self.log.read_text(encoding="utf-8"))
        self.assertIn(f"still reads {state.PLANNED}", out)
        self.assertNotIn("— transient:", out)


class TheRetriesRecordSurvivesTheFinalFailure(_BuilderCase):
    """Criterion (iv): `do_build`'s outer capture must not overwrite the per-attempt
    record the retries built."""

    def test_both_attempts_are_still_in_the_log(self) -> None:
        out = self._do(_TRANSIENT_THEN_SUBSTANTIVE)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("attempt 1", text)
        self.assertIn("first-death", text)
        self.assertIn("attempt 2", text)
        self.assertIn("second-death", text)
        self.assertIn("per-attempt record is in build.error.log", out)


class ASuccessfulBuildIsSpawnedAsToday(_BuilderCase):
    """Criterion (v): one spawn, today's prompt, today's arguments — including the #420
    memory log, now derived by the wrapper instead of named at the call site."""

    def test_one_spawn_with_todays_arguments(self) -> None:
        self._bundle(_TRANSIENT)
        with mock.patch.object(leaves, "_invoke", return_value=None) as spawn, \
                mock.patch.object(leaves, "time", self.clock):
            leaves.do_build(self.d, self.cfg)
        spawn.assert_called_once()
        args, kwargs = spawn.call_args
        self.assertEqual(args[2], leaves._build_prompt(self.d, self.cfg))
        self.assertEqual(kwargs["memory_log"], self.d / leaves.BUILD_MEMORY_LOG)
        self.assertEqual(kwargs["label"], f"Do {self.d.name}")
        self.assertTrue(kwargs["stream_json"])
        self.assertFalse(self.log.exists())
        self.assertEqual(self.clock.slept, [])


if __name__ == "__main__":
    unittest.main()
