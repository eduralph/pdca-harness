"""Per-attempt record ownership (issue #540) — stdlib unittest, no deps.

Run from the template root:
    PYTHONPATH=src python -m unittest tests.test_attempt_ownership -v

A retried reviewer/advisory leaf's ``*.error.log`` used to be written only once the retry
loop ENDED, so two things were broken at once and neither could be fixed alone:

1. while attempt 2 ran there was nothing on disk explaining attempt 1, and a run killed
   mid-retry lost every attempt's account — the file never existed;
2. the *presence* of that file meant "the leaf ran and FAILED" to four readers, so
   flushing it earlier made that sentence false at every one of them — including two
   executable recovery discriminators, which would then retire a leaf the death window
   merely INTERRUPTED and carry the bundle to sign-off with no review of the diff.

These tests drive the shipped wrapper with a stub "leaf" (a Python interpreter that dies
at invocation, i.e. the transient/retry path) and assert the end state from BEHAVIOUR
only — the mid-retry bytes are taken from the production write itself, and the line that
settles a record is read off those bytes, so nothing here re-implements or names the
mechanism under test.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import assemble, leaves, state
from pdca_harness.config import Config, LeafConfig

# A claude-family leaf so the stream path engages (the only path yielding the "did a
# session start" signal); argv is a Python interpreter running this script. It writes to
# stderr only and exits non-zero — no stdout, so no session started: the transient-infra
# signal the wrapper retries. From its SECOND invocation on it copies the bundle's error
# log for this leaf aside (with the identity of the file it read), which is a leaf
# OBSERVING the bundle while the earlier attempts' account should already be there. $SAY
# overrides the line it prints last on stderr.
_STUB = """
import os, shutil, sys

counter, log, snap = os.environ["CNT"], os.environ["LOG"], os.environ["SNAP"]
done = len(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "a").write("x")
if done and os.path.exists(log):
    shutil.copyfile(log, "%s.%d" % (snap, done))
    open("%s.%d.id" % (snap, done), "w").write(str(os.stat(log).st_ino))
sys.stderr.write(os.environ.get("SAY", "overloaded_error 529") + "\\n")
sys.exit(1)
"""

# The same leaf, transient ONCE and then healthy: the retry recovers it, so the run must
# leave no record behind at all (a leaf that succeeded is not a failed one).
_RECOVERS_ON_RETRY = """
import os, sys

counter = os.environ["CNT"]
done = len(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "a").write("x")
if done == 0:
    sys.stderr.write("overloaded_error 529\\n")
    sys.exit(1)
print('{"type": "result"}')
"""

# A run of the shipped wrapper under a file-size limit small enough that the SECOND record
# cannot be written whole — the shape a dying write (ENOSPC, a quota, RLIMIT_FSIZE) leaves,
# reproduced against production rather than simulated. Driven in a CHILD process because
# RLIMIT_FSIZE is process-wide: inside the test runner it would also cut the runner's own
# redirected output. SIGXFSZ is ignored so the write returns EFBIG instead of killing the
# process; only the SOFT limit is lowered, since the hard one cannot be raised back. It
# reports FACTS (bytes on disk), never a verdict — the verdict is the engine's, read back
# in the parent through pre-existing API.
_TORN_WRITE_DRIVER = """
import json, os, resource, signal, sys, tempfile
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import LeafConfig

tmp = Path(os.environ["WORKDIR"])
log = Path(os.environ["LOG"])
snap = os.environ["SNAP"]
sys.stderr = open(os.devnull, "w")   # the tee'd child stderr is not the measurement
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (int(os.environ["LIMIT"]), hard))
leaf = LeafConfig(mode="command", family="claude", interactive=False,
                  argv=[sys.executable, "-c", os.environ["STUB"]])
failed = ""
try:
    leaves._invoke_leaf_resilient(leaf, tmp, "review please", error_log=log,
                                  attempts=3, backoff=0.0, stream_json=True, env=dict(os.environ))
except OSError as exc:
    failed = type(exc).__name__
resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))

def _read(p):
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None

json.dump({"attempts": len(Path(os.environ["CNT"]).read_text()),
           "final_write": failed,
           "log": _read(log),
           "observed": {n: _read(Path(f"{snap}.{n}")) for n in (1, 2)},
           "stray": sorted(p.name for p in log.parent.glob(".*"))},
          sys.stdout)
"""

ADVISORY_ID = "code-review"


def _stub_config(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
    )


class AttemptOwnershipBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- driving the SHIPPED wrapper -------------------------------------------------
    def _drive(self, label: str = "run", *, say: str = "",
               error_log: Path | None = None) -> Exception | None:
        """One full run of ``leaves._invoke_leaf_resilient`` against the retrying stub."""
        self.counter = self.tmp / f"{label}.count"
        self.snap = self.tmp / f"{label}.observed"
        self.log = error_log if error_log is not None else self.tmp / f"{label}.error.log"
        env = {"CNT": str(self.counter), "LOG": str(self.log), "SNAP": str(self.snap)}
        if say:
            env["SAY"] = say
        leaf = LeafConfig(mode="command", family="claude", interactive=False,
                          argv=[sys.executable, "-c", _STUB])
        return leaves._invoke_leaf_resilient(
            leaf, self.tmp, "review please", error_log=self.log,
            attempts=3, backoff=0.0, stream_json=True, env=env)

    def _attempts_made(self) -> int:
        return (len(self.counter.read_text(encoding="utf-8"))
                if self.counter.exists() else 0)

    def _observed(self, after: int) -> str:
        """What the leaf found in the bundle when ``after`` attempts had already failed —
        captured by the stub itself, so the fixture is byte-for-byte the state a kill
        inside the retry loop leaves and cannot drift from the production shape."""
        snap = Path(f"{self.snap}.{after}")
        self.assertTrue(
            snap.exists(),
            f"attempt {after + 1} observed the bundle and found NO record of the "
            f"{after} attempt(s) before it: a run killed inside the retry loop leaves "
            "no post-mortem at all")
        return snap.read_text(encoding="utf-8")

    def _observed_id(self, after: int) -> str:
        """The identity of the file that record was read from."""
        self._observed(after)
        return Path(f"{self.snap}.{after}.id").read_text(encoding="utf-8")

    def _settled(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def _torn_write_run(self, *, limit: int, body: int) -> dict:
        """Drive the wrapper in a child whose writes die part-way (see
        ``_TORN_WRITE_DRIVER``) and return what it found on disk."""
        work = self.tmp / "torn-run"
        work.mkdir()
        env = {"PATH": os.environ.get("PATH", ""),
               "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
               "WORKDIR": str(work), "LOG": str(work / state.REVIEW_ERROR_LOG),
               "SNAP": str(self.tmp / "torn.observed"), "CNT": str(self.tmp / "torn.count"),
               "STUB": _STUB, "SAY": "x" * body, "LIMIT": str(limit)}
        proc = subprocess.run([sys.executable, "-c", _TORN_WRITE_DRIVER], env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, f"the driver died: {proc.stderr[-2000:]}")
        seen = json.loads(proc.stdout)
        seen["observed"] = {int(k): v for k, v in seen["observed"].items()}
        self.assertEqual(seen["final_write"], "OSError",
                         "the file-size limit did not cut the record write — nothing was "
                         "measured (has the record's size outgrown the limit?)")
        return seen

    # --- reading it back the way the engine does -------------------------------------
    def _bundle(self, review_log: str | None = None,
                advisory_log: str | None = None, name: str = "bundle") -> Path:
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        if review_log is not None:
            (d / state.REVIEW_ERROR_LOG).write_text(review_log, encoding="utf-8")
        if advisory_log is not None:
            leaves.advisory_error_log(d, ADVISORY_ID).write_text(
                advisory_log, encoding="utf-8")
        return d

    def _advisory_cfg(self) -> Config:
        cfg = _stub_config(self.tmp)
        cfg.advisory_leaves = [{"id": ADVISORY_ID, "role": "x", "mode": "stub"}]
        return cfg

    def _last_line(self, text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        self.assertTrue(lines, "the record was empty")
        return lines[-1]

    def _before_last_line(self, text: str) -> str:
        """``text`` up to its last non-blank line — the shape a write that died part-way
        through that line leaves behind."""
        return text[:text.rindex(self._last_line(text))]


class EachAttemptsRecordIsOnDiskBeforeTheNext(AttemptOwnershipBase):
    """Criterion 1 — a leaf observing the bundle during attempt 2 finds attempt 1's
    account already there, and each record lands whole."""

    def test_attempt_two_finds_attempt_ones_account(self) -> None:
        err = self._drive()
        self.assertIsInstance(err, leaves.LeafError)
        self.assertEqual(self._attempts_made(), 3)  # the shipped budget, unchanged
        text = self._observed(1)
        self.assertIn("attempt 1", text)             # whose attempt it accounts for
        self.assertIn("overloaded_error 529", text)  # attempt 1's captured stderr
        self.assertNotIn("attempt 2", text)          # …and only the spent attempt

    def test_each_record_lands_whole_and_never_truncates_the_one_on_disk(self) -> None:
        # All-or-nothing: a record is REPLACED, never rewritten in place. A write that
        # dies part-way (ENOSPC, RLIMIT_FSIZE, a killed run) would otherwise leave the
        # log 0-byte or truncated — destroying the very post-mortem the flush exists to
        # leave, and the complete one it overwrote with it. Observable form: each flush
        # lands a NEW file, so no reader can be shown a half-written one.
        self._drive()
        self.assertNotEqual(
            self._observed_id(1), self._observed_id(2),
            "the second record was written over the first IN PLACE — a write that dies "
            "part-way leaves a truncated log and takes the previous record with it")
        self.assertIn("attempt 2", self._observed(2))  # …and it did grow, whole

    @unittest.skipUnless(hasattr(signal, "SIGXFSZ"), "POSIX file-size limits only")
    def test_a_record_a_dying_write_cut_off_leaves_the_last_whole_one(self) -> None:
        # The same property, proved against a write that actually dies part-way instead of
        # its fingerprint: under a file-size limit that fits the first record and not the
        # second, the log must still hold a record some reader saw WHOLE — and the leaf
        # must be recovered, not retired. A record rewritten in place would instead leave a
        # truncated blob nobody ever saw, taking the mid-retry post-mortem with it.
        seen = self._torn_write_run(limit=600, body=300)
        self.assertIsNotNone(
            seen["observed"][1],
            "attempt 2 found no record of attempt 1: a run whose writes are failing "
            "leaves no post-mortem at all")
        self.assertEqual(
            seen["log"], seen["observed"][1],
            "the failed write left something other than the last WHOLE record on disk")
        self.assertTrue(
            leaves.review_never_ran(self._bundle(review_log=seen["log"], name="torn")),
            "a record a dying write cut off retired its leaf instead of re-running it")
        self.assertEqual(seen["attempts"], 3, "failing writes cost the leaf attempts")
        self.assertEqual(seen["stray"], [], "a partial sibling was left in the bundle")

    def test_a_leaf_that_recovers_on_retry_leaves_no_record_behind(self) -> None:
        # The shipped contract's other edge (criterion 4): the flushed record describes a
        # leaf that has since SUCCEEDED, so the successful run must clear it — a bundle
        # carrying an error log beside a produced artifact would be a lie.
        counter = self.tmp / "flaky.count"
        log = self.tmp / "flaky.error.log"
        flaky = LeafConfig(mode="command", family="claude", interactive=False,
                           argv=[sys.executable, "-c", _RECOVERS_ON_RETRY])
        err = leaves._invoke_leaf_resilient(
            flaky, self.tmp, "review please", error_log=log,
            attempts=3, backoff=0.0, stream_json=True, env={"CNT": str(counter)})
        self.assertIsNone(err)
        self.assertEqual(len(counter.read_text(encoding="utf-8")), 2)  # failed, recovered
        self.assertFalse(log.exists(), "a leaf that SUCCEEDED left an error log behind")

    def test_an_unwritable_record_does_not_narrow_the_retry_contract(self) -> None:
        # Criterion 5: the shipped stop rule is the ONLY stop rule. A record that cannot
        # be written (here: a bundle dir that is not there — the same OSError family a
        # read-only dir or ENOSPC raises, through the same call) must not end the run
        # early; the records stay in hand for the loop's final write, which fails exactly
        # as it does on the base. What must NOT change is the attempts.
        with self.assertRaises(OSError):
            self._drive(error_log=self.tmp / "vanished" / state.REVIEW_ERROR_LOG)
        self.assertEqual(self._attempts_made(), 3,
                         "a failed record flush cost the leaf attempts")


class AnUnsettledAccountDoesNotReadAsRanAndFailed(AttemptOwnershipBase):
    """Criterion 2 — every reader treats a record whose leaf has attempts left exactly as
    it treats an absent one, and the operator-facing wording names both shapes."""

    def _assert_recovered(self, log_text: str, why: str) -> None:
        # A fresh bundle per shape: an advisory artifact a previous shape's recovery wrote
        # would otherwise satisfy the next one without the leaf running again.
        self._seen = getattr(self, "_seen", 0) + 1
        d = self._bundle(review_log=log_text, advisory_log=log_text,
                         name=f"bundle-{self._seen}")
        self.assertTrue(leaves.review_never_ran(d), why)
        text = assemble._missing_review_text(d)
        self.assertNotIn("RAN AND FAILED", text)     # …and §6 does not assert it either
        self.assertIn(state.REVIEW_ERROR_LOG, text)  # while still naming the record
        leaves.run_advisory_leaves(d, self._advisory_cfg(), only_missing=True)
        self.assertTrue(leaves.advisory_artifact(d, ADVISORY_ID).exists(),
                        f"the CHECKED-resume skipped an advisory leaf: {why}")

    def test_a_mid_retry_record_recovers_its_leaf(self) -> None:
        self._drive()
        self._assert_recovered(
            self._observed(1),
            "a leaf interrupted mid-retry was retired as 'ran and FAILED' — the bundle "
            "would reach sign-off with no review of the diff at all")

    def test_a_torn_or_empty_record_recovers_its_leaf(self) -> None:
        # A record no writer ever finished: 0-byte, blank, or cut off part-way. Nothing
        # asserted that the leaf spent its attempts, so nothing may retire it — the same
        # fail-direction as an unreadable log (a needless re-run costs a leaf; the other
        # error costs the review).
        self._drive()
        settled = self._settled()
        head = len(self._before_last_line(settled))  # …up to the closing line
        for cut in ("", "\n  \n", settled[:1], settled[:head // 2], settled[:head],
                    settled[:head + len(self._last_line(settled)) // 2]):
            with self.subTest(cut=cut[-40:]):
                self._assert_recovered(
                    cut, "a record torn off by a dead write retired its leaf")

    def test_a_settled_record_still_means_ran_and_failed(self) -> None:
        # The other half of the same sentence, and the shipped behaviour (#138/#369):
        # once the attempts ARE spent, the log retires the leaf exactly as before.
        self._drive()
        settled = self._settled()
        d = self._bundle(review_log=settled, advisory_log=settled)
        self.assertFalse(leaves.review_never_ran(d))
        self.assertIn("RAN AND FAILED", assemble._missing_review_text(d))
        leaves.run_advisory_leaves(d, self._advisory_cfg(), only_missing=True)
        self.assertFalse(leaves.advisory_artifact(d, ADVISORY_ID).exists(),
                         "a leaf that ran and spent its attempts was re-run")


class ALeafsOwnTextCannotSettleTheRecord(AttemptOwnershipBase):
    """Criterion 3 — whatever distinguishes a spent account from an unfinished one is
    neutralised in the captured stderr the record embeds, and is recognised only as the
    log's LAST NON-BLANK LINE, alone."""

    def _settlement_line(self) -> str:
        """The line a spent leaf's record closes with. Derived, never named — the test
        knows only where it sits."""
        self._drive("first")
        return self._last_line(self._settled())

    def test_only_the_harness_can_close_a_record_with_that_line(self) -> None:
        line = self._settlement_line()
        # A second run whose stub prints exactly that line, alone, last on stderr — the
        # impersonation: the leaf's own text landing in the harness's account of the
        # leaf's own run, once per attempt.
        self._drive("impostor", say=line)
        lines = self._settled().splitlines()
        self.assertEqual(
            [ln.strip() for ln in lines].count(line), 1,
            "the leaf's own output is indistinguishable from the harness's closing line")
        self.assertEqual(self._last_line(self._settled()), line)  # …and it is the last
        # Neutralised, not dropped: every attempt's text survives in the post-mortem
        # (the #420 memory telemetry rides this same channel).
        self.assertEqual(sum(line in ln for ln in lines), self._attempts_made() + 1)

    def test_a_torn_record_carrying_that_line_still_recovers_its_leaf(self) -> None:
        # Where the impersonation would bite: a record cut off right after the leaf's
        # text, with the harness's own closing line never written. It is still not a leaf
        # that spent its attempts, and must not read as one.
        line = self._settlement_line()
        self._drive("impostor", say=line)
        torn = self._before_last_line(self._observed(1))
        d = self._bundle(review_log=torn)
        self.assertTrue(
            leaves.review_never_ran(d),
            "a leaf's own output closed the harness's account of the leaf's own run: a "
            "reviewer that was only INTERRUPTED would be retired, unreviewed")

    def test_the_line_is_recognised_only_whole_and_last(self) -> None:
        line = self._settlement_line()
        settled = self._settled()
        d = self._bundle()
        log = d / state.REVIEW_ERROR_LOG
        # Mid-line, as a substring of a longer line, and no longer last: not settlement.
        for impostor in (f"boom {line} boom\n", f"{line} — quoted by a leaf\n",
                         f"{settled}\ntrailing chatter from a later write\n"):
            with self.subTest(impostor=impostor[-40:]):
                log.write_text(impostor, encoding="utf-8")
                self.assertTrue(leaves.review_never_ran(d),
                                f"read as settled off a partial/non-final line: {impostor!r}")
        # …while the real thing is recognised through trailing blank lines.
        log.write_text(settled + "\n\n  \n", encoding="utf-8")
        self.assertFalse(leaves.review_never_ran(d),
                         "trailing blank lines hid a spent leaf's closing line")


if __name__ == "__main__":
    unittest.main()
