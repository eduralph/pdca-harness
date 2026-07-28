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

import contextlib
import datetime
import sys
import threading
from pathlib import Path

from . import (act, assemble, autoiterate, brief, driver, gates, integrate, lane, leaves,
               merge, merged, preflight, publish, queue, signoff, size_signal, sources,
               state, sweep, waves)
from .config import Config


class PreflightError(RuntimeError):
    """A ``lanes > 1`` fan-out was refused because a declared per-lane preflight failed
    (issue #213) — the resources a lane's gates need aren't present, so the batch is not
    driven (it would only produce false-red bundles)."""


def _wave_pools(cfg: Config, runnable: list[Path]) -> bool:
    """True iff driving this wave's ``runnable`` bundles fans out across lanes — ``lanes > 1``
    AND more than one runnable bundle (``_beat_sweep`` takes the serial path, setting no
    ``$PDCA_LANE``, for a single bundle). Keyed on the RESOLVED runnable set, not the raw
    wave: a wave ``_runnable`` filters down to one bundle (e.g. one blocked on an unmerged
    out-of-batch prereq) never pools, so it must not trip the preflight (issue #213 / PR #214
    / PR #215 reviews)."""
    return cfg.lanes > 1 and len(runnable) > 1


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
#: :func:`_apply_decision` outcome meaning "nothing was recorded, but the bundle was returned
#: to a state a later beat can act on". Distinct from ``None`` (nothing to do — stop) and from
#: ``"blocked"`` (C6 refused an accept — stop) precisely because the caller must NOT stop: the
#: bundle is mid-repair, and breaking would strand it one beat short of a fresh SUMMARY.
REASSEMBLE = "reassemble"


def _quarantine_summary(d: Path, today: str) -> Path | None:
    """Move a malformed ``SUMMARY.md`` aside so the bundle can reassemble; ``None`` on failure.

    Renamed rather than deleted: it may carry §6 boxes the human ticked, and it is the
    evidence about whichever leaf produced it. Date-stamped like ``revalidation-<date>.json``,
    with a counter so a second incident on the same day cannot overwrite the first.
    """
    for n in range(1, 100):
        suffix = "" if n == 1 else f"-{n}"
        dest = d / f"SUMMARY.malformed-{today}{suffix}.md"
        if dest.exists():
            continue
        try:
            (d / "SUMMARY.md").rename(dest)
        except OSError:
            return None
        return dest
    return None


def _repair_unsignable(d: Path, *, action: str, today: str, why: str) -> str | None:
    """Move an unsignable ``SUMMARY.md`` aside and drop the stale decision; the caller's
    return value.

    Leaving the file in place would NOT re-drive: with SUMMARY.md present the bundle sits at
    AWAITING_SIGNOFF, a HALTED state, so no beat reassembles it — the single-issue flow stops
    and the batch sweep re-presents the same unusable summary every pass until the budget
    runs out. Moving it aside drops the bundle to CHECKED, which the next beat rebuilds from
    the check artifacts.
    """
    (d / leaves.SIGNOFF_DECISION).unlink(missing_ok=True)
    kept = _quarantine_summary(d, today)
    where = f"kept as {kept.name}" if kept else "could not be moved aside"
    print(f"flow: {d.name} — decision '{action}' not recorded ({why}); unsignable "
          f"SUMMARY.md {where}; bundle returned to {state.state(d)} to reassemble",
          file=sys.stderr)
    return REASSEMBLE if kept else None


def _apply_decision(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool
) -> str | None:
    """Record the bundle's written sign-off decision under the C6 guard.

    Reads the ``signoff-decision`` token a sign-off session (single or batch) left,
    records §9, and (if ``apply_now``) advances the transition. Returns the action
    applied, ``None`` if no decision / unrecordable, :data:`REASSEMBLE` if a malformed
    SUMMARY was moved aside so a later beat can rebuild it, or ``"blocked"`` if an accept
    was refused because §6 NEEDS-HUMAN is still open. Pure deterministic code — no leaf.

    ``apply_now`` advances the bundle immediately (single-issue ``flow``). The batch
    sweep passes ``apply_now=False`` so an ``iterate-do`` does NOT rebuild on the spot —
    the human reviews the whole cheap-first queue first, and the next pass's build-all
    rebuilds. An ``iterate-plan`` re-open is applied **even then** (it only archives →
    UNPLANNED — no rebuild), so the next pass's serial Plan pre-pass re-plans it BEFORE
    those deferred rebuilds, not a pass later (issue #174). (``accept`` is final at
    ``record`` — ``state`` becomes COMPLETE without a re-drive.)
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
    # BEFORE the C6 guard, deliberately. `open_needs_human` is the lenient side of
    # `_section`, so on a summary with no §6 heading it scans the whole document and can
    # return "blocked" for an accept — which would stop here and never reach the repair
    # below, stranding the bundle exactly as an unrepaired malformed summary does (#330
    # review). Whether the artifact can be written to at all is a property of the artifact,
    # not of the decision, so it is settled first. C6 is not weakened: reassembly rebuilds §6
    # from the review artifacts with every box unticked, so the guard fires on the next pass.
    problem = signoff.unrecordable(d / "SUMMARY.md")
    if problem:
        return _repair_unsignable(d, action=action, today=today, why=problem)
    if action == "accept" and signoff.open_needs_human(d / "SUMMARY.md"):
        print(f"flow: {d.name} — cannot accept, §6 NEEDS-HUMAN still open (C6)", file=sys.stderr)
        return "blocked"
    # The iterate rationale ("why rejected / what to change") rides §9 → the driver
    # folds it into the brief's carry-forward so the next iteration isn't blind.
    # §9's "Iteration delta" is a single line, so flatten a multi-line rationale.
    rationale = " ".join(leaves.signoff_rationale(d).split())
    try:
        signoff.record(d / "SUMMARY.md", action=action, by=by or cfg.author or "unknown",
                       date=today, delta=rationale)
    except ValueError as exc:
        # The `unrecordable` pre-check above catches the ordinary shapes; this stays as the
        # backstop for the ones only the write can detect (a duplicated §9 body, where the
        # substitution lands on the wrong copy). Contained HERE rather than left to
        # `_isolate` because the single-issue flow has no `_isolate` around this call, and a
        # traceback would abandon the run instead of reporting one bad bundle.
        return _repair_unsignable(d, action=action, today=today, why=str(exc))
    (d / leaves.SIGNOFF_DECISION).unlink(missing_ok=True)
    # Apply now for single-issue flow; in the batch sweep apply an ``iterate-plan`` re-open
    # too — it only archives → UNPLANNED (no rebuild), so it can't interrupt the cheap-first
    # queue review, and the next pass's Plan pre-pass then re-plans it BEFORE the deferred
    # iterate-do rebuilds (issue #174). ``iterate-do`` (a headless rebuild) stays deferred.
    if apply_now or action == "iterate-plan":
        driver.run_issue(d, cfg)  # COMPLETE | ITERATE_* → re-loop (iterate-plan: archive → UNPLANNED)
    return action


def _signoff_and_apply(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool = True
) -> str | None:
    """Single-issue: run the interactive sign-off leaf, then apply its decision."""
    leaves.run_signoff(d, cfg)
    return _apply_decision(cfg, d, by=by, today=today, apply_now=apply_now)


def _maybe_auto_iterate(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool
) -> bool:
    """Rebuild without asking, when Check found only implementation defects (issue #264).

    Returns True iff the bundle was routed to ITERATE_DO. Every other outcome — auto-iterate
    off, the bundle not halted at AWAITING_SIGNOFF, an empty §6, any HUMAN-kind finding, or
    the per-bundle budget spent — returns False and leaves the bundle exactly where it was,
    for the human.

    Deliberately routed through the existing ``_apply_decision`` rather than calling
    ``signoff.record`` directly: §9 then stays authored solely by ``signoff.record``, and the
    C6 accept-guard stays on the accept path even though this decision can only ever be
    ``iterate-do``. ``by="auto-iterate"`` attributes §9 to the driver, not to a human who
    never looked.

    Not in ``driver.advance``: its contract is to STOP at AWAITING_SIGNOFF, it has no
    ``by``/``today``, and ``_beat_sweep_serial`` loops with no pass cap — an auto-iterate
    firing from inside a beat would have no budget to bound it.
    """
    if not cfg.auto_iterate or state.state(d) != state.AWAITING_SIGNOFF:
        return False
    try:
        items = assemble.collect_needs_human(d, cfg)
    except (OSError, ValueError) as exc:
        # An over-reaching leaf can clear a bundle's downstream (a deleted / truncated
        # check-gates.json). Never let that crash the single-issue flow, which — unlike the
        # wave sweep — has no `_isolate` around this: decline to auto-iterate and let the
        # ordinary sign-off path deal with the bundle.
        print(f"flow: {d.name} — cannot classify Check findings ({type(exc).__name__}: {exc}); "
              f"not auto-iterating", file=sys.stderr)
        return False
    if not autoiterate.eligible(items):
        # Say WHY when it was the size backstop (#324). This rule fires at 2 rounds while
        # `max_auto_iters` defaults to 3, so it deliberately stops the loop with a round
        # still nominally available — and an operator who set that number and sees the
        # loop halt early has to be able to read the reason, or their setting simply
        # appears not to work. Every other decline is an ordinary HUMAN finding the human
        # is about to read in §6 anyway.
        for item in items:
            if size_signal.is_size_item(item.text):
                print(f"flow: {d.name} — not auto-iterating: {item.text}",
                      file=sys.stderr)
                break
        return False
    spent = autoiterate.count(d)
    if spent >= cfg.max_auto_iters:
        print(f"flow: {d.name} — auto-iterate budget spent ({spent}/{cfg.max_auto_iters}); "
              f"handing the implementation findings to the human", file=sys.stderr)
        return False
    autoiterate.write_decision(d, items)
    print(f"flow: {d.name} — auto-iterate {spent + 1}/{cfg.max_auto_iters}: "
          f"{len(items)} implementation-level finding(s), no human judgment needed",
          file=sys.stderr)
    return _apply_decision(cfg, d, by="auto-iterate", today=today,
                           apply_now=apply_now) == "iterate-do"


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
    max_iters: int | None = None,
) -> str:
    """Drive one issue through the whole cycle; return its final state.

    ``max_iters`` defaults to ``cfg.max_passes`` (``[driver].max_passes``). Exhausting it
    with the bundle still iterating is NOT silent (issue #260) — the ``for``/``else`` names
    it with a resume hint. The ``break`` paths are the ordinary halts (COMPLETE, a human
    stop, a blocked accept), which report themselves."""
    max_iters = cfg.max_passes if max_iters is None else max_iters
    d = cfg.bundle(issue_id)
    today = today or datetime.date.today().isoformat()

    for _ in range(max_iters):
        if not _plan_if_unplanned(cfg, d, csv):
            break
        if driver.run_issue(d, cfg) != state.AWAITING_SIGNOFF:
            break  # reached COMPLETE, or halted somewhere the human must look at
        # Implementation-only findings? Rebuild without spending the human's attention
        # (#264). The `for` bounds this, and so does the per-bundle auto budget.
        if _maybe_auto_iterate(cfg, d, by=by, today=today, apply_now=True):
            continue
        applied = _signoff_and_apply(cfg, d, by=by, today=today)
        if applied == REASSEMBLE:
            # A malformed SUMMARY was moved aside; the bundle is back at CHECKED. Loop so
            # this pass's `run_issue` rebuilds §1–8 and offers sign-off again, rather than
            # stopping one beat short of a usable summary. Bounded by `max_iters` like every
            # other retry here, and reassembly is deterministic code, so it cannot spin.
            continue
        if applied in (None, "blocked"):
            break
        if state.state(d) == state.COMPLETE:
            break
    else:  # loop ran to exhaustion — the bundle never reached a halt of its own
        _warn_abandoned([d], why=f"iteration budget exhausted after {max_iters} iteration(s); "
                                 f"raise [driver].max_passes / PDCA_MAX_PASSES / --max-passes")

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
    _sweep_quietly(cfg, [d])  # publish/freeze boundary — reclaim footprint (#297)
    if do_act:
        _maybe_run_act(cfg, today, any_complete=(final == state.COMPLETE))
    return final


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
    """Beat-synchronise one wave through Do+Check to AWAITING_SIGNOFF / COMPLETE.

    A serial Plan pre-pass runs first (an ``iterate-plan`` may have re-opened a bundle to
    UNPLANNED, and the Plan leaf is **interactive** — it must never enter the sweep/pool).
    Then advance one beat per still-running bundle per round until nothing progresses:
    serial by default, or fanned across ``cfg.lanes`` workers when configured. Every bundle
    in a wave is eligible — the wave holds only mutually-independent work
    (:func:`waves.compute_waves`), so there is no in-wave dependency to gate on; a prior
    wave's accepted work has already been folded onto the base this wave builds on.
    """
    # Serial Plan pre-pass — the interactive Plan beat must never enter the sweep/pool.
    for d in bundles:
        _isolate(d, "plan", lambda d=d: _plan_if_unplanned(cfg, d, None))
    if cfg.lanes <= 1 or len(bundles) <= 1:
        _beat_sweep_serial(cfg, bundles)
    else:
        _beat_sweep_pooled(cfg, bundles)


def _beat_sweep_serial(cfg: Config, bundles: list[Path]) -> None:
    """Round-robin one beat per still-running bundle (sort-by-name) until no bundle
    progresses — so the wave advances all Dos, then all Checks, then all assembles. A
    bundle whose beat raises drops out (isolated); progress is the termination condition."""
    while True:
        progressed = False
        for d in bundles:
            if _running(d) and _advance_one(cfg, d):
                progressed = True
        if not progressed:
            return


def _beat_sweep_pooled(cfg: Config, bundles: list[Path]) -> None:
    """Pooled beat sweep: each round fans one beat across ``min(lanes, n)`` lane-pinned
    workers, then **joins (a barrier per beat)** before the next round.

    Each bundle is pinned to a **stable lane slot for the whole sweep**, so its per-cycle
    worktree (#94, keyed by slot) is the same across its Do and Check beats even though
    beats are in different rounds — a bundle must not change slots between beats. The
    conflict map (#36) still serialises any two bundles that name each other in
    ``Conflicts with``; within a wave that map is normally empty (conflicts are split into
    separate waves by :func:`waves.compute_waves`), so the pool fans freely."""
    conflicts = waves.conflict_map(cfg, bundles)
    n_lanes = min(cfg.lanes, len(bundles))
    slot_of: dict[str, int] = {}  # bundle name → its fixed lane slot (worktree affinity)
    while True:
        eligible = [d for d in bundles if _running(d)]
        if not eligible:
            return
        if not _run_beat_round_pooled(cfg, eligible, conflicts, slot_of, n_lanes):
            return  # nothing progressed (all failing) — leave for a later pass


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
            changed = False  # if _advance_one raises, the finally must not UnboundLocalError
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


def _sweep_quietly(cfg: Config, bundles: list[Path]) -> None:
    """Reclaim the harness's worktree/build footprint at the end of a run (issue #297).

    Runs only after every lane thread has joined (the callers sit past the drive loops),
    so it never races a live Do. Best-effort by contract: a sweep failure must never
    fail a run that already produced its results — one stderr summary, never a raise.
    """
    try:
        lines = sweep.sweep(cfg, bundles)
        if lines:
            print(f"flow: footprint sweep — {len(lines)} action(s) "
                  f"([driver].sweep_worktrees = {cfg.sweep_worktrees}):", file=sys.stderr)
            for line in lines:
                print(f"  {line}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — teardown must never fail the run
        print(f"flow: footprint sweep failed ({type(exc).__name__}: {exc}); "
              "run `pdca sweep` manually", file=sys.stderr)


def _publish_bundle(cfg: Config, d: Path, *, by: str, today: str,
                    texts_prevalidated: bool = False) -> None:
    """Publish one COMPLETE bundle (Check's closing step), isolated so a single failure
    can't abort the batch (testbed #3); a non-zero return is loud, never silent (#97).
    ``texts_prevalidated`` (#295 review): the wave pre-pass already drafted + T4-gated
    the texts, so publish runs mechanics-only (no second T4 run mid-wave)."""
    rc = _isolate(d, "publish", lambda: publish.publish(
        cfg, d.name.removeprefix("issue_"),
        dry_run=cfg.publisher.mode == "stub", by=by, today=today, skip_if_no_target=True,
        texts_prevalidated=texts_prevalidated))
    if rc not in (0, None):  # None ⇒ _isolate already logged an exception
        print(f"flow: {d.name} is COMPLETE but publish did not complete (rc {rc}) — NOT "
              f"published; run `pdca publish {d.name.removeprefix('issue_')}`.", file=sys.stderr)


# ----------------------------------------------------------------------------
# Shared multi-bundle driver: compute waves → per wave (drive → cheap-first sign-off →
# publish → fold onto the integration branch the next wave builds on) → Act once (docs 09).
# ----------------------------------------------------------------------------
def _runnable(cfg: Config, wave: list[Path], batch_names: set[str]) -> list[Path]:
    """Drop a wave bundle whose declared prerequisite isn't ready to build on top of.

    A prerequisite **in this run's batch** is carried into the dependent's base by the wave
    fold once it reaches COMPLETE (it sits in an earlier wave), so COMPLETE is the bar — e.g.
    a prereq DISCONTINUED earlier never gets there, and its dependent is skipped loudly. A
    prerequisite **outside this batch** (a prior run's) is gated on its on-disk COMPLETE state
    (archived `completed/` too, #171) — **except** an out-of-batch ``Depends on (merged)``
    prereq, which keeps its stricter #107 merge-gate (#186): nothing in *this* run carries an
    out-of-batch prereq's diff into the base, and COMPLETE means only "a draft PR was opened",
    so a dependent built on a COMPLETE-but-unmerged base would miss the prerequisite. It must
    wait until the PR is genuinely merged (``merged.is_merged``) — a later ``pdca flow`` run
    then picks it up. A skipped bundle never completes, so its own dependents fall out of later
    waves the same way (the skip cascades)."""
    runnable: list[Path] = []
    for d in wave:
        bp = d / "brief.md"
        merged_deps = set(brief.depends_on_merged(bp)) if bp.exists() else set()
        unmet: list[str] = []
        for dep in (waves.declared_deps(bp) if bp.exists() else []):
            out_of_batch = cfg.bundle(dep).name not in batch_names
            if out_of_batch and dep in merged_deps:
                if not merged.is_merged(cfg, dep):  # PR not yet merged — wait, don't build (#186)
                    unmet.append(dep)
            elif state.state(cfg.find_bundle(dep)) != state.COMPLETE:  # archived prereq too (#171)
                unmet.append(dep)
        if unmet:
            print(f"flow: {d.name} skipped — prerequisite(s) not ready "
                  f"({', '.join(unmet)}); not built on a base missing them.", file=sys.stderr)
        else:
            runnable.append(d)
    return runnable


def _point_at_integration(integ: dict[tuple[str, str], str], runnable: list[Path]) -> None:
    """Reconcile each runnable bundle's stack base with THIS run's integration state (#187).

    ``integ`` maps each integrated target to its run-scoped integration branch. A bundle is
    pointed at the branch for **its own** ``(repo, base)`` target only — never a sibling
    target's, which is absent on that repo or carries unrelated patches. A bundle whose target
    wasn't integrated this run has any **stale** stack base (left by a prior/resumed run)
    cleared, so it builds off its own target base rather than an old integration branch."""
    for d in runnable:
        branch = integ.get(publish._resolve_target(d)[:2])
        if branch:
            publish.write_stack_base(d, branch)
        else:
            publish.clear_stack_base(d)


def _audit_wave_overlap(wave: list[Path]) -> None:
    """Advisory (#wave-model): flag two bundles in one wave whose patches touch a shared
    file. A wave holds only non-conflicting work by construction, so any overlap is a
    conflict the planner did not declare (it would otherwise have split them into separate
    waves). Loud, but never a stop — the integration fold (and the optional re-gate) is the
    hard check."""
    touched = {d.name: waves.diff_files(d / "patch.diff") for d in wave}
    touched = {n: fs for n, fs in touched.items() if fs}
    names = sorted(touched)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = touched[a] & touched[b]
            if shared:
                print(f"flow: ⚠ {a} and {b} both touch {', '.join(sorted(shared))} but "
                      f"neither declares `Conflicts with` the other — a likely undeclared "
                      f"conflict; review before merge.", file=sys.stderr)


# Terminal: finished (COMPLETE), deliberately abandoned (DISCONTINUED), or settled in the
# tracker outside a cycle (RESOLVED, #302). A bundle left in ANY other state when the driver
# stops driving it is work in flight — it will not be published, and nothing else advances
# it this run.
_TERMINAL = (state.COMPLETE, state.DISCONTINUED, state.RESOLVED)


def _warn_abandoned(bundles: list[Path], *, why: str) -> None:
    """Name every bundle the driver is walking away from un-terminal (issue #260).

    ``_drive_wave`` stops at two points that are NOT "everything finished": the pass budget
    ran out, and a pass made no progress. A bundle left non-terminal at either is never
    advanced and never published (the caller publishes only COMPLETE), so the run would
    otherwise report as though it had finished cleanly while a bundle's next iteration was
    silently dropped. Say which, say why, say how to resume.

    The predicate is **"not terminal"**, not a hand-listed set of iterate states — that is
    exactly what the issue asks for, and the difference is load-bearing. ``UNPLANNED`` must be
    included: an ``iterate-plan`` recorded on the LAST allowed pass is applied immediately
    even under ``apply_now=False`` (it only archives → UNPLANNED, no rebuild), so the cap
    fall-through finds that bundle UNPLANNED — and ``flow_batch``'s resume set *excludes*
    UNPLANNED, so it would silently drop out of the next unattended sweep as well. Inside
    ``_drive_wave`` an UNPLANNED bundle can only mean "re-opened by iterate-plan": both
    ``flow_batch`` and ``flow_ids`` filter never-briefed bundles out (loudly) before driving,
    so this can never mis-flag an issue the planner simply skipped. PLANNED / BUILT / CHECKED
    likewise mean ``_build_all`` could not advance the bundle — also in flight.
    """
    stranded = [(d, state.state(d)) for d in bundles if state.state(d) not in _TERMINAL]
    if not stranded:
        return
    print(f"flow: {why} — {len(stranded)} bundle(s) left un-terminal and NOT published:",
          file=sys.stderr)
    for d, st in stranded:
        issue_id = d.name.removeprefix("issue_")
        print(f"flow:   {d.name} [{st}] — resume with `pdca flow {issue_id}`", file=sys.stderr)


def _drive_wave(cfg: Config, wave: list[Path], *, by: str, today: str,
                max_passes: int | None = None) -> None:
    """Drive ONE wave's bundles to all-terminal (COMPLETE / DISCONTINUED) with iteration,
    then the cheap-first sign-off restricted to the wave. Publishing and folding are the
    caller's. The pass loop mirrors the prior single-batch driver: build-all
    (beat-synchronised, isolated), then a chunked sign-off whose decisions are recorded
    (``apply_now=False``) so an iterate-do doesn't rebuild mid-review — looping until the
    wave makes no progress (an iterate-plan re-open #105 still counts as progress) or every
    bundle is terminal.

    Neither non-terminal exit is silent (issue #260): a bundle still iterating when the
    budget runs out, or when a pass stops making progress, is named with a resume hint.
    ``max_passes`` defaults to ``cfg.max_passes`` (``[driver].max_passes``)."""
    max_passes = cfg.max_passes if max_passes is None else max_passes
    names = {b.name for b in wave}
    for _ in range(max_passes):
        before = [state.state(d) for d in wave]
        _build_all(cfg, wave)
        # Before the human sees the queue, take the bundles whose findings are purely
        # implementation-level off it (#264): they become ITERATE_DO, drop out of
        # `awaiting_signoff`, and the next pass's build-all rebuilds them — exactly as a
        # deferred human `iterate-do` would. Isolated: an auto-iterate that raises must not
        # kill the sweep.
        auto_iterated = False
        if cfg.auto_iterate:
            for d in wave:
                if _isolate(d, "auto-iterate", lambda d=d: _maybe_auto_iterate(
                        cfg, d, by=by, today=today, apply_now=False)):
                    auto_iterated = True
        pending = [e.bundle for e in queue.awaiting_signoff(cfg) if e.bundle.name in names]
        if not pending:
            # A fired auto-iterate IS progress, even though it leaves the bundle in the state
            # the pass began in (PR #270 review). A bundle already ITERATE_DO gets rebuilt by
            # `_build_all` to AWAITING_SIGNOFF, re-Checked, then routed straight back to
            # ITERATE_DO — so the before/after snapshots match while a rebuild, a fresh
            # review, a recorded §9 iteration and one unit of `max_auto_iters` were all spent.
            # Comparing states alone declared the wave stuck on the SECOND consecutive auto
            # round and stranded the bundle with budget to spare. Termination still holds:
            # `max_auto_iters` (clamped below `max_passes`) bounds how many passes this can
            # consume, after which `_maybe_auto_iterate` declines, the bundle stays
            # AWAITING_SIGNOFF, and it reaches the human through `pending`.
            if not auto_iterated and [state.state(d) for d in wave] == before:
                # Genuinely stuck (all terminal / planner declined an UNPLANNED) — but an
                # ITERATE_* bundle here is progress the driver can no longer make.
                _warn_abandoned(wave, why="a full pass made no progress")
                return
            continue    # progress (e.g. an iterate-plan re-open) — give it another pass
        for chunk in _chunks(pending, SIGNOFF_BATCH_SIZE):
            try:
                leaves.run_signoff_batch(cfg, chunk)
            except Exception as exc:  # noqa: BLE001 — a dropped session is not fatal
                print(f"flow: sign-off session over {[b.name for b in chunk]} failed "
                      f"({type(exc).__name__}: {exc}); applying decisions written so far",
                      file=sys.stderr)
            for d in chunk:
                _isolate(d, "sign-off", lambda d=d: _apply_decision(
                    cfg, d, by=by, today=today, apply_now=False))
        if all(state.state(d) in _TERMINAL for d in wave):
            return
    # Budget spent with work still in flight. An `iterate-do` recorded on the last allowed
    # pass defers its rebuild to "the next pass's build-all" — which never comes.
    _warn_abandoned(wave, why=f"pass budget exhausted after {max_passes} pass(es); raise "
                             f"[driver].max_passes / PDCA_MAX_PASSES / --max-passes")


def _drive_and_act(
    cfg: Config,
    bundles: list[Path],
    *,
    do_publish: bool,
    do_act: bool,
    by: str,
    today: str,
    max_passes: int | None = None,
) -> dict[str, str]:
    """Drive a fixed set of in-flight bundles through the full cycle to Act, in waves.

    The shared body of both batch entry points. :func:`waves.compute_waves` orders the
    batch into dependency waves (rejecting a cycle / unresolved dep up front); each wave is
    driven to sign-off, its accepted bundles published, and — in the default ``stack``
    mode — its cumulative accepted work folded onto a run-scoped integration branch
    (:func:`integrate.fold`) the **next** wave builds on. So a dependent builds on its
    prerequisite's accepted result within one run, as a reviewable PR stack the human
    merges (the harness never merges). Act runs **once** across the batch at the end.

    ``--no-publish`` (``do_publish=False``) drives every wave to COMPLETE but sequences
    nothing — no publish, no fold — so a later wave builds on the unchanged base.
    """
    wave_list = waves.compute_waves(cfg, bundles)  # validates (raises) + levels the batch
    last = len(wave_list) - 1
    batch_names = {b.name for b in bundles}  # in-batch prereqs ride the fold; #186 gates the rest
    published: set[str] = set()
    accepted: list[Path] = []        # cumulative COMPLETE bundles, wave then name order
    integ: dict[tuple[str, str], str] = {}  # per-target (repo, base) → integration branch (#187)
    preflighted = False              # per-lane preflight runs at most once, before the first pool
    for k, wave in enumerate(wave_list):
        runnable = _runnable(cfg, wave, batch_names)
        if not runnable:
            continue
        # Per-lane resource preflight (issue #213): the FIRST wave that will actually pool —
        # lanes>1 AND >1 *runnable* bundle (a wave _runnable filters down to one bundle, e.g.
        # one blocked on an unmerged out-of-batch prereq, takes the serial path and sets no
        # $PDCA_LANE) — verifies the instance's declared per-lane resources before it fans
        # out, and aborts the run if they're missing rather than produce false-red bundles.
        # Gating on the resolved runnable set (not the raw wave) avoids false-gating a wave
        # that never pools (PR #214 / #215 reviews); runs at most once.
        if not preflighted and _wave_pools(cfg, runnable):
            preflighted = True
            ok, msgs = preflight.lane_preflight(cfg)
            if not ok:
                for m in msgs:
                    print(f"  {m}", file=sys.stderr)
                raise PreflightError(
                    f"lane preflight failed for a lanes={cfg.lanes} batch — not fanning out "
                    "(fix the per-lane resources above, then re-run)")
        # Reconcile each runnable bundle's stack base with this run's integration state:
        # point it at its OWN (repo, base) target's branch, or clear a stale marker a
        # prior/resumed run left so it builds off its own base (#187). Unconditional — the
        # stale-clear must run even before any wave has folded (integ still empty).
        _point_at_integration(integ, runnable)
        _drive_wave(cfg, runnable, by=by, today=today, max_passes=max_passes)
        complete = [d for d in sorted(runnable, key=lambda p: p.name)
                    if state.state(d) == state.COMPLETE]
        _audit_wave_overlap(complete)
        if do_publish:
            to_publish = [d for d in complete if d.name not in published]
            # #295: draft ALL publishing texts (commit-msg.txt + pr-description.md) and
            # gate them (T4) BEFORE any git/gh mechanics run, so text generation and
            # mechanical publishing are two distinct phases — a mid-wave drafting/T4
            # failure can no longer leave half the wave pushed. Isolated per bundle
            # (testbed #3): one bundle's weak texts block only that bundle, never a
            # sibling's accepted green work.
            # Two sub-phases (#295 review round 2): every publisher leaf finishes BEFORE
            # any T4 runs. The leaves execute from the shared project root, so a later
            # bundle's leaf can touch an earlier bundle's artifacts — interleaving
            # draft→T4 per bundle would let post-validation edits reach mechanics
            # unvalidated. T4 over the final contents only.
            drafted = {d.name: _isolate(d, "draft publish texts",
                                        lambda d=d: publish.draft_texts(cfg, d,
                                                                        run_t4=False))
                       for d in to_publish}
            # Validation-only (draft=False, #295 review round 4): a text missing HERE
            # means a later leaf deleted it — re-drafting would invoke a publisher
            # leaf mid-validation, reopening the mutation window; fail that bundle.
            ready = {d.name: bool(drafted.get(d.name))
                             and bool(_isolate(d, "validate publish texts (T4)",
                                               lambda d=d: publish.draft_texts(
                                                   cfg, d, draft=False)))
                     for d in to_publish}
            for d in to_publish:
                if ready.get(d.name):
                    # Mechanics-only: the pre-pass drafted AND T4-gated the texts — a
                    # second T4 run here could transiently fail AFTER siblings pushed,
                    # recreating the half-published wave (#295 review).
                    _publish_bundle(cfg, d, by=by, today=today, texts_prevalidated=True)
                else:
                    print(f"flow: {d.name} — publish texts not ready (draft/T4 failed); "
                          f"NOT published this run; fix and run `pdca publish "
                          f"{d.name.removeprefix('issue_')}`.", file=sys.stderr)
                published.add(d.name)
        accepted += complete
        # Carry this wave's accepted work to the NEXT wave's base (skipped on the final
        # wave, and by --no-publish). Default "stack": fold onto a run-scoped integration
        # branch the next wave builds on (fork-safe, no merge). Opt-in "merge": gh-merge the
        # wave's PRs so the next wave builds on the genuinely-merged base. Dry-run (stubbed
        # publisher: offline rehearse / CI) prints the plan and changes nothing.
        if k < last and do_publish:
            dry = cfg.publisher.mode == "stub"
            if cfg.wave_mode == "merge":
                if merge.merge_wave(cfg, complete, dry_run=dry, method=cfg.merge_method):
                    print(f"flow: wave {k} did not merge; STOPPING — later waves not run.",
                          file=sys.stderr)
                    break
            else:  # default: stack — fold onto a per-target integration branch
                # ONE lock scope covers fold AND re-gate (#297 review round 10): the
                # locks stack keeps every target's integ lock held between the two,
                # so no gap exists in which another flow's publish-boundary sweep
                # could remove the tree — or another fold rewrite it — before the
                # re-gate attests it.
                stop_wave = False
                with contextlib.ExitStack() as locks:
                    try:
                        folded = integrate.fold(cfg, accepted, dry_run=dry, locks=locks)
                    except integrate.IntegrationError as exc:
                        print(f"flow: wave {k} did not integrate ({exc}); STOPPING — "
                              f"later waves not run.", file=sys.stderr)
                        break
                    if folded and not dry:
                        integ = {tgt: branch for tgt, (branch, _wt) in folded.items()}
                        # Optional re-gate (#wave-model): validate EACH folded
                        # combination over its integration tip before the next wave
                        # builds on it; any red ⇒ STOP. hold_lock=False: the locks
                        # stack already holds this tree's lock (re-acquiring would
                        # deadlock on our own flock).
                        if cfg.regate_between_waves and any(
                                wt is not None
                                and gates.run_integration(cfg, wt, hold_lock=False)
                                        .get("overall") == "fail"
                                for _tgt, (_branch, wt) in folded.items()):
                            print(f"flow: wave {k} integration re-gate FAILED — a "
                                  f"combination is red though each fix was green alone; "
                                  f"STOPPING (later waves not run).", file=sys.stderr)
                            stop_wave = True
                if stop_wave:
                    break

    _sweep_quietly(cfg, bundles)  # publish/freeze boundary — reclaim footprint (#297)
    results = {d.name.replace("issue_", ""): state.state(d) for d in bundles}
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
    max_passes: int | None = None,
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
    # un-briefed), COMPLETE (done), DISCONTINUED (deliberately abandoned) and RESOLVED
    # (settled in the tracker, #302) are excluded, so a re-run is idempotent and a
    # discontinued or resolved bundle stays out of the sweep.
    bundles = sorted(
        (cfg.bundle_root / name for name in _bundle_dirs(cfg)
         if state.state(cfg.bundle_root / name)
         not in (state.COMPLETE, state.UNPLANNED, state.DISCONTINUED, state.RESOLVED)),
        key=lambda p: p.name,
    )
    if not bundles:
        print("flow: nothing to do — no in-flight briefs (all COMPLETE or none authored; "
              "brief new issues to add work).", file=sys.stderr)
        return {}
    # Resume tolerance (#191): the sweep pulls in EVERY in-flight bundle, so a stale /
    # misconfigured `Depends on` in an unrelated leftover must not abort the whole run. Hold
    # (skip this run, leave in-flight) any bundle with an unresolvable dependency or in a
    # cycle — plus its in-batch dependents — and drive the schedulable remainder.
    bundles, held = waves.partition_schedulable(cfg, bundles)
    for name, reason in sorted(held.items()):
        print(f"flow: {name} held this run — {reason}; left in-flight (resolve it, then "
              f"re-run).", file=sys.stderr)
    if not bundles:
        print("flow: nothing schedulable — every in-flight bundle is held on an unresolved "
              "dependency or a cycle.", file=sys.stderr)
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
    max_passes: int | None = None,
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

    # A cached RESOLVED marker may be stale (#302 review round 5): revalidate the
    # explicitly listed ids against the live tracker, exactly like the single-id CLI
    # path — a REOPENED issue clears its marker (and sets the closure-era notes aside)
    # BEFORE the plan-missing set is computed, so the bundle re-enters this very run
    # instead of being skipped as terminal forever.
    for iid in ids:
        b = cfg.bundle(iid)
        if (b.exists() and state.state(b) == state.RESOLVED
                and sources.tracker_issue_reopened(cfg, iid)):
            if sources.clear_resolved_marker(b):
                print(f"flow: issue_{iid} — the tracker issue is OPEN again; cleared "
                      "the resolved marker and planning it.", file=sys.stderr)
            # else: clear_resolved_marker printed the failure (#302 review round 11);
            # the bundle stays RESOLVED and the drive-set filter below skips it with
            # its own terminal note — loud, never a silent "planned" claim.

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
        if s in _TERMINAL:
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
