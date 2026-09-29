"""A leaf's death is classified by what actually happened (issue #539) — stdlib unittest.

`LeafError.transient` used to be `not produced`: "did the child say anything?" standing in
for "did it die at invocation?". That proxy is wrong both ways. It refused to retry an
18-minute builder that did real work and *then* lost its API connection — the CLI's own
marked report named the cause, and the harness filed a substantive failure. And it retried
a leaf a signal killed before its first stream event — a memory-capped leaf OOM-killed at
start-up bought three more OOMs.

What this module holds the harness to, criterion by criterion:

* (i) a leaf whose stream ended on its MAIN session's marked report of a cause the vendor
  marked transient — a lost connection, an overload or 5xx, a passing rate-limit
  rejection — and then exited non-zero is retried, however much work came first;
* (ii) it is NOT retried when the vendor marked the cause permanent — by its kind, or by a
  typed cause that outranks the kind (a usage-credit stop riding `rate_limit`), in either
  spelling — stamped a kind or typed a cause this harness does not know, or stamped
  nothing; when the text of a report the vendor could not classify names no category the
  harness retries (a 400 body is not promoted by a number or a field name inside it); when
  the leaf merely quoted an error; when real main-session work followed the report (the
  CLI recovered); when the report was a sub-agent's; or while the stream's newest word on
  the account's usage limit is that it refuses requests — a spent subscription window (the
  5-hour or a weekly limit) refuses every fresh attempt until it resets, hours away, so no
  `rate_limit` report, nor any other death, is transient then;
* (iii) a leaf a SIGNAL killed is never transient on the strength of having said nothing —
  as `-signum` from a direct child and as the shell's `128+signum` from a wrapper argv —
  while the harness's own wall-clock kill (`progress.TIMEOUT_RC`) keeps today's meaning;
* (iv) what the operator is told agrees with that: the retry line, the builder's failure
  line, the placeholder prose and the §6 label never say "did not run" / "no output" /
  "without emitting any work" of a leaf that ran, and never "safe to re-run" of a leaf
  its spent usage window stopped;
* (v) the report text is still retained, a recovered one included;
* (vi) nothing else moves — exit 0, a stream-less family, the codex format, `capture`.

Every case is a stub "leaf" that is a Python interpreter printing chosen stream events —
no vendor CLI, no API key, no network — driven through the production path
(`leaves._invoke_leaf_resilient` → `_invoke` → `progress.run_with_heartbeat`, or
`leaves.do_build` for the builder). Only API that existed before this change is imported,
so a red leg that reverts the fix fails on assertions, never on an import. The event
shapes are the vendor's; see tests/fixtures/README.md for which are observed and which
derived, and from which build.

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
import time
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, leaves, progress
from pdca_harness.config import Config, LeafConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The stub-leaf harness of tests/test_leaf_resilience.py, widened to die the ways a real
# leaf dies: argv is a python interpreter running an inline script; `_invoke` appends
# --output-format/--verbose (ignored) and feeds the prompt on stdin. The script counts its
# invocations into $CNT, prints $STREAM to stdout and $ERR to stderr, may sleep $SLEEP
# seconds, then either kills ITSELF with signal $SIG or exits $RC.
_STUB = (
    "import os,signal,sys,time\n"
    "open(os.environ['CNT'],'a').write('x')\n"
    "sys.stdout.write(os.environ['STREAM'])\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write(os.environ['ERR'])\n"
    "sys.stderr.flush()\n"
    "time.sleep(float(os.environ.get('SLEEP') or 0))\n"
    "if os.environ.get('SIG'):\n"
    "    os.kill(os.getpid(), getattr(signal, os.environ['SIG']))\n"
    "sys.exit(int(os.environ['RC']))\n"
)


def _leaf() -> LeafConfig:
    return LeafConfig(mode="command", family="claude",
                      argv=[sys.executable, "-c", _STUB], interactive=False)


def _wrapped_leaf() -> LeafConfig:
    """The same stub behind a real WRAPPER argv: the shell forks it (the trailing `exit`
    stops the shell exec-ing it), so a signal that kills the stub reaches the harness as
    the shell's own exit status, 128 + signum — the `docker run` / `sh -c` spelling."""
    return LeafConfig(mode="command", family="claude",
                      argv=["sh", "-c", '"$0" -c "$1"; exit $?', sys.executable, _STUB],
                      interactive=False)


# ---------------------------------------------------------------------------------
# Stream events, in the shapes claude-code emits them (tests/fixtures/README.md).
# ---------------------------------------------------------------------------------
def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# Main-session work: the main loop's emitters hard-code `parent_tool_use_id: null`, and
# a main-session tool result arrives as a `user` event.
_WORK = {"type": "assistant", "parent_tool_use_id": None, "message": _text("Editing x.py")}
_TOOL_USE = {"type": "assistant", "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_use", "id": "toolu_01Main",
                                      "name": "Bash", "input": {"command": "make test"}}]}}
_TOOL_RESULT = {"type": "user", "parent_tool_use_id": None,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01Main",
                     "content": "Ran 12 tests. FAILED (failures=1)"}]}}
# A sub-agent's traffic, forwarded with the Task's tool_use id.
_SUB_WORK = {"type": "assistant", "parent_tool_use_id": "toolu_01Task",
             "message": _text("sub-agent still draining")}
_SUB_TOOL_RESULT = {"type": "user", "parent_tool_use_id": "toolu_01Task",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_02Sub",
                         "content": "ok"}]}}

_LOST = "API Error: Connection lost mid-response. The response above may be incomplete."
# Text the reader of an UNCLASSIFIED report would take as transient — it leads with a 5xx
# and names a lost connection — used where a kind or a typed cause the vendor DID give
# must decide instead, whatever the text says.
_TEMPTING = ("API Error: 503 the service was overloaded, a rate limit hit, and the "
             "connection was lost mid-response (ECONNRESET)")

# What an `unknown` report reads like: `API Error: <status> <body>` for an API rejection
# the CLI did not classify, `API Error: <message>` for a failure that got no HTTP answer.
_BODY_536 = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
             '"message":"messages.536.content.0.text: cache_control cannot be set for empty '
             'text blocks"}}')
_BODY_TIMEOUT_FIELD = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_'
                       'error","message":"messages.40.content.1.tool_use.input.timeout: '
                       'Input should be a valid integer"}}')
_BODY_EVERY_PHRASE = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_'
                      'error","message":"tool_result quotes: Connection to the API was lost '
                      '(ECONNRESET); socket hang up; Request timed out; fetch failed; 503"}}')


def _report(kind="server_error", text: str = _LOST, **extra) -> dict:
    """The CLI's own marked report of an API error, from the MAIN session (stream
    spelling). ``kind=None`` leaves it unstamped."""
    ev = {"type": "assistant", "parent_tool_use_id": None, "is_api_error_message": True,
          "message": _text(text)}
    if kind is not None:
        ev["error"] = kind
    ev.update(extra)
    return ev


def _transcript_report(kind: str = "server_error", text: str = _LOST, **extra) -> dict:
    """The same report in the persisted transcript's spelling: `isApiErrorMessage`, the
    scope as `isSidechain`, no `parent_tool_use_id` key, and camel-cased typed causes."""
    return {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
            "error": kind, "message": _text(text), **extra}


def _wrapup(subtype: str = "success", status: int | None = None,
            text: str = _LOST, **extra) -> dict:
    """The session's `result` record. Only the `success` variant carries the HTTP status
    of the API error that ended it; an `error_*` variant carries an `errors` list."""
    ev: dict = {"type": "result", "subtype": subtype, "is_error": True}
    if subtype == "success":
        ev["result"] = text
    else:
        ev["errors"] = [text]
    if status is not None:
        ev["api_error_status"] = status
    ev.update(extra)
    return ev


# The account's usage-limit state, which claude-code reports in a `rate_limit_event`
# whenever that state changes. OBSERVED (2.1.277, a subscription session): the normal
# state names the limiting window too, so a `rateLimitType` alone is no refusal.
_OBSERVED_ALLOWED = {
    "status": "allowed", "resetsAt": 1789761600, "rateLimitType": "five_hour",
    "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
    "unifiedWindows": {"five_hour": {"utilization": 0.35, "resetsAt": 1789761600},
                       "seven_day": {"utilization": 0.48, "resetsAt": 1789830000}}}
# DERIVED (2.1.284): a 429 that names its window moves `status` to "rejected" — the
# spent-window refusal, the one 429 the CLI does not retry for a subscriber.
_SPENT_WINDOW = {**_OBSERVED_ALLOWED, "status": "rejected"}
# DERIVED (2.1.284): a subscriber's 429 that names no window — the server limiting
# requests, "not your usage limit" — leaves `status` "rejected" with no window named.
_PASSING_REJECTION = {"status": "rejected", "isUsingOverage": False,
                      "unifiedWindows": _OBSERVED_ALLOWED["unifiedWindows"]}
# How the CLI words the two `rate_limit` reports (2.1.284). Same kind; the text is never
# read for it — the usage-limit record decides.
_SPENT_TEXT = "You've hit your session limit · resets 3pm"
_PASSING_TEXT = ("API Error: Server is temporarily limiting requests (not your usage "
                 "limit) · this may be a temporary capacity issue. If it persists, check "
                 "status.claude.com.")


def _limit(info) -> dict:
    """The CLI's `rate_limit_event`, carrying ``info`` as its `rate_limit_info`."""
    return {"type": "rate_limit_event", "rate_limit_info": info,
            "uuid": "e7144c05-4c87-4aa4-8d61-11529b276b1d",
            "session_id": "0695ec12-8dd3-4243-b055-e2ba866382ed"}


def _stream(*events: dict) -> str:
    return "".join(json.dumps(ev) + "\n" for ev in events)


@contextlib.contextmanager
def _stdout_fd_to(path: Path):
    """Point this process's fd 1 at ``path`` — a stream-less leaf inherits stdout, and its
    output must not land in the suite's own (a gate reads that as the run's verdict)."""
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


class _Clock:
    """`time` as `leaves` sees it, minus the wait: each backoff is recorded, not slept
    (the shape tests/test_builder_retry.py uses — copied, not imported)."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __getattr__(self, name: str):
        return getattr(time, name)


class _StubLeafCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cnt = self.tmp / "count.txt"
        self.error_log = self.tmp / "check-review.error.log"
        self.stderr = ""

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, stream: str, err: str, rc: int, sig: str, sleep: float) -> dict:
        return {"CNT": str(self.cnt), "STREAM": stream, "ERR": err, "RC": str(rc),
                "SIG": sig, "SLEEP": str(sleep)}

    def _run(self, stream: str, *, err: str = "", rc: int = 1, sig: str = "",
             leaf: LeafConfig | None = None, stream_json: bool = True):
        """Through the production leaf path, with the shipped attempt budget spelled as
        tests/test_leaf_resilience.py spells it. Keeps what the harness printed."""
        self.cnt.unlink(missing_ok=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            err_ = leaves._invoke_leaf_resilient(
                leaf or _leaf(), self.tmp, "review please", error_log=self.error_log,
                attempts=3, backoff=0.0, stream_json=stream_json,
                env=self._env(stream, err, rc, sig, 0))
        self.stderr = buf.getvalue()
        return err_

    def _heartbeat(self, stream: str, *, rc: int = 1, sleep: float = 0,
                   timeout: int | None = None, capture: bool = False,
                   fmt: str = "claude-stream-json"):
        env = {**os.environ, **self._env(stream, "", rc, "", sleep)}
        with contextlib.redirect_stderr(io.StringIO()):
            return progress.run_with_heartbeat(
                [sys.executable, "-c", _STUB], env=env, capture=capture, stream_json=True,
                tee_stderr=True, stream_format=fmt, timeout=timeout)

    def _build(self, stream: str) -> str:
        """The stub as the Do builder, through the real `leaves.do_build` (the builder is
        on the same retry path, #537); returns what the harness printed. Each backoff is
        recorded, not slept."""
        cfg = Config(root=self.tmp, bundle_root=self.tmp / "results",
                     process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
                     default_branch="main", tracker_system="github", tracker_url="",
                     issue_id_example="#1", builder=_leaf(),
                     reviewer=LeafConfig(mode="stub", family="codex"), worktree=False)
        d = cfg.bundle("539")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** headline\n- **Test file:** tests/test_x.py\n",
                                    encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env(stream, "", 1, "", 0)), \
                mock.patch.object(leaves, "time", _Clock()), contextlib.redirect_stderr(out):
            with self.assertRaises(leaves.LeafError):
                leaves.do_build(d, cfg)
        return out.getvalue()

    def _section6(self, err) -> tuple[str, list]:
        """From a failed reviewer to what the human reads at sign-off: the placeholder's
        leaf status, and the §6 rows assembled from it."""
        with contextlib.redirect_stderr(io.StringIO()):
            leaves._review_unavailable(self.tmp, f"reviewer leaf failed: {err}",
                                       failure=leaves._failure_class(err),
                                       error_log=self.error_log)
        text = (self.tmp / "check-review.md").read_text(encoding="utf-8")
        return assemble.leaf_status(text), assemble._items_from_artifact(text)

    def _runs(self) -> int:
        return len(self.cnt.read_text()) if self.cnt.exists() else 0

    def _log(self) -> str:
        self.assertTrue(self.error_log.exists(), "no error log was written")
        return self.error_log.read_text(encoding="utf-8")

    def _assert_retried(self, err) -> None:
        self.assertIsInstance(err, leaves.LeafError)
        self.assertTrue(err.transient, f"expected transient; rc={err.returncode}")
        self.assertEqual(self._runs(), 3, "a transient death is retried to the budget")
        self.assertEqual(leaves._failure_class(err), leaves._FAIL_TRANSIENT)

    def _assert_not_retried(self, err) -> None:
        self.assertIsInstance(err, leaves.LeafError)
        self.assertFalse(err.transient, f"expected substantive; rc={err.returncode}")
        self.assertEqual(self._runs(), 1, "a non-transient death is not re-run")
        self.assertEqual(leaves._failure_class(err), leaves._FAIL_SUBSTANTIVE)

    def _each_retried(self, *streams: str) -> None:
        for stream in streams:
            with self.subTest(stream=stream):
                self._assert_retried(self._run(stream))

    def _each_not_retried(self, *streams: str) -> None:
        for stream in streams:
            with self.subTest(stream=stream):
                self._assert_not_retried(self._run(stream))


class TransientCauseAfterWorkIsRetried(_StubLeafCase):
    """(i) the vendor marked the cause transient ⇒ retried, however much work came first."""

    def test_a_lost_connection_after_real_work_is_retried(self) -> None:
        # The incident: work, then the CLI's own report of the dropped connection, exit 1.
        self._assert_retried(self._run(_stream(_WORK, _TOOL_USE, _TOOL_RESULT, _report())))

    def test_however_much_work_came_first(self) -> None:
        long_session = [_WORK, _TOOL_USE, _TOOL_RESULT] * 60
        self._assert_retried(self._run(_stream(*long_session, _report())))

    def test_each_kind_the_harness_retries(self) -> None:
        # `server_error` (a 5xx, a lost connection) and `overloaded` are the vendor's own
        # main-session rule; `rate_limit` — a mid-session rate-limit rejection — is this
        # project's policy (tests/fixtures/README.md).
        self._each_retried(*(_stream(_WORK, _report(kind))
                             for kind in ("server_error", "overloaded", "rate_limit")))

    def test_a_typed_cause_that_names_a_response_that_never_came(self) -> None:
        # The one typed cause in the retried category: no first byte before the deadline,
        # on any attempt. A typed cause outranks the kind, so it must be read as transient
        # in its own right.
        self._assert_retried(self._run(_stream(_WORK, _report(
            "server_error", "API Error: Request timed out. (waited 5m, then 5m on the retry)",
            api_error="no_response"))))

    def test_an_unclassified_report_that_leads_with_a_status_a_retry_can_clear(self) -> None:
        # `unknown` is the vendor saying "I could not classify this". Its leading status
        # is read: 408 and 409 are the only statuses the API client retries that reach it
        # (a 5xx and a 429 are stamped earlier), and a 5xx still counts if one does.
        self._each_retried(*(_stream(_WORK, _report("unknown", text)) for text in (
            "API Error: 408 Request Timeout",
            'API Error: 409 {"type":"error","error":{"type":"conflict_error"}}',
            "API Error: 503 Service Unavailable",
            "API Error: 529 Overloaded")))

    def test_an_unclassified_report_of_a_connection_that_failed(self) -> None:
        # No status to lead with: a failure that never got an HTTP answer, read only for
        # the wording a failed connection has — the SDK's, the CLI's, Node's, undici's —
        # and the CLI's own connection-error codes.
        self._each_retried(*(_stream(_WORK, _report("unknown", text)) for text in (
            "API Error: Connection error.",
            "API Error: Connection closed before the response finished",
            "API Error: Request timed out.",
            "API Error: socket hang up",
            "API Error: other side closed",
            "API Error: Premature close",
            "API Error: fetch failed",
            "API Error: terminated",
            "API Error: read ECONNRESET")))

    def test_the_wrapup_that_follows_the_report_does_not_bury_its_cause(self) -> None:
        # The `result` wrap-up names the effect; the report before it names the cause.
        self._each_retried(
            _stream(_WORK, _report(), _wrapup()),
            _stream(_WORK, _report(), _wrapup("error_during_execution",
                                              text="[ede_diagnostic] turn aborted")))

    def test_a_wrapup_carrying_a_429(self) -> None:
        # 429 is not in the 5xx range, so only the transient-status set can admit it.
        self._assert_retried(self._run(_stream(_WORK, _wrapup(status=429))))

    def test_a_wrapup_carrying_a_5xx(self) -> None:
        self._assert_retried(self._run(_stream(_WORK, _wrapup(status=503))))

    def test_a_sub_agent_that_keeps_draining_does_not_clear_the_report(self) -> None:
        # Only MAIN-session work means the CLI recovered: a Task's trailing traffic is not
        # the session carrying on.
        self._assert_retried(self._run(_stream(_WORK, _report(), _SUB_WORK,
                                               _SUB_TOOL_RESULT)))


class NotEveryReportIsTransient(_StubLeafCase):
    """(ii) permanent, typed, unknown-to-us, unstamped, unreadable, quoted, recovered or a
    sub-agent's ⇒ not retried."""

    def test_a_permanent_cause_is_not_promoted_by_its_prose(self) -> None:
        self._each_not_retried(*(_stream(_WORK, _report(kind, _TEMPTING)) for kind in (
            "invalid_request", "authentication_failed", "billing_error", "model_not_found")))

    def test_a_kind_this_harness_does_not_recognise(self) -> None:
        self._assert_not_retried(self._run(_stream(_WORK, _report("quota_v2", _TEMPTING))))

    def test_a_report_the_vendor_left_unstamped(self) -> None:
        # No `error` kind at all: the message never went through the vendor's mapper, so
        # there is no vendor judgement to follow — and neither prose nor a stray typed
        # cause is one.
        self._each_not_retried(_stream(_WORK, _report(None, _LOST)),
                               _stream(_WORK, _report(None, _LOST, api_error="no_response")))

    def test_a_rate_limit_the_vendor_typed_as_a_usage_credit_stop(self) -> None:
        # Stamped `rate_limit`, but typed as an entitlement stop: a request past the 200K
        # context boundary without usage credits, or a model that needs them. A fresh
        # attempt re-does the work and meets the same stop — the typed cause outranks the
        # kind the harness would otherwise retry.
        self._each_not_retried(*(_stream(_WORK, _report("rate_limit", text, api_error=cause))
                                 for cause, text in (
            ("long_context_credits_required",
             "API Error: Usage credits required for 1M context · run /usage-credits"),
            ("model_requires_usage_credits",
             "You're out of included usage for this model · turn on usage credits"))))

    def test_a_server_error_the_vendor_typed_as_a_stop_a_retry_meets_again(self) -> None:
        # Stamped `server_error`, typed as a TLS trust failure (a TLS-inspecting proxy, a
        # private CA) or a proxy that rewrote the response: configuration, not a blip.
        self._each_not_retried(*(_stream(_WORK, _report("server_error", _TEMPTING,
                                                        api_error=cause))
                                 for cause in ("tls_untrusted_ca", "gateway_content_type")))

    def test_a_typed_cause_this_harness_has_not_read(self) -> None:
        # The schema adds typed causes over time. One this harness has not read is not
        # taken as transient, whatever kind it rides — that only ever withholds a retry.
        self._assert_not_retried(self._run(_stream(_WORK, _report(
            "server_error", _TEMPTING, api_error="some_cause_added_next_year"))))

    def test_a_server_gate_code_outranks_the_kind(self) -> None:
        # `api_error_code` carries a server gate this CLI has no typed kind for yet — the
        # channel a new usage-credit stop arrives on before a CLI release names it.
        self._assert_not_retried(self._run(_stream(_WORK, _report(
            "rate_limit", "API Error: Request rejected (429)",
            api_error_code="credits_required"))))

    def test_the_transcript_spelling_of_a_typed_cause(self) -> None:
        # The same typed causes spelled as the persisted transcript spells them: the mark
        # (`isApiErrorMessage`) is honoured in that spelling, so the cause must be too.
        self._each_not_retried(
            _stream(_WORK, _transcript_report(
                "unknown", "API Error: gateway session timed out; connection lost",
                apiError="gateway_session_expired")),
            _stream(_WORK, _transcript_report(
                "rate_limit", "API Error: Usage credits required for 1M context",
                apiError="long_context_credits_required")),
            _stream(_WORK, _transcript_report(
                "rate_limit", "API Error: Request rejected (429)",
                apiErrorCode="credits_required")))

    def test_an_unclassified_report_the_vendor_typed_is_not_read_as_prose(self) -> None:
        # claude-code stamps `unknown` plus a typed cause for a gateway sign-in or a
        # provider credential failure: the vendor did type the cause, so its text is not
        # read.
        self._assert_not_retried(self._run(_stream(_WORK, _report(
            "unknown", "API Error: gateway session timed out; connection lost",
            api_error="gateway_session_expired"))))

    def test_an_unclassified_report_is_decided_by_its_leading_status_alone(self) -> None:
        # A 400 body the CLI could not classify: its message index (`messages.536…`) is
        # no 5xx, its Bash-input field name (`…input.timeout`) is no timeout, and a body
        # that quotes every connection phrase is still a 400.
        self._each_not_retried(*(_stream(_WORK, _report("unknown", body))
                                 for body in (_BODY_536, _BODY_TIMEOUT_FIELD,
                                              _BODY_EVERY_PHRASE)))

    def test_an_unclassified_report_without_a_status_names_no_failed_connection(self) -> None:
        # No leading status, and none of a failed connection's wording: an incidental
        # number and a field called `timeout` are not read as either.
        self._each_not_retried(*(_stream(_WORK, _report("unknown", text)) for text in (
            "API Error: Unexpected value at messages.536.content.0.input.timeout",
            "API Error: Unexpected token < in JSON at position 0",
            "API Error")))

    def test_a_leaf_that_merely_quotes_an_error_is_untouched(self) -> None:
        # An UNMARKED assistant message is the leaf talking, whatever it says.
        quoted = {"type": "assistant", "parent_tool_use_id": None, "error": "server_error",
                  "message": _text(_LOST)}
        self._assert_not_retried(self._run(_stream(_WORK, quoted)))

    def test_recovered_through_more_assistant_work(self) -> None:
        self._assert_not_retried(self._run(_stream(_WORK, _report(), _WORK)))

    def test_recovered_through_a_main_session_tool_cycle(self) -> None:
        # A main-session tool result is a `user` event: the CLI recovered, ran a tool, and
        # the leaf then failed on its own merits.
        self._assert_not_retried(self._run(_stream(_WORK, _report(), _TOOL_RESULT)))

    def test_a_sub_agents_report_never_classifies(self) -> None:
        sub = _report("overloaded", parent_tool_use_id="toolu_01Task")
        side = {**_report("overloaded"), "isSidechain": True}
        del side["parent_tool_use_id"]  # the transcript spelling carries no such key
        self._each_not_retried(_stream(_WORK, sub), _stream(_WORK, side))

    def test_the_status_on_an_execution_error_wrapup_is_not_read(self) -> None:
        # Only the `success` wrap-up is the CLI's "this session ended on my API-error
        # message"; an `error_*` one ends on whatever threw, whatever status it carries.
        wrap = {**_wrapup("error_during_execution", text="turn aborted"),
                "api_error_status": 503}
        self._assert_not_retried(self._run(_stream(_WORK, wrap)))

    def test_a_wrapup_status_no_retry_can_clear(self) -> None:
        self._each_not_retried(_stream(_WORK, _wrapup(status=400)),
                               _stream(_WORK, _wrapup(status=None)))

    def test_a_wrapup_carrying_a_server_gate_code(self) -> None:
        # The wrap-up repeats the gate code of the error that ended the turn: a 429 the
        # server gated is a stop, not a rejection a retry can clear.
        self._assert_not_retried(self._run(_stream(_WORK, _wrapup(
            status=429, api_error_code="credits_required"))))

    def test_the_leafs_newer_account_of_its_death_wins(self) -> None:
        self._assert_not_retried(self._run(_stream(_WORK, _report(),
                                                   _report("invalid_request", _TEMPTING))))

    def test_a_wrapup_status_cannot_overrule_the_reported_cause(self) -> None:
        self._assert_not_retried(self._run(_stream(
            _WORK, _report("invalid_request", _TEMPTING), _wrapup(status=503))))

    def test_a_malformed_record_is_a_verdict_not_a_crash(self) -> None:
        # The classifier runs in the stream's drain thread: a field of an unexpected type
        # must read as "not transient", never raise — or no line after it is read at all.
        for name, bad in (
                ("listed kind", _report(["server_error"])),
                ("mapping cause", _report("server_error", api_error={"no": "hash"})),
                ("listed cause", _report("server_error", api_error=["no_response"])),
                ("text status", {**_wrapup(), "api_error_status": "503"})):
            with self.subTest(field=name):
                self._assert_not_retried(self._run(_stream(_WORK, bad)))
                # …and the drain read on: a well-formed report after it still decides.
                self._assert_retried(self._run(_stream(_WORK, bad, _report())))


class SignalDeathIsNotTransient(_StubLeafCase):
    """(iii) the manner of the kill: a signal death is never re-run for having said
    nothing, in either returncode spelling; the harness's own timeout keeps its meaning."""

    def test_a_silent_leaf_killed_by_sigkill_is_not_re_run(self) -> None:
        err = self._run("", err="", sig="SIGKILL")
        self.assertEqual(err.returncode, -9)
        self._assert_not_retried(err)

    def test_a_silent_leaf_killed_by_sigterm_is_not_re_run(self) -> None:
        err = self._run("", sig="SIGTERM")
        self.assertEqual(err.returncode, -15)
        self._assert_not_retried(err)

    def test_the_wrapper_spelling_of_a_sigkill(self) -> None:
        # A real wrapper argv: the shell outlives the killed stub and exits 128 + 9.
        err = self._run("", sig="SIGKILL", leaf=_wrapped_leaf())
        self.assertEqual(err.returncode, 137)
        self._assert_not_retried(err)

    def test_a_bare_exit_137_is_read_as_the_same_death(self) -> None:
        err = self._run("", rc=137)
        self._assert_not_retried(err)

    def test_the_kill_outranks_a_transient_report(self) -> None:
        # What ended the leaf was the signal, not the API it reported on.
        self._assert_not_retried(self._run(_stream(_WORK, _report()), sig="SIGKILL"))

    def test_an_ordinary_silent_death_is_still_retried(self) -> None:
        # #138 unchanged: a non-zero exit before any work, no signal involved. 128 is the
        # shell's base alone (no signal 0) and 255 is past every signal number.
        for rc in (1, 2, 128, 255):
            with self.subTest(rc=rc):
                err = self._run("", err="overloaded_error 529\n", rc=rc)
                self.assertEqual(err.returncode, rc)
                self._assert_retried(err)

    def test_the_harness_timeout_keeps_todays_meaning(self) -> None:
        # TIMEOUT_RC is "the oracle did not answer": a silent timed-out leaf reads
        # transient as before, and one that worked is not re-labelled by its stream.
        self.assertTrue(leaves.LeafError(progress.TIMEOUT_RC, ["x"],
                                         produced=False).transient)
        self.assertFalse(leaves.LeafError(progress.TIMEOUT_RC, ["x"],
                                          produced=True).transient)
        rc, _, produced = self._heartbeat(_stream(_WORK, _report()), sleep=30, timeout=1)
        self.assertEqual(rc, progress.TIMEOUT_RC)
        self.assertTrue(produced)


class SpentUsageWindowIsNotTransient(_StubLeafCase):
    """(ii) a spent subscription window refuses every fresh attempt until it resets, hours
    away. A `rate_limit` report reads the same for it as for a passing rejection; the
    CLI's `rate_limit_event` — its account-wide, non-prose word on the usage limit — tells
    them apart, and its NEWEST state decides."""

    def test_a_spent_window_then_its_rate_limit_report_is_not_re_run(self) -> None:
        # The case the sign-off named: work, the window refusal, the report, exit 1.
        err = self._run(_stream(_WORK, _TOOL_USE, _TOOL_RESULT, _limit(_SPENT_WINDOW),
                                _report("rate_limit", _SPENT_TEXT)))
        self._assert_not_retried(err)
        self.assertNotIn("retry 1/2", self.stderr)

    def test_a_plain_rate_limit_report_is_still_retried(self) -> None:
        # The other case the sign-off named: no window refusal, so a passing rejection —
        # this project retries it (#138's twin). With no usage-limit record at all (an
        # API-key session has none), after the observed normal state, and after the state
        # a subscriber's passing 429 leaves: "rejected", naming no window.
        report = _report("rate_limit", _PASSING_TEXT)
        self._each_retried(_stream(_WORK, report),
                           _stream(_WORK, _limit(_OBSERVED_ALLOWED), report),
                           _stream(_WORK, _limit(_PASSING_REJECTION), report))

    def test_each_window_the_vendor_names(self) -> None:
        # The six windows 2.1.284's schema lists, and one it may add: a refusal that names
        # a window this harness has not read still withholds the retry.
        self._each_not_retried(*(
            _stream(_WORK, _limit({**_SPENT_WINDOW, "rateLimitType": window}),
                    _report("rate_limit", _SPENT_TEXT))
            for window in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
                           "seven_day_overage_included", "overage", "seven_day_next")))

    def test_a_named_window_is_no_refusal_while_requests_are_served(self) -> None:
        # The CLI names the limiting window on EVERY state — the observed normal one
        # included — so only `status: "rejected"` refuses; `allowed_warning` is a window
        # nearing its limit, still serving requests.
        report = _report("rate_limit", _PASSING_TEXT)
        self._each_retried(
            _stream(_WORK, _limit(_OBSERVED_ALLOWED), report),
            _stream(_WORK, _limit({**_OBSERVED_ALLOWED, "status": "allowed_warning",
                                   "rateLimitType": "seven_day", "utilization": 0.8}),
                    report))

    def test_a_spent_window_that_paid_extra_usage_is_covering(self) -> None:
        # `isUsingOverage`: the window is spent, but paid extra usage serves the requests —
        # nothing is refused, so a rejection that ends the leaf is a passing one.
        covered = {**_SPENT_WINDOW, "overageStatus": "allowed", "isUsingOverage": True}
        self._assert_retried(self._run(_stream(_WORK, _limit(covered),
                                               _report("rate_limit", _PASSING_TEXT))))

    def test_a_rejection_that_names_no_window(self) -> None:
        # How a subscriber's passing 429 leaves the state, in each spelling of "no window".
        report = _report("rate_limit", _PASSING_TEXT)
        self._each_retried(*(
            _stream(_WORK, _limit({**_PASSING_REJECTION, **named}), report)
            for named in ({}, {"rateLimitType": None}, {"rateLimitType": ""},
                          {"rateLimitType": ["five_hour"]})))

    def test_the_newest_state_decides(self) -> None:
        # The window reset (or the CLI waited it out) and the account serves requests
        # again: the earlier refusal is not how the leaf died. And the other way round.
        report = _report("rate_limit", _PASSING_TEXT)
        self._assert_retried(self._run(_stream(
            _limit(_SPENT_WINDOW), _WORK, _limit(_OBSERVED_ALLOWED), _WORK, report)))
        self._assert_not_retried(self._run(_stream(
            _limit(_OBSERVED_ALLOWED), _WORK, _limit(_SPENT_WINDOW),
            _report("rate_limit", _SPENT_TEXT))))

    def test_the_refusal_vetoes_every_shape_of_a_transient_death(self) -> None:
        # While the account is refused, a fresh attempt is refused on its first request —
        # however the leaf died: having emitted no work at all, on a wrap-up carrying the
        # 429, or on a report of another transient cause.
        self._each_not_retried(
            _stream(_limit(_SPENT_WINDOW)),
            _stream(_WORK, _limit(_SPENT_WINDOW), _wrapup(status=429, text=_SPENT_TEXT)),
            _stream(_WORK, _limit(_SPENT_WINDOW), _report()))

    def test_a_malformed_usage_limit_record_is_ignored_not_fatal(self) -> None:
        # A `rate_limit_info` that is no mapping says nothing — the vendor's own readers
        # drop it — so the state stands as it was, and the drain reads on. The field on any
        # OTHER record is not the usage-limit record.
        spent = _report("rate_limit", _SPENT_TEXT)
        garbled = {"type": "rate_limit_event", "rate_limit_info": "rejected"}
        self._each_not_retried(_stream(_WORK, _limit(_SPENT_WINDOW), garbled, spent))
        self._each_retried(
            _stream(_WORK, garbled, spent),
            _stream(_WORK, {"type": "system", "subtype": "status",
                            "rate_limit_info": _SPENT_WINDOW}, spent))

    def test_the_codex_format_has_no_usage_limit_record(self) -> None:
        # Degrade to today: a codex stream that emitted no work is #138's shape, whatever
        # a claude-shaped line in it says.
        _, _, produced = self._heartbeat(_stream(_limit(_SPENT_WINDOW)),
                                         fmt="codex-stream-json")
        self.assertFalse(produced)

    def test_exit_zero_and_the_harness_timeout_keep_todays_meaning(self) -> None:
        # The refusal describes a FAILED run: a leaf that exited 0 is reported as today,
        # and a run the harness killed on its own timeout is not re-labelled by it.
        rc, _, produced = self._heartbeat(_stream(_limit(_SPENT_WINDOW)), rc=0)
        self.assertEqual(rc, 0)
        self.assertFalse(produced)
        rc, _, produced = self._heartbeat(_stream(_limit(_SPENT_WINDOW)), sleep=30,
                                          timeout=1)
        self.assertEqual(rc, progress.TIMEOUT_RC)
        self.assertFalse(produced)

    def test_the_builder_is_not_re_run_into_a_spent_window(self) -> None:
        said = self._build(_stream(_WORK, _TOOL_USE, _TOOL_RESULT, _limit(_SPENT_WINDOW),
                                   _report("rate_limit", _SPENT_TEXT)))
        self.assertEqual(self._runs(), 1, "a builder its window stopped is not re-run")
        self.assertNotIn("— transient:", said)

    def test_the_section6_row_does_not_say_safe_to_re_run(self) -> None:
        # The harm the sign-off named: "safe to re-run" is false for hours here.
        status, items = self._section6(self._run(_stream(
            _WORK, _limit(_SPENT_WINDOW), _report("rate_limit", _SPENT_TEXT))))
        self.assertEqual(status, assemble.LEAF_STATUS_HUMAN)
        self.assertTrue(items)
        for it in items:
            self.assertNotIn("safe to re-run", it.text)

    def test_the_report_is_kept_though_it_is_not_retried(self) -> None:
        # (v) retention is unconditional: the log says which window, and when it resets.
        self._assert_not_retried(self._run(_stream(_WORK, _limit(_SPENT_WINDOW),
                                                   _report("rate_limit", _SPENT_TEXT))))
        self.assertIn(_SPENT_TEXT, self._log())


class WhatTheOperatorIsTold(_StubLeafCase):
    """(iv) every message about the decision states the same true thing."""

    def test_the_retry_line_does_not_say_no_output_of_a_leaf_that_worked(self) -> None:
        self._run(_stream(_WORK, _report()))
        self.assertIn("retry 1/2", self.stderr)
        self.assertIn("report of a transient API error", self.stderr)
        self.assertNotIn("no output", self.stderr)

    def test_the_builders_failure_line_for_the_headline_leaf(self) -> None:
        # The builder is on the same retry path (#537): an 18-minute builder that ends on
        # its own transient report is retried, and the line it leaves when the retries are
        # spent must not say it emitted no work.
        said = self._build(_stream(_WORK, _TOOL_USE, _TOOL_RESULT, _report()))
        self.assertEqual(self._runs(), 3, "the builder's transient death is retried")
        self.assertIn("— transient:", said)
        self.assertIn("report of a transient API error", said)
        self.assertNotIn("without emitting any work", said)

    def test_the_section6_row_for_the_headline_leaf(self) -> None:
        # From the stream to the row the human reads at sign-off.
        status, items = self._section6(
            self._run(_stream(_WORK, _TOOL_USE, _TOOL_RESULT, _report())))
        self.assertEqual(status, assemble.LEAF_STATUS_INFRA)
        self.assertTrue(items)
        for it in items:
            self.assertNotIn("did not run", it.text)
            self.assertIn("safe to re-run", it.text)

    def test_the_label_and_the_placeholder_name_both_shapes(self) -> None:
        label = assemble._LEAF_STATUS_LABEL[assemble.LEAF_STATUS_INFRA]
        prose = leaves._unavailable_classification(leaves._FAIL_TRANSIENT, None)
        for said in (label, prose):
            with self.subTest(said=said[:40]):
                self.assertNotIn("did not run", said)
                self.assertNotIn("with no output", said)
                self.assertIn("before emitting any work", said)          # shape one …
                self.assertIn("report of a transient API error", said)   # … and shape two


class RetentionIsUnchanged(_StubLeafCase):
    """(v) child-1's retention holds under the new verdict."""

    def test_every_attempt_keeps_its_report(self) -> None:
        self._run(_stream(_WORK, _report()))
        self.assertEqual(self._log().count(_LOST), 3)

    def test_a_recovered_report_is_kept_though_it_is_not_the_verdict(self) -> None:
        self._assert_not_retried(self._run(_stream(_WORK, _report(), _TOOL_RESULT)))
        self.assertIn(_LOST, self._log())
        self.assertNotIn("(no output captured)", self._log())

    def test_a_typed_stop_is_kept_though_it_is_not_retried(self) -> None:
        text = "API Error: Usage credits required for 1M context · run /usage-credits"
        self._assert_not_retried(self._run(_stream(_WORK, _report(
            "rate_limit", text, api_error="long_context_credits_required"))))
        self.assertIn(text, self._log())


class NothingElseChanges(_StubLeafCase):
    """(vi) the verdict moves nothing but the retry decision on a failed run."""

    def test_a_leaf_that_exits_zero_is_reported_as_today(self) -> None:
        for stream in (_stream(_WORK, _report()), _stream(_report(), _WORK)):
            with self.subTest(stream=stream[:30]):
                self.assertIsNone(self._run(stream, rc=0))
                self.assertEqual(self._runs(), 1)
                self.assertFalse(self.error_log.exists())

    def test_exit_zero_keeps_produced_as_it_was(self) -> None:
        # The verdict describes a run that FAILED: a session that ended on a transient
        # report and still exited 0 did work, and `produced` keeps saying so.
        rc, _, produced = self._heartbeat(_stream(_WORK, _report()), rc=0)
        self.assertEqual(rc, 0)
        self.assertTrue(produced)

    def test_a_stream_less_family_still_reports_substantive(self) -> None:
        # No stream parse, so no verdict: the `produced=True` fallback is untouched.
        with _stdout_fd_to(self.tmp / "inherited-stdout.txt"):
            err = self._run(_stream(_WORK, _report()), stream_json=False)
        self._assert_not_retried(err)

    def test_the_codex_format_does_not_read_a_claude_report(self) -> None:
        codex_work = {"type": "item.completed", "item": {"type": "agent_message"}}
        _, _, produced = self._heartbeat(_stream(codex_work, _report()),
                                         fmt="codex-stream-json")
        self.assertTrue(produced)

    def test_capture_still_returns_the_childs_raw_stdout(self) -> None:
        stream = _stream(_WORK, _report())
        _, output, _ = self._heartbeat(stream, capture=True)
        self.assertEqual(output, stream)


class PinnedVendorRecords(_StubLeafCase):
    """The same rules against bytes a real CLI wrote (tests/fixtures/README.md)."""

    def _fixture(self, name: str) -> str:
        path = FIXTURES / name
        if not path.is_file():
            self.skipTest(f"pinned vendor fixture {name} is not present")
        return path.read_text(encoding="utf-8")

    def test_the_observed_incident_record_after_work_is_retried(self) -> None:
        for name in ("claude_api_error_death.stream.jsonl",
                     "claude_api_error_death.transcript.jsonl"):
            with self.subTest(fixture=name):
                self._assert_retried(self._run(_stream(_WORK) + self._fixture(name)))

    def test_the_observed_permanent_record_after_work_is_not(self) -> None:
        for name in ("claude_api_error_permanent.stream.jsonl",
                     "claude_api_error_permanent.transcript.jsonl"):
            with self.subTest(fixture=name):
                self._assert_not_retried(self._run(_stream(_WORK) + self._fixture(name)))


if __name__ == "__main__":
    unittest.main()
