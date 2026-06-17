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

_OUTCOME_RE = re.compile(r"^- Outcome:\s*(.*?)\s*$", re.MULTILINE)
# Anchored with [ \t] (NOT \s) so an empty field stops at the line end instead of
# running past the newline into the next line.
_DELTA_RE = re.compile(r"^- Iteration delta \(if iterating\):[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def outcome_token(summary_path: Path) -> str:
    """The §9 Outcome value, or "" if unset or the summary is absent. Scoped to §9.

    An absent ``SUMMARY.md`` (a leaf deleted it, or it never assembled) is "no
    outcome", not a crash — :func:`state.state` and the batch sweep treat every
    bundle file as possibly-absent (testbed issue #3).
    """
    if not summary_path.exists():
        return ""
    text = summary_path.read_text(encoding="utf-8")
    # Restrict to the §9 section so a stray "Outcome:" elsewhere can't match.
    section = _section(text, "9. Check sign-off")
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
    section = _section(summary_path.read_text(encoding="utf-8"), "9. Check sign-off")
    m = _DELTA_RE.search(section)
    return (m.group(1).strip() if m else "")


def open_needs_human(summary_path: Path) -> list[str]:
    """Unchecked ``- [ ]`` items under §6 NEEDS-HUMAN (must be empty before accept)."""
    section = _section(summary_path.read_text(encoding="utf-8"), "6. NEEDS-HUMAN")
    return [
        line.strip()
        for line in section.splitlines()
        if line.lstrip().startswith("- [ ]")
    ]


def record(summary_path: Path, *, action: str, by: str, date: str, delta: str = "") -> None:
    """Write the human's §9 decision into ``SUMMARY.md`` in place.

    ``action`` is one of ``accept`` / ``iterate-do`` / ``iterate-plan`` / ``discontinue``.
    """
    outcome = ACTION_TO_OUTCOME[action]
    text = summary_path.read_text(encoding="utf-8")

    def set_field(body: str, label: str, value: str) -> str:
        pat = re.compile(rf"^(- {re.escape(label)}:).*?$", re.MULTILINE)
        repl = rf"\g<1> {value}" if value else r"\g<1>"
        new, n = pat.subn(repl, body, count=1)
        return new if n else body

    section = _section(text, "9. Check sign-off")
    updated = set_field(section, "Outcome", outcome)
    updated = set_field(updated, "By / date", f"{by} / {date}")
    if delta:
        updated = set_field(updated, "Iteration delta (if iterating)", delta)
    summary_path.write_text(text.replace(section, updated, 1), encoding="utf-8")


def _section(text: str, heading_substr: str) -> str:
    """Return the body of the ``## ...`` section whose heading contains the substr.

    Used to scope a field search to one section. Returns the whole text if no
    matching heading is found (lenient — the caller's regex still has to match).
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading_substr in line:
            start = i
            break
    if start is None:
        return text
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "".join(lines[start:end])
