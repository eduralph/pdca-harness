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

from . import act, brief, driver, lane, leaves, merged, publish, queue, signoff, state
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


def _maybe_run_act(cfg: Config, today: str, *, any_complete: bool) -> None:
    """Run the Act beat after a flow only when it's *due* by cadence (issue #109).

    Act is a cross-cycle beat that yields a real delta only once enough cycles have
    frozen to show a pattern, so auto-running it after every small flow spends an
    interactive leaf on insufficient signal. Run it only when ``act_cadence`` cycles have
    frozen SINCE the last Act (counted from a durable marker, so it holds across separate
    flow invocations — five one-bundle flows trip it on the fifth). Below the threshold,
    skip with a hint; ``--no-act`` (``do_act=False``) still forces skip upstream.
    """
    if not any_complete:
        return
    if act.act_due(cfg):
        leaves.run_act(cfg, today)
    else:
        n = act.cycles_since_review(cfg)
        print(f"flow: Act skipped — {n} cycle(s) frozen since the last Act "
              f"(cadence {cfg.act_cadence}); run `pdca act log` when the backlog is "
              f"worth a review.", file=sys.stderr)


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
    if do_act:
        _maybe_run_act(cfg, today, any_complete=(final == state.COMPLETE))
    return final


# ----------------------------------------------------------------------------
# Declared inter-bundle ordering (docs 09, issue #36). Bundles may declare
# `Depends on:` / `Conflicts with:` in their brief; the scheduler gates dispatch on
# them. With NO fields declared every bundle is always eligible, so dispatch is
# byte-for-byte today's sort-by-name pool.
# ----------------------------------------------------------------------------
def _declared_deps(bp: Path) -> list[str]:
    """All declared prerequisite ids — COMPLETE-gated (`Depends on`) and merge-gated
    (`Depends on (merged)`, #107) — for DAG validation and dispatch."""
    return brief.depends_on(bp) + brief.depends_on_merged(bp)


def _merged_snapshot(cfg: Config, bundles: list[Path]) -> set[str]:
    """Ids whose ``Depends on (merged)`` prereq is merged right now (issue #107).

    Computed **once per build pass** rather than inside the dispatch loop, so the merge
    check (a ``gh`` call per distinct prereq) stays out of the lane pool's lock and isn't
    repeated per eligibility test. A prereq is never merged mid-run (the flow only opens
    draft PRs), so a per-pass snapshot is exact — a held dependent is picked up by a later
    ``pdca flow`` run, after its prereq's PR merges.
    """
    wanted: set[str] = set()
    for b in bundles:
        bp = b / "brief.md"
        if bp.exists():
            wanted.update(brief.depends_on_merged(bp))
    return {dep for dep in wanted if merged.is_merged(cfg, dep)}


def _deps_met(cfg: Config, d: Path, merged_ids: set[str]) -> bool:
    """True iff every prerequisite ``d`` declares is satisfied.

    ``Depends on`` prereqs must be COMPLETE; ``Depends on (merged)`` prereqs must be in
    ``merged_ids`` (this pass's merged set, #107). An unplanned/reopened bundle (no brief
    yet) declares nothing, so it is eligible; its deps are honoured once it is re-planned.
    """
    bp = d / "brief.md"
    if not bp.exists():
        return True
    return (all(state.state(cfg.bundle(dep)) == state.COMPLETE
                for dep in brief.depends_on(bp))
            and all(dep in merged_ids for dep in brief.depends_on_merged(bp)))


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
        # Both ordering fields are topological prerequisites for the DAG (existence +
        # cycle); they differ only in the gate (#107): `Depends on` waits for COMPLETE,
        # `Depends on (merged)` waits for the prereq's PR to merge.
        for dep in (_declared_deps(bp) if bp.exists() else []):
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
# The unattended band: advance every bundle through Do + Check (docs 09), **one beat at
# a time across the wave** — all Dos, then all Checks, then all SUMMARY assembles —
# mirroring the batched Plan (one session) and sign-off (chunked sessions), instead of
# driving each bundle end-to-end before the next (issue #104). This is **ordering only**:
# every per-bundle leaf, its worktree (#94) and the reviewer sandbox (#75) stay intact —
# the beats are just synchronised, never merged into one shared session.
# ----------------------------------------------------------------------------
def _advance_one(cfg: Config, d: Path) -> bool:
    """Advance bundle ``d`` by ONE beat (``driver.advance``), isolated; return whether its
    file-state changed = it progressed. A raising leaf is contained → state unchanged →
    ``False``, so the bundle drops out of the sweep (a later ``_drive_and_act`` pass
    re-drives it) and no round can spin forever. Progress is the termination condition."""
    before = state.state(d)
    _isolate(d, "build/check", lambda: driver.advance(d, cfg))
    return state.state(d) != before


def _running(d: Path) -> bool:
    """True while the bundle is still inside the unattended Do+Check band (not halted) —
    i.e. PLANNED / BUILT / CHECKED / ITERATE_* — so the beat sweep keeps advancing it."""
    return state.state(d) not in state.HALTED


def _build_all(cfg: Config, bundles: list[Path]) -> None:
    """Beat-synchronise the wave through Do+Check to AWAITING_SIGNOFF / COMPLETE.

    A serial Plan pre-pass runs first (an ``iterate-plan`` may have re-opened a bundle to
    UNPLANNED, and the Plan leaf is **interactive** — it must never enter the sweep/pool).
    Then advance one beat per still-running, deps-met bundle per round until nothing
    progresses: serial by default, or fanned across ``cfg.lanes`` workers when configured.
    """
    # Merge-gate snapshot once per pass (#107) — keeps the gh merge check out of the beat
    # sweep / lane-pool dispatch; threaded into _deps_met below.
    merged_ids = _merged_snapshot(cfg, bundles)
    # Serial Plan pre-pass — the interactive Plan beat must never enter the sweep/pool.
    for d in bundles:
        _isolate(d, "plan", lambda d=d: _plan_if_unplanned(cfg, d, None))
    if cfg.lanes <= 1 or len(bundles) <= 1:
        _beat_sweep_serial(cfg, bundles, merged_ids)
    else:
        _beat_sweep_pooled(cfg, bundles, merged_ids)


def _beat_sweep_serial(cfg: Config, bundles: list[Path], merged_ids: set[str]) -> None:
    """Round-robin one beat per still-running, deps-met bundle (sort-by-name) until no
    bundle progresses — so the wave advances all Dos, then all Checks, then all assembles.
    A dep-blocked bundle simply isn't advanced (a later pass picks it up once its prereq
    is COMPLETE / merged); a bundle whose beat raises drops out (isolated)."""
    while True:
        progressed = False
        for d in bundles:
            if _running(d) and _deps_met(cfg, d, merged_ids) and _advance_one(cfg, d):
                progressed = True
        if not progressed:
            return


def _beat_sweep_pooled(cfg: Config, bundles: list[Path], merged_ids: set[str]) -> None:
    """Pooled beat sweep: each round fans one beat across ``min(lanes, n)`` lane-pinned
    workers, conflict-aware, then **joins (a barrier per beat)** before the next round.

    Each bundle is pinned to a **stable lane slot for the whole sweep**, so its per-cycle
    worktree (#94, keyed by slot) is the same across its Do and Check beats even though
    beats are in different rounds — a bundle must not change slots between beats. Conflicts
    (#36) are serialised through a shared in-flight set so two bundles that edit a shared
    resource are never advanced concurrently, on any slots."""
    conflicts = _conflict_map(cfg, bundles)
    n_lanes = min(cfg.lanes, len(bundles))
    slot_of: dict[str, int] = {}  # bundle name → its fixed lane slot (worktree affinity)
    while True:
        eligible = [d for d in bundles if _running(d) and _deps_met(cfg, d, merged_ids)]
        if not eligible:
            return
        if not _run_beat_round_pooled(cfg, eligible, conflicts, slot_of, n_lanes):
            return  # nothing progressed (all dep-blocked / failing) — leave for a later pass


def _run_beat_round_pooled(
    cfg: Config, eligible: list[Path], conflicts: dict[str, set[str]],
    slot_of: dict[str, int], n_lanes: int,
) -> bool:
    """Advance each eligible bundle exactly ONE beat this round, ≤ ``n_lanes`` at a time,
    never two conflicting bundles concurrently; join all before returning. Returns whether
    any bundle progressed."""
    for d in eligible:  # assign a stable slot on first sight (round-robin), keep it after
        slot_of.setdefault(d.name, len(slot_of) % n_lanes)
    by_slot: dict[int, list[Path]] = {}
    for d in eligible:
        by_slot.setdefault(slot_of[d.name], []).append(d)

    inflight: set[str] = set()
    progressed = [False]
    cond = threading.Condition()

    def run_slot(slot: int, ds: list[Path]) -> None:
        lane.set_current(slot)
        for d in ds:
            with cond:  # hold off while a conflicting bundle is advancing on any slot
                while not conflicts[d.name].isdisjoint(inflight):
                    cond.wait()
                inflight.add(d.name)
            try:
                changed = _advance_one(cfg, d)
            finally:
                with cond:
                    if changed:
                        progressed[0] = True
                    inflight.discard(d.name)
                    cond.notify_all()

    threads = [threading.Thread(target=run_slot, args=(s, ds), name=f"pdca-lane{s}")
               for s, ds in by_slot.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return progressed[0]


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
        before = [state.state(d) for d in bundles]
        _build_all(cfg, bundles)
        # Sign-off, cheap-first, restricted to this batch. ONE interactive session
        # per chunk (≤ SIGNOFF_BATCH_SIZE) walks several bundles — like batch Plan —
        # then every decision is recorded FIRST (apply_now=False) so an iterate-do
        # doesn't rebuild mid-sweep and interrupt review of the rest; the next pass's
        # build-all above applies all the iterations together.
        pending = [e.bundle for e in queue.awaiting_signoff(cfg) if e.bundle.name in names]
        if not pending:
            # Break only when the band made NO progress this pass. iterate-plan archives
            # a bundle back to UNPLANNED — a HALTED state that needs the Plan pre-pass on a
            # LATER pass; on the pass where that archive happens nothing is awaiting
            # sign-off, so a bare `break` stranded it at UNPLANNED (#105). A state change
            # means progress (the re-open) — loop again so the next pass re-plans + rebuilds.
            if [state.state(d) for d in bundles] == before:
                break       # genuinely stuck (all terminal / planner declined an UNPLANNED)
            continue        # progress — give the re-opened bundle its Plan pass
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
    if do_act:
        _maybe_run_act(cfg, today,
                       any_complete=any(s == state.COMPLETE for s in results.values()))
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
