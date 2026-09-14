"""A leaf's own account of its death is kept (issue #506) — stdlib unittest, no deps.

`progress.run_with_heartbeat` reads every event a leaf's stream emits and used to keep
none of it: the drain parsed each line for a session flag and a tool label, then dropped
it. The CLI's own marked API-error report arrives as a stream event on **stdout**, so it
was displayed and discarded, and the failed leaf's `*.error.log` fell back to
`(no output captured)` — "a post-mortem artifact that explains nothing" by the harness's
own rule — with the cause legible only in the CLI's session transcript under
`~/.claude/projects/`, which no post-mortem reads.

What this module holds the harness to:

* the marked report reaches the leaf's `*.error.log`, by the same route the stderr tail
  takes (appended to what `run_with_heartbeat` returns as `output`);
* retention is **unconditional** — every marked report is kept whatever its cause,
  including one the session then recovered from and one the vendor marked permanent;
* a report the CLI forwarded for a **sub-agent** is kept *labelled as such*, in both
  spellings of that scope (`parent_tool_use_id`, `isSidechain`);
* where several candidate records arrive, the one nearest the leaf's own death wins, in
  either arrival order;
* **nothing is classified**: `LeafError.transient` and the retry counts are what they
  were, and the guards below assert the invocation counts explicitly so a later change
  cannot silently move them;
* nothing else changes: `capture` still returns the child's raw stdout unmodified, the
  codex stream format and a stream-less family degrade to today's behaviour, and a leaf
  that exits 0 is spawned and reported exactly as today;
* what is kept is bounded like the stderr tail, and a report the bound CUT says it was
  cut — a fragment must never read as the leaf's whole account of its death;
* the drain decodes each stream line **once** and classifies it three ways, rather than
  each classifier re-parsing the same bytes in what is a hot loop.

Every case is driven by a stub "leaf" that is a Python interpreter emitting chosen stream
events — no vendor CLI, no API key, no network. The cases that replay pinned vendor bytes
skip themselves when `tests/fixtures/` is absent (a red leg reverts it as production).

Run from the project root: PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import leaves, progress
from pdca_harness.config import LeafConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A claude-family leaf so the stream path engages (the only path that reads events at
# all). Shape copied from tests/test_leaf_resilience.py: argv is a python interpreter
# running an inline script; `_invoke` appends --output-format/--verbose (ignored) and
# feeds the prompt on stdin. The script counts its own invocations into $CNT so a test
# can assert the retry count, writes $STREAM to stdout (the events), $ERR to stderr and
# exits $RC.
_STUB = (
    "import os,sys\n"
    "open(os.environ['CNT'],'a').write('x')\n"
    "sys.stdout.write(os.environ['STREAM'])\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write(os.environ['ERR'])\n"
    "sys.exit(int(os.environ['RC']))\n"
)

# ---------------------------------------------------------------------------------
# The event records. Shapes are the vendor's (see tests/fixtures/README.md, which pins
# the observed records and quotes the emitters every field here comes from); the prose
# is trimmed to what a test needs to tell one record from another.
# ---------------------------------------------------------------------------------
_WORK_TEXT = "Editing progress.py"
_WORK = {"type": "assistant", "parent_tool_use_id": None,
         "message": {"content": [{"type": "text", "text": _WORK_TEXT}]}}

_DEATH_TEXT = ("API Error: Connection lost mid-response. "
               "The response above may be incomplete.")
_REPORT = {"type": "assistant", "parent_tool_use_id": None, "error": "server_error",
           "is_api_error_message": True,
           "message": {"content": [{"type": "text", "text": _DEATH_TEXT}]}}

_PERMANENT_TEXT = ("There's an issue with the selected model "
                   "(claude-3-5-haiku-20241022). It may not exist or you may not "
                   "have access to it.")
_PERMANENT = {"type": "assistant", "parent_tool_use_id": None, "error": "model_not_found",
              "is_api_error_message": True,
              "message": {"content": [{"type": "text", "text": _PERMANENT_TEXT}]}}

_SUBAGENT_TEXT = "API Error: overloaded (the Task's own connection)"
_SUBAGENT = {"type": "assistant", "parent_tool_use_id": "toolu_01TaskUse",
             "error": "overloaded", "is_api_error_message": True,
             "subagent_type": "builder",
             "message": {"content": [{"type": "text", "text": _SUBAGENT_TEXT}]}}
# The same record in the persisted transcript's spelling of both the mark and the scope:
# it carries no `parent_tool_use_id` key at all, so `isSidechain` is the only thing that
# says whose report it is.
_SIDECHAIN = {"type": "assistant", "isSidechain": True, "error": "overloaded",
              "isApiErrorMessage": True,
              "message": {"content": [{"type": "text", "text": _SUBAGENT_TEXT}]}}

_WRAPUP_TEXT = "[ede_diagnostic] turn aborted (stop_reason=null)"
_WRAPUP = {"type": "result", "subtype": "error_during_execution", "is_error": True,
           "errors": [_WRAPUP_TEXT]}
# The same wrap-up with nothing to say: an error record carrying no text at all. There is
# no evidence in it, so it must produce no banner — a header over an empty line is the
# "(no output captured)" shape in a new costume.
_MUTE_WRAPUP = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                "errors": [], "result": "   "}

# A report far longer than the retained bound: it must be kept as much as the bound
# allows AND say that it was cut, so a fragment is never read as the whole account.
_LONG_HEAD = "API Error: the vendor said a great deal about this one."
_LONG_TAIL = "and here is the part past the bound"
_LONG_TEXT = f"{_LONG_HEAD} {'filler word ' * 200}{_LONG_TAIL}"
_LONG_REPORT = {"type": "assistant", "parent_tool_use_id": None, "error": "server_error",
                "is_api_error_message": True,
                "message": {"content": [{"type": "text", "text": _LONG_TEXT}]}}


def _stream(*events: dict) -> str:
    """The events as the child will print them: one JSON record per line."""
    return "".join(json.dumps(ev) + "\n" for ev in events)


class _CountingJson:
    """Counts the decodes the drain performs, delegating every one to the real
    ``json.loads`` — the production code under it runs unchanged; only the tally is new.

    Patched over ``progress.json`` alone, so nothing else in the process is affected, and
    read after `run_with_heartbeat` has joined its drain thread."""

    def __init__(self) -> None:
        self.loads_calls = 0

    def loads(self, s, *args, **kwargs):
        self.loads_calls += 1
        return json.loads(s, *args, **kwargs)


def _leaf() -> LeafConfig:
    return LeafConfig(mode="command", family="claude",
                      argv=[sys.executable, "-c", _STUB], interactive=False)


@contextlib.contextmanager
def _stdout_fd_to(path: Path):
    """Point THIS process's fd 1 at ``path`` for the block.

    Needed by the stream-less case only: without ``stream_json`` the child inherits the
    terminal's stdout (that is the behaviour under test), so its output would otherwise
    land in the suite's own stdout — which a gate reads as the run's verdict (#402).
    """
    sys.stdout.flush()
    saved = os.dup(1)
    with open(path, "w", encoding="utf-8") as sink:
        os.dup2(sink.fileno(), 1)
    try:
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


class _StubLeafCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cnt = self.tmp / "count.txt"
        self.error_log = self.tmp / "build.error.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- driving the stub ---------------------------------------------------------
    def _env(self, stream: str, err: str, rc: int) -> dict:
        return {"CNT": str(self.cnt), "STREAM": stream, "ERR": err, "RC": str(rc)}

    def _run_leaf(self, stream: str, *, err: str = "", rc: int = 1,
                  attempts: int = 3) -> Exception | None:
        """Through the production leaf path: `_invoke_leaf_resilient` → `_invoke` →
        `run_with_heartbeat`, ending in the `*.error.log` a post-mortem reads."""
        with contextlib.redirect_stderr(io.StringIO()):  # the tee'd child stderr
            return leaves._invoke_leaf_resilient(
                _leaf(), self.tmp, "build the fix", error_log=self.error_log,
                attempts=attempts, backoff=0.0, stream_json=True,
                env=self._env(stream, err, rc))

    def _heartbeat(self, stream: str, *, err: str = "", rc: int = 1,
                   capture: bool = False, stream_json: bool = True,
                   tee_stderr: bool = True,
                   fmt: str = "claude-stream-json") -> tuple[int, str, bool]:
        """Straight at `run_with_heartbeat`, where `output` is assembled."""
        env = {**os.environ, **self._env(stream, err, rc)}
        with contextlib.redirect_stderr(io.StringIO()):
            return progress.run_with_heartbeat(
                [sys.executable, "-c", _STUB], env=env, capture=capture,
                stream_json=stream_json, tee_stderr=tee_stderr, stream_format=fmt)

    def _log(self) -> str:
        self.assertTrue(self.error_log.exists(), "no error log was written")
        return self.error_log.read_text(encoding="utf-8")

    def _runs(self) -> int:
        return len(self.cnt.read_text()) if self.cnt.exists() else 0

    def _fixture(self, name: str) -> str:
        path = FIXTURES / name
        if not path.is_file():
            self.skipTest(f"pinned vendor fixture {name} is not present")
        return path.read_text(encoding="utf-8")


class ReportReachesTheErrorLog(_StubLeafCase):
    """(i) the marked report is in the artifact a post-mortem opens."""

    def test_marked_report_lands_in_the_error_log(self) -> None:
        err = self._run_leaf(_stream(_WORK, _REPORT))
        self.assertIsInstance(err, leaves.LeafError)
        log = self._log()
        self.assertIn(_DEATH_TEXT, log)
        self.assertNotIn("(no output captured)", log)

    def test_report_rides_the_output_run_with_heartbeat_returns(self) -> None:
        rc, output, _ = self._heartbeat(_stream(_WORK, _REPORT))
        self.assertEqual(rc, 1)
        self.assertIn(_DEATH_TEXT, output)  # the same route the stderr tail takes

    def test_report_is_appended_after_the_stderr_tail_not_instead_of_it(self) -> None:
        # The route the #420 memory post-mortem also rides: whatever is already in
        # `output` stays, and the report is appended after it.
        self._run_leaf(_stream(_REPORT), err="node: warning about nothing\n")
        log = self._log()
        self.assertIn("node: warning about nothing", log)
        self.assertIn(_DEATH_TEXT, log)
        self.assertLess(log.index("node: warning about nothing"), log.index(_DEATH_TEXT))

    def test_a_leaf_that_reports_nothing_is_unchanged(self) -> None:
        # No marked record anywhere: the log is exactly the stderr tail it was.
        self._run_leaf(_stream(_WORK), err="boom\n")
        log = self._log()
        self.assertIn("boom", log)
        self.assertNotIn("leaf stream report", log)

    def test_an_error_record_with_nothing_to_say_files_no_banner(self) -> None:
        # A `result`/`is_error` wrap-up carrying no text is not evidence: keeping
        # today's silence beats announcing a report and printing an empty line — that
        # is "(no output captured)" wearing a header.
        self._run_leaf(_stream(_WORK, _MUTE_WRAPUP), err="boom\n")
        log = self._log()
        self.assertIn("boom", log)
        self.assertNotIn("leaf stream report", log)

    def test_a_report_past_the_bound_is_kept_and_says_it_was_cut(self) -> None:
        # Bounded like the stderr tail — but a cut report must SAY it was cut, or a
        # fragment reads as the leaf's whole account of its death.
        self._run_leaf(_stream(_LONG_REPORT))
        log = self._log()
        self.assertIn(_LONG_HEAD, log)          # what was kept is really there
        self.assertNotIn(_LONG_TAIL, log)       # and the bound really bounds
        self.assertIn("truncated", log)         # …and the reader is told so


class RetentionIsUnconditional(_StubLeafCase):
    """(ii) every marked report is kept, whatever its cause."""

    def test_report_kept_even_when_the_session_carried_on_afterwards(self) -> None:
        # The CLI recovered and worked on before dying anyway. A harness holding the
        # text must never file "(no output captured)" — whose death it was is a
        # question for the reader, not a reason to drop the evidence.
        self._run_leaf(_stream(_REPORT, _WORK))
        log = self._log()
        self.assertIn(_DEATH_TEXT, log)
        self.assertNotIn("(no output captured)", log)

    def test_report_kept_for_a_cause_no_retry_can_clear(self) -> None:
        err = self._run_leaf(_stream(_WORK, _PERMANENT))
        self.assertIn(_PERMANENT_TEXT, self._log())
        # …and it is still not classified: same verdict, same one attempt, as today.
        self.assertFalse(getattr(err, "transient", False))
        self.assertEqual(self._runs(), 1)

    def test_report_kept_when_it_is_the_only_thing_the_stream_said(self) -> None:
        self._run_leaf(_stream(_REPORT))
        self.assertIn(_DEATH_TEXT, self._log())


class SubAgentReportsAreLabelled(_StubLeafCase):
    """(iii) a sub-agent's report is evidence, never the leaf's own death."""

    def _subagent_line(self, log: str) -> str:
        lines = [ln for ln in log.splitlines() if _SUBAGENT_TEXT in ln]
        self.assertEqual(len(lines), 1, f"sub-agent report not retained once:\n{log}")
        return lines[0]

    def test_stream_spelling_is_kept_labelled(self) -> None:
        self._run_leaf(_stream(_WORK, _SUBAGENT))
        line = self._subagent_line(self._log())
        self.assertIn("sub-agent", line.lower())
        self.assertIn("not the leaf's own death", line.lower())

    def test_transcript_spelling_is_kept_labelled(self) -> None:
        # `isSidechain` carries no `parent_tool_use_id` at all: one predicate answers
        # both spellings, so they cannot drift apart.
        self._run_leaf(_stream(_WORK, _SIDECHAIN))
        line = self._subagent_line(self._log())
        self.assertIn("sub-agent", line.lower())

    def test_the_main_sessions_own_report_is_not_labelled_as_a_sub_agents(self) -> None:
        self._run_leaf(_stream(_REPORT))
        log = self._log()
        self.assertIn(_DEATH_TEXT, log)
        self.assertNotIn("sub-agent", log.lower())


class NearestRecordWins(_StubLeafCase):
    """(iv) the record nearest the leaf's own death wins, in either arrival order."""

    def _assert_report_won(self, stream: str, buried: str) -> None:
        self._run_leaf(stream)
        log = self._log()
        self.assertIn(_DEATH_TEXT, log)
        self.assertNotIn(buried, log)

    def test_wrapup_after_the_report_cannot_bury_it(self) -> None:
        # The `result` wrap-up names the EFFECT (the session ended); the report before
        # it names the cause, and the cause is what a post-mortem needs.
        self._assert_report_won(_stream(_WORK, _REPORT, _WRAPUP), _WRAPUP_TEXT)

    def test_wrapup_before_the_report_cannot_pre_empt_it(self) -> None:
        self._assert_report_won(_stream(_WORK, _WRAPUP, _REPORT), _WRAPUP_TEXT)

    def test_subagent_report_after_the_report_cannot_bury_it(self) -> None:
        # Chatter from a Task still draining when the session died must not become the
        # log's account of the death.
        self._assert_report_won(_stream(_REPORT, _SUBAGENT), _SUBAGENT_TEXT)

    def test_subagent_report_before_the_report_cannot_pre_empt_it(self) -> None:
        self._assert_report_won(_stream(_SUBAGENT, _REPORT), _SUBAGENT_TEXT)

    def test_a_newer_main_session_report_replaces_the_older_one(self) -> None:
        # Same shape: the leaf's newer account of its own death wins outright.
        self._run_leaf(_stream(_REPORT, _PERMANENT))
        log = self._log()
        self.assertIn(_PERMANENT_TEXT, log)
        self.assertNotIn(_DEATH_TEXT, log)

    def test_a_wrapup_alone_is_still_kept(self) -> None:
        # Nothing nearer arrived, so the only record there is, is the evidence there is.
        self._run_leaf(_stream(_WORK, _WRAPUP))
        self.assertIn(_WRAPUP_TEXT, self._log())

    def test_a_mute_wrapup_cannot_bury_the_evidence_it_has_none_to_add_to(self) -> None:
        # An error record with NOTHING to say outranks a sub-agent's report by shape —
        # so if emptiness were kept as a record, a `result` carrying no text at all
        # would displace the only account of a failure the stream ever gave, and the
        # log would be back to "(no output captured)". It is not a record.
        self._run_leaf(_stream(_WORK, _SUBAGENT, _MUTE_WRAPUP))
        log = self._log()
        self.assertIn(_SUBAGENT_TEXT, log)
        self.assertNotIn("(no output captured)", log)


class NothingIsClassified(_StubLeafCase):
    """(v) the guard: `transient` and the retry counts are what they were."""

    def test_a_no_output_death_is_still_transient_and_retried_thrice(self) -> None:
        err = self._run_leaf("", err="overloaded_error 529\n")
        self.assertTrue(getattr(err, "transient", None))
        self.assertEqual(self._runs(), 3)
        self.assertIn("overloaded_error 529", self._log())

    def test_a_report_after_real_work_is_still_substantive_and_not_retried(self) -> None:
        err = self._run_leaf(_stream(_WORK, _REPORT))
        self.assertFalse(getattr(err, "transient", True))
        self.assertEqual(self._runs(), 1)

    def test_a_report_alone_is_still_substantive_and_not_retried(self) -> None:
        # The marked report IS an `assistant` event, so it has always counted as
        # "produced" — retaining its text must not move that by a hair.
        err = self._run_leaf(_stream(_REPORT))
        self.assertFalse(getattr(err, "transient", True))
        self.assertEqual(self._runs(), 1)

    def test_produced_is_unmoved_by_a_retained_report(self) -> None:
        _, _, produced = self._heartbeat(_stream(_WORK, _REPORT))
        self.assertTrue(produced)
        _, _, produced_report_only = self._heartbeat(_stream(_REPORT))
        self.assertTrue(produced_report_only)
        _, _, produced_none = self._heartbeat("", err="overloaded_error 529\n")
        self.assertFalse(produced_none)


class NothingElseChanges(_StubLeafCase):
    """(vi) every other caller and family behaves exactly as today."""

    def test_capture_returns_the_childs_raw_stdout_unmodified(self) -> None:
        # `capture=True` is what the three non-leaf callers pass (a gate's evidence
        # line, the notes fetch, publish): those bytes are the child's, verbatim.
        stream = _stream(_WORK, _REPORT)
        _, output, _ = self._heartbeat(stream, capture=True)
        self.assertEqual(output, stream)

    def test_a_gate_shaped_capture_is_untouched(self) -> None:
        stream = _stream(_REPORT)
        _, output, _ = self._heartbeat(stream, capture=True, stream_json=False,
                                       tee_stderr=False)
        self.assertEqual(output, stream)

    def test_the_codex_stream_format_degrades_to_todays_behaviour(self) -> None:
        # A claude record read under another family's format would be a guess. The
        # output is the stderr tail and nothing else.
        _, output, _ = self._heartbeat(_stream(_WORK, _REPORT), err="codex is sad\n",
                                       fmt="codex-stream-json")
        self.assertEqual(output, "codex is sad\n")

    def test_a_stream_less_family_is_untouched(self) -> None:
        # No stream parse at all: stdout inherits the terminal (captured here so the
        # suite's own stdout stays clean) and `output` is exactly the stderr tail.
        sink = self.tmp / "inherited-stdout.txt"
        with _stdout_fd_to(sink):
            _, output, _ = self._heartbeat(_stream(_REPORT), err="stream-less boom\n",
                                           stream_json=False, tee_stderr=True)
        self.assertIn(_DEATH_TEXT, sink.read_text(encoding="utf-8"))  # it WAS emitted
        self.assertEqual(output, "stream-less boom\n")

    def test_a_leaf_that_exits_zero_is_reported_exactly_as_today(self) -> None:
        # A session that reported a blip, recovered and exited 0: no error, no log.
        err = self._run_leaf(_stream(_REPORT, _WORK), rc=0)
        self.assertIsNone(err)
        self.assertFalse(self.error_log.exists())
        self.assertEqual(self._runs(), 1)

    def test_a_non_json_stream_line_is_ignored(self) -> None:
        self._run_leaf("not json at all\n" + _stream(_REPORT), err="")
        log = self._log()
        self.assertIn(_DEATH_TEXT, log)
        self.assertNotIn("not json at all", log)


class OneDecodePerStreamLine(_StubLeafCase):
    """The drain reads each line once, decodes it once, and classifies it three ways.

    Retention is a THIRD question asked of every stream line, next to "did real work
    happen" and "what tool is it running" — and the drain is a hot loop, a long session
    streaming thousands of lines. Each classifier used to parse the same bytes for
    itself: two decodes a line before retention was a question, and three had the new
    one followed suit, each with its own copy of the same "is it an object" guard, free
    to drift from the others. They share one decode instead — asserted here rather than
    left to a reading of the source.
    """

    def _decodes_for(self, stream: str) -> tuple[int, str]:
        counter = _CountingJson()
        with mock.patch.object(progress, "json", counter):
            _, output, _ = self._heartbeat(stream)
        return counter.loads_calls, output

    def test_a_stream_line_is_decoded_once_not_once_per_classifier(self) -> None:
        # One line the drain must answer all three questions about, plus a line each
        # for the records that compete for retention, plus one that is not JSON at all
        # (a failed decode is still exactly one decode).
        lines = ["not json at all"] + [json.dumps(ev) for ev in
                                       (_WORK, _WRAPUP, _SUBAGENT, _REPORT)]
        decodes, output = self._decodes_for("".join(f"{ln}\n" for ln in lines))
        self.assertIn(_DEATH_TEXT, output)  # the drain still did all three jobs…
        self.assertEqual(decodes, len(lines))  # …at one decode per line

    def test_the_decode_count_tracks_the_line_count(self) -> None:
        # Not a constant that happens to match: twice the lines, twice the decodes.
        lines = (_WORK, _REPORT)
        one, _ = self._decodes_for(_stream(*lines))
        two, _ = self._decodes_for(_stream(*(lines * 2)))
        self.assertEqual((one, two), (len(lines), 2 * len(lines)))


class PinnedVendorRecords(_StubLeafCase):
    """The same rules against bytes a real CLI wrote (tests/fixtures/README.md)."""

    def test_observed_death_record_stream_spelling_is_retained(self) -> None:
        line = self._fixture("claude_api_error_death.stream.jsonl")
        self._run_leaf(line)
        self.assertIn("Connection lost mid-response", self._log())

    def test_observed_death_record_transcript_spelling_is_retained(self) -> None:
        line = self._fixture("claude_api_error_death.transcript.jsonl")
        rc, output, _ = self._heartbeat(line)
        self.assertEqual(rc, 1)
        self.assertIn("Connection lost mid-response", output)

    def test_observed_permanent_record_is_retained_and_not_classified(self) -> None:
        line = self._fixture("claude_api_error_permanent.stream.jsonl")
        err = self._run_leaf(line)
        self.assertIn("issue with the selected model", self._log())
        self.assertFalse(getattr(err, "transient", True))
        self.assertEqual(self._runs(), 1)


if __name__ == "__main__":
    unittest.main()
