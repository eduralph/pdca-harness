"""Bundle state derived from files present — no database (docs 03 §state machine).

The state of an issue *is* the set of files in its bundle directory. This module
is the single source of truth for "what state is issue N in"; the driver acts on
the answer. Keeping state in the filesystem is what makes the pipeline resumable
and inspectable (``ls`` answers the question).
"""

from __future__ import annotations

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

# States where the driver does nothing (human work, or done).
HALTED = {UNPLANNED, AWAITING_SIGNOFF, COMPLETE, DISCONTINUED}

# Close-disposition fast path (issue #60): a bundle whose Plan concluded a close /
# no-fix outcome never builds a patch. Its close marker is the Do artifact — the
# symmetric stand-in for patch.diff — so the state machine reads it as "past Do".
CLOSE_MARKER = "close-disposition"

# §9 outcome token → bundle state. state owns the state names, so the mapping
# lives here; signoff knows only the tokens (no import cycle).
_OUTCOME_TO_STATE = {
    "merged-wider": COMPLETE,
    "accepted": COMPLETE,
    "iterated-to-Do": ITERATE_DO,
    "iterated-to-Plan": ITERATE_PLAN,
    "discontinued": DISCONTINUED,
}


def state(d: Path) -> str:
    """Return the bundle's state from the files present (docs 03 §state)."""
    bp = d / "brief.md"
    if not bp.exists():
        return UNPLANNED
    # Do is done when there's a patch — OR, on the close-disposition fast path, the
    # close marker that stands in for it (a close bundle never builds a patch.diff).
    if not (d / "patch.diff").exists() and not (d / CLOSE_MARKER).exists():
        # Pre-Do only: a brief that's still an unfilled template (Slug missing / a `<…>`
        # placeholder) means the planner never authored it, so treat it as UNPLANNED and
        # let the Plan beat re-plan it instead of being skipped (issue #113). Scoped to
        # the pre-Do boundary so a real, progressed bundle is never reclassified.
        return UNPLANNED if brief.is_placeholder(bp) else PLANNED
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
