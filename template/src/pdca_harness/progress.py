"""Run a subprocess while ticking an elapsed-time heartbeat (docs 03 §automation).

A headless ``claude -p`` leaf and a Docker-backed gate both produce no output for
minutes; without a heartbeat the flow looks hung and the human kills a job that is
working. This is the single place that pattern lives — shared by the model leaves
(:mod:`pdca_harness.leaves`) and the deterministic gates (:mod:`pdca_harness.gates`).

A ``status`` probe lets the heartbeat show *what* is happening (which artifacts exist
yet, how long since the last write), not just that time passed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path


def run_with_heartbeat(
    cmd,
    *,
    cwd=None,
    shell: bool = False,
    env=None,
    input_text: str | None = None,
    capture: bool = False,
    stream_json: bool = False,
    interval: int = 15,
    label: str = "",
    status: Callable[[], str] | None = None,
) -> tuple[int, str, bool]:
    """Run ``cmd``, printing ``… still working (NmSSs elapsed)`` every ``interval`` s.

    Returns ``(returncode, output, produced)``. ``output`` is the combined
    stdout+stderr when ``capture`` is True (so a gate can keep its evidence line);
    the bounded **stderr tail** when ``stream_json`` is True (so a failed claude
    leaf's real error — usage/rate limit, 5xx, auth — survives in the bundle
    instead of scrolling past on a console nobody is watching); ``""`` otherwise.
    ``produced`` is whether the child emitted a **substantive** stream event — an
    ``assistant`` / ``user`` / ``result`` event, i.e. a session that did real work.
    Claude emits a ``system``/``init`` event (and ``system``/``api_retry`` on a
    retryable API error) *before* doing anything, so those do NOT count: a non-zero
    exit with ``produced is False`` is the transient-infra signal (the child died
    at/near invocation — usage/rate limit, 5xx, auth — before any real output).
    ``input_text``, if given, is written to stdin.

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
    """
    tee_stderr = stream_json and not capture
    capture_out = capture or stream_json
    stdin = subprocess.PIPE if input_text is not None else None
    if capture:
        stdout, stderr = subprocess.PIPE, subprocess.STDOUT
    elif stream_json:
        stdout, stderr = subprocess.PIPE, subprocess.PIPE  # parse stdout; tee stderr
    else:
        stdout, stderr = None, None
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, env=env, text=True,
        stdin=stdin, stdout=stdout, stderr=stderr,
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
                    if _is_session_event(line):
                        produced["session"] = True  # a system/init line does NOT count
                    lbl = _stream_tool_label(line)
                    if lbl:
                        latest_tool["label"] = lbl
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        readers.append(t)
    if tee_stderr:
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

    suffix = f" — {label}" if label else ""
    start = time.monotonic()
    while True:
        try:
            proc.wait(timeout=interval)
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
            extra = (" · " + " · ".join(bits)) if bits else ""
            print(f"   … still working ({mins}m{secs:02d}s elapsed){suffix}{extra}",
                  file=sys.stderr, flush=True)
    for reader in readers:
        reader.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    output = "".join(chunks) if capture else ("".join(err_tail) if stream_json else "")
    return proc.returncode, output, produced["session"]


# ----------------------------------------------------------------------------
# Tier 3 — parse Claude's --output-format stream-json for the live tool-use.
# Vendor-specific (Claude's event shape); a leaf opts in only for a claude family,
# so it is a no-op for a codex/other leaf, which still gets Tiers 1+2.
# ----------------------------------------------------------------------------
_SESSION_EVENT_TYPES = frozenset({"assistant", "user", "result"})


def _is_session_event(line: str) -> bool:
    """True iff a stream-json line is **substantive work** — an assistant/user/result
    event — as opposed to a ``system`` event (``init`` on startup, ``api_retry`` on a
    retryable API error) the CLI emits before doing anything. A non-zero exit having
    produced no such event is the transient-infra signal a retry should target (#138)."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return False
    return isinstance(ev, dict) and ev.get("type") in _SESSION_EVENT_TYPES


def _stream_tool_label(line: str) -> str:
    """A human label for the tool-use in one stream-json line, or "" if none.

    Claude emits newline-delimited events; an ``assistant`` event's message content
    can hold ``tool_use`` blocks. We surface the **last** one in the line (the tool
    just invoked). Best-effort: a non-JSON / non-tool line yields ""."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return ""
    if not isinstance(ev, dict) or ev.get("type") != "assistant":
        return ""
    content = (ev.get("message") or {}).get("content") or []
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return _tool_label(block.get("name", ""), block.get("input") or {})
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
