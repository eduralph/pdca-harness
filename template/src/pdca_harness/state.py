"""Bundle state derived from files present — no database (docs 03 §state machine).

The state of an issue *is* the set of files in its bundle directory. This module
is the single source of truth for "what state is issue N in"; the driver acts on
the answer. Keeping state in the filesystem is what makes the pipeline resumable
and inspectable (``ls`` answers the question).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import brief, signoff

# The ordered states a bundle moves through. The terminal/halted states
# (UNPLANNED, AWAITING_SIGNOFF, COMPLETE) are where the driver stops and a human
# acts; the rest the driver advances through unattended.
UNPLANNED = "UNPLANNED"  # no brief — human authors it (Plan)
PLANNED = "PLANNED"  # brief present, ready for Do
BUILT = "BUILT"  # patch present, ready for Check (gates + reviewer)
CHECKED = "CHECKED"  # gates + review present, ready to assemble SUMMARY
AWAITING_SIGNOFF = "AWAITING_SIGNOFF"  # SUMMARY assembled, §9 empty — STOP, human
ITERATE_DO = "ITERATE_DO"  # sign-off chose iterate-to-Do
ITERATE_PLAN = "ITERATE_PLAN"  # sign-off chose iterate-to-Plan
COMPLETE = "COMPLETE"  # sign-off accepted — bundle frozen
DISCONTINUED = "DISCONTINUED"  # sign-off chose discontinue — deliberately abandoned, no transition
RESOLVED = "RESOLVED"  # briefless tracker bundle; notes.json records a terminal tracker resolution

# States where the driver does nothing (human work, or done).
HALTED = {UNPLANNED, AWAITING_SIGNOFF, COMPLETE, DISCONTINUED, RESOLVED}

# Terminal-FINISHED states (issue #317): the cycle is over and the bundle's files are
# final — accepted (COMPLETE), deliberately abandoned (DISCONTINUED), or settled in the
# tracker outside a cycle (RESOLVED). Deliberately NOT the whole HALTED set: UNPLANNED
# and AWAITING_SIGNOFF are halted FOR a human — work is pending and the files still
# change. Defined here, in the one module that owns the state names, so a consumer
# (`record` selects on this) never re-enumerates states and drifts.
TERMINAL = frozenset({COMPLETE, DISCONTINUED, RESOLVED})

# Close-disposition fast path (issue #60): a bundle whose Plan concluded a close /
# no-fix outcome never builds a patch. Its close marker is the Do artifact — the
# symmetric stand-in for patch.diff — so the state machine reads it as "past Do".
CLOSE_MARKER = "close-disposition"

# Do-exit dependency adjudication record (issue #341): how the driver adjudicated a
# builder-declared unmet external dependency at BUILT (confirmed ⇒ the beat rerouted to
# the close fast path; refuted ⇒ full Check ran, the refutation lifted into §6). Named
# here, like CLOSE_MARKER, so `dependency_halt` and the archive list share one spelling.
DEPENDENCY_ADJUDICATION = "dependency-adjudication.json"

# The sign-off session's live carry-forward (issue #331): the FULL multi-line rationale
# the session wrote below its decision token, captured by the driver (flow) at the moment
# the decision is consumed — §9's "Iteration delta" flattens it to one line and the
# decision file itself is unlinked, so without this capture the only structured copy is
# destroyed before the iterate transition reads it. Consumed (merged into the brief's
# carry-forward block) by driver._carry_forward_into_brief, then archived with its
# attempt via DOWNSTREAM_OF_BRIEF below.
SESSION_CARRY = "session-carry-forward"

# The reviewer leaf's captured-error tail (#138): written when the reviewer RAN AND
# FAILED (retries exhausted), removed at the start of a successful run — which makes it
# the discriminator (#369) between a reviewer that ran-and-failed and one that NEVER ran
# (an interrupted beat), since neither leaves a check-review.md. Named here (like
# CLOSE_MARKER) so the writer (``leaves``), the CHECKED-resume check (``driver``) and
# the §6 wording split (``assemble``) share one spelling.
REVIEW_ERROR_LOG = "check-review.error.log"

# The per-rule gate evidence logs (issue #370): ``gate-logs/<rule_id>.log`` — the full
# combined output behind each ``check-gates.json`` row, written by a bundle-scoped
# ``gates.run_gates``. Named here (like CLOSE_MARKER / SESSION_CARRY) so ``gates`` (the
# writer) and the archive list below share one spelling.
GATE_LOGS_DIR = "gate-logs"

# Everything Do and Check write, i.e. everything downstream of brief.md. Includes the
# close marker (issue #60) so an iterate archives it too — reopening a close bundle to a
# fix path then clears the marker and runs the real Do+Check band.
#
# Lives here rather than in `driver` (#334) because `is_resolved` must read it and
# `driver` already imports this module — the other direction would be a cycle. `driver`
# re-exports the name, so `driver.DOWNSTREAM_OF_BRIEF` still resolves.
DOWNSTREAM_OF_BRIEF = [
    "patch.diff",
    "build-notes.md",
    CLOSE_MARKER,
    "MANUAL-VERIFICATION.md",
    "check-gates.json",
    "check-gates.md",
    "check-review.md",
    "SUMMARY.md",
    # The rubric snapshot (#314): a Do/Check-era artifact, so an iterate archives it and
    # the rebuild takes a fresh one — a rubric that changed between attempts SHOULD apply
    # to the next.
    "rubric-snapshot.md",
    # The empirical size measurement (#324). Same reasoning, plus a sharper one: it is
    # measured FROM patch.diff, which this list archives. Left behind it would describe an
    # attempt that is no longer there — and the archive of a rejected attempt would lack
    # the very numbers that justified rejecting it. Not in CYCLE_EVIDENCE_ONLY: unlike the
    # auto-iterate budget it does not accumulate, it is rewritten wholesale each Check.
    "size-signal.json",
    # The Do-exit dependency adjudication (#341). A Do/Check-era verdict about THIS
    # attempt's build-notes.md, so an iterate archives it with the attempt and the
    # rebuild is adjudicated fresh — a stale record left behind would describe a
    # declaration the new build-notes.md may no longer make.
    DEPENDENCY_ADJUDICATION,
    # The captured sign-off-session carry-forward (#331): written by flow when the
    # decision is consumed, merged into the brief by _carry_forward_into_brief, and
    # archived here WITH the attempt it describes — never left to leak into the next one.
    SESSION_CARRY,
    # The gate evidence logs (#370): a directory, one <rule_id>.log per configured check.
    # Archived per round WITH the verdict they explain, so each iteration-v<N>/ keeps the
    # full basis of its own gate run — the state-is-files doctrine applied to evidence.
    GATE_LOGS_DIR,
]

# Cycle artifacts matched by pattern rather than name. ONE definition, read by both
# `_archive_iteration` (what an iterate moves) and `is_resolved` (what counts as evidence
# a cycle ran), so those two answers cannot drift apart. `*.memory.jsonl` is a leaf run's
# scope telemetry (`leaves._MemoryTelemetry`): per-attempt like its `*.error.log` twin, so
# it archives with the round it describes — and a bundle can only hold one if a leaf ran.
DOWNSTREAM_GLOBS = ("check-advisory-*.md", "*.error.log", "*.memory.jsonl")

# Cycle evidence that must NOT be archived — the one set where "what the archive moves"
# and "what proves a cycle ran" deliberately differ, so it is deliberately NOT read by
# `_archive_iteration`.
#
# Both files accumulate ACROSS rebuilds by design, and archiving either breaks the
# feature that depends on the accumulation:
#   auto-iterate.json   — the round budget; archive it and the count resets every
#                         iterate, so auto-iterate never terminates.
#   loop-telemetry.json — `leaves._record_loop_attempt`: "The file persists across
#                         iterations (it is not archived), so it accumulates."
# Yet a bundle cannot hold either without having run a cycle, so both are unambiguous
# evidence. Adding them to DOWNSTREAM_OF_BRIEF instead would fix the misclassification
# below and break termination, which is the worse bug.
CYCLE_EVIDENCE_ONLY = ("auto-iterate.json", "loop-telemetry.json")

# §9 outcome token → bundle state. state owns the state names, so the mapping
# lives here; signoff knows only the tokens (no import cycle).
_OUTCOME_TO_STATE = {
    "merged-wider": COMPLETE,
    "accepted": COMPLETE,
    "iterated-to-Do": ITERATE_DO,
    "iterated-to-Plan": ITERATE_PLAN,
    "discontinued": DISCONTINUED,
}


def is_resolved(d: Path) -> bool:
    """Briefless-tracker terminal marker (issue #302): notes.json carries a top-level
    dict ``resolved`` (e.g. ``{github_state, state_reason, closed_at, note}``) — the
    question was settled in the tracker, outside a cycle. Defensive: absent /
    unreadable / malformed notes.json, or a non-object ``resolved``, is False — never
    a crash (testbed issue #3). Callers scope this to BRIEFLESS bundles only, so a
    real cycle bundle is never reclassified by a stray key; note that a brief archived
    by iterate-plan makes the bundle briefless again — a ``resolved`` written then
    deliberately means "stop re-planning, the tracker settled it"."""
    notes = d / "notes.json"
    if not notes.exists():
        return False
    try:
        data = json.loads(notes.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if not (isinstance(data, dict) and isinstance(data.get("resolved"), dict)):
        return False
    # RESOLVED is terminal: the bundle leaves the resume set and `do_plan` returns early
    # rather than briefing it (#302). So a marker arriving while a cycle is IN FLIGHT —
    # a stale scrape, a tracker item closed as a duplicate, a human closing the ticket
    # while the fix is being built — must not settle it. The docstring's "callers scope
    # this to BRIEFLESS bundles" is not a guard the caller can honour: an iterate-to-Plan
    # ARCHIVES brief.md, so a bundle mid-cycle with a full iteration history is briefless
    # too. Decide it here, from evidence on disk (#334).
    return not has_cycle_evidence(d)


def has_cycle_evidence(d: Path) -> bool:
    """True if anything in the bundle proves a cycle actually ran (issue #334).

    Only a genuinely notes-only bundle can be RESOLVED. Every other artifact class means
    work happened that a terminal marker would silently abandon — and the failure is
    silent in the direction that costs most: the bundle drops out of the resume set and
    Plan skips it, so a cycle with real iteration history ends with nothing reported.
    """
    bp = d / "brief.md"
    if bp.exists() and not brief.is_placeholder(bp):
        # An AUTHORED brief only. An unfilled template copy is "never authored" — the same
        # standing as no brief at all — so the tracker's resolution still wins there
        # (#302 review), which `test_placeholder_brief_does_not_unresolve_a_resolved_tracker`
        # locks. Read via `whole_field`, so a Slug written beneath its label is recognised
        # as authored rather than mistaken for a template (#336).
        return True
    if any((d / name).exists() for name in DOWNSTREAM_OF_BRIEF):
        return True
    if any((d / name).exists() for name in CYCLE_EVIDENCE_ONLY):
        return True
    if any(next(d.glob(pattern), None) for pattern in DOWNSTREAM_GLOBS):
        return True
    return next(d.glob("iteration-v*"), None) is not None


def state(d: Path) -> str:
    """Return the bundle's state from the files present (docs 03 §state)."""
    bp = d / "brief.md"
    if not bp.exists():
        # No brief ever authored — pending Plan, unless the tracker itself settled the
        # question (a notes-only bundle with a `resolved` record is terminal, #302).
        return RESOLVED if is_resolved(d) else UNPLANNED
    # Do is done when there's a patch — OR, on the close-disposition fast path, the
    # close marker that stands in for it (a close bundle never builds a patch.diff).
    if not (d / "patch.diff").exists() and not (d / CLOSE_MARKER).exists():
        # Pre-Do only: a brief that's still an unfilled template (Slug missing / a `<…>`
        # placeholder) means the planner never authored it, so treat it as UNPLANNED and
        # let the Plan beat re-plan it instead of being skipped (issue #113). Scoped to
        # the pre-Do boundary so a real, progressed bundle is never reclassified.
        # A placeholder is "never authored" — the same standing as no brief at all — so
        # the tracker's terminal `resolved` marker still wins there (#302 review): a
        # resolved notes-only bundle that picked up a stray template copy must not
        # reappear as pending. An AUTHORED brief keeps its normal PLANNED path.
        if brief.is_placeholder(bp):
            return RESOLVED if is_resolved(d) else UNPLANNED
        return PLANNED
    if not (d / "check-gates.json").exists():
        return BUILT
    if not (d / "SUMMARY.md").exists():
        return CHECKED
    if not signoff.is_set(d / "SUMMARY.md"):
        return AWAITING_SIGNOFF
    # is_set() guarantees the token is one of VALID_OUTCOMES, but stay defensive: a
    # token without a mapping (a future outcome added to signoff but not here) means
    # "not validly complete" → AWAITING_SIGNOFF, never a KeyError out of the one
    # primitive the whole driver depends on (testbed issue #3).
    return _OUTCOME_TO_STATE.get(signoff.outcome_token(d / "SUMMARY.md"), AWAITING_SIGNOFF)
