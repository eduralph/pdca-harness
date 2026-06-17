"""The cheap-first sign-off queue (docs 03 §sign-off queue, §Batch fan-out).

After the driver fans out over a batch, the human works a burn-down: bundles
halted at AWAITING_SIGNOFF, ordered so the near-instant confirms come first
(empty §6 — typically already-fixed / wontfix / by-design) and the real
adjudications (non-empty §6 NEEDS-HUMAN) come last. This module is the pure
ordering logic; the CLI renders it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import signoff, state
from .config import Config


@dataclass
class QueueEntry:
    bundle: Path
    open_needs_human: int  # 0 ⇒ cheap confirm; >0 ⇒ real adjudication

    @property
    def cheap(self) -> bool:
        return self.open_needs_human == 0


def awaiting_signoff(cfg: Config) -> list[QueueEntry]:
    """AWAITING_SIGNOFF bundles, cheap-first (empty §6 before NEEDS-HUMAN)."""
    if not cfg.bundle_root.exists():
        return []
    entries: list[QueueEntry] = []
    for d in sorted(cfg.bundle_root.glob("issue_*")):
        # Per-bundle isolation: a bundle whose files a leaf left in an unexpected
        # state (deleted SUMMARY.md between the state check and the §6 read, garbled
        # JSON, …) is skipped + flagged — it must not crash the queue computation and
        # take the whole sweep down with it (testbed issue #3).
        try:
            if not d.is_dir() or state.state(d) != state.AWAITING_SIGNOFF:
                continue
            n = len(signoff.open_needs_human(d / "SUMMARY.md"))
        except Exception as exc:  # noqa: BLE001 — isolate one bundle, not the queue
            print(f"queue: skipping {d.name} — {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        entries.append(QueueEntry(bundle=d, open_needs_human=n))
    # Cheap (0 open) first; among equals, by name for stable output.
    entries.sort(key=lambda e: (e.open_needs_human, e.bundle.name))
    return entries
