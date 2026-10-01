"""Reading and writing the human sign-off in ``SUMMARY.md`` §9 (docs 02 §9).

``SUMMARY.md`` is the source of truth for the per-contribution verdict — there is
no separate sign-off database. This module parses §9 (the outcome) and §6
(NEEDS-HUMAN), and records the human's decision back into the file. The driver
reads the result via :mod:`pdca_harness.state`.
"""

from __future__ import annotations

import re
from pathlib import Path

# Canonical §9 outcome tokens written into SUMMARY.md. The token → bundle-state
# mapping lives in :mod:`pdca_harness.state` (which owns the state names); this
# module knows only the tokens, so there is no import cycle between the two.
VALID_OUTCOMES = frozenset(
    {"merged-wider", "accepted", "iterated-to-Do", "iterated-to-Plan", "discontinued"})

# What `signoff --accept/--iterate-do/--iterate-plan/--discontinue` writes into the Outcome line.
ACTION_TO_OUTCOME = {
    "accept": "merged-wider",
    "iterate-do": "iterated-to-Do",
    "iterate-plan": "iterated-to-Plan",
    "discontinue": "discontinued",
}

# Both anchored with [ \t] (NOT \s) so an empty field stops at the line end instead of
# running past the newline into the next line. `\s` matches `\n`, so `- Outcome:` with no
# value captured the FOLLOWING line — `outcome_token` returned "- By / date:" for an
# unsigned bundle, and a bare valid token on that line would have signed it off (#328).
_OUTCOME_RE = re.compile(r"^- Outcome:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_DELTA_RE = re.compile(r"^- Iteration delta \(if iterating\):[ \t]*(.*?)[ \t]*$", re.MULTILINE)

#: The §9 heading. Spelled once: every use is load-bearing (an outcome read outside this
#: section is not a sign-off, #327), so a typo in one copy would reopen the fail-open.
SIGNOFF_HEADING = "9. Check sign-off"

#: The §6 heading. Its ABSENCE is load-bearing too — see :func:`unrecordable`. Every §6
#: reader takes the LAST such heading (:func:`_needs_human_section` says why).
NEEDS_HUMAN_HEADING = "6. NEEDS-HUMAN"


def heading_is(heading_text: str, canonical: str) -> bool:
    """True iff a ``## `` heading's text names ``canonical`` — the section, not a lookalike.

    Prefix **plus a boundary**, which is neither of the two things tried before it:

    * containment matched ``## 19. Check sign-off`` and ``## Notes about 9. Check sign-off``;
    * a bare prefix still matched ``## 9. Check sign-off-not-authoritative``.

    Each let a leaf-written summary put ``- Outcome: accepted`` under a heading that is not
    §9 and reach COMPLETE, which releases publish (#330 review). Equality is not an option
    either: the shipped template writes ``## 9. Check sign-off   ← human completes Check
    here``. So the canonical text must be followed by whitespace or nothing at all.

    Shared with :func:`act._find`, which matched the same way and so could read a bundle's
    outcome out of a lookalike section. One implementation, because every time this rule has
    been fixed in one place and not the other it has come straight back.
    """
    if not heading_text.startswith(canonical):
        return False
    tail = heading_text[len(canonical):]
    return tail == "" or tail[0].isspace()


def outcome_token(summary_path: Path) -> str:
    """The §9 Outcome value, or "" if unset or the summary is absent. Scoped to §9.

    An absent ``SUMMARY.md`` (a leaf deleted it, or it never assembled) is "no
    outcome", not a crash — :func:`state.state` and the batch sweep treat every
    bundle file as possibly-absent (testbed issue #3). A SUMMARY with no §9 section is the
    same answer for the same reason: malformed is "not signed off", never "signed off".
    """
    if not summary_path.exists():
        return ""
    text = summary_path.read_text(encoding="utf-8")
    # Restrict to §9 so a stray "Outcome:" elsewhere can't match — strictly, because falling
    # back to the whole document is what let any such line grant a sign-off (#327).
    section = _section(text, SIGNOFF_HEADING, whole_on_missing=False)
    m = _OUTCOME_RE.search(section)
    return (m.group(1).strip() if m else "")


def is_set(summary_path: Path) -> bool:
    """True once §9 Outcome holds a recognized token (placeholders don't count)."""
    return outcome_token(summary_path) in VALID_OUTCOMES


def iteration_delta(summary_path: Path) -> str:
    """The §9 'Iteration delta (if iterating)' value, or "" if unset/absent.

    The human's rationale for an iterate ("why rejected / what to change"), which the
    driver folds into the brief's carry-forward so the next iteration isn't blind."""
    if not summary_path.exists():
        return ""
    section = _section(summary_path.read_text(encoding="utf-8"), SIGNOFF_HEADING,
                       whole_on_missing=False)
    m = _DELTA_RE.search(section)
    return (m.group(1).strip() if m else "")


def _needs_human_section(text: str, *, whole_on_missing: bool) -> str:
    """§6 as ``assemble`` wrote it: the section under the LAST ``## 6. NEEDS-HUMAN`` heading.

    Every §6 reader goes through here — the C6 accept-guard (:func:`open_needs_human`), the
    tick reader (:func:`cleared_needs_human`), the row writer (:func:`ensure_needs_human_item`)
    — so they cannot disagree about which §6 is the real one. The first heading can belong to
    a leaf: an artifact can quote a whole ``## 6. NEEDS-HUMAN`` block, and ``assemble`` pastes
    review and advisory text into §5 verbatim, ABOVE the §6 it writes itself. Read there, a
    quoted block of ticked rows let C6 pass an accept while the real §6 still had open rows,
    and a row added for the human landed inside the quote. Nothing after the assembled §6 is
    a leaf's multi-line text — §6's own rows are one line each, and §9 holds only the
    sign-off record, whose iteration delta the flow flattens to one line — so the last §6
    heading is the one ``assemble`` wrote, and the one the human ticks.

    ``whole_on_missing`` is the caller's fail-safe direction, as for :func:`_section`.
    """
    return _section(text, NEEDS_HUMAN_HEADING, whole_on_missing=whole_on_missing, last=True)


def open_needs_human(summary_path: Path) -> list[str]:
    """Unchecked ``- [ ]`` items under §6 NEEDS-HUMAN (must be empty before accept).

    An absent ``SUMMARY.md`` is "no open items", not a crash — every bundle file
    is possibly-absent (testbed issue #3), same contract as :func:`outcome_token`.

    Deliberately the LENIENT side of :func:`_section`, unlike §9: with no §6 heading this
    scans the whole document, which can only find more ``- [ ]`` items and so blocks accept
    harder. Tightening it in sympathy with the §9 fix (#327) would turn a fail-safe into a
    fail-open — a malformed summary would report zero open items.

    The §6 it reads is the one ``assemble`` wrote, never a block a leaf quoted into §5
    (:func:`_needs_human_section`)."""
    if not summary_path.exists():
        return []
    section = _needs_human_section(summary_path.read_text(encoding="utf-8"),
                                   whole_on_missing=True)
    return [
        line.strip()
        for line in section.splitlines()
        if line.lstrip().startswith("- [ ]")
    ]


def cleared_needs_human(summary_path: Path) -> list[str]:
    """Ticked ``- [x]`` items under §6 NEEDS-HUMAN — what the human positively cleared.

    The STRICT side of :func:`_section`, the opposite of :func:`open_needs_human`, because
    the failure directions are opposite. A tick RETIRES a deferred finding
    (``autoiterate.retire_cleared``), so leniency here fails open: with no §6 heading a
    whole-document scan would read a ``- [x]`` quoted in §5's review text — which
    ``assemble`` pastes in verbatim — as the human's clearance. With no §6 there are no
    ticks. Absent ``SUMMARY.md`` is "no ticks" for the same reason. A §6 block a leaf quoted
    is not read either (:func:`_needs_human_section`): read there, a quoted tick emptied the
    ledger on an auto-iterate round nobody watched.
    """
    if not summary_path.exists():
        return []
    section = _needs_human_section(summary_path.read_text(encoding="utf-8"),
                                   whole_on_missing=False)
    return [
        line.strip()
        for line in section.splitlines()
        if line.lstrip().startswith(("- [x]", "- [X]"))
    ]


def ensure_needs_human_item(summary_path: Path, item: str) -> bool:
    """Make sure §6 carries ``item`` as a checkbox row; add it UNTICKED if it is missing.

    Returns True iff a row was added. A row already there — open, or ticked by the human —
    is left alone: a tick is the human's clearance, and C6 honours it for every other row.
    The ``- (none — …)`` placeholder an empty §6 renders is dropped, since it would now be
    false. A summary with no §6 section is not touched (returns False); callers settle
    :func:`unrecordable` first, which refuses exactly that shape.

    It reads and writes the §6 C6 reads (:func:`_needs_human_section`): a row written
    anywhere else would be one C6 cannot see, and a copy ``assemble`` already rendered there
    would be missed and the row added twice. The new row is spliced in by line position,
    not by finding the section's text — a leaf can quote a §6 block that is byte-for-byte
    the real one, and the first match of that text is the quote.
    """
    if not summary_path.exists():
        return False
    lines = summary_path.read_text(encoding="utf-8").splitlines(keepends=True)
    span = _section_span(lines, NEEDS_HUMAN_HEADING, last=True)
    if span is None:
        return False
    start, end = span
    want = " ".join(item.split()).casefold()
    rows = lines[start + 1:end]
    for line in rows:
        body = line.strip()
        for box in ("- [ ]", "- [x]", "- [X]"):
            if body.startswith(box) and " ".join(body[len(box):].split()).casefold() == want:
                return False
    body = [ln for ln in rows if not ln.strip().startswith("- (none")]
    cut = len(body)
    while cut and not body[cut - 1].strip():
        cut -= 1
    head = [lines[start]] + body[:cut]
    if not head[-1].endswith("\n"):
        head[-1] += "\n"
    lines[start:end] = head + [f"- [ ] {item}\n"] + body[cut:]
    summary_path.write_text("".join(lines), encoding="utf-8")
    return True


def unrecordable(summary_path: Path) -> str:
    """Why a sign-off cannot be written into this summary, or ``""`` when it can.

    The single place that answers "is this artifact signable?". :func:`record` raises on it,
    and ``flow`` consults it BEFORE the C6 accept-guard: ``open_needs_human`` is deliberately
    lenient, so on a summary with no §6 heading it scans the whole document and can return
    "blocked" for an accept — stopping before the repair path and stranding the bundle
    exactly as an unrepaired malformed summary does (#330 review). Whether the artifact can
    be written to is a property of the artifact, not of the decision, so it is settled first.

    A §9 that exists but carries no ``- Outcome:`` line counts as unrecordable: ``set_field``
    would substitute nothing and return success, so ``pdca signoff --accept`` exited 0 while
    leaving the bundle at AWAITING_SIGNOFF — a silent no-op reported as a sign-off.

    **A missing §6 counts too, and that is the subtle one.** :func:`open_needs_human` falls
    back to scanning the whole document, which is safe only while the checkboxes survive
    SOMEWHERE — deleting the heading finds more items, deleting the *section* deletes its
    items with it. Then C6 sees an empty list and reads "the human cleared everything" from
    an artifact that merely lost the evidence, so an accept records and publish is released
    (#330 review). Section deletion is explicitly in the leaf-damage threat model
    (``flow._isolate``), so zero surviving checkboxes cannot be treated as proof C6 is clear.
    Reassembly rebuilds §6 from the review artifacts, which is where the real items live.
    """
    if not summary_path.exists():
        return "no SUMMARY.md"
    text = summary_path.read_text(encoding="utf-8")
    section = _section(text, SIGNOFF_HEADING, whole_on_missing=False)
    if not section:
        return f"no '## {SIGNOFF_HEADING}' section"
    if not _OUTCOME_RE.search(section):
        return f"'## {SIGNOFF_HEADING}' has no '- Outcome:' field to record into"
    if not _needs_human_section(text, whole_on_missing=False):
        return (f"no '## {NEEDS_HUMAN_HEADING}' section — C6 cannot be evaluated, and an "
                "empty scan is not evidence the human cleared it")
    return ""


def record(summary_path: Path, *, action: str, by: str, date: str, delta: str = "") -> None:
    """Write the human's §9 decision into ``SUMMARY.md`` in place.

    ``action`` is one of ``accept`` / ``iterate-do`` / ``iterate-plan`` / ``discontinue``.

    Raises ``ValueError`` rather than half-writing: see :func:`unrecordable` for what counts.
    The contract is that this function records or it raises — never "returns having changed
    nothing", which is what let a `--accept` exit 0 over a bundle it did not sign off. Callers
    all handle the raise (``cli._signoff`` reports and exits 1; ``flow._apply_decision``
    quarantines the summary so the bundle reassembles).
    """
    outcome = ACTION_TO_OUTCOME[action]
    problem = unrecordable(summary_path)
    if problem:
        raise ValueError(
            f"{summary_path}: {problem} — refusing to record a sign-off into a malformed "
            "SUMMARY.md (the decision would be unreadable, so the bundle would never "
            "advance). Re-run Check to reassemble it.")
    text = summary_path.read_text(encoding="utf-8")

    def set_field(body: str, label: str, value: str) -> tuple[str, int]:
        """``(body, substitutions)`` — the count matters for ``Outcome``, see below.

        ``value`` is unsanitised human text (a sign-off rationale or a ``--by``), so
        it MUST NOT be passed to ``re.subn`` as the ``repl`` string — that argument
        is a replacement TEMPLATE and has its own backslash-escape syntax (``\\g<1>``,
        ``\\1``, ...), which `re.sub`'s documentation contrasts with a callable `repl`:
        a callable's return value is used as-is, with no escape processing. A string
        `repl` raised `re.error` on a value containing e.g. `\\W` and silently expanded
        a value that happened to spell a valid group reference (#529). A callable
        closes over `value` and returns it untouched, so every byte the human wrote is
        recorded literally, whatever it contains.
        """
        pat = re.compile(rf"^(- {re.escape(label)}:).*?$", re.MULTILINE)
        def repl(m: re.Match[str]) -> str:
            return f"{m.group(1)} {value}" if value else m.group(1)
        new, n = pat.subn(repl, body, count=1)
        return (new, n) if n else (body, 0)

    section = _section(text, SIGNOFF_HEADING, whole_on_missing=False)
    updated, wrote_outcome = set_field(section, "Outcome", outcome)
    # Asserted on the MATCH COUNT, not on the text changing: re-recording the same outcome
    # (the batch sweep defers an iterate-do, then the single-issue path applies it) is a
    # legitimate no-op whose text is identical, and treating that as a failure broke a real
    # flow. What must never pass silently is the field not being there at all.
    if not wrote_outcome:
        raise ValueError(
            f"{summary_path}: '- Outcome:' was not substituted in '## {SIGNOFF_HEADING}' — "
            "refusing to report a sign-off that did not take. Re-run Check to reassemble it.")
    # These counts are checked too, not discarded. A §9 missing `- By / date:` loses the
    # sign-off attribution; a §9 missing `- Iteration delta (if iterating):` loses the
    # human's stated reason for the iterate, which `driver._carry_forward_into_brief` folds
    # into the brief — so the next Do would rebuild knowing only that it was rejected, not
    # why, and the human's requested change would be silently dropped (#330 review). Both
    # raise BEFORE the write below, so a refusal never leaves a half-recorded §9.
    updated, wrote_by = set_field(updated, "By / date", f"{by} / {date}")
    if not wrote_by:
        raise ValueError(
            f"{summary_path}: '## {SIGNOFF_HEADING}' has no '- By / date:' field — refusing "
            "to record a sign-off with no attribution. Re-run Check to reassemble it.")
    if delta:
        updated, wrote_delta = set_field(updated, "Iteration delta (if iterating)", delta)
        if not wrote_delta:
            raise ValueError(
                f"{summary_path}: '## {SIGNOFF_HEADING}' has no "
                "'- Iteration delta (if iterating):' field — refusing to record an iterate "
                "whose reason would be dropped before the next Do reads it. Re-run Check to "
                "reassemble it.")
    summary_path.write_text(text.replace(section, updated, 1), encoding="utf-8")


def _section(text: str, heading_substr: str, *, whole_on_missing: bool,
             last: bool = False) -> str:
    """Return the body of the ``## ...`` section whose heading starts with the substr.

    ``whole_on_missing`` says what an ABSENT heading means. It has no default because the
    two callers need opposite answers — their failure directions are opposite:

    * ``True`` — fall back to the whole text. Correct for §6 NEEDS-HUMAN: scanning
      everything finds *more* ``- [ ]`` items, so a malformed summary blocks accept harder.
      Fails safe.
    * ``False`` — return ``""``. Required for §9, which is the AUTHORITY section. Falling
      back there let **any** ``- Outcome:`` line in the file grant a sign-off, so a summary
      whose §9 heading was lost or demoted to ``###`` read as COMPLETE — with §6 items still
      unticked — and COMPLETE releases publish (#327). The C6 accept-guard only covers the
      *write* path (:func:`record`); :mod:`state` trusts this read outright, so it is the
      one place leniency cannot be afforded.

    A leaf with Write/Bash can leave any bundle file malformed (``flow._isolate``), so this
    is a live input, not a theoretical one.

    Which headings count is :func:`heading_is` — a prefix plus a boundary, so a lookalike
    cannot pose as the section.

    When the heading occurs more than once, the FIRST wins — unless ``last``, which takes the
    last one. The §6 readers pass it (:func:`_needs_human_section` says why); every other
    caller keeps the first.
    """
    lines = text.splitlines(keepends=True)
    span = _section_span(lines, heading_substr, last=last)
    if span is None:
        return text if whole_on_missing else ""
    return "".join(lines[span[0]:span[1]])


def _section_span(lines: list[str], heading_substr: str, *,
                  last: bool) -> tuple[int, int] | None:
    """``(start, end)`` line indices of the section :func:`_section` returns, or ``None`` when
    no heading names it — for a caller that must write back into that section, at that
    position."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading_is(line[3:].lstrip(), heading_substr):
            start = i
            if not last:
                break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return start, end
