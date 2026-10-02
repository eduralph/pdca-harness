"""Auto-iterate: keep rebuilding while Check still finds implementation work.

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

So: when a bundle reaches ``AWAITING_SIGNOFF`` with at least one IMPL item, the driver writes
an ``iterate-do`` decision and re-drives Do. HUMAN items beside the IMPL ones do NOT veto that
rebuild (#409): they are **deferred** — held in :data:`DEFERRED_FILE` and merged back into §6
at handover, so each one still reaches the human under the C6 accept-guard. The veto was what
stopped the loop in practice: auto-iterate fired on 31 of 230 eligible-checked attempts
(13.5%), because one situational HUMAN row beside any number of build defects declined the
round. A set with NO implementation work — an empty §6, or HUMAN items only — still halts at
once: there is nothing for a rebuild to address. One kind of HUMAN row is not deferred: a
review or a gate that gave NO verdict this round (``NeedsHumanItem.no_verdict``). The next
Check runs them again, so its §6 carries that row exactly while the failure lasts, and a
failure that has recovered never needs clearing by hand at handover.

Exactly two things stop the loop while implementation work remains, and neither is new:

* **the size backstop** (#324) — a HUMAN item ``size_signal.is_size_item`` recognises. It is
  the one HUMAN item that stops the loop by its KIND rather than being deferred, because it
  is evidence that more rebuilds are the wrong move. It fires at 2 rounds by default, below
  the hard cap, on purpose (``rounds`` under ``[driver.size_signal]`` in ``pdca.toml``);
* **the hard cap** — ``[driver].max_auto_iters``.

The reviewer's ``Validation — fitness-to-purpose`` row (:data:`assemble.STANDING`) is neither
work nor a deferral: its prompt hard-codes it to NEEDS-HUMAN on EVERY cycle, so it is a
constant that carries no signal (#293). It is not recorded in the ledger — the fresh §6 of
every Check already carries it.

Four properties hold by construction:

* **It only ever writes ``iterate-do``.** Never ``accept``, never ``discontinue``. The
  decision goes through the same C6-guarded ``flow._apply_decision`` a human sign-off uses,
  so §9 stays authored solely by ``signoff.record``.
* **It never clears a §6 box.** An ``iterate-do`` archives the whole SUMMARY, unticked, into
  ``iteration-v<N>/``; the rebuild produces a fresh §6. Only a box the HUMAN ticked retires
  a deferred finding (:func:`retire_cleared`).
* **It defers, it does not drop.** Every HUMAN finding a round iterates past is in the ledger
  before the decision is written, and the ledger is not archived. A ledger that exists but
  cannot be read is itself a §6 item that blocks accept.
* **It is bounded.** ``[driver].max_auto_iters`` automatic rounds per bundle, counted in
  ``auto-iterate.json`` (deliberately NOT in ``driver.DOWNSTREAM_OF_BRIEF``, so the archive
  step doesn't move it and the count accumulates across rebuilds). On exhaustion the bundle
  is left at ``AWAITING_SIGNOFF`` for the human — never dropped.

Opt-in: ``[driver].auto_iterate = false`` by default.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from . import signoff, size_signal
from .assemble import FINDING_LABELS, HUMAN, IMPL, NeedsHumanItem
from .leaves import SIGNOFF_DECISION

BUDGET_FILE = "auto-iterate.json"

# The HUMAN findings a round iterated PAST (#409). An `iterate-do` archives SUMMARY.md and
# check-review.md and the rebuild assembles a fresh §6, so a HUMAN finding raised in an early
# round exists nowhere afterwards unless a later reviewer happens to raise it again. Each one
# is recorded here and merged back into §6 at every assembly (`assemble.assemble_summary`).
# Like BUDGET_FILE it is deliberately NOT in `state.DOWNSTREAM_OF_BRIEF`, so the archive
# step leaves it in place and it accumulates across rebuilds; it IS in
# `state.CYCLE_EVIDENCE_ONLY`, since a bundle cannot hold one without having run a cycle.
DEFERRED_FILE = "deferred-findings.json"

# The only token this module is ever allowed to write.
DECISION = "iterate-do"

# The §6 row raised when the ledger exists but cannot be read. A FIXED text, so the
# assembly path and `flow._apply_decision` render the same row and never add a second copy.
UNREADABLE_LEDGER_ITEM = (
    f"{DEFERRED_FILE} exists but cannot be read — findings an auto-iterate round deferred "
    "to this handover may be missing from this §6. Recover them from the archived "
    "iteration-v*/SUMMARY.md §6, then repair or remove the file before accepting.")

# Matching a §6 row to a ledger entry when the human edited it. An annotation appended, a tail
# trimmed or a note prefixed leaves one text inside the other (containment). A word changed in
# the MIDDLE leaves neither inside the other, only a long shared opening, and a fixed character
# threshold for that is brittle exactly where it matters — so that test is PROPORTIONAL: the
# shared opening must be most of the shorter text.
#
# Both tests read only the FINDING, past any label the two rows share
# (`assemble.FINDING_LABELS`: a 5/5/1 Item such as "C5 Causal adequacy", or a placeholder's
# leaf-status label). A label says where a finding came from, not what it says. Counted as
# shared text, the 21 characters of "C5 Causal adequacy — " cleared the floor on their own,
# and a leaf-status label, often longer than the finding after it, all but cleared the ratio
# too — so a tick on one finding could retire a different finding nobody ticked.
#
# The floor bounds both tests. A text under it — a few words — sits inside countless findings,
# so a short ticked row that happens to occur in a deleted entry says nothing about that entry,
# and retiring it would read consent from absence. Below the floor only an exact match counts
# (exact past a label both rows share, so a retyped separator is still exact).
#
# No threshold tells a one-word edit from a different finding: "the fix does not cover the
# retry path" and "…the CLI path" share most of their text either way. So the matching is only
# ever asked about a row the human EDITED. A row equal to one assembly rendered is that row —
# never an edit of another (`retire_cleared`).
_MATCH_RATIO = 0.6      # of the shorter finding text
_MATCH_FLOOR = 20       # …and never fewer than this many characters of it

_BOXES = ("- [ ]", "- [x]", "- [X]")


def _norm(text: str) -> str:
    """A §6 text reduced for comparison: whitespace collapsed, case folded."""
    return " ".join(str(text).split()).casefold()


def _row_text(line: str) -> str:
    """A §6 checkbox line reduced to its normalised finding text, checkbox stripped."""
    body = line.strip()
    for box in _BOXES:
        if body.startswith(box):
            body = body[len(box):]
            break
    return _norm(body)


# The labels as a normalised row spells them — longest first, so a label that happens to be a
# prefix of another can never shadow it — and what may follow one: a separator (the " — "
# assemble renders, or a ":" or "-" a human typed) or the end of the row.
_LABELS = tuple(sorted({_norm(label) for label in FINDING_LABELS}, key=len, reverse=True))
_SEPARATORS = " —–:-"


def _split_label(text: str) -> tuple[str, str]:
    """``(label, rest)`` for a normalised §6 text that opens with a label, the separator
    stripped from ``rest``; ``("", text)`` when it opens with none."""
    for label in _LABELS:
        if not text.startswith(label):
            continue
        rest = text[len(label):]
        if not rest or rest[0] in _SEPARATORS:
            return label, rest.lstrip(_SEPARATORS)
    return "", text


def _past_shared_labels(a: str, b: str) -> tuple[str, str]:
    """``a`` and ``b`` past every label they BOTH open with. A loop, because labels nest: a
    placeholder row puts its leaf-status label in front of whatever the artifact said, and that
    can open with a 5/5/1 label of its own."""
    while True:
        (label_a, rest_a), (label_b, rest_b) = _split_label(a), _split_label(b)
        if not label_a or label_a != label_b:
            return a, b
        a, b = rest_a, rest_b


def _same_finding(a: str, b: str) -> bool:
    """Do these two §6 texts name the same finding, allowing for a human's edit?

    Equal after normalisation, or equal past any label both open with
    (:func:`_past_shared_labels`) — a retyped separator, ``C5 Causal adequacy: …`` for
    ``C5 Causal adequacy — …``, is not an edit of the finding, however short it is. Otherwise
    compared past those labels, and only when the shorter of what remains is at least
    :data:`_MATCH_FLOOR` characters: one containing the other (an annotation appended, a tail
    trimmed, a note prefixed), or a shared opening that is most of the shorter text (an edit
    in the middle). The ONE relation a tick is matched with and an open row protects with
    (:func:`retire_cleared`) — two relations there is what #335 was.
    """
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    a, b = _past_shared_labels(a, b)
    if a == b:
        return True     # only the separator after a shared label differs
    shorter = min(len(a), len(b))
    if shorter < _MATCH_FLOOR:
        return False    # too short to name one finding: containment is not evidence
    if a in b or b in a:
        return True
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    return shared >= max(_MATCH_FLOOR, int(shorter * _MATCH_RATIO))


def eligible(items: list[NeedsHumanItem]) -> bool:
    """True iff a rebuild is the right next step: at least one IMPL finding, and no
    size-backstop item.

    An **empty** §6 is deliberately not eligible — that is a clean bundle awaiting a human
    *accept*, and auto-iterate must never accept. Neither is a §6 of HUMAN items with no IMPL
    item beside them: there is nothing for a rebuild to address, so it goes straight to the
    human.

    An ordinary HUMAN item beside an IMPL one no longer vetoes the rebuild (#409). It is
    DEFERRED — :func:`write_decision` records it in :data:`DEFERRED_FILE` and assembly
    returns it to §6 at handover — because a finding that needs a human is not evidence that
    the implementation work is done. The veto made the loop fire on 13.5% of attempts.

    The one HUMAN item that still stops the loop is the size backstop's (#324), and it stops
    it by KIND: ``size_signal.is_size_item`` on a HUMAN item. Its whole message is "further
    rebuilds are the wrong move", so deferring it would turn the backstop into an
    accelerator for the spiral it exists to break. The same text tagged IMPL is not the
    backstop's item and still rebuilds — the tag stays part of the mechanism.

    The STANDING `Validation` row never counted either way (#293): the reviewer's prompt
    emits it on every cycle whatever it found, so it is a constant, not evidence.
    """
    return (any(item.kind == IMPL for item in items)
            and not any(item.kind == HUMAN and size_signal.is_size_item(item.text)
                        for item in items))


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


class DeferredLedgerUnreadable(Exception):
    """The deferred ledger exists but cannot be read.

    Kept apart from an ABSENT ledger on purpose. Absent means "nothing has been deferred" —
    the ordinary state, read as an empty list. A file that exists and will not parse is a
    ledger whose contents are LOST, and reading that as empty is the one failure this
    mechanism exists to prevent: the next :func:`defer` would rewrite the file from the
    current §6 alone, and the next ``iterate-do`` would archive the current SUMMARY, so every
    finding deferred in an earlier round would be gone from every live artifact at once.

    Every other bundle-file reader in the driver is tolerant of a garbled file. This one is
    not, because the tolerant reading fails in the ACCEPTING direction.
    """


def deferred(d: Path) -> list[str]:
    """Every HUMAN finding this bundle has iterated past, oldest first.

    An absent file is ``[]``. A file that exists but cannot be read — not JSON, not an
    ``items`` list, a non-string entry — raises :class:`DeferredLedgerUnreadable`. A
    non-string entry fails closed too: filtering it out would report the ledger readable
    while dropping an entry nobody can interpret, and the next write would erase it.
    """
    p = d / DEFERRED_FILE
    if not p.exists() and not p.is_symlink():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:   # UnicodeDecodeError is a ValueError
        raise DeferredLedgerUnreadable(f"{p} exists but cannot be read: {exc}") from exc
    rows = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise DeferredLedgerUnreadable(f"{p} holds no `items` list")
    if not all(isinstance(r, str) for r in rows):
        raise DeferredLedgerUnreadable(
            f"{p} holds a non-string entry — cannot tell what was deferred")
    return list(rows)


def ledger_problem(d: Path) -> str:
    """Why the ledger cannot be read, or ``""`` when it can (or is absent)."""
    try:
        deferred(d)
    except DeferredLedgerUnreadable as exc:
        return str(exc)
    return ""


def _write_ledger(d: Path, entries: list[str]) -> None:
    """Replace the ledger in one step (temp sibling + ``os.replace``), so an interrupted
    write leaves the previous ledger, not a torn one that reads as unreadable."""
    p = d / DEFERRED_FILE
    tmp = d / f".{DEFERRED_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"items": entries}, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)


def deferrable(items: list[NeedsHumanItem]) -> list[str]:
    """The findings in this round's §6 items that a round holds for the handover, each once.

    HUMAN findings only, deduplicated on the normalised text, first spelling kept. IMPL items
    are the round's work, not a deferral, and STANDING is emitted every cycle whatever the
    reviewer found — neither is held.

    Nor is a ``no_verdict`` row — a review or a gate that gave no verdict this round (the
    review missing or a placeholder, an advisory leaf's placeholder, a gate that could not
    run). It reports on this Check's own run, not on the patch, and every Check runs the
    review and the gates again, so the next Check's §6 carries the row exactly while the
    problem lasts and drops it once it has recovered. Held instead, a failure that had since
    recovered would still come back at handover for the human to clear by hand — the human
    ruled it must not (#409 sign-off).

    The one definition :func:`defer` records, and :func:`rationale` and the flow's notice
    count, so neither can report a finding held that the ledger does not hold.
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = _norm(item.text)
        if item.kind != HUMAN or item.no_verdict or not key or key in seen:
            continue
        seen.add(key)
        out.append(item.text)
    return out


def defer(d: Path, items: list[NeedsHumanItem]) -> list[str]:
    """Record the findings this round is iterating past (:func:`deferrable`); return the full
    ledger.

    Deduplicated against the ledger on the normalised text, oldest first: a reviewer that
    raises the same objection every round must not grow the handover §6 by one copy per
    round. Raises :class:`DeferredLedgerUnreadable` rather than rewrite a ledger it could not
    read. Writes nothing when there is nothing new, so a bundle that never deferred anything
    never grows the file.
    """
    ledger = deferred(d)
    seen = {_norm(t) for t in ledger}
    new = [t for t in deferrable(items) if _norm(t) not in seen]
    if new:
        ledger += new
        _write_ledger(d, ledger)
    return ledger


def retire_cleared(d: Path, summary_path: Path, *, fresh: Iterable[str]) -> list[str]:
    """Drop the ledger entries the human positively TICKED in §6; return what remains.

    Called at an iterate transition, before ``driver._archive_iteration`` moves the ticked
    SUMMARY away. Without it an entry the human adjudicated re-enters §6 unticked at the next
    assembly and blocks accept again — every round, with no way to clear it.

    Only a tick retires, read POSITIVELY from ``- [x]`` rows: absence is not consent (an
    edited row is neither open under its old text nor ticked). Ticks and open rows are both
    read from the §6 ``assemble`` wrote — the LAST §6 heading, never a §6 block a leaf quoted
    into §5 (``signoff._needs_human_section``) — and the two readers take OPPOSITE
    fail-safe directions, per ``signoff._section``'s contract:

    * ticks come from :func:`signoff.cleared_needs_human` — strict, no §6 heading means no
      ticks, so a ``- [x]`` quoted in §5's review text can never retire an entry;
    * open rows come from :func:`signoff.open_needs_human` — lenient, no §6 heading scans
      the whole document, which finds more open rows and so protects more.

    A tick belongs to ONE row assembly rendered, or to none. The ledger's entries are some of
    those rows; ``fresh`` is the rest — this Check's own findings, as
    ``assemble.collect_needs_human`` produced them. A tick EQUAL to a rendered row is that
    row's clearance: a ledger entry's, which retires it, or a fresh finding's, which retires
    nothing. Only a tick equal to no rendered row is an edit, and only an edit is matched
    with :func:`_same_finding` — against every rendered row, fresh ones included. To a text
    matcher a one-word edit and a different finding look the same ("the fix does not cover
    the retry path" / "…the CLI path"), so a tick on a fresh finding, as rendered or edited,
    must never be read as an edit of a deferred one whose own row the human deleted. More
    than one hit fails closed, and so does a hit on a fresh row.

    A still-open row protects entries by the SAME relation, assigned exact-first over the same
    rendered rows, in two tiers (#335):

    * an open row verbatim-equal to a rendered row IS that row. It protects that entry when
      the row is a ledger entry, and nothing when it is a fresh finding — so no still-open
      near-twin, deferred or fresh, can shield its exactly-ticked neighbour, and a
      near-identical pair stays drainable one tick at a time;
    * any other open row — one the human edited — protects EVERY entry it matches: fail
      closed, a lingering finding is visible, a lost one is unrecoverable.

    Not the flat ``any(_same_finding(entry, o) for o in still_open)``: that protects an
    exactly-ticked entry behind its open near-twin and re-creates the unclearable pair.

    An unreadable ledger is left untouched (never rewritten from a partial read); §6 already
    carries :data:`UNREADABLE_LEDGER_ITEM` for it.
    """
    try:
        ledger = deferred(d)
    except DeferredLedgerUnreadable:
        return []
    if not ledger:
        return ledger
    norm = [_norm(t) for t in ledger]
    # Every row assembly rendered, the ledger's entries first — so an index below
    # len(ledger) is an entry, and only an entry is ever retired. A fresh finding the ledger
    # already holds rendered once, as that entry (`assemble._deferred_needs_human`).
    rendered, rnorm = list(ledger), list(norm)
    for text in fresh:
        key = _norm(text)
        if key and key not in rnorm:
            rendered.append(text)
            rnorm.append(key)
    still_open = [_row_text(line) for line in signoff.open_needs_human(summary_path)]
    ticked = [_row_text(line) for line in signoff.cleared_needs_human(summary_path)]

    protected: set[int] = set()
    for o in still_open:
        owned = [i for i, t in enumerate(rnorm) if t == o]
        protected.update([i for i in owned if i < len(ledger)] if owned else
                         [i for i, t in enumerate(ledger) if _same_finding(t, o)])

    retire: set[int] = set()
    for row in ticked:
        exact = [i for i, t in enumerate(rnorm) if t == row]
        hits = exact or [i for i, t in enumerate(rendered) if _same_finding(t, row)]
        if len(hits) == 1 and hits[0] < len(ledger) and hits[0] not in protected:
            retire.add(hits[0])
    if not retire:
        return ledger
    kept = [t for i, t in enumerate(ledger) if i not in retire]
    _write_ledger(d, kept)
    return kept


def rationale(items: list[NeedsHumanItem], *, attempt: int) -> str:
    """The §9 "Iteration delta" line, which the driver folds into the brief's carry-forward
    so the next Do iteration isn't blind about why it was rejected.

    It names what the round addresses — the IMPL findings, quoted — and what it deferred.
    The deferred HUMAN findings are COUNTED, never quoted: this line IS the builder's
    carry-forward, and handing the next Do a human-only judgment call as though it were a
    defect to fix is the failure PR #294's review caught. Their text is in
    :data:`DEFERRED_FILE` and returns to §6 at handover. The count is :func:`deferrable`'s,
    what the ledger holds: not the STANDING row, not a ``no_verdict`` row, and a finding
    raised twice counts once.
    """
    findings = "; ".join(item.text for item in items if item.kind == IMPL)
    line = (f"Auto-iterate (round {attempt}): rebuilding for the implementation-level "
            f"findings — {findings}")
    held = len(deferrable(items))
    if held:
        line += (f". Deferred, not addressed here: {held} finding(s) that need human "
                 f"judgment, held in {DEFERRED_FILE} for the human at handover — not build "
                 "work")
    return line


def write_decision(d: Path, items: list[NeedsHumanItem]) -> None:
    """Record the deferrals, write the ``iterate-do`` decision + rationale, and spend one
    round of the budget.

    Guarded: refuses to write anything for an ineligible item set, so no caller can turn
    this into an auto-accept. The ledger is written FIRST: if it cannot be read,
    :class:`DeferredLedgerUnreadable` propagates before any budget is spent or any decision
    exists, so no round can archive a SUMMARY whose HUMAN findings were not recorded.
    """
    if not eligible(items):
        raise ValueError("auto-iterate: refusing to decide on a finding set with no "
                         "implementation work, or one the size backstop has stopped")
    defer(d, items)
    attempt = bump(d)
    (d / SIGNOFF_DECISION).write_text(
        f"{DECISION}\n{rationale(items, attempt=attempt)}\n", encoding="utf-8")
