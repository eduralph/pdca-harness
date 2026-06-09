"""The current worker lane — a thread-local slot id for in-driver concurrency (docs 09).

When the driver runs the unattended Do+Check band across a worker pool
(:func:`flow._drive_and_act`), each worker thread is pinned to a fixed slot index
``0..lanes-1`` for its lifetime. Code that addresses a *shared mutable resource* a
cycle touches outside its bundle — today only the gate commands (which a project may
back with a checkout / container / port) — reads :func:`current` to scope that
resource per lane, without threading a lane parameter through every call.

The serial path (``lanes == 1``) never sets a lane, so :func:`current` returns
``None`` and gates run exactly as before — no ``PDCA_LANE`` in their environment.
"""

from __future__ import annotations

import threading

_local = threading.local()


def set_current(lane_id: int | None) -> None:
    """Pin the calling thread to ``lane_id`` (a worker slot), or clear it with ``None``."""
    _local.lane = lane_id


def current() -> int | None:
    """The calling thread's worker-slot id, or ``None`` when running serially."""
    return getattr(_local, "lane", None)
