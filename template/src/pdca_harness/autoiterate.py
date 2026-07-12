"""Auto-iterate: resolve implementation-only Check findings without stopping for a human.

Issue #264. A big Do lands with implementation defects the reviewer or the adversary
catches — a logic slip, a weak test, a failing gate. Today every one of those parks the
bundle at ``AWAITING_SIGNOFF`` and asks the human to press "iterate-do", which is exactly
the decision the driver could have made itself. The human's judgment is owed only to
findings that are *architecturally* relevant.

The split already exists in the codebase: ``gates._FIVE_FIVE_ONE`` tags each of the 11
check cells ``input | gate | judgment``. The ``gate`` cells (C2 reproduction, C4
verification, T1..T4) are mechanically checkable, so a rebuild can address them; the
``judgment`` cells (C5 causal adequacy, T5 judgment, V validation) and the ``input`` cells
(C1 spec, C3 change) are the human's. ``assemble.collect_needs_human`` tags every §6 item
IMPL or HUMAN from exactly that source.

So: when a bundle reaches ``AWAITING_SIGNOFF`` with at least one IMPL item and nothing the
human must see first, the driver writes an ``iterate-do`` decision and re-drives Do. Anything
else — an empty §6 (a clean bundle awaiting a human accept), a situational HUMAN item, an
exhausted budget — halts as before.

One item is deliberately NOT "something the human must see first": the reviewer's
``Validation — fitness-to-purpose`` row, which its prompt hard-codes to NEEDS-HUMAN on EVERY
cycle whatever it finds (:data:`assemble.STANDING`). It is a constant, so it carries no
signal. Counting it as an ordinary HUMAN item made the original ``all(IMPL)`` rule impossible
to satisfy on a real bundle, and this feature never fired once in production (#293).

Three properties hold by construction:

* **It only ever writes ``iterate-do``.** Never ``accept``, never ``discontinue``. The
  decision goes through the same C6-guarded ``flow._apply_decision`` a human sign-off uses,
  so §9 stays authored solely by ``signoff.record``.
* **It never clears a §6 box.** An ``iterate-do`` archives the whole SUMMARY, unticked, into
  ``iteration-v<N>/``; the rebuild produces a fresh §6.
* **It is bounded.** ``[driver].max_auto_iters`` automatic rounds per bundle, counted in
  ``auto-iterate.json`` (deliberately NOT in ``driver.DOWNSTREAM_OF_BRIEF``, so the archive
  step doesn't move it and the count accumulates across rebuilds). On exhaustion the bundle
  is left at ``AWAITING_SIGNOFF`` for the human — never dropped.

Opt-in: ``[driver].auto_iterate = false`` by default.
"""

from __future__ import annotations

import json
from pathlib import Path

from .assemble import IMPL, STANDING, NeedsHumanItem
from .leaves import SIGNOFF_DECISION

BUDGET_FILE = "auto-iterate.json"

# The only token this module is ever allowed to write.
DECISION = "iterate-do"


def eligible(items: list[NeedsHumanItem]) -> bool:
    """True iff a rebuild is the right next step: at least one IMPL finding, and nothing
    else the human must see *first*.

    An **empty** §6 is deliberately not eligible — that is a clean bundle awaiting a human
    *accept*, and auto-iterate must never accept. A situational HUMAN item still disqualifies
    the whole bundle: the human has to look at it anyway, so there is nothing to save by
    rebuilding first.

    But a **STANDING** item does not. The reviewer's prompt hard-codes `Validation —
    fitness-to-purpose` to NEEDS-HUMAN on EVERY cycle regardless of what it found, so that row
    is a constant, and a constant is not evidence that a human must look right now. Requiring
    `all(IMPL)` therefore made this function unreachable in production: every real review
    artifact carries that row, so auto-iterate never fired once (#293). The bundle still halts
    for the human as soon as the implementation findings are gone — which is the whole point:
    iterate Do→Check while the reviewer keeps finding defects only Do can fix, then hand over.
    """
    return (any(item.kind == IMPL for item in items)
            and all(item.kind in (IMPL, STANDING) for item in items))


def count(d: Path) -> int:
    """How many automatic iterations this bundle has already spent. Tolerant of a missing
    or garbled file, like ``loop-telemetry.json``."""
    try:
        return int(json.loads((d / BUDGET_FILE).read_text(encoding="utf-8"))["count"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def bump(d: Path) -> int:
    """Spend one automatic iteration; return the new count."""
    n = count(d) + 1
    (d / BUDGET_FILE).write_text(json.dumps({"count": n}) + "\n", encoding="utf-8")
    return n


def rationale(items: list[NeedsHumanItem], *, attempt: int) -> str:
    """The §9 "Iteration delta" line, which the driver folds into the brief's carry-forward
    so the next Do iteration isn't blind about why it was rejected.

    IMPL items ONLY. The STANDING `Validation` row rides along in ``items`` (it does not veto
    the rebuild, #293), but it is not a finding and no builder can act on it — carrying it
    forward would hand the next Do a human-only judgment call as though it were a defect to fix,
    under a sentence claiming the set is "implementation-level items only" (PR #294 review).
    """
    findings = "; ".join(item.text for item in items if item.kind == IMPL)
    return (f"Auto-iterate (round {attempt}): Check found implementation-level items only, "
            f"no architectural judgment required — {findings}")


def write_decision(d: Path, items: list[NeedsHumanItem]) -> None:
    """Write the ``iterate-do`` decision + rationale, and spend one round of the budget.

    Guarded: refuses to write anything for an ineligible item set, so no caller can turn
    this into an auto-accept.
    """
    if not eligible(items):
        raise ValueError("auto-iterate: refusing to decide on a non-implementation finding set")
    attempt = bump(d)
    (d / SIGNOFF_DECISION).write_text(
        f"{DECISION}\n{rationale(items, attempt=attempt)}\n", encoding="utf-8")
