"""The continuous orchestrator — Plan → Do → Check → sign-off → Act as one flow.

``flow`` drives a single issue; ``flow_batch`` handles the case where one Plan
session briefs several issues from the same documents: it plans them all, builds +
gates + reviews them all unattended, then walks the **cheap-first sign-off queue**
(:func:`queue.awaiting_signoff`) interactively, and runs Act once across the batch.

Control flow stays deterministic code: :mod:`driver` advances the state machine,
the gates gate, and the C6 accept-guard (in :func:`_signoff_and_apply`) governs
accept — models only fill leaf artifacts. Iteration is native (``iterate-do``
rebuilds; ``iterate-plan`` re-opens Plan) and bounded so a cycle can't spin forever.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from . import driver, leaves, queue, signoff, state
from .config import Config


# ----------------------------------------------------------------------------
# Shared: the interactive sign-off + deterministic record/transition for one bundle.
# ----------------------------------------------------------------------------
def _signoff_and_apply(cfg: Config, d: Path, *, by: str, today: str) -> str | None:
    """Run the sign-off leaf, then record the decision under the C6 guard.

    Returns the action token applied, ``None`` if the leaf gave no decision, or
    ``"blocked"`` if an accept was refused because §6 NEEDS-HUMAN is still open.
    """
    leaves.run_signoff(d, cfg)
    action = leaves.signoff_decision(d)
    if not action:
        print(f"flow: {d.name} — sign-off recorded no decision", file=sys.stderr)
        return None
    if action == "accept" and signoff.open_needs_human(d / "SUMMARY.md"):
        print(f"flow: {d.name} — cannot accept, §6 NEEDS-HUMAN still open (C6)", file=sys.stderr)
        return "blocked"
    signoff.record(d / "SUMMARY.md", action=action, by=by or cfg.author or "unknown", date=today)
    (d / leaves.SIGNOFF_DECISION).unlink(missing_ok=True)
    driver.run_issue(d, cfg)  # apply the transition: COMPLETE | ITERATE_* → re-loop
    return action


def _plan_if_unplanned(cfg: Config, d: Path, csv: str | None) -> bool:
    """If the bundle has no brief, run the (single) Plan leaf. Return True if planned."""
    if state.state(d) != state.UNPLANNED:
        return True
    leaves.do_plan(d, cfg, csv)
    if state.state(d) == state.UNPLANNED:
        print(f"flow: Plan produced no brief.md in {d}", file=sys.stderr)
        return False
    return True


# ----------------------------------------------------------------------------
# Single-issue flow.
# ----------------------------------------------------------------------------
def flow(
    cfg: Config,
    issue_id: str,
    *,
    csv: str | None = None,
    do_act: bool = False,
    by: str = "",
    today: str | None = None,
    max_iters: int = 10,
) -> str:
    """Drive one issue through the whole cycle; return its final state."""
    d = cfg.bundle(issue_id)
    today = today or datetime.date.today().isoformat()

    for _ in range(max_iters):
        if not _plan_if_unplanned(cfg, d, csv):
            break
        if driver.run_issue(d, cfg) != state.AWAITING_SIGNOFF:
            break  # reached COMPLETE, or parked somewhere the human must look at
        if _signoff_and_apply(cfg, d, by=by, today=today) in (None, "blocked"):
            break
        if state.state(d) == state.COMPLETE:
            break

    final = state.state(d)
    if do_act and final == state.COMPLETE:
        leaves.run_act(cfg, today)
    return final


# ----------------------------------------------------------------------------
# Batch flow — one Plan session briefs several issues; build all, then sign off.
# ----------------------------------------------------------------------------
def flow_batch(
    cfg: Config,
    *,
    csv: str | None = None,
    do_act: bool = False,
    by: str = "",
    today: str | None = None,
    max_passes: int = 10,
) -> dict[str, str]:
    """Plan many → build all (unattended) → cheap-first sign-off queue → Act once.

    Returns ``{issue_id: final_state}`` for every bundle the Plan session created.
    """
    today = today or datetime.date.today().isoformat()

    before = _bundle_dirs(cfg)
    leaves.do_plan_batch(cfg, csv)
    new = sorted(_bundle_dirs(cfg) - before)
    if not new:
        print("flow: Plan produced no new briefs", file=sys.stderr)
        return {}
    bundles = [cfg.bundle_root / name for name in new]
    names = {b.name for b in bundles}

    for _ in range(max_passes):
        # Build-all (unattended): advance each bundle to AWAITING_SIGNOFF / COMPLETE.
        for d in bundles:
            _plan_if_unplanned(cfg, d, None)  # iterate-plan may have re-opened it
            driver.run_issue(d, cfg)
        # Sign-off, cheap-first, restricted to this batch.
        pending = [e.bundle for e in queue.awaiting_signoff(cfg) if e.bundle.name in names]
        if not pending:
            break
        for d in pending:
            _signoff_and_apply(cfg, d, by=by, today=today)
        if all(state.state(d) == state.COMPLETE for d in bundles):
            break

    results = {d.name.replace("issue_", ""): state.state(d) for d in bundles}
    if do_act and any(s == state.COMPLETE for s in results.values()):
        leaves.run_act(cfg, today)
    return results


def _bundle_dirs(cfg: Config) -> set[str]:
    """Names of the existing ``issue_*`` bundle directories."""
    if not cfg.bundle_root.exists():
        return set()
    return {p.name for p in cfg.bundle_root.glob("issue_*") if p.is_dir()}
