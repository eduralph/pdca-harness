"""Bundle state derived from files present — no database (docs 03 §state machine).

The state of an issue *is* the set of files in its bundle directory. This module
is the single source of truth for "what state is issue N in"; the driver acts on
the answer. Keeping state in the filesystem is what makes the pipeline resumable
and inspectable (``ls`` answers the question).
"""

from __future__ import annotations

from pathlib import Path

from . import signoff

# The ordered states a bundle moves through. The terminal/parked states
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

# States where the driver does nothing (human work, or done).
PARKED = {UNPLANNED, AWAITING_SIGNOFF, COMPLETE}

# §9 outcome token → bundle state. state owns the state names, so the mapping
# lives here; signoff knows only the tokens (no import cycle).
_OUTCOME_TO_STATE = {
    "merged-wider": COMPLETE,
    "accepted": COMPLETE,
    "iterated-to-Do": ITERATE_DO,
    "iterated-to-Plan": ITERATE_PLAN,
}


def state(d: Path) -> str:
    """Return the bundle's state from the files present (docs 03 §state)."""
    if not (d / "brief.md").exists():
        return UNPLANNED
    if not (d / "patch.diff").exists():
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
