"""The driver — a thin, deterministic loop over a bundle's file-state (docs 03).

No model in the control path: ``state`` / ``advance`` / ``run_issue`` are pure
code, and the two model leaves are reached only inside :mod:`pdca_harness.leaves`.
The driver advances an issue beat by beat, writing each artifact, and STOPS at
AWAITING_SIGNOFF (the human touch point). The iterate transitions deliberately
clear downstream artifacts so a rebuild starts clean; brief versions are
preserved on iterate-to-Plan.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import assemble, brief, gates, leaves, state
from .config import Config


def _say(msg: str) -> None:
    """Per-beat progress to stderr, so a headless leaf or a slow gate never looks hung."""
    print(msg, file=sys.stderr, flush=True)


def _headless_note(leaf) -> str:
    return " (headless Claude — no live output, may take minutes)" if leaf.mode == "command" else ""

# Everything Do and Check write, i.e. everything downstream of brief.md.
DOWNSTREAM_OF_BRIEF = [
    "patch.diff",
    "build-notes.md",
    "check-gates.json",
    "check-gates.md",
    "check-review.md",
    "SUMMARY.md",
]


def advance(d: Path, cfg: Config) -> None:
    """Run the one beat the bundle's current state calls for."""
    s = state.state(d)
    if s == state.PLANNED:
        _say(f"→ {d.name}: Do — builder writing patch.diff + test{_headless_note(cfg.builder)}…")
        leaves.do_build(d, cfg)  # leaf 1 — Do
    elif s == state.BUILT:
        _say(f"→ {d.name}: Check — running gates…")
        gates.run_gates(d, cfg)  # deterministic gates
        _say(f"→ {d.name}: Check — advisory reviewer{_headless_note(cfg.reviewer)}…")
        leaves.run_review(d, cfg)  # leaf 2 — reviewer (advisory)
    elif s == state.CHECKED:
        _say(f"→ {d.name}: assembling SUMMARY…")
        assemble.assemble_summary(d, cfg)  # pure code → SUMMARY.md §1–8
    elif s == state.ITERATE_DO:
        _say(f"→ {d.name}: iterate-to-Do — clearing downstream, rebuilding…")
        _clear_downstream_of_brief(d)  # re-run Do against the same brief
    elif s == state.ITERATE_PLAN:
        _say(f"→ {d.name}: iterate-to-Plan — versioning brief…")
        _version_brief_and_clear(d)  # preserve brief.vN.md; human re-authors
    # UNPLANNED / AWAITING_SIGNOFF / COMPLETE: nothing for the driver to do.


def run_issue(d: Path, cfg: Config) -> str:
    """Advance until the bundle reaches a parked state; return that state."""
    while state.state(d) not in state.PARKED:
        advance(d, cfg)
    return state.state(d)


# ----------------------------------------------------------------------------
# Iterate transitions — a deliberate clear, not an idempotency violation.
# ----------------------------------------------------------------------------
def _clear_downstream_of_brief(d: Path) -> None:
    """Iterate-to-Do: unlink every Do+Check artifact so state() → PLANNED.

    The shipped test's path is read from brief.md, so this must run while
    brief.md is still in place (see _version_brief_and_clear's ordering).
    """
    for name in DOWNSTREAM_OF_BRIEF:
        (d / name).unlink(missing_ok=True)
    if (d / "brief.md").exists():
        for tf in brief.test_files(d / "brief.md"):
            (d / tf).unlink(missing_ok=True)


def _version_brief_and_clear(d: Path) -> None:
    """Iterate-to-Plan: clear downstream, then brief.md → brief.vN.md (preserved).

    Clear first — while brief.md still names the test file — then version, so
    state() returns UNPLANNED (no brief.md) and the human re-authors.
    """
    n = _next_brief_version(d)
    _clear_downstream_of_brief(d)
    (d / "brief.md").rename(d / f"brief.v{n}.md")


def _next_brief_version(d: Path) -> int:
    existing = [p.stem for p in d.glob("brief.v*.md")]
    nums = [int(s.split("brief.v")[1]) for s in existing if s.split("brief.v")[1].isdigit()]
    return (max(nums) + 1) if nums else 1
