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
import re
import sys
import threading
from pathlib import Path

from . import (act, assemble, autoiterate, brief, driver, gates, integrate, lane, leaves,
               merge, merged, preflight, publish, queue, signoff, size_signal, sources,
               split, state, sweep, waves)
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
    # Driver-side capture of the session's carry-forward (issue #331). The rationale
    # lines below the token are the LIVE channel — the sign-off session writes them as
    # each decision is made — and the unlink below destroys the only full copy while §9
    # keeps a single flattened line. Captured for the iterate paths, where
    # driver._carry_forward_into_brief consumes and merges it (the registering and the
    # consuming of this channel ship together); the file is OUTSIDE the reviewer's
    # inputs and is archived with its attempt (state.DOWNSTREAM_OF_BRIEF).
    if action in ("iterate-do", "iterate-plan"):
        full = leaves.signoff_rationale(d)
        if full:
            (d / state.SESSION_CARRY).write_text(full + "\n", encoding="utf-8")
    (d / leaves.SIGNOFF_DECISION).unlink(missing_ok=True)
    # Apply now for single-issue flow; in the batch sweep apply an ``iterate-plan`` re-open
    # too — it only archives → UNPLANNED (no rebuild), so it can't interrupt the cheap-first
    # queue review, and the next pass's Plan pre-pass then re-plans it BEFORE the deferred
    # iterate-do rebuilds (issue #174). ``iterate-do`` (a headless rebuild) stays deferred.
    if apply_now or action == "iterate-plan":
        driver.run_issue(d, cfg)  # COMPLETE | ITERATE_* → re-loop (iterate-plan: archive → UNPLANNED)
    return action


#: :func:`_apply_recorded_decision` outcome meaning "the bundle carries no decision, so it
#: still owes a sign-off session". Deliberately distinct from every other outcome, because
#: only this one may open a session: ``None`` means a decision WAS on disk but could not be
#: recorded (the bundle was repaired / dropped to a state a later beat re-drives — asking
#: again would ask about an artifact that no longer exists), :data:`REASSEMBLE` likewise,
#: and ``"blocked"`` is the one case that DOES fall through (C6 refused an accept).
UNDECIDED = "undecided"


def _apply_recorded_decision(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool
) -> str | None:
    """Consume the decision the bundle **already carries**, before any session is offered.

    Read-before-asking (issue #453). ``signoff-decision`` is a bundle file, so it is
    durable, un-consumed *input* to the driver — not an in-process by-product of the
    session that wrote it. A run that dies between the leaf's write and the apply (a ``^C``:
    :func:`_isolate` deliberately does not contain ``KeyboardInterrupt``) leaves that
    decision orphaned on disk with §9 unrecorded and the bundle still AWAITING_SIGNOFF.
    Every later pass and every later run then re-presents it, opens a **fresh session for a
    bundle the human already judged**, and that session's write destroys their decision.
    The state of an issue *is* the files in its bundle (:mod:`state` module docstring) —
    a file read only through the variable of the call that produced it is not state.

    Returns :data:`UNDECIDED` when there is nothing recorded (the caller must ask); anything
    else is :func:`_apply_decision`'s own outcome, so the callers keep the single
    C6-guarded record/transition path and handle ``REASSEMBLE`` / ``"blocked"`` / ``None``
    exactly as they already do after a session. Never silent: an apply with no session
    names the bundle and the action on stderr.
    """
    action = leaves.signoff_decision(d)
    if not action:
        return UNDECIDED
    print(f"flow: {d.name} — applying the '{action}' sign-off decision already recorded in "
          f"the bundle; no new session", file=sys.stderr)
    return _apply_decision(cfg, d, by=by, today=today, apply_now=apply_now)


def _signoff_and_apply(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool = True
) -> str | None:
    """Single-issue: apply the decision the bundle already carries (issue #453); only when
    there is none — or when C6 refuses it, so the human genuinely must come back — run the
    interactive sign-off leaf and apply the decision it writes."""
    applied = _apply_recorded_decision(cfg, d, by=by, today=today, apply_now=apply_now)
    if applied not in (UNDECIDED, "blocked"):
        return applied
    leaves.run_signoff(d, cfg)
    return _apply_decision(cfg, d, by=by, today=today, apply_now=apply_now)


def _maybe_auto_iterate(
    cfg: Config, d: Path, *, by: str, today: str, apply_now: bool
) -> bool:
    """Rebuild without asking, when Check found only implementation defects (issue #264).

    Returns True iff the bundle was routed to ITERATE_DO. Every other outcome — auto-iterate
    off, the bundle not halted at AWAITING_SIGNOFF, a decision already recorded in the bundle
    and not yet consumed, an empty §6, any HUMAN-kind finding, or the per-bundle budget
    spent — returns False and leaves the bundle exactly where it was, for the human.

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
    # Never author a decision over one this driver did not write (issue #453).
    # ``autoiterate.write_decision`` below is unconditional, so a `signoff-decision` still on
    # disk — an earlier session's call, orphaned when that run died before the driver applied
    # it — would be silently replaced by an `iterate-do` the human never gave. Declining also
    # spends no budget on a bundle that is already decided: the sign-off paths apply it.
    recorded = leaves.signoff_decision(d)
    if recorded:
        print(f"flow: {d.name} — not auto-iterating: a '{recorded}' sign-off decision is "
              f"already recorded in the bundle, waiting to be applied", file=sys.stderr)
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

    **Not a CLI route** (issue #468). ``pdca flow <id>`` and ``pdca flow <id> <id>`` both go
    through :func:`flow_ids` and the one results map it returns; this is the single-bundle
    library driver (used by the offline slices, which inject a per-bundle
    ``leaves.run_signoff`` the wave path does not call). Anything that must report or exit
    on a bundle's disposition belongs on ``flow_ids`` — a state STRING cannot carry the
    per-id map both CLI shapes derive their report and exit code from, and giving one shape
    its own drive path is exactly the asymmetry #468 removed. ``cli._flow`` calling this
    again is a regression a test pins (``test_flow_entrypoint_parity``).

    For the same reason this does **not** adopt the children of a split (#469): adoption
    lives once on the shared wave path (:func:`_drive_and_act`), which every CLI shape and
    ``flow_batch`` reach, and a second copy here — driving a bundle that is not in a
    ``wave_list`` and keeps no run-scoped budget — is precisely the divergence #468 removed
    and #449 spent five iterations chasing. A caller that wants a split driven wants
    ``flow_ids([id])``.

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


def _lineage_children(record: dict) -> list[str]:
    """The child ids a lineage record's ``children`` edge names, or ``[]`` — never raises.

    The value-level half of the tolerance :func:`split.read_lineage` gives the FILE
    (``split.py:373``), mirroring :func:`split._recorded_depth` (``split.py:405``): the
    reader abstains on a file it cannot parse, this abstains on a VALUE it cannot format
    with. ``{"children": 7}`` and ``{"children": [1, null]}`` are valid JSON with a valid
    ``version``, so the reader hands them straight back — and ``" ".join(...)`` on either
    raises ``TypeError`` out of :func:`flow_ids`, aborting the run on a hand-edited
    provenance file exactly as a raising reader would. Tolerating the file but not its
    contents would only move the throw one line down.
    """
    value = record.get("children")
    if not isinstance(value, list):
        return []
    # Stripped, so a hand-added space cannot render an uncopyable `pdca flow " 469 "` hint;
    # `split.validate` only ever writes `[A-Za-z0-9._-]+` tokens (``split.py:281``).
    return [c.strip() for c in value if isinstance(c, str) and c.strip()]


def _terminal_hint(d: Path, s: str) -> str:
    """What an operator can DO about a bundle the drive set skipped as terminal, or ``""``.

    One hint, on the one shared path (issue #468). Both ``pdca flow <id>`` and ``pdca flow
    <id> <id>`` reach a terminal bundle through :func:`flow_ids`'s filter below, so the
    recovery advice cannot differ by CLI arity — which is exactly how the single-id route
    came to print ``rm -rf`` at a SPLIT PARENT while the batch route printed a bare skip.

    Deleting a split parent is destructive: its ``split-lineage.json`` (``split.py:47``) is
    the only on-disk record of the split's edges, so ``rm -rf`` there orphans the children
    the parent already produced. A record carrying a ``children`` key IS a split parent
    (independent optional edges, ``children`` iff split — ``split.py:392``), so the key's
    mere PRESENCE suppresses the destructive advice; only the ability to NAME the children
    depends on the value being usable (:func:`_lineage_children`).
    """
    record = split.read_lineage(d) or {}
    if "children" in record:
        kids = _lineage_children(record)
        if kids:
            return (f"split into children — nothing to run here; drive them instead with "
                    f"`pdca flow {' '.join(kids)}`. Do not delete this bundle: its "
                    f"{split.LINEAGE} is the only on-disk record of the split.")
        return (f"split into children, but its {split.LINEAGE} names none readably — "
                f"nothing to run here. Do not delete this bundle; read that file (or the "
                f"parent's tracker issue) to find the child bundles.")
    if s == state.RESOLVED:
        # Verbatim from the single-id CLI short-circuit this shared path replaces
        # (`cli.py:631-635` on f7876f2). The remediation names the WHOLE file (#302
        # review round 15): deleting only the `resolved` key would leave the closure-era
        # notes.json in place, and ensure_notes refuses to re-fetch while it exists — so
        # Plan would brief from the pre-reopen thread.
        return ("tracker item resolved outside a cycle — nothing to run. Reopen it in the "
                "tracker (a reachable GitHub tracker is then picked up here automatically; "
                "otherwise rename notes.json away — e.g. to notes.superseded-by-reopen.json "
                "— so the next Plan re-fetches the fresh thread) to plan it again.")
    if s == state.COMPLETE:
        return f"already complete — nothing to run. To redo it: rm -rf {d}"
    return ""


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


def _report_held(held: dict[str, str]) -> None:
    """The per-name "held this run" report of the resume path's tolerance (#191).

    ONE definition, read by :func:`flow_batch`'s resume sweep and by split adoption
    (#469), so the two cannot drift: a bundle whose declared dependency cannot be resolved
    is named, left in-flight, and the run carries on — an unschedulable bundle never aborts
    a run.

    What the hold COSTS the run is the CALLER's, and this line promises nothing about it: a
    held ADOPTED CHILD is dropped from the drive set and the results map (work this run
    created and could not schedule is not work it did, :func:`_adopt_split_children`), while
    a held id the operator NAMED stays in the map as PLANNED and the run FAILS, because a run
    owes an answer for every id it was given
    (``test_a_named_id_in_the_re_scheduled_tail_is_held_not_lost``).
    """
    for name, reason in sorted(held.items()):
        print(f"flow: {name} held this run — {reason}; left in-flight (resolve it, then "
              f"re-run).", file=sys.stderr)


# ----------------------------------------------------------------------------
# Split adoption (issue #469, mechanics from #449) — a bundle that reaches
# ``close-disposition = split`` WHILE THIS RUN IS DRIVING IT hands its children back to the
# run, instead of stranding them PLANNED for a hand-typed ``pdca flow <child-ids>``.
#
# Implemented ONCE, on the shared drive path (#468): ``cli._flow`` routes BOTH CLI shapes
# through :func:`flow_ids` → :func:`_drive_and_act`, and ``flow_batch``'s drive phase is
# that same call — so entry-point parity here is structural, not something two
# implementations have to be kept in step about (which is exactly what five iterations of
# #449 could not do).
#
# Adoption follows the LINEAGE EDGE, never the disk: a run adopts the children of the
# bundles IT IS DRIVING (transitively — an adopted child that splits in its turn is adopted
# in ITS wave), so the deliberate difference between ``flow_ids`` and the CSV resume sweep
# survives. It is bounded by ADOPTION, not by arithmetic (#473): a bundle already in the
# drive set is never adopted again, a candidate is examined at most once per splice, and
# every adopted child is a bundle already on disk — so each splice consumes a finite set and
# a chain of splits stops. The pass pool then funds every wave that chain produced
# (:func:`_pass_pool`) instead of truncating a schedule the run itself created.
#
# A bundle ALREADY terminal on a split when the run STARTS is the RECOVERY case (#473) — the
# children an earlier run (a crash, a ``^C``, a split accepted in another session) left
# PLANNED. ``flow_ids`` still SKIPS such an id, because a terminal bundle has nothing to
# build, and still prints :func:`_terminal_hint`'s non-destructive advice; what is new is
# that it hands the bundle on as an adoption SEED. The seed's children are spliced by this
# same code at ``k = -1`` ("the wave before wave 0"), under the same guards, the same
# reschedule and the same announcements a mid-run split gets — recovery is one more USE of
# the adoption path, never a second mechanism beside it.
# ----------------------------------------------------------------------------
#: The close-disposition ``split.accept`` writes into a parent it has decomposed
#: (``split.py:635``). Spelled here rather than imported because the split command owns
#: that write; this module only READS the marker it leaves.
SPLIT_DISPOSITION = "split"

#: The only id shape a lineage record can legitimately name. ``split.validate`` refuses
#: anything else at WRITE time — "ids may hold letters, digits, dot, underscore and hyphen
#: only" (``split.py:297``) — so this is the reader's half of that rule, mirrored rather
#: than imported (the split command owns what it writes; this module owns what it is
#: willing to drive, and the record between them is a file an operator can hand-edit).
#: Enforcing it here is what lets every branch below interpolate the id and the bundle name
#: into a report unquoted: a value containing a newline would otherwise break one stderr
#: line into two, the same "uncopyable hint" hazard :func:`_lineage_children`'s strip
#: (``flow.py:694``) exists to close. ``.`` / ``..`` need no special case (the writer
#: excludes them, ``split.py:297``): ``cfg.bundle`` prefixes ``issue_``, so they can only
#: ever name the ordinary directories ``issue_.`` / ``issue_..``, never a parent.
_PLAIN_ID = re.compile(r"[A-Za-z0-9._-]+")


def _real(p: Path) -> Path | None:
    """``p`` with every symlink resolved, or ``None`` if it will not resolve (a symlink loop,
    a permission wall). ONE resolver, so adoption's two questions about a path — really
    inside the bundle root (:func:`_inside_bundle_root`)? really a directory this run already
    drives (:func:`_adoptable`)? — cannot answer with different notions of "really". Both
    callers refuse a ``None``; neither ever raises."""
    try:
        return p.resolve()
    except Exception:  # noqa: BLE001 — a path that will not resolve is not one this can
        # vouch for. Refuse it; never raise.
        return None


def _inside_bundle_root(cfg: Config, d: Path) -> bool:
    """True iff ``d`` is REALLY a direct child of the bundle root — symlinks resolved.

    Containment has to be decided on the RESOLVED path. ``cfg.bundle`` builds
    ``<bundle_root>/issue_<id>``, whose ``.parent`` is the bundle root by construction, so a
    lexical comparison answers "inside" for an ``issue_<id>`` that is a **symlink** to a
    directory anywhere on the filesystem — and the run would then drive, sign off and
    publish from a bundle outside the instance. Both sides are resolved, so an instance
    whose own root is reached through a symlink (a symlinked checkout, ``/tmp`` →
    ``/private/tmp``) still compares equal, and a child with no bundle yet resolves to its
    own canonical path — the "no brief.md" report is the one that names that case.

    Lexical traversal (``"../../etc"``) is NOT this guard's job and must not be left to it:
    ``realpath`` NORMALISES ``results/issue_../../etc`` back to ``results/etc``, which is
    inside the root by this test. :data:`_PLAIN_ID` rejects those ids before a path is ever
    built; the two guards are complementary, neither is redundant.

    A link that stays INSIDE the root (one ``issue_<id>`` aliasing another bundle) passes THIS
    test, and should: it names a bundle this instance owns. The hazard it carries — one
    DIRECTORY entering the run twice under two names — takes the run's drive set to see, so it
    is refused in :func:`_adoptable`, not by re-keying that run's sets by resolved path.
    """
    real, root = _real(d), _real(cfg.bundle_root)
    return real is not None and root is not None and real.parent == root


def _is_split_parent(d: Path) -> bool:
    """True iff ``d`` is terminal AND its close marker records a ``split``.

    Terminal is part of the predicate on purpose. ``split.accept`` writes the marker
    (``split.py:635``), but the human still confirms the decomposition at sign-off — the
    close fast path raises a §6 NEEDS-HUMAN for exactly that — so a parent still
    AWAITING_SIGNOFF (or sent back with ``iterate-do``) is a split nobody has accepted yet,
    and driving its children would spend whole cycles on work the next sign-off may reopen.
    Same predicate as :func:`_warn_abandoned`'s (``flow.py:759``): "not terminal" means
    "still in flight".

    The catch is TOTAL, for the reason the sibling reader this builds on spells out
    (:func:`split.read_lineage`, ``split.py:382-390``): a ``close-disposition`` whose bytes
    are not UTF-8 raises ``UnicodeDecodeError`` out of the *read*, where only ``OSError``
    was expected. A marker that cannot be read is not a split; it is never a verdict on the
    run.
    """
    if state.state(d) not in _TERMINAL:
        return False
    try:
        return (d / state.CLOSE_MARKER).read_text(
            encoding="utf-8").strip() == SPLIT_DISPOSITION
    except Exception:  # noqa: BLE001 — a hint that cannot be read must never end a run
        return False


def _adoptable(cfg: Config, parent: Path, *, known: set[str],
               refused: list[tuple[str, str, str]]) -> tuple[list[Path], list[Path]]:
    """The children of ``parent`` this run can drive, and the ones to WALK THROUGH —
    ``([], [])`` unless it split.

    Detect and validate in ONE step: a bundle that is not terminal on a ``split``
    (:func:`_is_split_parent`) has no adoptable children, so no caller can decide "is this a
    split" a second way.

    The ids come from the parent's ``split-lineage.json`` (:func:`split.read_lineage`,
    ``split.py:373``), read through the same tolerant :func:`_lineage_children` the terminal
    hint uses (``flow.py:679``) — a READ of a machine-readable record, never a parse of the
    prose breadcrumb ``accept`` leaves in ``build-notes.md`` (``split.py:627-634``), which
    is written for a human and is not a contract. A parent carrying the marker but no
    readable record is reported and skipped: that degrades to the operator's ``pdca flow
    <child-ids>``, never a crash and never a guess.

    EVERY entry is accounted for out loud, including the ones that never reach the loop:
    ``_lineage_children`` drops what it cannot format with (a non-string, an empty string)
    before this sees it, and a child dropped THERE would be stranded in silence — never
    adopted, never named, the operator left reading a sibling's ``unresolved dependency
    (601)`` as if 601 did not exist while a briefed bundle sits in the next directory. So what
    it refused is counted here from its own answer, never a second copy of its predicate.

    Each id is then filtered exactly as :func:`flow_ids` filters an explicitly named one: a
    bundle that does not exist or has no brief, and one already terminal, is named and
    dropped. An id LISTED TWICE yields one child, and an id that is not a plain tracker id
    (:data:`_PLAIN_ID`) or whose bundle is not really inside the bundle root
    (:func:`_inside_bundle_root`) is dropped as well — the record is a file an operator can
    hand-edit, and ``cfg.bundle`` would happily build a path outside the root (the hazard
    ``split.validate`` guards at write time, ``split.py:297-311``) or the same bundle twice
    (which would enter the wave computation, the drive set and the announcement twice over).

    "The same bundle" is decided on the RESOLVED directory, not the name alone, because two
    names can be one bundle: an ``issue_<id>`` symlinked to another inside the root passes
    :func:`_inside_bundle_root` by design, and adopting it beside the bundle it aliases puts
    one directory in one wave twice — under ``lanes > 1``, two lanes on one bundle.

    One skip is NOT reported here: a child already in ``known`` — this run's drive set plus
    what a sibling parent has just claimed — is appended to ``refused`` for the caller to
    report AFTER the splice (:func:`_report_refused`), with the name the run holds that
    DIRECTORY under (the child's own, except for an alias), since that is the name whose
    disposition the report looks up. What is true about such a child is not yet decided at
    this point in the run, and the eager line said the wrong one of the two.

    One dropped child is not a dead end (#473). A child that is terminal **on a split of its
    own** cannot be driven — but the generation below it can still be sitting where an
    earlier run left it (a 500 → 601 → 701 chain abandoned part-way down). Those come back
    as the SECOND return value, for the caller's queue, so the walk continues by re-entering
    this reader under that child's name — with these same guards, and its grandchildren
    attributed to the parent that actually declared them — rather than by a second reader of
    the same lineage record. It is a pair, not one list, because the two are different
    claims: the first is work this run takes on, the second is a bundle to ASK about.
    """
    if not _is_split_parent(parent):
        return [], []
    record = split.read_lineage(parent) or {}  # tolerant by contract: never raises
    ids = _lineage_children(record)
    entries = record.get("children")
    entries = entries if isinstance(entries, list) else []
    if len(entries) > len(ids):
        # The record is echoed so the operator can recognise the child it failed to name —
        # bounded, and `repr`-quoted so a newline inside an entry cannot break the line in two.
        head, more = entries[:8], (f" (first 8 of {len(entries)})" if len(entries) > 8 else "")
        print(f"flow: {parent.name} — {len(entries) - len(ids)} of the {len(entries)} "
              f"entries in {split.LINEAGE}'s `children` name no usable child id, so they "
              f"were NOT adopted: the record reads {head!r}{more}. Repair that file (or "
              f"drive them by hand with `pdca flow <child-ids>`) — a child it fails to name "
              f"is a bundle nothing in this run will drive.", file=sys.stderr)
    if not ids:
        print(f"flow: {parent.name} — close-disposition '{SPLIT_DISPOSITION}' but no "
              f"readable children record ({split.LINEAGE}); its children were NOT adopted "
              f"— drive them with `pdca flow <child-ids>`", file=sys.stderr)
        return [], []
    # The run's drive set as DIRECTORIES: `known` is keyed by name, and the name a bundle is
    # driven under is not the only one that can reach it. First name wins, sorted, so the one
    # reported does not depend on set iteration order.
    driven: dict[Path, str] = {}
    for name in sorted(known):
        root_real = _real(cfg.bundle_root / name)
        if root_real is not None:
            driven.setdefault(root_real, name)
    out: list[Path] = []
    walk: list[Path] = []          # terminal on a split of their own — asked, not driven
    seen: set[str] = set()
    seen_real: set[Path] = set()
    for cid in ids:
        d = cfg.bundle(cid)
        if d.name in seen:
            # One bundle, one adoption. A hand-edited record listing a child twice (or twice
            # in two spellings that resolve to one bundle) would otherwise be adopted twice:
            # duplicated into `remaining + children` for the reschedule, counted twice in the
            # drive set, and announced twice. Deduped on the resolved bundle name, not on the
            # raw id, because that is what every downstream set is keyed by.
            continue
        seen.add(d.name)
        if not _PLAIN_ID.fullmatch(cid):
            print(f"flow: {parent.name} — ignoring child id {cid!r} in {split.LINEAGE}: it "
                  f"is not a plain tracker id (letters, digits, dot, underscore and hyphen "
                  f"only — the one shape `pdca split --accept` writes), so it names no "
                  f"bundle under {cfg.bundle_root}", file=sys.stderr)
            continue
        real = _real(d)
        if real is None or not _inside_bundle_root(cfg, d):
            print(f"flow: {parent.name} — ignoring child id {cid!r} in {split.LINEAGE}: "
                  f"{d.name} resolves outside {cfg.bundle_root} (a symlinked bundle is not "
                  f"this instance's to drive)", file=sys.stderr)
            continue
        if real in seen_real:
            # The alias half of "one bundle, one adoption": a second entry in THIS record
            # reaching the same directory through a link. Silent like the duplicate name above
            # and for its reason — that directory IS adopted, under the first name that
            # reached it, so nothing is stranded and there is nothing to resume.
            continue
        seen_real.add(real)
        owner = d.name if d.name in known else driven.get(real, "")
        if owner:
            # Named, not silent — but named by the CALLER, once the splice has run. `known`
            # is `batch_names | taken`, and a child a sibling parent `taken` a moment ago can
            # still be HELD by the reschedule that follows: "already in this run's drive set"
            # would then be a false statement about the run at exactly the moment an operator
            # is reading the log to find out who owns that child. `owner` is that child's own
            # name, except through a link — then it is the name the run drives the directory
            # under, the one with a disposition to report.
            refused.append((parent.name, d.name, owner))
            continue
        s = state.state(d)
        if not d.exists() or s == state.UNPLANNED:
            print(f"flow: {d.name} — child of {parent.name} NOT adopted: no brief.md "
                  f"(brief it at Plan, then `pdca flow {cid}`)", file=sys.stderr)
            continue
        if s in _TERMINAL:
            if _is_split_parent(d):
                # Terminal AND split: nothing to drive HERE, but this is the only route to
                # a generation an earlier run may have stranded below it. Handed back to
                # the caller's queue instead of dropped, so recovery reaches the whole brood
                # rather than stopping at the first child that already closed.
                print(f"flow: {d.name} — child of {parent.name} is itself terminal on a "
                      f"split; examining it for children to adopt", file=sys.stderr)
                walk.append(d)
            else:
                print(f"flow: {d.name} — child of {parent.name} NOT adopted: already "
                      f"terminal ({s})", file=sys.stderr)
            continue
        out.append(d)
    return sorted(out, key=lambda p: p.name), sorted(walk, key=lambda p: p.name)


def _reschedule(cfg: Config, remaining: list[Path]) -> list[list[Path]] | None:
    """Wave-order what is LEFT of the run together with the newly adopted children.

    The resume path's TOLERANCE (:func:`waves.partition_schedulable` + the per-name
    :func:`_report_held`), not :func:`waves.compute_waves`' strictness: a child whose
    declared dependency cannot be resolved — or that sits in a cycle — is held, reported
    and left in-flight while the run carries on. A split must never be able to abort the
    flow that caused it. ``None`` iff not even that could be computed, which leaves the
    caller with today's waves (and says so) rather than a half-spliced run.
    """
    try:
        schedulable, held = waves.partition_schedulable(cfg, remaining)
        _report_held(held)
        # partition_schedulable's contract: the remainder is a valid DAG compute_waves
        # levels without raising. The try still covers it — adoption is an enhancement,
        # and no enhancement may take down a run that is already producing results.
        return waves.compute_waves(cfg, schedulable) if schedulable else []
    except Exception as exc:  # noqa: BLE001 — deliberately broad: never abort the run
        print(f"flow: could not re-wave the run after a split "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def _report_refused(refused: list[tuple[str, str, str]], batch_names: set[str]) -> None:
    """Say what became of each child adoption refused as a duplicate — AFTER the splice.

    A refusal is only half an answer when it is made. ``known`` is ``batch_names | taken``
    (:func:`_adopt_split_children`), and the ``taken`` half is a claim, not a schedule: the
    reschedule that follows can HOLD that child (:func:`_reschedule`), and a later one can
    drop a child adopted earlier back out of the run. Reported eagerly, one child then drew
    two lines that contradict each other — ``already in this run's drive set`` beside
    ``held this run — …; left in-flight`` — while it sat in neither the drive set nor the
    results map, which is precisely the "one situation, two report shapes" this feature
    exists to end.

    So the line is written here, against the drive set as it FINALLY stands after the
    splice, and there is exactly one shape per outcome: it IS in the run (skip it, the
    other parent has it), or it is not (nobody in this run owns it — resume it by hand).
    Pinned by ``test_a_shared_child_the_reschedule_holds_is_not_reported_as_driven``.

    The disposition looked up is the OWNER's — the name the run holds that directory under,
    the child's own except through a symlinked ``issue_<id>`` (pinned by
    ``test_a_symlinked_alias_of_a_bundle_this_run_drives_is_driven_once``). Looking the alias
    up instead finds nothing and says the run does not own a bundle it is driving under its
    other name; the line names both, so one directory under two names reads as that.
    """
    for parent_name, child, owner in refused:
        alias = "" if owner == child else f" (the same directory as {owner})"
        if owner in batch_names:
            print(f"flow: {child} — child of {parent_name} not adopted again: already in "
                  f"this run's drive set{alias}", file=sys.stderr)
        else:
            print(f"flow: {child} — child of {parent_name} NOT adopted: another parent in "
                  f"this run claimed it first and it is not scheduled — it is NOT in this "
                  f"run's drive set or its results; resume it with `pdca flow "
                  f"{child.removeprefix('issue_')}`{alias}", file=sys.stderr)


def _adopt_split_children(cfg: Config, candidates: list[Path], *, k: int,
                          wave_list: list[list[Path]], bundles: list[Path],
                          batch_names: set[str], named: frozenset[str]) -> None:
    """Splice the children of any bundle in ``candidates`` that has split into the waves
    AFTER ``k``, and announce each child's REAL wave. ``k`` is the wave just driven; ``-1``
    is the pre-pass over the run's adoption SEEDS (#473) — ids the operator named whose
    bundles were ALREADY terminal on a split when the run started — whose children are
    levelled in front of the whole schedule (``wave_list[-1 + 1:]`` is ``wave_list[0:]``).

    ``candidates`` seeds a work QUEUE, not a list read once: a child that is itself terminal
    on a split comes back from :func:`_adoptable` and is examined in its turn, under its own
    name, so a chain an earlier run abandoned part-way down (500 → 601 → 701, with 601
    already split) hands over the descendants that are ACTUALLY stranded instead of stopping
    at the first closed generation. Each bundle leaves the queue at most once, so a
    hand-edited record naming an ancestor drains rather than spins.

    Mutates the run's state in place: ``wave_list[k+1:]`` is replaced by the recomputed
    remainder (so the children join a wave AFTER the one their parent was in — never the
    wave being driven, whose fold has a base about to move, and never one that has already
    folded), and the scheduled children join ``bundles`` / ``batch_names``, the sets the
    results map, the final :func:`_sweep_quietly` and :func:`_runnable`'s in-batch prereq
    rule already cover. They are pointed at this run's per-target integration branch by the
    ordinary :func:`_point_at_integration` call at the top of their wave (``flow.py:1424``),
    and driven by the ordinary :func:`_drive_wave` out of the run's own pass budget — one
    mechanism, not a second.

    The announced wave is READ BACK from the recomputed schedule, never assumed to be the
    parent's index + 1: the children are levelled by their own ``Depends on`` /
    ``Conflicts with``, so two children of one parent routinely land in different waves and
    a hardcoded ``k + 1`` names a wave the second one is not in. A child the reschedule
    HELD is not announced as adopted at all, and never joins the results map —
    :func:`_report_held` has already named it as left in-flight.

    ``named`` is the set of bundles the run SET OUT to drive, and it is what makes that
    exclusion hold whenever the hold happens rather than only the first time. Each splice
    re-levels the WHOLE un-driven tail, so a child adopted into a later wave can be held by
    a LATER call — its prerequisite ended the run un-terminal, or its brief was re-planned
    onto one this run cannot resolve. Held is held: such a child is taken back out of
    ``bundles`` / ``batch_names`` and its adoption announcement RETRACTED on stderr, so the
    same situation ("this run created work it could not schedule") cannot produce two
    different report shapes — and two different exit codes — depending only on WHICH
    reschedule first saw it (``test_a_child_held_by_a_later_reschedule_leaves_the_run``).
    An id the operator NAMED is never dropped that way: the run owes an answer for every id
    it was given, so a held one stays in the map (``flow.py:1370-1379``,
    ``test_a_named_id_in_the_re_scheduled_tail_is_held_not_lost``).

    Adoption is bounded, and since #473 it is what bounds the RUN: a child already in the
    drive set is never re-adopted (``known``), a candidate leaves the queue once
    (``examined``), and every adopted child is a bundle already on disk — so each splice
    strictly consumes a finite set, and the schedule :func:`_pass_pool` funds cannot grow
    forever. (A child RETRACTED above is no longer in the set, so a later parent naming it
    may take it up again; each such round still needs a fresh split.) An adopted child that
    splits LATER, while this run drives it, is picked up by this same call in ITS wave — a
    recursion bound, not a recursion reset: nothing gives the run back what it has already
    spent, and no wave ever gets a second allowance. Either way the scope stays "the children
    of the bundles this run is driving, transitively" and never becomes a glob of
    ``results/``.

    ``known`` is ``batch_names | taken``, and it needs both halves: ``taken`` dedupes WITHIN
    one call — two parents that split in the SAME wave, the second's record also naming the
    first's child (``test_two_parents_splitting_in_one_wave_adopt_a_shared_child_once``) —
    while ``batch_names``, which this call grows, is the only thing that carries an adoption
    ACROSS calls, so a later wave's parent whose record names a child an earlier wave
    already adopted is skipped rather than scheduling, driving and announcing one bundle
    twice (``test_a_child_adopted_earlier_is_not_re_adopted_by_a_later_parent``).

    The two halves differ in one respect that is invisible in the dedup and load-bearing in
    the REPORT: ``batch_names`` is a schedule, ``taken`` is only a claim — the reschedule
    below can still hold that child. So a refusal is collected, not printed, and answered
    by :func:`_report_refused` once the splice has settled which of the two it was.

    Two things this deliberately does NOT do, both visible in a run and neither a
    consequence anyone should have to rediscover from the code:

    * A bundle that declared ``Depends on <parent>`` is levelled by its OWN edges, not
      re-pointed at the children the parent decomposed into — so it can share a wave with
      them. Its base is no worse than before adoption existed (a split parent closes with no
      patch either way), but the fold grouping is new. Re-pointing a dependent is a
      ``waves`` semantics change, deliberately out of this scope.
    * A child the reschedule HELD is excluded from the results map — on the call that
      adopts it, and equally on a later one that holds it again (it is dropped back out) —
      so a run whose only unfinished work is a held child still exits 0. That is the
      contract asked for: a held CHILD — work this run created, not work it was asked for —
      is not counted as work the run did (a held NAMED id stays in the map instead), and the
      hold itself is loud (:func:`_report_held`). But it does mean "this run created work it
      could not schedule" is reported on stderr rather than in the exit code.
    """
    adopted: list[tuple[Path, list[Path]]] = []
    taken: set[str] = set()
    # (parent, child, the name the run holds that child's DIRECTORY under) — skipped as
    # already-claimed, answered by `_report_refused` once the splice has settled which.
    refused: list[tuple[str, str, str]] = []
    # A work queue (not the `queue` MODULE this file imports): a child terminal on a split
    # of its own re-enters it under its own name, and `examined` lets each bundle leave it
    # once — so the walk drains on any record, including a hand-edited one naming an
    # ancestor. `pop(0)` keeps it breadth-first, so a generation is examined before the one
    # below it and the announcements read in lineage order.
    to_examine = list(candidates)
    examined: set[str] = set()
    while to_examine:
        parent = to_examine.pop(0)
        if parent.name in examined:
            continue
        examined.add(parent.name)
        # `_isolate` returns None iff the read RAISED: that candidate then contributes
        # neither children nor a walk-through, and the run carries on (its own report).
        got = _isolate(parent, "split adoption", lambda parent=parent: _adoptable(
            cfg, parent, known=batch_names | taken, refused=refused))
        kids, onward = got or ([], [])
        to_examine += onward      # a generation that already closed is walked THROUGH
        if kids:
            taken |= {c.name for c in kids}
            adopted.append((parent, kids))
    # One exit, so every refusal is answered on every path — including the two where nothing
    # is spliced (nothing adoptable; the reschedule failed). `wave_of` stays empty there, so
    # the announcement below prints nothing and `_report_refused` reads an unchanged drive
    # set: a child refused against a claim that never became a schedule is reported as the
    # run's non-owner, not as work in flight.
    wave_of: dict[str, int] = {}
    if adopted:
        children = [c for _parent, kids in adopted for c in kids]
        remaining = [d for w in wave_list[k + 1:] for d in w]
        tail = _reschedule(cfg, remaining + children)
        if tail is None:
            parents = ", ".join(p.name for p, _kids in adopted)
            print(f"flow: the children of {parents} could not be scheduled; they are left "
                  f"in-flight — drive them with `pdca flow "
                  f"{' '.join(c.name.removeprefix('issue_') for c in children)}`",
                  file=sys.stderr)
        else:
            wave_list[k + 1:] = tail      # the enumerate() in _drive_and_act picks the tail up
            wave_of = {d.name: k + 1 + j for j, w in enumerate(tail) for d in w}
            scheduled = [c for c in children if c.name in wave_of]
            bundles += scheduled
            batch_names |= {c.name for c in scheduled}
            # The other end of that rule: a child an EARLIER call adopted, still un-driven,
            # that THIS reschedule holds. It is gone from the schedule (it is not in `tail`),
            # so leaving it in the drive set would report a bundle this run neither drove nor
            # intends to — the very "excluded from the results map" the held report promises,
            # honoured only when the FIRST reschedule happened to be the one that held it.
            # Dropped from both sets, and the earlier announcement retracted by name, next to
            # `_report_held`'s reason.
            retracted = sorted(d.name for d in remaining
                               if d.name not in named and d.name not in wave_of)
            if retracted:
                gone = set(retracted)
                bundles[:] = [d for d in bundles if d.name not in gone]
                batch_names -= gone
                for name in retracted:
                    print(f"flow: {name} — adopted earlier this run, now held: it is NOT "
                          f"scheduled and NOT in this run's results (the earlier adoption "
                          f"line no longer stands)", file=sys.stderr)
    _report_refused(refused, batch_names)
    for parent, kids in adopted:
        by_wave: dict[int, list[str]] = {}
        for c in kids:
            if c.name in wave_of:
                by_wave.setdefault(wave_of[c.name], []).append(c.name)
        for idx in sorted(by_wave):
            print(f"flow: {parent.name} split → adopted children "
                  f"{', '.join(sorted(by_wave[idx]))} into wave {idx}", file=sys.stderr)


def _pass_pool(allowance: int, wave_list: list[list[Path]]) -> int:
    """The run's pass pool: one wave ``allowance`` for every wave the schedule holds RIGHT
    NOW — the waves the run set out to drive AND the ones adoption has spliced in (#473).

    Sized ONCE, before the loop, the pool was a promise the run could not keep: a splice
    pushes work into waves the arithmetic never counted, and the run then abandons bundles it
    had SCHEDULED with "the run's pass budget is spent". Two reproductions, at both ends of
    the feature: ``pdca flow 500 810 --max-passes 2``, with 500 splitting into 601 and 810
    declaring ``Conflicts with: 601``, left **810 — an id the operator typed** — PLANNED and
    exited 1; and a pure RECOVERY run, whose own drive set is empty, sized a pool off nothing
    at all however many children the seed then handed over.

    So it is read off the LIVE ``wave_list`` at each wave, which is the same thing as
    recomputing it at every splice — a splice is the only thing that changes that list — and
    the run's ceiling moves to somewhere it can be honoured: each wave gets the allowance the
    operator set, no wave gets two (:func:`_drive_wave`'s own cap), and nothing gives back
    what the run has already spent. What stops a chain of splits is that ADOPTION is bounded
    — a bundle is adopted once, a candidate examined once, the disk is finite
    (:func:`_adopt_split_children`) — not an arithmetic that starves whatever the schedule
    grew past.
    """
    return allowance * len(wave_list)


def _drive_wave(cfg: Config, wave: list[Path], *, by: str, today: str,
                max_passes: int | None = None) -> int:
    """Drive ONE wave's bundles to all-terminal (COMPLETE / DISCONTINUED) with iteration,
    then the cheap-first sign-off restricted to the wave. Publishing and folding are the
    caller's. The pass loop mirrors the prior single-batch driver: build-all
    (beat-synchronised, isolated), then — before anything is offered a session — a
    pre-apply of every pending bundle that already carries a decision from an earlier,
    interrupted session (issue #453), then a chunked sign-off over the bundles still
    undecided, whose decisions are recorded (``apply_now=False``) so an iterate-do doesn't
    rebuild mid-review — looping until the wave makes no progress (an iterate-plan re-open
    #105 still counts as progress) or every bundle is terminal.

    Neither non-terminal exit is silent (issue #260): a bundle still iterating when the
    budget runs out, or when a pass stops making progress, is named with a resume hint.
    ``max_passes`` is this wave's ALLOWANCE, and the count of passes actually consumed
    comes back to the caller (#469) — so :func:`_drive_and_act` can draw every wave of a
    run, including the ones split adoption adds, from ONE pool. Every exit reports it, and
    the two UN-finished ones matter most: a wave that ran its allowance out, or one that
    stopped making progress, is precisely the wave that spent the most, so reporting 0 there
    would hand the next (possibly adopted) wave a budget the operator never allowed. Both
    are pinned — ``test_a_wave_that_runs_its_allowance_out_still_charges_the_run_pool``,
    ``test_a_wave_that_stalls_charges_the_run_pool_for_what_it_spent``. Defaults to
    ``cfg.max_passes`` (``[driver].max_passes``)."""
    max_passes = cfg.max_passes if max_passes is None else max_passes
    names = {b.name for b in wave}
    used = 0
    for _ in range(max_passes):
        used += 1
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
                return used
            continue    # progress (e.g. an iterate-plan re-open) — give it another pass
        # Read before asking (issue #453). A pending bundle may already carry a decision from
        # an earlier session whose run died before the driver applied it (a `^C`, which
        # `_isolate` deliberately does not contain) — durable, un-consumed input, not a
        # by-product of that session. Record + transition it HERE, isolated and deferred
        # exactly like the post-session apply below, so the queue never re-presents a bundle
        # the human already judged and no session overwrites their decision. Only two
        # outcomes still owe a session: nothing was recorded, and an `accept` C6 refused (§6
        # NEEDS-HUMAN still open, so that human really must come back). A pre-apply that
        # RAISED is `_isolate`'s None — loudly skipped for this pass, like any other
        # per-bundle step that fails, rather than handed a session over an unread decision.
        needing_session: list[Path] = []
        for d in pending:
            applied = _isolate(d, "applying the recorded sign-off decision",
                               lambda d=d: _apply_recorded_decision(
                                   cfg, d, by=by, today=today, apply_now=False))
            if applied in (UNDECIDED, "blocked"):
                needing_session.append(d)
        for chunk in _chunks(needing_session, SIGNOFF_BATCH_SIZE):
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
            return used
    # Budget spent with work still in flight. An `iterate-do` recorded on the last allowed
    # pass defers its rebuild to "the next pass's build-all" — which never comes. The
    # allowance is named as THIS WAVE's (#469): once a run adopts, a later wave gets what is
    # left of the run's pool, and "exhausted after 1 pass(es); raise [driver].max_passes"
    # while that setting reads 20 is a contradiction to the operator it is addressed to.
    _warn_abandoned(wave, why=f"pass budget exhausted after {max_passes} pass(es) — this "
                             f"wave's allowance out of the run's pool; raise "
                             f"[driver].max_passes / PDCA_MAX_PASSES / --max-passes")
    return used


def _drive_and_act(
    cfg: Config,
    bundles: list[Path],
    *,
    do_publish: bool,
    do_act: bool,
    by: str,
    today: str,
    max_passes: int | None = None,
    adopt_seeds: list[Path] | None = None,
) -> dict[str, str]:
    """Drive a set of in-flight bundles through the full cycle to Act, in waves.

    The shared body of both batch entry points. :func:`waves.compute_waves` orders the
    batch into dependency waves (rejecting a cycle / unresolved dep up front); each wave is
    driven to sign-off, its accepted bundles published, and — in the default ``stack``
    mode — its cumulative accepted work folded onto a run-scoped integration branch
    (:func:`integrate.fold`) the **next** wave builds on. So a dependent builds on its
    prerequisite's accepted result within one run, as a reviewable PR stack the human
    merges (the harness never merges). Act runs **once** across the batch at the end.

    The drive set is no longer frozen (#469): a bundle that reaches
    ``close-disposition = split`` while this run is driving it hands its children back to
    the run, spliced into the waves after its own (:func:`_adopt_split_children`).
    ``adopt_seeds`` extends that to bundles the caller is NOT driving but wants examined for
    stranded children (#473) — an id the operator named whose bundle was ALREADY terminal on
    a split when the run started, which is how a run that stopped before its children were
    driven is recovered. They are spliced by the same call at ``k=-1``, so recovery is one
    more use of the adoption path rather than a second mechanism beside it.

    ``max_passes`` (``[driver].max_passes``) keeps its meaning — the allowance ONE wave gets
    — and sizes the run's **pool** at one allowance per wave the schedule holds, read LIVE so
    that a splice which grows it re-sizes it (:func:`_pass_pool`, #473). So a wave the run
    acquired mid-flight is funded like any other, and no bundle the run has SCHEDULED — an
    adopted child, or an id the operator typed that a splice pushed further back — is
    abandoned to arithmetic done before that wave existed. What bounds a chain of splits is
    that adoption itself is bounded; ``spent`` is never reset, and a wave still gets no more
    than one allowance. Running the pool out stops the run and names what it walked away
    from (#260's discipline).

    ``--no-publish`` (``do_publish=False``) drives every wave to COMPLETE but sequences
    nothing — no publish, no fold — so a later wave builds on the unchanged base.
    """
    bundles = list(bundles)          # the drive set — split adoption extends it (#469)
    allowance = cfg.max_passes if max_passes is None else max_passes
    spent = 0                        # passes this RUN has consumed, across every wave
    batch_names = {b.name for b in bundles}  # in-batch prereqs ride the fold; #186 gates the rest
    # The ids this run SET OUT to drive, frozen (#469). The drive set grows and shrinks with
    # adoption; this does not — it is what separates "an id the operator named", which is
    # answered for even when held, from "a child this run adopted", which is dropped again
    # if a later reschedule holds it (:func:`_adopt_split_children`).
    named = frozenset(batch_names)
    published: set[str] = set()
    accepted: list[Path] = []        # cumulative COMPLETE bundles, wave then name order
    integ: dict[tuple[str, str], str] = {}  # per-target (repo, base) → integration branch (#187)
    preflighted = False              # per-lane preflight runs at most once, before the first pool
    # The batch the caller NAMED is levelled first and strictly, exactly as before —
    # `compute_waves` raises on a cycle or an unresolvable dependency. That contract belongs
    # to the request (`waves.partition_schedulable`'s own docstring, waves.py:243-246, calls
    # raising "right for an explicit `flow <ids>`"), and adoption never relaxes it.
    #
    # It is not a promise about the RE-levelling, and the difference is worth stating
    # plainly (PR review): a splice re-waves `remaining + children` through the tolerant
    # path, and `remaining` is the un-driven tail of the operator's own id list. So a named
    # id whose prerequisite this run left un-terminal is HELD and reported there, where an
    # un-spliced run would have reached its wave and let `_runnable` skip it. Same end state
    # — PLANNED, in the results map, the run fails — one different line, and never a raise
    # mid-run (`_reschedule`). Pinned by
    # `test_a_named_id_in_the_re_scheduled_tail_is_held_not_lost`. An ADOPTED child held by
    # that same re-levelling goes the other way — out of the drive set again, so it is not
    # reported as this run's work (`_adopt_split_children`, `named` above).
    wave_list = waves.compute_waves(cfg, bundles)  # validates (raises) + levels the batch
    # Recovery (#473): a seed is an id the operator named whose bundle was ALREADY terminal
    # on a split, so an earlier run's children may still be sitting where it left them.
    # `k=-1` makes `wave_list[k+1:]` the WHOLE schedule — the children are levelled in front
    # of everything else, by the same splice, the same guards and the same report a mid-run
    # split gets. With no seeds this is not even entered and the waves above stand.
    if adopt_seeds:
        _adopt_split_children(cfg, list(adopt_seeds), k=-1, wave_list=wave_list,
                              bundles=bundles, batch_names=batch_names, named=named)
        if not bundles:
            # A recovery run whose seeds offered nothing adoptable (the brood was already
            # driven, or every child was refused and named above). There is no schedule, so
            # there is nothing to drive, sweep, publish or Act on — the quiet no-op naming a
            # finished bundle has always been. The seeds' own dispositions are `flow_ids`'
            # `skipped` map, which is what the caller reports.
            return {}
    # `wave_list` is iterated by a LIST iterator on purpose (#469): adoption splices the
    # recomputed remainder into `wave_list[k+1:]` after wave k drives, and the iterator —
    # which simply indexes forward — picks the new tail up. So "how many waves are left" is
    # read live below (never cached in a `last`), and an adopted child's wave is driven,
    # published and folded by exactly the code every other wave goes through.
    for k, wave in enumerate(wave_list):
        runnable = _runnable(cfg, wave, batch_names)
        if not runnable:
            continue
        # The pool, read off the schedule as it stands NOW — so a splice below has already
        # re-sized it by the time the wave it created asks to open (#473).
        budget = _pass_pool(allowance, wave_list)
        if spent >= budget:
            # No wave opens on budget the pool does not hold (#469) — the pool's ADMISSION
            # rule, which is not the same statement as the arithmetic that feeds it. Since
            # #473 the pool covers the LIVE schedule, so a run whose waves each spend
            # ≤ `allowance` no longer reaches this, and that is the point: the wave adoption
            # added is funded instead of abandoned. The rule stays because it still has to
            # hold when the arithmetic does not — a wave that came to spend more than it was
            # handed must stop the run here rather than overspend silently. The one input
            # that still reaches it is an allowance of 0, which no operator can type
            # (`config.py` clamps `[driver].max_passes` / `PDCA_MAX_PASSES`, `cli.py:572`
            # clamps `--max-passes`) but a `Config` built in-process can hold.
            # Never silent (#260): everything still in flight is named with a hint.
            _warn_abandoned([d for w in wave_list[k:] for d in w],
                            why=f"the run's pass budget is spent ({budget} pass(es) over "
                                f"{k} wave(s)); raise [driver].max_passes / "
                                f"PDCA_MAX_PASSES / --max-passes")
            break
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
        # This wave's allowance is its own cap AND what is left of the run's pool, whichever
        # is smaller — the second term is the admission rule again, applied to the wave that
        # was let in (#469); with the pool covering the live schedule (#473) it can only bite
        # if a wave came to spend more than it was handed.
        spent += _drive_wave(cfg, runnable, by=by, today=today,
                             max_passes=min(allowance, budget - spent))
        # A bundle that SPLIT during this wave hands its children back to the run that
        # caused the split (#469), spliced into the waves AFTER this one — instead of
        # ending with the parent terminal, the children PLANNED, and the operator
        # restarting by hand with `pdca flow <child-ids>`. The waves it adds are funded by
        # the `_pass_pool` read at the top of the NEXT iteration (#473); `spent` is untouched
        # here and never anywhere else — a bound, not a reset.
        _adopt_split_children(cfg, runnable, k=k, wave_list=wave_list, bundles=bundles,
                              batch_names=batch_names, named=named)
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
        if k < len(wave_list) - 1 and do_publish:
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
    _report_held(held)  # one report shape, shared with split adoption (#469)
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
    (planner skipped them) and terminal ids (COMPLETE / DISCONTINUED) are left alone —
    except that an id terminal on a ``split`` is ALSO handed to :func:`_drive_and_act` as an
    **adoption seed** (#473), so a run named after a parent whose children an earlier run
    stranded drives those children instead of only re-printing the hint at them.

    Returns ``{issue_id: state}`` for **every id it was given** (issue #468), driven or
    skipped — a skipped id's disposition is its state on disk, the same value
    ``_drive_and_act`` records for a bundle it DID drive. TOTAL, because this map is the
    single authority both ``pdca flow <id>`` and ``pdca flow <id> <id>`` report and derive
    their exit code from (``cli._flow``): a map that silently omitted the ids it skipped
    would force each caller to fill the gaps itself, which is precisely how the two CLI
    shapes came to disagree — the same DISCONTINUED bundle exited 1 alone and 0 next to a
    completing sibling, because only one shape could see it. Unlike ``flow_batch``, whose
    caller passes no id list and can therefore only be told what was driven, every id here
    was ASKED FOR by name and so gets an answer.
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
    skipped: dict[str, str] = {}   # asked for, not driven — still gets a disposition (#468)
    seeds: list[Path] = []       # terminal on a split — examined for stranded kids (#473)
    for iid in ids:
        d = cfg.bundle(iid)
        s = state.state(d)
        if not d.exists() or s == state.UNPLANNED:
            print(f"flow: {d.name} — no brief.md, skipped (brief it at Plan first)", file=sys.stderr)
            skipped[iid] = s
            continue
        if s in _TERMINAL:
            print(f"flow: {d.name} — already terminal ({s}), skipped", file=sys.stderr)
            hint = _terminal_hint(d, s)   # the ONE recovery advice, both CLI shapes (#468)
            if hint:
                print(f"  {hint}", file=sys.stderr)
            skipped[iid] = s
            if _is_split_parent(d):
                # Terminal on a `split` is nothing to DRIVE — but its children may still be
                # sitting PLANNED where the split left them, and naming the parent again is
                # the operator's recovery (#473). Dropping the id HERE, with nothing else
                # done about it, is precisely what stranded them; the run is handed the
                # parent as an adoption seed instead. Its own disposition stays in
                # `skipped`, so the results map both CLI shapes report still answers for
                # every id it was given (#468), and each child gets its own line from the
                # adoption report. The hint above still stands for whatever this run cannot
                # adopt, which is why it is qualified rather than replaced.
                print("  its children are examined for adoption into THIS run — that "
                      "command is only needed for whatever cannot be adopted, which is "
                      "named below.", file=sys.stderr)
                seeds.append(d)
            continue
        bundles.append(d)
    if not bundles and not seeds:
        return skipped
    bundles.sort(key=lambda p: p.name)
    return skipped | _drive_and_act(cfg, bundles, adopt_seeds=seeds, do_publish=do_publish,
                                    do_act=do_act, by=by, today=today,
                                    max_passes=max_passes)


def _bundle_dirs(cfg: Config) -> set[str]:
    """Names of the existing ``issue_*`` bundle directories."""
    if not cfg.bundle_root.exists():
        return set()
    return {p.name for p in cfg.bundle_root.glob("issue_*") if p.is_dir()}
