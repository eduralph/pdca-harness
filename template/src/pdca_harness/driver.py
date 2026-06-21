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
import shutil
import sys
from pathlib import Path

from . import assemble, brief, gates, leaves, signoff, state
from .config import Config


def _say(msg: str) -> None:
    """Per-beat progress to stderr, so a headless leaf or a slow gate never looks hung."""
    print(msg, file=sys.stderr, flush=True)


def _headless_note(leaf) -> str:
    return " (headless Claude — no live output, may take minutes)" if leaf.mode == "command" else ""

# Everything Do and Check write, i.e. everything downstream of brief.md. Includes the
# close marker (issue #60) so an iterate archives it too — reopening a close bundle to a
# fix path then clears the marker and runs the real Do+Check band.
DOWNSTREAM_OF_BRIEF = [
    "patch.diff",
    "build-notes.md",
    state.CLOSE_MARKER,
    "MANUAL-VERIFICATION.md",
    "check-gates.json",
    "check-gates.md",
    "check-review.md",
    "SUMMARY.md",
]


def advance(d: Path, cfg: Config) -> None:
    """Run the one beat the bundle's current state calls for."""
    s = state.state(d)
    close = _close_class(d, cfg)
    if s == state.PLANNED:
        if close:
            _say(f"→ {d.name}: close disposition '{close}' — skipping builder leaf (no patch to build)…")
            _do_close(d, cfg, close)  # write the close marker + breadcrumb instead of leaf 1
        else:
            _say(f"→ {d.name}: Do — builder writing patch.diff + test{_headless_note(cfg.builder)}…")
            leaves.do_build(d, cfg)  # leaf 1 — Do
    elif s == state.BUILT:
        if close:
            _say(f"→ {d.name}: close disposition — recording N/A gates, skipping reviewer leaf…")
            gates.run_close_gates(d, cfg)  # deterministic N/A matrix, no gate subprocess
            _close_review_note(d, close)   # stand-in for leaf 2 — close-confirm → §6
        else:
            _say(f"→ {d.name}: Check — running gates…")
            gates.run_gates(d, cfg)  # deterministic gates
            _say(f"→ {d.name}: Check — advisory reviewer{_headless_note(cfg.reviewer)}…")
            leaves.run_review(d, cfg)  # leaf 2 — reviewer (advisory)
            if cfg.advisory_leaves:  # optional extra advisory reviewers (issue #64)
                _say(f"→ {d.name}: Check — advisory reviewers ({len(cfg.advisory_leaves)})…")
                leaves.run_advisory_leaves(d, cfg)
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
    """Advance until the bundle reaches a halted state; return that state."""
    while state.state(d) not in state.HALTED:
        advance(d, cfg)
    return state.state(d)


# ----------------------------------------------------------------------------
# Close-disposition fast path (issue #60) — skip the speculative build for a bundle
# whose Plan already concluded a close / no-fix outcome. It elides the two model
# leaves (the engine's only token spend); it does NOT decide the disposition — the
# human confirms or overrides the close at sign-off (C6 forces a conscious confirm).
# ----------------------------------------------------------------------------
def _close_class(d: Path, cfg: Config) -> str:
    """The close class for a bundle taking the fast path, or "" for the normal Do path.

    Active iff the brief's Disposition hint matches a configured close class AND this is
    the FIRST attempt (no ``iteration-v*`` archive). The first-attempt guard keeps it a
    hint, not a gate: reopening to a fix path (iterate-do/-plan) archives the close marker
    and leaves an iteration behind, so the next pass returns "" and runs the real build.
    """
    bp = d / "brief.md"
    if not bp.exists() or list(d.glob("iteration-v*")):
        return ""
    return cfg.close_class(brief.disposition_hint(bp))


def _do_close(d: Path, cfg: Config, close_class: str) -> None:
    """Stand in for the Do builder leaf: write the close marker + an audit breadcrumb.

    The marker is the bundle's Do artifact (state reads it as past Do). build-notes.md
    records WHY no patch exists, so a frozen close bundle never looks like an incomplete
    Do. A manual-verification close also seeds MANUAL-VERIFICATION.md for the human.
    """
    (d / state.CLOSE_MARKER).write_text(close_class + "\n", encoding="utf-8")
    (d / "build-notes.md").write_text(
        "# Build notes — NO PATCH (close disposition)\n\n"
        f"Leaves skipped: disposition={close_class}. The Plan concluded a close / no-fix "
        "outcome, so the builder and reviewer model leaves were NOT run — there is nothing "
        "to build. The human confirms or overrides the close at sign-off; reopening to a "
        "fix path (iterate-to-Do) re-enables the full Do+Check band.\n",
        encoding="utf-8",
    )
    if _is_manual_verification(close_class):
        tpl = cfg.templates_dir / "MANUAL-VERIFICATION.md.tpl"
        dst = d / "MANUAL-VERIFICATION.md"
        if tpl.exists() and not dst.exists():
            shutil.copyfile(tpl, dst)


def _close_review_note(d: Path, close_class: str) -> None:
    """Stand in for the advisory reviewer leaf on a close bundle: write check-review.md.

    No patch means nothing to review, but the human must still consciously confirm the
    close. The ``- NEEDS-HUMAN —`` bullets parse into SUMMARY §6 (assemble._needs_human),
    so the C6 accept-guard blocks accept until the human ticks them — confirming the close
    or overriding it via iterate-to-Do.
    """
    lines = [
        "# Advisory review — SKIPPED (close disposition)\n",
        f"The reviewer leaf was skipped: this bundle's Plan concluded a close / no-fix "
        f"disposition ({close_class}), so there is no patch to review.\n",
        f"- NEEDS-HUMAN — Confirm the close disposition '{close_class}' (no patch was "
        "built). Override to a fix path (iterate-to-Do) if the close is wrong.",
    ]
    if _is_manual_verification(close_class):
        lines.append(
            "- NEEDS-HUMAN — Complete MANUAL-VERIFICATION.md and record the verdict "
            "(the manual check the gates cannot run).")
    (d / "check-review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_manual_verification(close_class: str) -> bool:
    return "manual" in close_class.lower()


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
    names += [p.name for p in d.glob("check-advisory-*.md")]  # advisory artifacts (#64)
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
