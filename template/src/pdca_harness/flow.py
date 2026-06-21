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
import threading
from pathlib import Path

from . import brief, driver, lane, leaves, publish, queue, signoff, state
from .config import Config


def _isolate(d: Path, what: str, fn):
    """Run one bundle's step; contain any error so it can't kill the whole sweep.

    A leaf with Write/Bash can leave a bundle in any state (a deleted SUMMARY.md, a
    truncated check-gates.json); the deterministic spine treats every bundle file as
    possibly-absent. When a per-bundle step still raises, skip + flag *that* bundle
    and let the others proceed — never lose a batch's progress to one bad bundle
    (testbed issue #3). KeyboardInterrupt / SystemExit propagate (only ``Exception``
    is contained), so a human ^C still stops the run.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — deliberately broad: isolate the bundle
        try:
            left = state.state(d)
        except Exception:  # noqa: BLE001 — even state-read must not raise here
            left = "unreadable"
        print(f"flow: {d.name} — {what} failed ({type(exc).__name__}: {exc}); "
              f"skipping this bundle (left {left})", file=sys.stderr)
        return None


# How many bundles one interactive sign-off session covers — bounds context + blast
# radius (a dropped session loses at most one chunk's un-applied decisions).
SIGNOFF_BATCH_SIZE = 5


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ----------------------------------------------------------------------------
# Shared: the deterministic record/transition for one already-decided bundle, and
# the single-issue "run the sign-off leaf then apply" convenience.
# ----------------------------------------------------------------------------
def _apply_decision(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool
) -> str | None:
    """Record the bundle's written sign-off decision under the C6 guard.

    Reads the ``signoff-decision`` token a sign-off session (single or batch) left,
    records §9, and (if ``apply_now``) advances the transition. Returns the action
    applied, ``None`` if no decision / unrecordable, or ``"blocked"`` if an accept was
    refused because §6 NEEDS-HUMAN is still open. Pure deterministic code — no leaf.

    ``apply_now`` advances the bundle immediately (single-issue ``flow``); the batch
    sweep passes ``apply_now=False`` so an ``iterate-do`` / ``iterate-plan`` does NOT
    rebuild on the spot — the human first reviews the whole sign-off queue, and the
    next pass's build-all applies every iteration together. (``accept`` is final at
    ``record`` regardless — ``state`` becomes COMPLETE without a re-drive.)
    """
    action = leaves.signoff_decision(d)
    if not action:
        print(f"flow: {d.name} — sign-off recorded no decision", file=sys.stderr)
        return None
    # The session must only write the decision + clear §6 — the driver owns the
    # transition. But an over-reaching leaf can clear the bundle's downstream
    # (deleting SUMMARY.md); don't let that crash the whole sweep. If there's no
    # SUMMARY.md to record into, the bundle isn't in a recordable state — drop the
    # stale decision and let the next build-all pass re-drive it.
    if not (d / "SUMMARY.md").exists():
        print(f"flow: {d.name} — decision '{action}' but no SUMMARY.md (bundle left "
              f"{state.state(d)}); skipping record, will re-drive", file=sys.stderr)
        (d / leaves.SIGNOFF_DECISION).unlink(missing_ok=True)
        return None
    if action == "accept" and signoff.open_needs_human(d / "SUMMARY.md"):
        print(f"flow: {d.name} — cannot accept, §6 NEEDS-HUMAN still open (C6)", file=sys.stderr)
        return "blocked"
    # The iterate rationale ("why rejected / what to change") rides §9 → the driver
    # folds it into the brief's carry-forward so the next iteration isn't blind.
    # §9's "Iteration delta" is a single line, so flatten a multi-line rationale.
    rationale = " ".join(leaves.signoff_rationale(d).split())
    signoff.record(d / "SUMMARY.md", action=action, by=by or cfg.author or "unknown",
                   date=today, delta=rationale)
    (d / leaves.SIGNOFF_DECISION).unlink(missing_ok=True)
    if apply_now:
        driver.run_issue(d, cfg)  # apply the transition: COMPLETE | ITERATE_* → re-loop
    return action


def _signoff_and_apply(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool = True
) -> str | None:
    """Single-issue: run the interactive sign-off leaf, then apply its decision."""
    leaves.run_signoff(d, cfg)
    return _apply_decision(cfg, d, by=by, today=today, apply_now=apply_now)


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
            break  # reached COMPLETE, or halted somewhere the human must look at
        if _signoff_and_apply(cfg, d, by=by, today=today) in (None, "blocked"):
            break
        if state.state(d) == state.COMPLETE:
            break

    final = state.state(d)
    if do_publish and final == state.COMPLETE:
        # Closing step of Check. Dry-run when the publisher leaf is stubbed (offline
        # rehearse / CI) so the flow never pushes without a live model. A real failure
        # is LOUD (#97) — never silently leave a COMPLETE bundle unpublished.
        rc = publish.publish(cfg, issue_id, dry_run=cfg.publisher.mode == "stub",
                             by=by, today=today, skip_if_no_target=True)
        if rc:
            print(f"flow: issue_{issue_id} is COMPLETE but publish did not complete "
                  f"(rc {rc}) — NOT published; run `pdca publish {issue_id}`.", file=sys.stderr)
    if do_act and final == state.COMPLETE:
        leaves.run_act(cfg, today)
    return final


# ----------------------------------------------------------------------------
# Declared inter-bundle ordering (docs 09, issue #36). Bundles may declare
# `Depends on:` / `Conflicts with:` in their brief; the scheduler gates dispatch on
# them. With NO fields declared every bundle is always eligible, so dispatch is
# byte-for-byte today's sort-by-name pool.
# ----------------------------------------------------------------------------
def _deps_met(cfg: Config, d: Path) -> bool:
    """True iff every bundle ``d`` declares ``Depends on`` is COMPLETE.

    An unplanned/reopened bundle (no brief yet) declares nothing, so it is eligible;
    its deps, if any, are honoured once it is re-planned on a later pass.
    """
    bp = d / "brief.md"
    if not bp.exists():
        return True
    return all(state.state(cfg.bundle(dep)) == state.COMPLETE
               for dep in brief.depends_on(bp))


def _conflict_map(cfg: Config, bundles: list[Path]) -> dict[str, set[str]]:
    """Symmetric bundle-name → conflicting-bundle-names map, restricted to this wave.

    A declared conflict naming a bundle outside the wave is moot (it cannot be
    co-scheduled with something that is not running) and is dropped.
    """
    names = {b.name for b in bundles}
    conflicts: dict[str, set[str]] = {b.name: set() for b in bundles}
    for b in bundles:
        bp = b / "brief.md"
        if not bp.exists():
            continue
        for cid in brief.conflicts_with(bp):
            other = cfg.bundle(cid).name
            if other in names and other != b.name:
                conflicts[b.name].add(other)
                conflicts[other].add(b.name)
    return conflicts


def _check_dep_graph(cfg: Config, bundles: list[Path]) -> None:
    """Validate the declared `Depends on` DAG before any build (issue #36).

    A dependency that is neither in this wave nor an already-COMPLETE bundle on
    disk is a misconfigured brief; a cycle is unschedulable. Both raise ``ValueError``
    so the run aborts before touching any bundle. No deps declared ⇒ no-op.
    """
    names = {b.name for b in bundles}
    graph: dict[str, list[str]] = {}
    for b in bundles:
        bp = b / "brief.md"
        edges: list[str] = []
        for dep in (brief.depends_on(bp) if bp.exists() else []):
            dn = cfg.bundle(dep).name
            if dn in names:
                edges.append(dn)
            elif state.state(cfg.bundle(dep)) != state.COMPLETE:
                raise ValueError(
                    f"{b.name}: declared dependency '{dep}' is neither in this batch "
                    f"nor an existing COMPLETE bundle")
        graph[b.name] = edges

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    path: list[str] = []

    def visit(n: str) -> None:
        color[n] = GRAY
        path.append(n)
        for m in graph[n]:
            if color[m] == GRAY:
                cyc = path[path.index(m):] + [m]
                raise ValueError("dependency cycle: " + " → ".join(cyc))
            if color[m] == WHITE:
                visit(m)
        path.pop()
        color[n] = BLACK

    for n in graph:
        if color[n] == WHITE:
            visit(n)


# ----------------------------------------------------------------------------
# The unattended band: advance every bundle through Do + Check (docs 09). Serial by
# default; a worker pool of cfg.lanes lanes when configured (PDCA_LANES / [driver].lanes).
# ----------------------------------------------------------------------------
def _build_all(cfg: Config, bundles: list[Path]) -> None:
    """Drive each bundle through the unattended Do+Check band to AWAITING_SIGNOFF / COMPLETE.

    ``cfg.lanes <= 1`` keeps the original strictly-serial loop (Plan-if-unplanned then
    drive, per bundle). With ``cfg.lanes > 1`` the *drive* fans out across a worker pool:
    a serial Plan pre-pass runs first (an ``iterate-plan`` may have re-opened a bundle to
    UNPLANNED, and the Plan leaf is **interactive** — it must never enter the pool), then
    ``min(lanes, len(bundles))`` worker threads, each pinned to a fixed lane slot for its
    lifetime (so only ``lanes`` lane-scoped checkouts/runners are ever needed), pull
    bundles off a shared queue and run the unattended ``driver.run_issue``.
    """
    if cfg.lanes <= 1 or len(bundles) <= 1:
        for d in bundles:
            if not _deps_met(cfg, d):
                continue  # a declared prereq isn't COMPLETE yet — a later pass picks it up
            def _build(d=d):
                _plan_if_unplanned(cfg, d, None)  # iterate-plan may have re-opened it
                driver.run_issue(d, cfg)
            _isolate(d, "build/check", _build)
        return

    # Serial Plan pre-pass — the interactive Plan beat stays out of the pool. After it
    # every bundle has a brief, so the declared-conflict map is complete.
    for d in bundles:
        _isolate(d, "plan", lambda d=d: _plan_if_unplanned(cfg, d, None))
    conflicts = _conflict_map(cfg, bundles)
    # Pooled drive — fixed lane slot per worker; gates read it via lane.current().
    # A worker claims the first queued bundle whose declared deps are COMPLETE and which
    # conflicts with nothing currently in flight; with no fields declared the first
    # queued bundle is always eligible, so this is the same FIFO pool as before.
    remaining = list(bundles)  # preserves the caller's sort-by-name order
    inflight: set[str] = set()
    cond = threading.Condition()

    def _next_eligible() -> Path | None:
        # caller holds `cond`. Pop+return the first eligible bundle, else None.
        for i, d in enumerate(remaining):
            if _deps_met(cfg, d) and conflicts[d.name].isdisjoint(inflight):
                inflight.add(d.name)
                return remaining.pop(i)
        return None

    def worker(slot: int) -> None:
        lane.set_current(slot)
        while True:
            with cond:
                while True:
                    if not remaining:
                        return
                    d = _next_eligible()
                    if d is not None:
                        break
                    # Nothing eligible right now. If nothing is in flight to unblock the
                    # rest, they are dep-blocked on prereqs that only go COMPLETE after a
                    # later sign-off pass — leave them and exit. Otherwise wait for an
                    # in-flight bundle to finish and re-check.
                    if not inflight:
                        cond.notify_all()
                        return
                    cond.wait()
            _isolate(d, "build/check", lambda d=d: driver.run_issue(d, cfg))
            with cond:
                inflight.discard(d.name)
                cond.notify_all()

    threads = [threading.Thread(target=worker, args=(k,), name=f"pdca-lane{k}")
               for k in range(min(cfg.lanes, len(bundles)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


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
    # Reject an unschedulable declared-ordering graph (cycle / unresolved dep) before any
    # build touches a bundle (issue #36). No `Depends on` fields ⇒ no-op.
    _check_dep_graph(cfg, bundles)
    for _ in range(max_passes):
        # Build-all (unattended): advance each bundle to AWAITING_SIGNOFF / COMPLETE.
        # Each bundle is isolated — one that raises (a leaf left it half-written) is
        # skipped this pass, never crashing the sweep and losing the others' progress.
        # Serial by default; fans out across cfg.lanes lanes when configured (docs 09).
        _build_all(cfg, bundles)
        # Sign-off, cheap-first, restricted to this batch. ONE interactive session
        # per chunk (≤ SIGNOFF_BATCH_SIZE) walks several bundles — like batch Plan —
        # then every decision is recorded FIRST (apply_now=False) so an iterate-do
        # doesn't rebuild mid-sweep and interrupt review of the rest; the next pass's
        # build-all above applies all the iterations together.
        pending = [e.bundle for e in queue.awaiting_signoff(cfg) if e.bundle.name in names]
        if not pending:
            break
        for chunk in _chunks(pending, SIGNOFF_BATCH_SIZE):
            # The session writes a decision per bundle as it goes; a dropped session
            # still leaves the finished ones, applied below. Isolate it so a crashed
            # session can't take the sweep down — we then apply whatever it wrote.
            try:
                leaves.run_signoff_batch(cfg, chunk)
            except Exception as exc:  # noqa: BLE001 — a dropped session is not fatal
                print(f"flow: sign-off session over {[b.name for b in chunk]} failed "
                      f"({type(exc).__name__}: {exc}); applying decisions written so far",
                      file=sys.stderr)
            for d in chunk:
                _isolate(d, "sign-off", lambda d=d: _apply_decision(
                    cfg, d, by=by, today=today, apply_now=False))
        if all(state.state(d) == state.COMPLETE for d in bundles):
            break

    results = {d.name.replace("issue_", ""): state.state(d) for d in bundles}
    if do_publish:
        # Isolated like the other per-bundle loops — one bundle whose publish raises
        # must not abort the batch return / Act for the rest (testbed issue #3).
        for d in bundles:
            if state.state(d) == state.COMPLETE:
                rc = _isolate(d, "publish", lambda d=d: publish.publish(
                    cfg, d.name.removeprefix("issue_"),
                    dry_run=cfg.publisher.mode == "stub", by=by, today=today,
                    skip_if_no_target=True))
                # rc != 0 (and not None — None means _isolate already logged an exception):
                # a publish that returned failure must not pass silently (#97).
                if rc not in (0, None):
                    print(f"flow: {d.name} is COMPLETE but publish did not complete "
                          f"(rc {rc}) — NOT published; run `pdca publish "
                          f"{d.name.removeprefix('issue_')}`.", file=sys.stderr)
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
    "no new briefs". COMPLETE bundles (done), DISCONTINUED ones (abandoned) and UNPLANNED
    ones (no brief — e.g. an issue the planner chose to skip) are left alone. Returns
    ``{issue_id: state}``.
    """
    today = today or datetime.date.today().isoformat()

    leaves.do_plan_batch(cfg, csv)
    # Resume set: every bundle with a brief that isn't finished. UNPLANNED (skipped /
    # un-briefed), COMPLETE (done) and DISCONTINUED (deliberately abandoned)
    # are excluded, so a re-run is idempotent and a discontinued bundle stays out of the sweep.
    bundles = sorted(
        (cfg.bundle_root / name for name in _bundle_dirs(cfg)
         if state.state(cfg.bundle_root / name)
         not in (state.COMPLETE, state.UNPLANNED, state.DISCONTINUED)),
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
    plan_missing: bool = False,
    csv: str | None = None,
    do_publish: bool = True,
    do_act: bool = False,
    by: str = "",
    today: str | None = None,
    max_passes: int = 10,
) -> dict[str, str]:
    """Drive specific bundles by id through the FULL cycle to Act.

    Like :func:`flow_batch` but seeded by explicit ids. By default there is **no Plan
    beat** — the bundles must already have a brief. With ``plan_missing`` (issue #65) a
    **Plan pre-pass** first briefs any UNPLANNED id in the list in ONE shared interactive
    session (``do_plan_batch`` over those ids, reading each bundle's ``notes.json``), making
    this the id-seeded analogue of ``flow_batch``. Ids still UNPLANNED after the pre-pass
    (planner skipped them) and terminal ids (COMPLETE / DISCONTINUED) are left alone.
    Returns ``{issue_id: state}``.
    """
    today = today or datetime.date.today().isoformat()

    # Optional Plan pre-pass (#65): brief the UNPLANNED ids in one shared session, before
    # the drive set is filtered, so the un-briefed ones become drivable. A csv enables it too.
    if plan_missing or csv:
        plan_targets = [iid for iid in ids
                        if state.state(cfg.bundle(iid)) == state.UNPLANNED]
        if plan_targets:
            for iid in plan_targets:
                cfg.bundle(iid).mkdir(parents=True, exist_ok=True)
            leaves.do_plan_batch(cfg, csv, ids=plan_targets)

    bundles: list[Path] = []
    for iid in ids:
        d = cfg.bundle(iid)
        s = state.state(d)
        if not d.exists() or s == state.UNPLANNED:
            print(f"flow: {d.name} — no brief.md, skipped (brief it at Plan first)", file=sys.stderr)
            continue
        if s in (state.COMPLETE, state.DISCONTINUED):
            print(f"flow: {d.name} — already terminal ({s}), skipped", file=sys.stderr)
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
