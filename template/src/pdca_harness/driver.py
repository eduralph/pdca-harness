"""The driver — a thin, deterministic loop over a bundle's file-state (docs 03).

No model in the control path: ``state`` / ``advance`` / ``run_issue`` are pure
code, and the two model leaves are reached only inside :mod:`pdca_harness.leaves`.
The driver advances an issue beat by beat, writing each artifact, and STOPS at
AWAITING_SIGNOFF (the human touch point). The iterate transitions deliberately
clear downstream artifacts so a rebuild starts clean; brief versions are
preserved on iterate-to-Plan.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import assemble, brief, gates, leaves, signoff, state
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
        _carry_forward_into_brief(d)  # persist the WHY before the clear wipes it
        _clear_downstream_of_brief(d)  # re-run Do against the (now annotated) brief
    elif s == state.ITERATE_PLAN:
        _say(f"→ {d.name}: iterate-to-Plan — versioning brief…")
        _carry_forward_into_brief(d)  # annotate the brief before it is versioned
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


# ----------------------------------------------------------------------------
# Iterate carry-forward — persist the WHY into the one input the next beat reads.
# ----------------------------------------------------------------------------
def _carry_forward_into_brief(d: Path) -> None:
    """Fold the previous iteration's insight into ``brief.md`` BEFORE the clear wipes
    SUMMARY/check-*, so the next attempt isn't blind. On iterate-do the annotated
    brief stays in place (the rebuild reads it); on iterate-plan it is versioned to
    ``brief.vN.md`` with the re-authoring context attached.

    Captures whatever is available — the §9 sign-off rationale AND the failing gates
    (gating *and* advisory, since an iterate is often driven by an advisory red), so
    an iterate with no recorded rationale still carries context. Best-effort: it must
    never break the transition, so any failure is swallowed.
    """
    brief_path = d / "brief.md"
    if not brief_path.exists():
        return
    try:
        delta = signoff.iteration_delta(d / "SUMMARY.md")
        fails = _failing_gate_lines(d / "check-gates.json")
        if not delta and not fails:
            return
        n = brief_path.read_text(encoding="utf-8").count("## Iteration ") + 1
        out = [f"\n## Iteration {n} — carry-forward (from the previous attempt)\n"]
        if delta:
            out.append(f"- Sign-off rationale: {delta}\n")
        for f in fails:
            out.append(f"- Failing gate: {f}\n")
        out.append("- Address the above; do NOT re-attempt the rejected approach "
                   "unchanged. Satisfy the brief's Success criterion (the end result).\n")
        with brief_path.open("a", encoding="utf-8") as fh:
            fh.write("".join(out))
    except Exception:  # noqa: BLE001 — carry-forward is advisory; never break the iterate
        pass


def _failing_gate_lines(gates_json: Path) -> list[str]:
    """``"check — evidence"`` for each failing row in ``check-gates.json`` — gating AND
    advisory, since an iterate is often driven by an advisory red. Best-effort."""
    if not gates_json.exists():
        return []
    try:
        data = json.loads(gates_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    out: list[str] = []
    for r in data.get("rows", []):
        if r.get("result") == "fail":
            ev = r.get("path_line") or r.get("oracle") or ""
            tag = "" if r.get("gating") else " (advisory)"
            out.append(f"{r.get('check', '?')}{tag} — {ev}".strip(" —"))
    return out
