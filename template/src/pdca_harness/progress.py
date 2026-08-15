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
) -> tuple[int, str, bool]:
    """Run ``cmd``, printing ``… still working (NmSSs elapsed)`` every ``interval`` s.

    Returns ``(returncode, output, produced)``. ``output`` is the combined
    stdout+stderr when ``capture`` is True (so a gate can keep its evidence line);
    the bounded **stderr tail** when ``stream_json`` or ``tee_stderr`` is set (so a
    failed leaf's real error — usage/rate limit, 5xx, auth — survives in the bundle
    instead of scrolling past on a console nobody is watching); ``""`` otherwise.
    ``produced`` is whether the child emitted a **substantive** stream event — an
    ``assistant`` / ``user`` / ``result`` event, i.e. a session that did real work.
    Claude emits a ``system``/``init`` event (and ``system``/``api_retry`` on a
    retryable API error) *before* doing anything, so those do NOT count: a non-zero
    exit with ``produced is False`` is the transient-infra signal (the child died
    at/near invocation — usage/rate limit, 5xx, auth — before any real output).
    ``input_text``, if given, is written to stdin.

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
    latest_tool = {"label": ""}  # most recent tool-use, updated by the drain thread
    readers: list[threading.Thread] = []
    if capture_out:
        def _drain() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:  # drain so the pipe can't fill and stall the child
                if capture:
                    chunks.append(line)
                if stream_json:
                    if _is_session_event(line, stream_format):
                        produced["session"] = True  # a startup/init line does NOT count
                    lbl = _stream_tool_label(line, stream_format)
                    if lbl:
                        latest_tool["label"] = lbl
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
    rc = TIMEOUT_RC if timed_out else proc.returncode
    return rc, output, produced["session"]


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


def _is_session_event(line: str, fmt: str = "claude-stream-json") -> bool:
    """True iff a stream line is **substantive work** — not a startup/init event the CLI
    emits before doing anything. A non-zero exit having produced no such event is the
    transient-infra signal a retry should target (#138). Best-effort: non-JSON → False."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(ev, dict):
        return False
    if fmt == "codex-stream-json":
        return ev.get("type") in _CODEX_SESSION_TYPES
    return ev.get("type") in _SESSION_EVENT_TYPES


def _stream_tool_label(line: str, fmt: str = "claude-stream-json") -> str:
    """A human label for the tool-use in one stream line, or "" if none.
    Best-effort: a non-JSON / non-tool line yields ""."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return ""
    if not isinstance(ev, dict):
        return ""
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
