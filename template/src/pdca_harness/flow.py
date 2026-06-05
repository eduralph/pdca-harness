"""The continuous orchestrator — Plan → Do → Check(gates → review → sign-off →
publish) → Act as one flow.

``flow`` drives a single issue; ``flow_batch`` handles the case where one Plan
session briefs several issues from the same documents: it plans them all, builds +
gates + reviews them all unattended, then walks the **cheap-first sign-off queue**
(:func:`queue.awaiting_signoff`) interactively, and runs Act once across the batch.

On an **accept** (the bundle reaches ``state.COMPLETE``) the flow runs **publish** —
the closing step of Check — which contributes the fix as a draft PR (``--no-publish``
to skip). When the leaves are stubbed (offline ``rehearse`` / CI) publish dry-runs, so
the continuous flow never pushes without a live model. Act is opt-in and runs last.

Control flow stays deterministic code: :mod:`driver` advances the state machine,
the gates gate, and the C6 accept-guard (in :func:`_signoff_and_apply`) governs
accept — models only fill leaf artifacts. Iteration is native (``iterate-do``
rebuilds; ``iterate-plan`` re-opens Plan) and bounded so a cycle can't spin forever.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from . import driver, leaves, publish, queue, signoff, state
from .config import Config


# ----------------------------------------------------------------------------
# Shared: the interactive sign-off + deterministic record/transition for one bundle.
# ----------------------------------------------------------------------------
def _signoff_and_apply(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool = True
) -> str | None:
    """Run the sign-off leaf, then record the decision under the C6 guard.

    Returns the action token applied, ``None`` if the leaf gave no decision, or
    ``"blocked"`` if an accept was refused because §6 NEEDS-HUMAN is still open.

    ``apply_now`` (default) advances the bundle through the recorded transition
    immediately — right for the single-issue ``flow``. The batch sweep passes
    ``apply_now=False`` so an ``iterate-do`` / ``iterate-plan`` does NOT rebuild on
    the spot; the human first reviews the whole sign-off queue, and the next pass's
    build-all then applies every iteration together. (``accept`` is final at
    ``record`` regardless — ``state`` becomes COMPLETE without a re-drive.)
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
    if apply_now:
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
    do_publish: bool = True,
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
    if do_publish and final == state.COMPLETE:
        # Closing step of Check. Dry-run when the publisher leaf is stubbed (offline
        # rehearse / CI) so the flow never pushes without a live model.
        publish.publish(cfg, issue_id, dry_run=cfg.publisher.mode == "stub",
                        by=by, today=today, skip_if_no_target=True)
    if do_act and final == state.COMPLETE:
        leaves.run_act(cfg, today)
    return final


# ----------------------------------------------------------------------------
# Shared multi-bundle driver: build all → cheap-first sign-off → publish → Act once.
# ----------------------------------------------------------------------------
def _drive_and_act(
    cfg: Config,
    bundles: list[Path],
    *,
    do_publish: bool,
    do_act: bool,
    by: str,
    today: str,
    max_passes: int = 10,
) -> dict[str, str]:
    """Drive a fixed set of in-flight bundles through the full cycle to Act.

    The shared body of both batch entry points: each pass builds / gates / reviews
    every bundle unattended, then walks the cheap-first sign-off queue
    (:func:`queue.awaiting_signoff`) interactively, restricted to this set; iteration
    re-loops. When all are COMPLETE, publish runs per accepted bundle (Check's closing
    step; dry-run when the publisher leaf is stubbed) and Act runs **once** across the
    batch — the endpoint is Act, like any single cycle, just fanned over several bundles.
    """
    names = {b.name for b in bundles}
    for _ in range(max_passes):
        # Build-all (unattended): advance each bundle to AWAITING_SIGNOFF / COMPLETE.
        for d in bundles:
            _plan_if_unplanned(cfg, d, None)  # iterate-plan may have re-opened it
            driver.run_issue(d, cfg)
        # Sign-off, cheap-first, restricted to this batch. Record every decision
        # across the queue FIRST (apply_now=False) — so an iterate-do doesn't rebuild
        # mid-sweep and interrupt review of the rest. The next pass's build-all above
        # applies all the iterations together; an accept is already COMPLETE at record.
        pending = [e.bundle for e in queue.awaiting_signoff(cfg) if e.bundle.name in names]
        if not pending:
            break
        for d in pending:
            _signoff_and_apply(cfg, d, by=by, today=today, apply_now=False)
        if all(state.state(d) == state.COMPLETE for d in bundles):
            break

    results = {d.name.replace("issue_", ""): state.state(d) for d in bundles}
    if do_publish:
        for d in bundles:
            if state.state(d) == state.COMPLETE:
                publish.publish(cfg, d.name.removeprefix("issue_"),
                                dry_run=cfg.publisher.mode == "stub", by=by, today=today,
                                skip_if_no_target=True)
    if do_act and any(s == state.COMPLETE for s in results.values()):
        leaves.run_act(cfg, today)
    return results


# ----------------------------------------------------------------------------
# Batch flow — one Plan session briefs several issues; build all, then sign off.
# ----------------------------------------------------------------------------
def flow_batch(
    cfg: Config,
    *,
    csv: str | None = None,
    do_publish: bool = True,
    do_act: bool = False,
    by: str = "",
    today: str | None = None,
    max_passes: int = 10,
) -> dict[str, str]:
    """Plan many → drive every in-flight bundle to sign-off → publish → Act once. **Resumable.**

    Runs the batch Plan session, then builds / checks / signs off EVERY bundle that
    has work left — the ones this session briefed AND any already in flight — so
    re-running ``flow --from-csv`` picks up where it left off instead of failing on
    "no new briefs". COMPLETE bundles (done) and UNPLANNED ones (no brief — e.g. an
    issue the planner chose to skip) are left alone. Returns ``{issue_id: state}``.
    """
    today = today or datetime.date.today().isoformat()

    leaves.do_plan_batch(cfg, csv)
    # Resume set: every bundle with a brief that isn't finished. UNPLANNED (skipped /
    # un-briefed) and COMPLETE (done) are excluded, so a re-run is idempotent.
    bundles = sorted(
        (cfg.bundle_root / name for name in _bundle_dirs(cfg)
         if state.state(cfg.bundle_root / name) not in (state.COMPLETE, state.UNPLANNED)),
        key=lambda p: p.name,
    )
    if not bundles:
        print("flow: nothing to do — no in-flight briefs (all COMPLETE or none authored; "
              "brief new issues to add work).", file=sys.stderr)
        return {}
    return _drive_and_act(cfg, bundles, do_publish=do_publish, do_act=do_act, by=by,
                          today=today, max_passes=max_passes)


# ----------------------------------------------------------------------------
# Id-seeded flow — drive specific already-briefed bundles, no Plan beat.
# ----------------------------------------------------------------------------
def flow_ids(
    cfg: Config,
    ids: list[str],
    *,
    do_publish: bool = True,
    do_act: bool = False,
    by: str = "",
    today: str | None = None,
    max_passes: int = 10,
) -> dict[str, str]:
    """Drive specific already-briefed bundles by id through the FULL cycle to Act.

    Like :func:`flow_batch` but seeded by explicit ids with **no Plan beat** — the
    bundles must already have a brief. Missing / un-briefed (UNPLANNED) ids are
    skipped with a note (brief them at Plan first); already-COMPLETE ids are left
    alone. Returns ``{issue_id: state}``.
    """
    today = today or datetime.date.today().isoformat()
    bundles: list[Path] = []
    for iid in ids:
        d = cfg.bundle(iid)
        s = state.state(d)
        if not d.exists() or s == state.UNPLANNED:
            print(f"flow: {d.name} — no brief.md, skipped (brief it at Plan first)", file=sys.stderr)
            continue
        if s == state.COMPLETE:
            print(f"flow: {d.name} — already COMPLETE, skipped", file=sys.stderr)
            continue
        bundles.append(d)
    if not bundles:
        return {}
    bundles.sort(key=lambda p: p.name)
    return _drive_and_act(cfg, bundles, do_publish=do_publish, do_act=do_act, by=by,
                          today=today, max_passes=max_passes)


def _bundle_dirs(cfg: Config) -> set[str]:
    """Names of the existing ``issue_*`` bundle directories."""
    if not cfg.bundle_root.exists():
        return set()
    return {p.name for p in cfg.bundle_root.glob("issue_*") if p.is_dir()}
