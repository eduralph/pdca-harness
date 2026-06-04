"""Run a subprocess while ticking an elapsed-time heartbeat (docs 03 §automation).

A headless ``claude -p`` leaf and a Docker-backed gate both produce no output for
minutes; without a heartbeat the flow looks hung and the human kills a job that is
working. This is the single place that pattern lives — shared by the model leaves
(:mod:`pdca_harness.leaves`) and the deterministic gates (:mod:`pdca_harness.gates`).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time


def run_with_heartbeat(
    cmd,
    *,
    cwd=None,
    shell: bool = False,
    env=None,
    input_text: str | None = None,
    capture: bool = False,
    interval: int = 15,
    label: str = "",
) -> tuple[int, str]:
    """Run ``cmd``, printing ``… still working (NmSSs elapsed)`` every ``interval`` s.

    Returns ``(returncode, output)``. ``output`` is the combined stdout+stderr when
    ``capture`` is True (so a gate can keep its evidence line), else ``""`` and the
    child inherits the terminal. ``input_text``, if given, is written to stdin.
    """
    stdin = subprocess.PIPE if input_text is not None else None
    stdout = subprocess.PIPE if capture else None
    stderr = subprocess.STDOUT if capture else None
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, env=env, text=True,
        stdin=stdin, stdout=stdout, stderr=stderr,
    )

    chunks: list[str] = []
    reader: threading.Thread | None = None
    if capture:
        def _drain() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:  # drain so the pipe can't fill and stall the child
                chunks.append(line)
        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

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
            print(f"   … still working ({mins}m{secs:02d}s elapsed){suffix}",
                  file=sys.stderr, flush=True)
    if reader is not None:
        reader.join(timeout=5)
    return proc.returncode, "".join(chunks)
