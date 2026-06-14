"""The driver — a thin, deterministic loop over a bundle's file-state (docs 03).

No model in the control path: ``state`` / ``advance`` / ``run_issue`` are pure
code, and the two model leaves are reached only inside :mod:`pdca_harness.leaves`.
The driver advances an issue beat by beat, writing each artifact, and STOPS at
AWAITING_SIGNOFF (the human touch point). The iterate transitions deliberately
**archive** the previous attempt into ``iteration-v<N>/`` (never delete it) so a
rebuild starts clean while the rejected attempt is preserved; on iterate-to-Plan
the ``brief.md`` is archived with it (state → UNPLANNED) for the re-authoring human.
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
        n = _next_iteration_no(d)
        _say(f"→ {d.name}: iterate-to-Do — archiving the attempt to iteration-v{n}/, rebuilding…")
        _carry_forward_into_brief(d, n)  # fold prior insight into the surviving brief
        _archive_iteration(d, n, include_brief=False)  # rebuild against the annotated brief
    elif s == state.ITERATE_PLAN:
        n = _next_iteration_no(d)
        _say(f"→ {d.name}: iterate-to-Plan — archiving the attempt to iteration-v{n}/, re-planning…")
        _carry_forward_into_brief(d, n)  # appended to the brief, archived with it
        _archive_iteration(d, n, include_brief=True)  # brief archived too → UNPLANNED
    # UNPLANNED / AWAITING_SIGNOFF / COMPLETE / DISCONTINUED: nothing for the driver to do.


def run_issue(d: Path, cfg: Config) -> str:
    """Advance until the bundle reaches a parked state; return that state."""
    while state.state(d) not in state.PARKED:
        advance(d, cfg)
    return state.state(d)


# ----------------------------------------------------------------------------
# Iterate transitions — a deliberate ARCHIVE, not a delete: the previous attempt is
# moved into iteration-v<N>/ so a rejected attempt is preserved, never lost.
# ----------------------------------------------------------------------------
def _next_iteration_no(d: Path) -> int:
    """Next iteration number = (count of existing iteration-v* archives) + 1."""
    return len(list(d.glob("iteration-v*"))) + 1


# ----------------------------------------------------------------------------
# Iterate carry-forward — persist the WHY into the one input the next beat reads.
# ----------------------------------------------------------------------------
def _carry_forward_into_brief(d: Path, n: int) -> None:
    """Fold the previous iteration's insight into ``brief.md`` BEFORE the attempt is
    archived — so the next Do/Plan isn't blind. On iterate-do the brief stays at the
    top level (the rebuild reads it); on iterate-plan the annotated brief is archived
    with the attempt for the re-authoring human.

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
        out = [f"\n## Iteration {n} — carry-forward (from the previous attempt)\n"]
        if delta:
            out.append(f"- Sign-off rationale: {delta}\n")
        for f in fails:
            out.append(f"- Failing gate: {f}\n")
        out.append(f"- Full previous attempt preserved in `iteration-v{n}/` "
                   "(patch.diff, build-notes.md, SUMMARY.md, check-*).\n")
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


def _within(p: Path, parent: Path) -> bool:
    try:
        p.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _archive_iteration(d: Path, n: int, *, include_brief: bool) -> None:
    """Move the previous attempt's artifacts into ``d/iteration-v<N>/`` rather than
    deleting them: the Do+Check downstream always, plus ``brief.md`` on iterate-plan
    (so state() → UNPLANNED and the human re-authors a fresh brief). Most tests ride
    in patch.diff; a test file written *into the bundle* is archived too. External
    paths (e.g. a sibling repo's test) are left untouched, never deleted.
    """
    arch = d / f"iteration-v{n}"
    names = list(DOWNSTREAM_OF_BRIEF)
    if include_brief:
        names.append("brief.md")
    if (d / "brief.md").exists():
        for tf in brief.test_files(d / "brief.md"):
            p = d / tf
            if p.is_file() and _within(p, d):
                names.append(str(tf))
    for name in names:
        src = d / name
        if src.is_file():
            arch.mkdir(parents=True, exist_ok=True)
            src.rename(arch / Path(name).name)
