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

#: The §6 heading. Its ABSENCE is load-bearing too — see :func:`unrecordable`.
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


def open_needs_human(summary_path: Path) -> list[str]:
    """Unchecked ``- [ ]`` items under §6 NEEDS-HUMAN (must be empty before accept).

    An absent ``SUMMARY.md`` is "no open items", not a crash — every bundle file
    is possibly-absent (testbed issue #3), same contract as :func:`outcome_token`.

    Deliberately the LENIENT side of :func:`_section`, unlike §9: with no §6 heading this
    scans the whole document, which can only find more ``- [ ]`` items and so blocks accept
    harder. Tightening it in sympathy with the §9 fix (#327) would turn a fail-safe into a
    fail-open — a malformed summary would report zero open items."""
    if not summary_path.exists():
        return []
    section = _section(summary_path.read_text(encoding="utf-8"), "6. NEEDS-HUMAN",
                       whole_on_missing=True)
    return [
        line.strip()
        for line in section.splitlines()
        if line.lstrip().startswith("- [ ]")
    ]


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
    if not _section(text, NEEDS_HUMAN_HEADING, whole_on_missing=False):
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
        """``(body, substitutions)`` — the count matters for ``Outcome``, see below."""
        pat = re.compile(rf"^(- {re.escape(label)}:).*?$", re.MULTILINE)
        repl = rf"\g<1> {value}" if value else r"\g<1>"
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


def _section(text: str, heading_substr: str, *, whole_on_missing: bool) -> str:
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
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading_is(line[3:].lstrip(), heading_substr):
            start = i
            break
    if start is None:
        return text if whole_on_missing else ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "".join(lines[start:end])
