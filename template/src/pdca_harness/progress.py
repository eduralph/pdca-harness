"""Run a subprocess while ticking an elapsed-time heartbeat (docs 03 §automation).

A headless ``claude -p`` leaf and a Docker-backed gate both produce no output for
minutes; without a heartbeat the flow looks hung and the human kills a job that is
working. This is the single place that pattern lives — shared by the model leaves
(:mod:`pdca_harness.leaves`) and the deterministic gates (:mod:`pdca_harness.gates`).

A ``status`` probe lets the heartbeat show *what* is happening (which artifacts exist
yet, how long since the last write), not just that time passed.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path

# The distinguishable timed-out outcome (issue #368): returned in the returncode slot
# when a configured ``timeout`` expired and the child's process GROUP was terminated.
# Outside the range a real child can produce (0..255 on exit, ``-signum`` on a signal
# death), so a caller can route "the oracle did not answer" separately from any
# pass/fail verdict the child itself could have expressed.
TIMEOUT_RC = -1001

#: The highest signal number a POSIX host delivers (``SIGRTMAX``: 64 on Linux, lower on
#: the BSDs — the wider bound is the safe one, since a hit only ever buys "not transient").
_MAX_SIGNUM = 64
#: How a WRAPPER argv reports that ITS child died of a signal: the shell's ``128 + signum``
#: (137 = SIGKILL, 143 = SIGTERM), as ``sh -c`` and ``docker run`` spell it.
_SHELL_SIGNAL_BASE = 128


def is_signal_death(rc: int) -> bool:
    """Did a **signal** end this child? Answered for both spellings of the same death,
    because which one the harness sees depends on nothing but the argv's shape:
    :mod:`subprocess` reports a direct child's signal death as ``-signum``, while a wrapper
    argv has already exited normally with the shell's ``128 + signum``. The memory cap
    (#420) kills with SIGKILL, and a rule that knew only one spelling would retry the same
    OOM whenever the leaf ran behind a wrapper.

    Why a retry decision asks it (#539): a signal death is not an API failure the vendor
    reported, and it repeats — the same cap, OOM killer or operator kills the same leaf on
    every attempt — so it is never "transient" merely because the leaf had said nothing yet
    (what to do with it instead is issue #510's question). :data:`TIMEOUT_RC` lies outside
    both ranges by construction, so the harness's own wall-clock kill answers ``False``
    here and keeps its own meaning.
    """
    if rc < 0:
        return 1 <= -rc <= _MAX_SIGNUM
    return _SHELL_SIGNAL_BASE < rc <= _SHELL_SIGNAL_BASE + _MAX_SIGNUM


def run_with_heartbeat(
    cmd,
    *,
    cwd=None,
    shell: bool = False,
    env=None,
    input_text: str | None = None,
    capture: bool = False,
    stream_json: bool = False,
    tee_stderr: bool = False,
    stream_format: str = "claude-stream-json",
    interval: int = 15,
    timeout: int | None = None,
    label: str = "",
    status: Callable[[], str] | None = None,
    telemetry: Callable[[int], str] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> tuple[int, str, bool]:
    """Run ``cmd``, printing ``… still working (NmSSs elapsed)`` every ``interval`` s.

    Returns ``(returncode, output, produced)``. ``output`` is the combined
    stdout+stderr when ``capture`` is True (so a gate can keep its evidence line);
    the bounded **stderr tail** when ``stream_json`` or ``tee_stderr`` is set (so a
    failed leaf's real error — usage/rate limit, 5xx, auth — survives in the bundle
    instead of scrolling past on a console nobody is watching), **plus the leaf's own
    terminal error report** when the stream carried one (below); ``""`` otherwise.
    ``produced`` is whether the child did work that **stands as its own**: it emitted a
    substantive stream event — an ``assistant`` / ``user`` / ``result`` event, i.e. a
    session that did real work — and did not then exit non-zero with its stream ENDED on
    the vendor's own report of a transient infrastructure failure (below). Claude emits a
    ``system``/``init`` event (and ``system``/``api_retry`` on a retryable API error)
    *before* doing anything, so those do NOT count. A non-zero exit with ``produced is
    False`` is therefore the transient-infra signal in **both** of its shapes: the child
    died at/near invocation, before emitting any work, where no report says why (a rate
    limit, a 5xx, a network blip); or its main session's last word was the CLI's marked
    report of a cause the vendor marks transient. One exception overrides both: a non-zero
    exit while the stream's newest word on the account's usage limit is that it refuses
    requests — a spent subscription window, which no fresh attempt clears until it resets —
    reports ``produced`` as ``True`` whatever the child did, so it never reads as transient
    (:func:`_usage_limit_refusal`). How the child was killed is the caller's second
    question, answered from the returncode (:func:`is_signal_death`). ``input_text``, if
    given, is written to stdin.

    **What a transient death is, decided once** (issue #539). "Did it emit any work?" was
    only a proxy for "did it die at invocation?", and it failed exactly where an attempt
    is dearest: an 18-minute leaf whose API connection dropped mid-response did plenty of
    work and still died of infrastructure it did not cause. The stream already says how the
    leaf died — the CLI marks the report it synthesises for an API error and stamps its
    own error kind, and often a typed cause, on it — so that record is classified
    (:func:`_reports_transient_cause`): when it came from the MAIN session, the vendor
    marked its cause transient, no main-session work followed it (that would mean the CLI
    recovered, :func:`_is_main_session_work`), and the child then exited non-zero,
    ``produced`` is ``False`` however much work came first. The vendor's word decides, the
    most specific first: a typed cause outranks the kind, no prose promotes a cause the
    vendor marked or typed as permanent, an unrecognised kind or cause and an unstamped
    report stay as they were, and a run the harness killed on its own ``timeout`` is not
    re-labelled by what its stream said last. One record outranks the report: the CLI's
    account-wide ``rate_limit_event``. A ``rate_limit`` report reads the same for a passing
    rejection and for a spent subscription window (the 5-hour or a weekly limit), and only
    that record tells them apart — so while its newest state is a refusal, the death is not
    transient in either shape, and the operator is never told "safe to re-run" of a limit
    that holds for hours (:func:`_usage_limit_refusal`).

    **The leaf's own account of its death is kept** (issue #506). The CLI *marks* the
    message it synthesises for an API error, and that report arrives as a stream event
    on stdout — which this function already reads, parsed for a tool label and then
    dropped. So the marked report is retained (:func:`_terminal_error`) and appended to
    ``output``, reaching a caller's ``*.error.log`` by the same route the stderr tail
    takes — the one existing precedent being the #420 memory post-mortem, which appends
    to the same string. An 18-minute leaf whose API connection dropped mid-response used
    to file ``(no output captured)``, with the cause legible only in the CLI's session
    transcript under ``~/.claude/projects/`` — a post-mortem artifact that explains
    nothing. Retention itself classifies nothing; the verdict above is read off the record
    it keeps, and clearing that verdict when the session recovers leaves the text kept.

    Three properties this retention is held to:

    * it is **unconditional** — every marked report is kept whatever its cause, including
      one the session then recovered from and one the vendor marked permanent, because a
      harness that is holding the text must never file ``(no output captured)``;
    * a report the CLI forwarded for a **sub-agent** (the Task's ``parent_tool_use_id``,
      ``isSidechain`` in the persisted-transcript spelling) is kept **labelled as such**
      (:data:`_SUBAGENT_NOTE`) — a log that confidently names the wrong death is worse
      than the silence it replaces;
    * where a stream carries several candidate records, the one **nearest the leaf's own
      death** wins and a farther one — a ``result`` wrap-up naming only the effect, a
      sub-agent's report — cannot bury it, in either arrival order
      (:func:`_note_terminal`).

    The append is skipped under ``capture``: there ``output`` is the raw stdout the
    caller asked to keep verbatim (JSONL for a stream family, a gate's evidence line),
    and the report is already in those bytes. Families whose stream this module has no
    observed error shape for (codex) and stream-less families degrade to today's
    behaviour rather than guessing at a vendor's error text.

    ``timeout``, if given, is the wall-clock bound in seconds (issue #368): on expiry
    the child's whole process GROUP is terminated (SIGTERM, then SIGKILL after a
    grace) and the returncode slot carries :data:`TIMEOUT_RC` — a distinguishable
    "the oracle did not answer" outcome, never a verdict the child produced. The
    child is started in its own session for this, because a ``shell=True`` gate's
    real work is a *grandchild*: killing only the shell would orphan it, still
    running. ``timeout=None`` (the default) is today's unbounded behaviour,
    unchanged — the heartbeat keeps a hung child looking alive forever, which is
    exactly the 19h-hung-gate failure this bound exists to end.

    On POSIX, every child whose stdio the harness owns (``capture`` /
    ``stream_json``) or whose wall-clock is bounded runs in its own session, and
    whatever it leaves running in its process group when it exits — by any path:
    normal return, timeout, Ctrl-C — is swept (SIGTERM, short grace, SIGKILL),
    with one stderr note naming the command (issue #372). ``proc.wait`` returning
    only proves the *direct* child exited; under ``shell=True`` (every gate) that
    child is just the shell, so surviving work is the rule, not the edge case —
    measured: a leaked test process burned ~100% of a core for 21 hours, and a
    straggler still holds ports, locks and fixtures when the next cycle's gates
    run in the same lane worktree. A child that exits leaving no survivors sees
    no sweep and no note. The interactive leaves (no capture, no stream, no
    bound) are never sessionized: they keep the terminal exactly as today.

    ``status``, if given, is called on every tick to append a live snapshot of the
    child's work (e.g. which artifacts exist yet, time since the last write) — so the
    heartbeat shows *what* is happening, not just that time passed (Tier 1+2). It is
    best-effort: any exception it raises is swallowed so a probe can never break the run.

    ``stream_json`` (Tier 3) parses the child's stdout as Claude's
    ``--output-format stream-json`` event stream and surfaces the **tool it is using
    right now** (``▸ Editing patch.diff`` / ``▸ Running run-tests``) on each tick.
    stdout is consumed for parsing (not echoed); stderr is **teed** — still echoed
    live so real errors show, *and* its tail retained for the caller. Mutually
    exclusive with ``capture`` (capture wins if both set).

    ``tee_stderr`` asks for that same stderr tee **without** the stream parse, for a
    family that has no event stream (``generic``, ``gemini``): stdout keeps inheriting
    the terminal (its output stays live, exactly as before), but stderr is piped, echoed,
    and its tail returned — so a stream-less leaf's failure is diagnosable too, and not
    only claude's. Implied by ``stream_json``; ignored under ``capture`` (which already
    keeps everything).

    ``telemetry``, if given, is called with the **child's pid** once right after the
    spawn and again on every tick; a non-empty return is appended to the tick line.
    It is the resource-observation hook (`leaves._MemoryTelemetry` samples the child's
    scope cgroup through it): the pid is the one datum only this function has, and the
    tick is the one moment the harness is already awake while the child works. The same
    best-effort contract as ``status``: any exception it raises is swallowed — an
    observer can never break the run it observes.

    ``on_event``, if given, is called with every decoded ``stream_json`` event, in
    arrival order, from the drain thread (issue #526). The stream is otherwise read here
    and dropped, and on a **clean exit** nothing of it reaches the caller: a leaf that
    exits 0 without doing its job — every command it tried refused by a sandbox that
    could not start — leaves no trace of why. This hook is how a caller keeps what it
    needs from the stream without this function learning the caller's question. Called
    only on the stream path, and only for a line that decodes to a JSON object. The
    same best-effort contract as ``telemetry``: any exception it raises is swallowed.
    """
    tee_err = (stream_json or tee_stderr) and not capture
    capture_out = capture or stream_json
    stdin = subprocess.PIPE if input_text is not None else None
    if capture:
        stdout, stderr = subprocess.PIPE, subprocess.STDOUT
    elif stream_json:
        stdout, stderr = subprocess.PIPE, subprocess.PIPE  # parse stdout; tee stderr
    elif tee_err:
        stdout, stderr = None, subprocess.PIPE  # stdout stays live; tee stderr only
    else:
        stdout, stderr = None, None
    # Sessionize (POSIX only) every child whose stdio the harness owns — capture or
    # stream_json — as well as any bounded child (#368's condition, widened by #372).
    # A new session makes the child the leader of its own process group
    # (pgid == proc.pid), the only handle that still reaches what a shell=True
    # child spawned after the shell itself exits. The interactive leaves (no
    # capture, no stream, no bound) are NOT sessionized, so they keep the
    # terminal's foreground process group exactly as today.
    sessionize = os.name == "posix" and (capture or stream_json or timeout is not None)
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, env=env, text=True,
        stdin=stdin, stdout=stdout, stderr=stderr,
        start_new_session=sessionize,
    )

    chunks: list[str] = []
    err_tail: deque[str] = deque(maxlen=200)  # bounded stderr tail for a failed leaf
    produced = {"session": False}  # did a substantive stream event arrive (real work)?
    # The leaf's OWN account of a terminal death (#506), kept for the caller's error log.
    # ``shape`` keeps the marked records apart by how near each is to the leaf's own
    # death, so neither the session's wrap-up nor a sub-agent's report can overwrite the
    # main session's own account of the cause (:func:`_note_terminal`).
    terminal = {"text": "", "shape": ""}
    # How the leaf died, by its own account (#539): does the record ``terminal`` keeps name
    # a cause the vendor marks transient? Main-session work after it means the CLI
    # recovered, which clears this verdict only — the kept text stays for the error log.
    # Cleared, it stays cleared until a record at least as near as the kept one names a
    # death again: a fresh main-session report does; a later ``result`` wrap-up cannot,
    # being farther (:func:`_note_terminal` does not keep it, so it sets nothing).
    died_of = {"transient": False}
    # Is the account's usage limit refusing requests, by the stream's NEWEST word on it
    # (#539)? A spent subscription window fails every attempt until it resets, hours away,
    # so it outranks any verdict above (:func:`_usage_limit_refusal`).
    usage_limit = {"refused": False}
    latest_tool = {"label": ""}  # most recent tool-use, updated by the drain thread
    readers: list[threading.Thread] = []
    if capture_out:
        def _drain() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:  # drain so the pipe can't fill and stall the child
                if capture:
                    chunks.append(line)
                if stream_json:
                    # Decoded ONCE per line, then classified by each reader. This is a hot
                    # loop — a long session streams thousands of lines — and each
                    # classifier below used to re-parse the same bytes for itself. A
                    # line that is no JSON object decodes to ``{}``, which every
                    # classifier answers exactly as it answered the raw line before.
                    ev = _stream_event(line)
                    if _is_session_event(ev, stream_format):
                        produced["session"] = True  # a startup/init line does NOT count
                    text, shape = _terminal_error(ev, stream_format)
                    if shape:  # the CLI's own marked report — keep it, whatever its cause
                        if _note_terminal(terminal, text, shape):
                            # The verdict follows the record kept, so a farther record
                            # cannot overrule the cause the nearest one reported.
                            died_of["transient"] = _reports_transient_cause(
                                ev, stream_format)
                    elif died_of["transient"] and _is_main_session_work(ev, stream_format):
                        died_of["transient"] = False  # the CLI recovered: not the death
                    refused = _usage_limit_refusal(ev, stream_format)
                    if refused is not None:  # the account's state as it now stands
                        usage_limit["refused"] = refused
                    lbl = _stream_tool_label(ev, stream_format)
                    if lbl:
                        latest_tool["label"] = lbl
                    if on_event is not None and ev:
                        try:
                            on_event(ev)
                        except Exception:  # an observer must never break the run
                            pass
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        readers.append(t)
    if tee_err:
        def _drain_err() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:  # echo live (errors still show) AND keep the tail
                sys.stderr.write(line)
                sys.stderr.flush()
                err_tail.append(line)
        t = threading.Thread(target=_drain_err, daemon=True)
        t.start()
        readers.append(t)

    if input_text is not None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(input_text)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    if telemetry is not None:
        # Baseline sample at t≈0. The observer's cgroup discovery is racy this early
        # (systemd-run may not have entered its scope yet) and skips itself when it is;
        # the point is that a leaf which dies before the first tick still had one chance
        # to be observed.
        try:
            telemetry(proc.pid)
        except Exception:
            pass
    suffix = f" — {label}" if label else ""
    start = time.monotonic()
    deadline = None if timeout is None else start + timeout
    timed_out = False
    try:
        while True:
            wait_for = interval
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # The bound expired: kill the whole group, then reap. The heartbeat
                    # would otherwise keep printing "… still working" forever — the very
                    # mechanism built so a slow gate would not look hung is what kept a
                    # genuinely hung gate from looking hung (#368).
                    timed_out = True
                    _terminate_group(proc)
                    break
                wait_for = min(interval, remaining)
            try:
                proc.wait(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                mins, secs = divmod(int(time.monotonic() - start), 60)
                bits: list[str] = []
                if stream_json and latest_tool["label"]:
                    bits.append(f"▸ {latest_tool['label']}")
                if status is not None:
                    try:
                        snap = status()
                        if snap:
                            bits.append(snap)
                    except Exception:  # a status probe must never break the run
                        pass
                if telemetry is not None:
                    try:
                        sample = telemetry(proc.pid)
                        if sample:
                            bits.append(sample)
                    except Exception:  # an observer must never break the run
                        pass
                extra = (" · " + " · ".join(bits)) if bits else ""
                print(f"   … still working ({mins}m{secs:02d}s elapsed){suffix}{extra}",
                      file=sys.stderr, flush=True)
    except BaseException:
        # Ctrl-C / abort mid-wait: the same no-survivors contract as expiry. The
        # sweep condition is "sessionized", not "bounded" (#372 widens #368's
        # timeout-only kill): any group this invocation owns must not outlive
        # it, however the wait ends.
        if sessionize:
            _terminate_group(proc)
        raise
    if sessionize and not timed_out:
        # Normal exit — the overwhelmingly common path — sweeps too (#372), and it
        # runs BEFORE the drain-join/close below, mirroring the timeout path's
        # kill-then-close order: a straggler that inherited the capture pipe keeps
        # the drain thread blocked mid-read, and closing the stream then waits on
        # that blocked reader (measured: two ~5-minute hangs before the reorder).
        # Killed first, the last writer dies, the drain sees EOF, and neither the
        # join nor the close can block.
        _sweep_stragglers(proc.pid, cmd)
    for reader in readers:
        reader.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    output = "".join(chunks) if capture else ("".join(err_tail) if tee_err else "")
    if terminal["text"] and not capture:
        # The diagnostic the incident had to be dug out of ~/.claude/projects/ rides
        # `output` into the caller's `*.error.log` by the same route the stderr tail
        # does, so no reader downstream needs a special case for it (#506) — appended
        # after the tail, exactly as the #420 memory post-mortem is. NOT under
        # `capture`: that output is the child's raw stdout, which a gate keeps as its
        # evidence line and a stream family emits as JSONL — the report is in it already.
        sep = "" if not output or output.endswith("\n") else "\n"
        output = f"{output}{sep}{_TERMINAL_REPORT_HEADER}\n{terminal['text']}\n"
    rc = TIMEOUT_RC if timed_out else proc.returncode
    # The verdicts are applied only to the outcome they describe (#539): a run that ended
    # non-zero. Exit 0 keeps `produced` as it was, and so does the harness's own timeout —
    # "the oracle did not answer" is not a death the stream diagnosed. A signal kill is the
    # caller's question, answered from `rc` (`is_signal_death`), not this stream's.
    died = rc not in (0, TIMEOUT_RC)
    ended_on_reported_infra = died and died_of["transient"]
    # A spent usage window is no transient death in EITHER shape: whatever the leaf did or
    # reported, a fresh attempt is refused on its first request until the window resets.
    refused_by_usage_limit = died and usage_limit["refused"]
    return (rc, output,
            (produced["session"] and not ended_on_reported_infra) or refused_by_usage_limit)


def _terminate_group(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """SIGTERM the child's process GROUP, escalating to SIGKILL after ``grace`` seconds.

    Gates run under ``shell=True``, so ``proc`` is the shell and the real work is a
    grandchild — terminating only ``proc`` would orphan it, still consuming the very
    wall-clock the bound exists to cap. The child was started with
    ``start_new_session=True`` (see :func:`run_with_heartbeat`), so its process-group
    id is ``proc.pid`` and ``os.killpg`` reaches every member. Best-effort on the
    signals (the group may already be gone); the final ``wait`` reaps the child so no
    zombie survives the timeout path.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _sweep_stragglers(pgid: int, cmd, grace: float = 2.0) -> None:
    """Kill whatever an exited child left running in its process group (#372).

    ``proc.wait`` returning only proves the *direct* child exited; under
    ``shell=True`` that child is just the shell, and work it backgrounded
    survives the call — measured: one leaked test process burned a core for 21
    hours, and a straggler holding ports/locks/fixtures into the next cycle's
    gates is the class of one-off never-reproducible gate red. The child was
    sessionized (``pgid == proc.pid``), so the group id still reaches every
    survivor after the leader is gone. SIGTERM → ``grace`` seconds → SIGKILL,
    with ONE stderr note naming the command: a straggler is a signal worth
    surfacing, not just a mess to mop. No survivors ⇒ no signal, no note —
    the common clean exit stays byte-identical.

    Known limitation (deferred to the subreaper design, #383): the swept
    grandchild is not this process's child, so it cannot be reaped here — under
    a non-reaping init (e.g. a minimal container) the group kill leaves a
    zombie table entry. A naive ``waitpid(-1)`` reaper thread would be strictly
    worse: it can steal exit statuses from concurrent lane ``Popen.wait()``s
    and corrupt a gate verdict, so no global reaper is added in this change.
    """
    if not _group_alive(pgid):
        return
    shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    print(f"   ⚠ swept surviving processes of: {shown[:200]}",
          file=sys.stderr, flush=True)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _group_alive(pgid: int) -> bool:
    """Is any LIVE (non-zombie) member left in process group ``pgid``?

    Prefers ``/proc`` (Linux) because it is zombie-aware: ``killpg(pgid, 0)``
    counts an unreaped zombie as a member, and under a non-reaping PID 1 (a
    container where the runner is init) that would make a fully-dead group look
    alive forever — a phantom sweep note on every clean exit. Where ``/proc``
    is absent (other POSIX), falls back to the ``killpg`` probe.
    """
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
            except OSError:
                continue  # the process raced away between listing and reading
            # /proc/<pid>/stat is `pid (comm) state ppid pgrp …`; comm may hold
            # spaces/parens, so split AFTER the last `)`.
            fields = stat.rpartition(")")[2].split()
            if len(fields) >= 3 and fields[0] != "Z" and fields[2] == str(pgid):
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # the group exists but is not ours to signal — still alive
    return True


# ----------------------------------------------------------------------------
# Tier 3 — parse a leaf's JSONL event stream for the live tool-use surfaced on each
# heartbeat tick. Vendor event shapes differ, so the parsers dispatch on the family's
# ``stream_format``; a family whose format isn't in ``STREAM_FORMATS`` runs stream-less
# (Tiers 1+2 only). claude: ``--output-format stream-json``; codex: ``exec --json``.
# ----------------------------------------------------------------------------
STREAM_FORMATS = frozenset({"claude-stream-json", "codex-stream-json"})

_SESSION_EVENT_TYPES = frozenset({"assistant", "user", "result"})
# codex `exec --json`: real work is an item/turn event; thread.started / turn.started
# are startup-only (like claude's ``system`` init), so they don't count as "produced".
_CODEX_SESSION_TYPES = frozenset({"item.started", "item.completed", "turn.completed"})


def _stream_event(line: str | dict) -> dict:
    """One drained stream line decoded **once**: the record, or ``{}`` when the line is
    no JSON object (a non-JSON line, a bare scalar, an empty read).

    The drain reads a line and classifies it three ways — did real work happen
    (:func:`_is_session_event`), did the leaf report its own death
    (:func:`_terminal_error`), what tool is it running (:func:`_stream_tool_label`).
    Each parsed the same bytes for itself, so the drain paid one ``json.loads`` per
    classifier on every line of what is a hot loop (two before retention was a
    question, three with it) and carried a copy of the same "is it an object" guard in
    each, free to drift apart. They share this one decode now, and each still accepts a
    raw line — an already-decoded record passes straight through — so a caller holding
    only the text is unaffected.

    ``{}`` rather than ``None`` for "not a record": every classifier reads the record
    with ``.get``, so the empty one answers each of them exactly as the unparseable line
    it came from did, with no separate not-a-record branch to keep in step.
    """
    if isinstance(line, dict):
        return line
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return {}
    return ev if isinstance(ev, dict) else {}


def _is_session_event(line: str | dict, fmt: str = "claude-stream-json") -> bool:
    """True iff a stream line is **substantive work** — not a startup/init event the CLI
    emits before doing anything. A non-zero exit having produced no such event, not ended
    by a signal and not refused by a spent usage limit, is one of the two transient-infra
    shapes (#138; the other, #539, is a stream that ends on the vendor's own transient
    report — :func:`run_with_heartbeat`). Best-effort: non-JSON → False."""
    ev = _stream_event(line)
    if fmt == "codex-stream-json":
        return ev.get("type") in _CODEX_SESSION_TYPES
    return ev.get("type") in _SESSION_EVENT_TYPES


# ----------------------------------------------------------------------------
# The leaf's own account of a TERMINAL failure (issue #506) — RETENTION ONLY.
#
# The incident: an escalated builder ran ~18 minutes, the API connection dropped
# mid-response, the CLI exited 1, and `build.error.log` read "(no output captured)" —
# "a post-mortem artifact that explains nothing" by the harness's own written rule. The
# cause was legible only in the CLI's session transcript under ~/.claude/projects/,
# which no post-mortem reads. The stream carried it the whole time: the drain parsed
# each line for a tool label and dropped it.
#
# The discriminator is NOT the prose. The CLI *marks* the message it synthesises for an
# API error, so a leaf that merely writes about an API error — a builder fixing this very
# defect, quoting the incident line — is never mistaken for one dying of it, whatever its
# wording. Nothing in THIS section reads a cause, a kind or a status: it keeps the text
# and says whose it is. Whether that cause is worth another attempt is the next
# section's question (#539), asked of the same marked record.
#
# WHICH field lives on WHICH event is read out of the shipped binary (claude-code
# 2.1.234), not assumed: a rule keyed on a field the CLI never emits is a branch that
# looks like coverage and is not. The main loop's stream emitter is
# `{type:"assistant", …, session_id, parent_tool_use_id: null, uuid, timestamp,
# error: r.error, …r.isApiErrorMessage===!0 && {is_api_error_message:!0}}`; the
# persisted transcript spells the same mark `isApiErrorMessage`. The session's `result`
# wrap-up is built as `{subtype:"success", api_error_status: …, result: <its text>,
# is_error: …}` or, when the turn threw, `{subtype:"error_during_execution",
# errors:[…]}`.
#
# WHOSE death it is, is read out of the same binary: the mark is forwarded to a SUBAGENT's
# messages too (the `agent_progress` branch yields `{type:"assistant",
# parent_tool_use_id: e.parentToolUseID, …, …i.isApiErrorMessage===!0 &&
# {is_api_error_message:!0}}`), and the CLI recovers from those itself. So a sub-agent's
# report is kept LABELLED — never presented as the leaf's own death, and never allowed to
# bury the main session's own report. Both spellings of the scope (the stream's
# `parent_tool_use_id`, the transcript's `isSidechain`) are answered by one predicate,
# `_is_subagent_event`, so they cannot drift apart.
# (template/tests/fixtures/README.md pins the observed records and marks every claim
# above observed or derived.)
# ----------------------------------------------------------------------------
#: A marked API-error message the MAIN session emitted: the CLI's own report of the
#: failure it is giving up on — the record nearest the leaf's own death.
_REPORT = "report"
#: The same marked message emitted for a SUBAGENT (:func:`_is_subagent_event`). Kept as
#: evidence, prefixed :data:`_SUBAGENT_NOTE` so the log never passes it off as the leaf's
#: own death: the CLI has its own recovery for a subagent's API-error termination, so
#: "a Task hit a 529" is not "the leaf died of a 529".
_SUBAGENT_REPORT = "subagent-report"
#: The session's ``result`` wrap-up: it records the EFFECT (the session ended, in error)
#: — never the cause.
_WRAPUP = "wrapup"
#: How near each record is to the leaf's OWN death — the order :func:`_note_terminal`
#: keeps, so a farther record cannot overwrite a nearer one, in either arrival order.
_TERMINAL_PRECEDENCE = {_SUBAGENT_REPORT: 1, _WRAPUP: 2, _REPORT: 3}
#: Prefix for a retained sub-agent report, so a post-mortem reader sees what it is. A log
#: that confidently names the wrong death is worse than the "(no output captured)" this
#: change replaces.
_SUBAGENT_NOTE = "[sub-agent report, not the leaf's own death] "
#: Banner for the retained report in ``output``, so a reader of the error log can tell
#: the leaf's own stream report from the stderr tail it is appended to (the #420 memory
#: post-mortem announces itself the same way).
_TERMINAL_REPORT_HEADER = "----- leaf stream report -----"
#: One line of post-mortem, not a transcript — the same bounded retention the stderr tail
#: already gets (``err_tail``). Every observed API-error report is far shorter (the
#: incident's was 78 characters); the bound is there for the ``result`` wrap-up, which can
#: restate a whole final message.
_TERMINAL_ERROR_MAX = 500
#: Marks a report the bound above cut, so the artifact never presents a fragment as the
#: leaf's whole account of its death — the reader is told there is more, and where the
#: rest is (the session transcript this retention exists to stop being the only copy).
_TRUNCATED_NOTE = f" … [report truncated at {_TERMINAL_ERROR_MAX} chars]"


def _terminal_error(line: str | dict, fmt: str = "claude-stream-json") -> tuple[str, str]:
    """The leaf's own report of a terminal failure in one stream line: its text, and
    which marked record it is (:data:`_REPORT` / :data:`_SUBAGENT_REPORT` /
    :data:`_WRAPUP`). ``("", "")`` when the line is not such a record (#506).

    The retention companion to :func:`_is_session_event`, in the same shape: dispatch on
    the family's ``stream_format``, the drain's shared best-effort decode
    (:func:`_stream_event`), and a **degrade-to-today** default. Only the claude format
    has an observed error shape, so codex — and every stream-less family — answers
    ``("", "")`` and keeps exactly today's behaviour instead of guessing at a vendor's
    error text.

    Two event shapes carry it, both marked by the CLI itself:

    * an ``assistant`` event flagged ``is_api_error_message`` (the stream spelling) /
      ``isApiErrorMessage`` (the persisted transcript's), whose text is the failure the
      CLI is giving up on. **Whose** failure it was decides which record it is
      (:func:`_is_subagent_event`): the main loop's own emitter hard-codes
      ``parent_tool_use_id: null`` (:data:`_REPORT`), while the ``agent_progress`` branch
      forwards the same mark for a sub-agent with the Task's ``parent_tool_use_id`` —
      ``isSidechain`` in the transcript spelling (:data:`_SUBAGENT_REPORT`);
    * a ``result`` event with ``is_error`` — the session's wrap-up (:data:`_WRAPUP`),
      which names the effect and, in the ``error_*`` variants, whatever else ended the
      turn. Kept when it is all there is, outranked by the report that names the cause.

    The text is returned for **any** such record: retention is unconditional, because the
    cause the vendor reported is not this function's to judge, and a permanent failure
    must explain itself in the bundle just as loudly as a transient one. Nothing here
    feeds a retry decision; :func:`_reports_transient_cause` judges the record this keeps
    (#539).
    """
    if fmt != "claude-stream-json":
        return "", ""
    ev = _stream_event(line)
    if ev.get("type") == "assistant" and (ev.get("is_api_error_message")
                                          or ev.get("isApiErrorMessage")):
        kind = ev.get("error")
        kind = kind.strip() if isinstance(kind, str) and kind.strip() else ""
        text = (_report_line(_claude_message_texts(ev.get("message")))
                or f"API error ({kind or 'no kind reported'}) reported by the leaf")
        if _is_subagent_event(ev):
            return f"{_SUBAGENT_NOTE}{text}", _SUBAGENT_REPORT
        return text, _REPORT
    if ev.get("type") == "result" and ev.get("is_error"):
        text = _report_line(_claude_result_texts(ev))
        # An error record with nothing to say is not evidence: keep today's silence
        # rather than file a banner over an empty line.
        return (text, _WRAPUP) if text else ("", "")
    return "", ""


def _note_terminal(terminal: dict, text: str, shape: str) -> bool:
    """Record one marked terminal record into the drain's ``terminal`` state: newest wins
    **within a shape**, but a record farther from the leaf's own death never overwrites a
    nearer one (:data:`_TERMINAL_PRECEDENCE`).

    An api-error death does not end the stream. Two kinds of record keep arriving after
    the report that named the cause, and read newest-wins either buries it:

    * the session's ``result`` wrap-up, which names no cause — it restates the dead
      message, or (when the turn threw or was aborted) carries only a diagnostic about
      the turn. Let it win and the artifact records the effect while the cause, which the
      harness was holding, is dropped again;
    * a **sub-agent's** report — chatter from a Task that was still draining when the
      session died. Let it win and the error log names the wrong death: a permanent
      main-session failure reported as some Task's blip.

    Precedence is by shape, not "any later record disagrees": a SECOND main-session report
    is the leaf's newer account of its own death and wins outright. Symmetrically, a
    farther record arriving FIRST cannot pre-empt the report — so the nearest record wins
    in either arrival order.

    Returns whether the record was kept, so the drain's verdict (#539) follows the kept
    record: a farther record that cannot bury the cause's text cannot overrule its
    classification either.
    """
    if _TERMINAL_PRECEDENCE[shape] < _TERMINAL_PRECEDENCE.get(terminal["shape"], 0):
        return False
    terminal.update(text=text, shape=shape)
    return True


def _is_subagent_event(ev: dict) -> bool:
    """Was this record emitted for a **sub-agent** (a Task), rather than by the main
    session itself? One predicate for both spellings of the same scope, so they can never
    drift apart.

    The stream says it with ``parent_tool_use_id`` — the ``agent_progress`` branch
    forwards the Task's tool_use id, while the main loop's own emitters hard-code
    ``null``; the persisted transcript says it with ``isSidechain``, and carries no
    ``parent_tool_use_id`` at all (so ``.get()`` returning ``None`` there must not be read
    as "the main session"). Absent both, the record is the main session's own — which is
    what every observed main-session record looks like in either spelling.
    """
    return ev.get("parent_tool_use_id") is not None or ev.get("isSidechain") is True


def _claude_message_texts(message) -> Iterable[str]:
    """The human-readable text blocks of a claude ``assistant`` event's message — where
    the CLI puts the API failure it is giving up on."""
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            yield str(block.get("text") or "")


def _claude_result_texts(ev: dict) -> Iterable[str]:
    """The error strings a claude ``result`` event carries: its ``errors`` list (the
    ``error_*`` variants) or its ``result`` string (the ``success`` variant, where an
    API-error ending puts the dead message's own text); a plain ``error`` key is read too,
    as a fallback for a shape neither of those covers."""
    errors = ev.get("errors")
    for item in errors if isinstance(errors, list) else []:
        if isinstance(item, str) and item.strip():
            yield item
    for key in ("result", "error"):
        val = ev.get(key)
        if isinstance(val, str) and val.strip():
            yield val


def _report_line(texts: Iterable[str]) -> str:
    """The first non-empty report, whitespace-flattened and bounded — a diagnostic line
    for the error log, not a transcript.

    A line that WAS cut says so (:data:`_TRUNCATED_NOTE`). The bound keeps one death from
    filling an error log, but a silently-cut report reads as the leaf's whole account of
    its death, and a reader would never know to go looking for the rest.
    """
    for text in texts:
        flat = " ".join(text.split())
        if not flat:
            continue
        if len(flat) <= _TERMINAL_ERROR_MAX:
            return flat
        return flat[:_TERMINAL_ERROR_MAX] + _TRUNCATED_NOTE
    return ""


# ----------------------------------------------------------------------------
# What a transient death IS (issue #539) — the classification of the record kept above.
#
# The vendor's own marks decide, the most specific first, and the harness's reading of
# prose never outranks one. A typed cause is the CLI's most specific word, so it decides
# whenever the report carries one; otherwise the error kind (`error`) does. A kind outside
# the transient set — a 400, an auth or billing stop, a kind a later CLI adds — keeps the
# substantive verdict whatever its text says. The one exception is the kind the CLI uses
# for "I could not classify this", whose text alone is read: its leading HTTP status when
# it has one, and otherwise only the wording of a connection that failed. Above all of it
# sits the account's usage limit: while the stream's newest `rate_limit_event` says the
# limit refuses requests, no death is transient, whatever the report said
# (`_usage_limit_refusal`). Every field and set below was re-read out of claude-code
# 2.1.284 (tests/fixtures/README.md: which claims are observed, which derived, and the
# greps that re-verify them).
# ----------------------------------------------------------------------------
#: The error kinds this harness retries. ``server_error`` and ``overloaded`` are the
#: vendor's own main-session rule (``apiErrorIsTransient===!0 || error==="overloaded" ||
#: error==="server_error"``; the flag never reaches the stream), and ``server_error`` is
#: what the CLI stamps on a 5xx and on a lost connection (``Connection to the API was lost
#: (<code>)``, ``Connection lost mid-response``). ``rate_limit`` is this PROJECT's policy,
#: not that rule: a PASSING mid-session rate-limit rejection is the twin of the
#: invocation-time one #138 already retried. A rejection because a subscription window is
#: spent is not one — every fresh attempt is refused on its first request until the window
#: resets — and the stream's own rate-limit record vetoes it (:func:`_usage_limit_refusal`).
#: The three-kind set is borrowed from the CLI's handling of a SUB-AGENT an API error cut
#: off (``new Set(["rate_limit","overloaded","server_error"])``: it keeps that Task's
#: partial output instead of failing it).
_CLAUDE_TRANSIENT_KINDS = frozenset({"server_error", "overloaded", "rate_limit"})
#: Where the vendor TYPES a report's cause — stream spelling, then transcript spelling: the
#: CLI's own typed kind, and the server's gate code for a gate this CLI has no typed kind
#: for yet. Either one outranks the kind: the usage-credit stops ride ``rate_limit``, a TLS
#: trust failure rides ``server_error``.
_CLAUDE_CAUSE_FIELDS = ("api_error", "apiError", "api_error_code", "apiErrorCode")
#: The one typed cause that names a category this harness retries: no response arrived
#: before the first-byte deadline, on any attempt — a response that never came, the lost-
#: connection family. Every other typed cause names a condition a fresh attempt meets
#: again — a certificate store, a proxy, credentials, an entitlement, the request itself —
#: and so, for want of a reading, does one this harness has not seen.
_CLAUDE_TRANSIENT_CAUSES = frozenset({"no_response"})
#: The kind the CLI's mapper falls back to when it could not classify the failure itself.
_CLAUDE_UNKNOWN_KIND = "unknown"
#: HTTP statuses worth another attempt besides 5xx — the API client's own retry rule
#: (``status===408 || status===409 || status===429 || status>=500``).
_TRANSIENT_STATUSES = frozenset({408, 409, 429})
#: The HTTP status an ``unknown`` report LEADS with. The CLI writes an API rejection it did
#: not classify as ``API Error: <status> <body>``, so the status is read at the head of the
#: text only: a number further in — a message index (``messages.536…``), a field value, a
#: count — belongs to the body, not to the answer.
_UNKNOWN_STATUS_RE = re.compile(r"\s*API\s+Error:\s*(\d{3})\b")
#: How a connection that FAILED reads — a failure that never got an HTTP answer, so its
#: report has no status to lead with. Read ONLY in an ``unknown`` report that carries no
#: typed cause and no leading status: the wording Node, undici, the Anthropic SDK and the
#: CLI itself use for a lost, dropped, refused or timed-out connection, and the CLI's own
#: connection-error codes (its ``JF`` and ``J0`` sets). No bare word a request body could
#: carry by accident — not ``timeout``, not a number.
_CONNECTION_FAILED_RE = re.compile(r"""
      \bconnection\s+(?:to\s+the\s+api\s+)?(?:was\s+)?
         (?:lost|dropped|reset|closed|refused|aborted|interrupted|error|timed\s+out)\b
    | \brequest\s+timed\s+out\b
    | \bsocket\s+hang\s+up\b
    | \bother\s+side\s+closed\b
    | \bpremature\s+close\b
    | \bfetch\s+failed\b
    | \AAPI\s+Error:\s*terminated\W*\Z
    | \b(?:ECONNRESET|EPIPE|ConnectionClosed|UND_ERR_SOCKET|ETIMEDOUT|ECONNABORTED
         |ERR_SOCKET_CLOSED|StreamSuspended|StreamTruncated|ECONNREFUSED|ConnectionRefused
         |ENOTFOUND|ENETUNREACH|ENETDOWN|EHOSTUNREACH|EHOSTDOWN|EAI_AGAIN
         |FailedToOpenSocket|ERR_PROXY_TUNNEL)\b
""", re.I | re.X)


def _reports_transient_cause(line: str | dict, fmt: str = "claude-stream-json") -> bool:
    """Does this marked record say the leaf died of a cause the **vendor** marks transient?

    The classification companion to :func:`_terminal_error` (which keeps the record) and
    :func:`_is_session_event`, in their shape: dispatch on ``stream_format``, the drain's
    shared decode, and ``False`` for every family and record it does not know — so codex
    and every stream-less family keep exactly today's verdict. A field of an unexpected
    type is a ``False`` too, never an exception: this runs in the drain thread.

    * A main-session **report** the vendor left **unstamped** (no ``error`` kind) is not
      transient — there is no vendor judgement to follow. A **typed cause**
      (:data:`_CLAUDE_CAUSE_FIELDS`, either spelling) decides next, before the kind: only
      :data:`_CLAUDE_TRANSIENT_CAUSES` is transient, so a ``rate_limit`` the CLI typed as a
      usage-credit stop is not, nor a ``server_error`` it typed as a TLS trust failure.
      Untyped, the kind decides (:data:`_CLAUDE_TRANSIENT_KINDS`). A kind stamped
      :data:`_CLAUDE_UNKNOWN_KIND` is the vendor saying it could not tell, so its text is
      read — the only door prose comes in through (:func:`_unclassified_text_is_transient`).
      Any other kind (the permanent ones, one this harness has not read) is not transient,
      whatever the text says.
    * A **sub-agent's** report never is: the CLI handles a Task an API error cut off
      itself, so the report says nothing about how the session ended.
    * The ``result`` wrap-up is transient only in its ``success`` variant — the one the CLI
      builds when the session ended on its own API-error message, and the only one that
      carries that error's HTTP status — with a status a retry can clear (5xx, or one of
      :data:`_TRANSIENT_STATUSES`) and no server gate code. An ``error_*`` wrap-up ends on
      whatever threw, so its status, if any, is not the death's.
    """
    if fmt != "claude-stream-json":
        return False
    ev = _stream_event(line)
    if ev.get("type") == "assistant" and (ev.get("is_api_error_message")
                                          or ev.get("isApiErrorMessage")):
        if _is_subagent_event(ev):
            return False
        kind = ev.get("error")
        if not isinstance(kind, str):
            return False  # unstamped: the vendor made no judgement to follow
        causes = _typed_causes(ev)
        if causes:  # the vendor typed the cause, and that outranks the kind
            return all(isinstance(c, str) and c in _CLAUDE_TRANSIENT_CAUSES for c in causes)
        if kind in _CLAUDE_TRANSIENT_KINDS:
            return True
        if kind != _CLAUDE_UNKNOWN_KIND:
            return False
        texts = (t for t in _claude_message_texts(ev.get("message")) if t.strip())
        return _unclassified_text_is_transient(next(texts, ""))
    if ev.get("type") == "result" and ev.get("is_error"):
        return (ev.get("subtype") == "success" and not _typed_causes(ev)
                and _is_transient_status(ev.get("api_error_status")))
    return False


def _typed_causes(ev: dict) -> list:
    """Every typed cause the vendor put on a record, in either spelling; ``[]`` if none."""
    return [ev[key] for key in _CLAUDE_CAUSE_FIELDS if ev.get(key) is not None]


def _is_transient_status(status) -> bool:
    """An HTTP status a retry can clear: 5xx, or one of :data:`_TRANSIENT_STATUSES`."""
    return isinstance(status, int) and (status in _TRANSIENT_STATUSES or 500 <= status <= 599)


def _unclassified_text_is_transient(text: str) -> bool:
    """Read the text of a report the vendor could NOT classify — and only for the
    categories the harness promises to retry.

    A leading HTTP status decides alone (:data:`_UNKNOWN_STATUS_RE`): what follows it is
    the API's body, so a 400 is never promoted by a word or a number inside it. Without
    one, the report is of a failure that got no HTTP answer at all, and only the wording
    of a connection that failed counts (:data:`_CONNECTION_FAILED_RE`)."""
    lead = _UNKNOWN_STATUS_RE.match(text)
    if lead:
        return _is_transient_status(int(lead.group(1)))
    return bool(_CONNECTION_FAILED_RE.search(text.strip()))


#: The event types that are the session doing turn work. A ``result`` is its wrap-up, so
#: it is not "the session carried on".
_WORK_EVENT_TYPES = frozenset({"assistant", "user"})


def _is_main_session_work(line: str | dict, fmt: str = "claude-stream-json") -> bool:
    """Is this line the MAIN session carrying on — so a report before it was not the
    leaf's death, and whatever fails later is the leaf's own (#539)?

    ``user`` belongs here as much as ``assistant``: a session that recovers and runs a
    tool answers with a main-session ``tool_result``, emitted as ``type: "user"`` with
    ``parent_tool_use_id: null``. **Main-session** matters as much: a sub-agent's traffic
    (:func:`_is_subagent_event`) is a Task still draining, and letting it count would clear
    the very report that explains the death.
    """
    if fmt != "claude-stream-json":
        return False
    ev = _stream_event(line)
    return ev.get("type") in _WORK_EVENT_TYPES and not _is_subagent_event(ev)


#: The record in which claude-code reports the account's usage-limit state, emitted
#: whenever that state changes: ``{type:"rate_limit_event", rate_limit_info:{status,
#: resetsAt, rateLimitType, isUsingOverage, …}, uuid, session_id}``.
_RATE_LIMIT_EVENT = "rate_limit_event"
#: The usage-limit ``status`` in which requests are refused (the others are ``allowed``
#: and ``allowed_warning``).
_LIMIT_REFUSED = "rejected"


def _usage_limit_refusal(line: str | dict, fmt: str = "claude-stream-json") -> bool | None:
    """Does this line say the account's **usage limit** is refusing requests — a spent
    subscription window (the 5-hour or a weekly limit), which fails every request until it
    resets, hours away? ``None`` when the line is no usage-limit record at all, so the
    drain keeps the newest state the stream reported (#539).

    claude-code emits a ``rate_limit_event`` whenever the account's usage-limit state
    changes. Every agent's requests draw on the one account, so the record carries no scope
    to check. It is the vendor's non-prose word on what a ``rate_limit`` report cannot say
    by its kind: whether the refusal was a passing rejection or a spent window.

    A refusal is ``status == "rejected"`` naming the window it holds to
    (``rateLimitType``) — the stream twin of the CLI's own test for a usage-limit 429, the
    one 429 it does not retry for a subscriber — and not ``isUsingOverage``, the vendor's
    flag for "past the window, but paid extra usage is serving the requests". A window name
    alone means nothing: the CLI names the limiting window on an ``allowed`` state too.
    Every other state (``allowed``, ``allowed_warning``, a ``rejected`` naming no window,
    which is how a passing 429 leaves it) is not a refusal, and replaces an earlier one. A
    ``rate_limit_info`` that is no mapping is ignored, as the vendor's own readers drop it.
    Codex and every stream-less family have no such record and answer ``None``.
    """
    if fmt != "claude-stream-json":
        return None
    ev = _stream_event(line)
    if ev.get("type") != _RATE_LIMIT_EVENT:
        return None
    info = ev.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    window = info.get("rateLimitType")
    return (info.get("status") == _LIMIT_REFUSED and isinstance(window, str)
            and bool(window) and info.get("isUsingOverage") is not True)


def _stream_tool_label(line: str | dict, fmt: str = "claude-stream-json") -> str:
    """A human label for the tool-use in one stream line, or "" if none.
    Best-effort: a non-JSON / non-tool line yields ""."""
    ev = _stream_event(line)
    if fmt == "codex-stream-json":
        return _codex_item_label(ev)
    # claude: an ``assistant`` event's message content can hold ``tool_use`` blocks;
    # surface the LAST one in the line (the tool just invoked).
    if ev.get("type") != "assistant":
        return ""
    content = (ev.get("message") or {}).get("content") or []
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return _tool_label(block.get("name", ""), block.get("input") or {})
    return ""


def _codex_item_label(ev: dict) -> str:
    """Label a codex ``exec --json`` item event (command_execution / file_change), or "".

    Codex emits ``item.started`` / ``item.completed`` with an ``item`` carrying its type;
    an ``agent_message`` item is prose, not a tool, so it yields ""."""
    if ev.get("type") not in ("item.started", "item.completed"):
        return ""
    item = ev.get("item") or {}
    kind = item.get("type")
    if kind == "command_execution":
        cmd = str(item.get("command") or "")
        # unwrap a `/bin/bash -lc '<cmd>'` wrapper to the inner command's first line
        m = re.search(r"-lc?\s+'(.*)'", cmd, re.S)
        inner = (m.group(1) if m else cmd).strip().splitlines()
        first = inner[0] if inner else ""
        return f"Running {first[:48]}" if first else "Running a command"
    if kind == "file_change":
        changes = item.get("changes") or []
        if changes and isinstance(changes[0], dict):
            path = Path(str(changes[0].get("path") or "")).name
            verb = {"add": "Adding", "delete": "Removing"}.get(changes[0].get("kind"), "Editing")
            return f"{verb} {path}" if path else "Editing files"
        return "Editing files"
    return ""


def _tool_label(name: str, inp: dict) -> str:
    """Compact description of a tool call — what the leaf is doing right now."""
    base = Path(str(inp.get("file_path") or inp.get("path") or "")).name
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"Editing {base}" if base else name
    if name == "Read":
        return f"Reading {base}" if base else "Reading"
    if name == "Bash":
        first = (inp.get("command") or "").strip().splitlines()
        cmd = first[0] if first else ""
        return f"Running {cmd[:48]}" if cmd else "Running a command"
    if name in ("Grep", "Glob"):
        pat = str(inp.get("pattern") or inp.get("query") or "")
        return f"Searching {pat[:32]}" if pat else "Searching"
    if name in ("Task", "Agent"):
        desc = str(inp.get("description") or "").strip()
        return f"Subagent: {desc[:32]}" if desc else "Subagent"
    return name or "working"


# ----------------------------------------------------------------------------
# Status probe — what a leaf/gate is doing right now: which artifacts exist in the
# watched dir, and how long since the newest write (a stalled job stops writing).
# Project-agnostic; a project whose leaves run a long containerized job can extend
# this with a runner probe (e.g. `docker ps --filter name=<your-prefix>`).
# ----------------------------------------------------------------------------
def bundle_activity(watch_dir, expected: Iterable[str] = ()) -> str:
    """A one-line snapshot of the work in ``watch_dir`` for a heartbeat tick.

    Reports each ``expected`` artifact (``name ✓ <size>`` once written, else
    ``name —``), then how long since the newest write in the dir (``last write 12s
    ago`` / soft ``⚠ no writes 6m`` once a leaf has gone quiet for ≥5 min) — so the
    human can see a leaf is still producing, or has stalled. Best-effort — returns
    ``""`` on any error.
    """
    try:
        watch = Path(watch_dir)
        parts: list[str] = []

        arts = [
            f"{name} ✓ {_fmt_size((watch / name).stat().st_size)}"
            if (watch / name).exists() else f"{name} —"
            for name in expected
        ]
        if arts:
            parts.append(" · ".join(arts))

        newest = _newest_mtime(watch)
        if newest:
            age = int(time.time() - newest)
            if age >= 300:
                parts.append(f"⚠ no writes {age // 60}m")
            elif age >= 120:
                parts.append(f"last write {age // 60}m ago")
            else:
                parts.append(f"last write {age}s ago")
        return " · ".join(p for p in parts if p)
    except Exception:
        return ""


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _newest_mtime(watch: Path) -> float:
    newest = 0.0
    try:
        for f in watch.iterdir():
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
    except OSError:
        return 0.0
    return newest
