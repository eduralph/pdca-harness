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


def record(summary_path: Path, *, action: str, by: str, date: str, delta: str = "") -> None:
    """Write the human's §9 decision into ``SUMMARY.md`` in place.

    ``action`` is one of ``accept`` / ``iterate-do`` / ``iterate-plan`` / ``discontinue``.

    Raises ``ValueError`` when the summary has no §9 section. Two reasons, and the second is
    the important one: the final ``text.replace(section, ...)`` would insert at position 0 if
    handed an empty section, corrupting the file; and a decision written where
    :func:`outcome_token` (now strict, #327) cannot see it would silently never take effect,
    so the human's accept would appear to do nothing. ``flow._isolate`` contains the raise
    per-bundle, which leaves that bundle unpublished — the safe direction.
    """
    outcome = ACTION_TO_OUTCOME[action]
    text = summary_path.read_text(encoding="utf-8")

    def set_field(body: str, label: str, value: str) -> str:
        pat = re.compile(rf"^(- {re.escape(label)}:).*?$", re.MULTILINE)
        repl = rf"\g<1> {value}" if value else r"\g<1>"
        new, n = pat.subn(repl, body, count=1)
        return new if n else body

    section = _section(text, SIGNOFF_HEADING, whole_on_missing=False)
    if not section:
        raise ValueError(
            f"{summary_path}: no '## {SIGNOFF_HEADING}' section — refusing to record a "
            "sign-off into a malformed SUMMARY.md (the decision would be unreadable, so the "
            "bundle would never advance). Re-run Check to reassemble it.")
    updated = set_field(section, "Outcome", outcome)
    updated = set_field(updated, "By / date", f"{by} / {date}")
    if delta:
        updated = set_field(updated, "Iteration delta (if iterating)", delta)
    summary_path.write_text(text.replace(section, updated, 1), encoding="utf-8")


def _section(text: str, heading_substr: str, *, whole_on_missing: bool) -> str:
    """Return the body of the ``## ...`` section whose heading contains the substr.

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
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading_substr in line:
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
