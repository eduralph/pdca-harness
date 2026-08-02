"""Do-exit halt on a builder-declared unmet external dependency (issue #341).

The builder contract (agents/builder.md) makes an honest builder declare a dependency it
could not build or verify against with a marker line in ``build-notes.md`` —

    NEEDS-HUMAN external dependency: <dependency> — <what it blocks>

— plus a fenced ``[[doctor.checks]]`` TOML block proposing the detect row Plan should
have registered. Before #341 the declaration changed nothing: BUILT unconditionally
bought the full Check beat (gates, cross-vendor reviewer, adversary) to adjudicate a
patch already *stated* to be unverifiable — so the honest and dishonest builder paths
proceeded identically, with the dishonest bundle looking better at Check.

This module is the deterministic adjudicator between them. The self-report NEVER decides
on its own (the inverse failure #332 documents — a statement driving control flow with
nothing checking it): the named dependency must resolve to a ``[[doctor.checks]]`` row —
registered in ``pdca.toml``, else parsed from the builder's proposed fenced block — and
that row's detect ``cmd`` must actually exit non-zero, probed exactly the way the
Plan-exit guard probes (#340, :func:`doctor.probe`). Three verdicts:

* **confirmed** — a row resolved AND its detect cmd exited non-zero. The driver routes
  the bundle through the existing close fast path (N/A matrix via
  ``gates.run_close_gates``; no reviewer, no adversary) to AWAITING_SIGNOFF — never to a
  terminal state: sign-off alone owns COMPLETE/DISCONTINUED, and the bundle stays
  RESUMABLE (install the dependency, answer iterate-do).
* **refuted** — the detect cmd exited 0: the dependency is present and the claim does
  not excuse Check. Full Check runs unchanged; the refutation is recorded here and
  lifted into SUMMARY §6 (``assemble.collect_needs_human``), where the human — and
  ``pdca act index``, which reads §6 — can see it.
* **unconfirmed** — no registered row and no parseable proposed row names the
  dependency (a malformed TOML block lands here too). Fail toward review, never toward
  skipping it: full Check runs.

Config-gated opt-in (``[driver].dependency_halt``, a STRICT boolean, default false):
while off nothing here runs — not even a detect cmd is spawned — so the beat is
byte-identical to today.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import NamedTuple

from . import doctor, state

CONFIRMED = "confirmed"
REFUTED = "refuted"
UNCONFIRMED = "unconfirmed"


class Verdict(NamedTuple):
    """The deterministic adjudication of ONE declared dependency."""

    dependency: str
    verdict: str            # CONFIRMED | REFUTED | UNCONFIRMED
    source: str             # "registered" | "proposed" | "" (no row resolved)
    cmd: str                # the detect cmd that ran ("" when none resolved)
    exit_code: int | None   # None when no cmd ran
    hint: str               # the row's install hint, for the sign-off human
    detail: str             # one line of why, for the record / review note


# The marker's remainder after ``assemble._declared_external_deps`` strips `NEEDS-HUMAN`:
# "external dependency: <dependency> — <what it blocks>". The <dependency> part is what
# must match a [[doctor.checks]] row id (case-insensitively, like `doctor.registered_ids`).
_AFTER_MARKER_RE = re.compile(r"external\s+dependency\s*[:—–-]*\s*(.*)$", re.IGNORECASE)
# The contract separates the name from "what it blocks" with a dash surrounded by spaces.
_BLOCKS_SPLIT_RE = re.compile(r"\s+(?:—|–|--|-)\s+")
# The fenced block the builder contract mandates for the proposed row.
_TOML_FENCE_RE = re.compile(r"```\s*toml[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def declared_dependencies(text: str) -> list[str]:
    """The dependency names the builder declared, in order, deduped case-insensitively.

    Delegates the marker match to ``assemble._declared_external_deps`` (#250) — ONE
    parser for "did the builder declare a dependency", so the §6 item and this
    adjudication can never disagree on that question. Local import: ``assemble`` reads
    this module's record back into §6, so a top-level import would be a cycle.
    """
    from . import assemble  # local: assemble imports this module (record → §6)
    names: list[str] = []
    seen: set[str] = set()
    for item in assemble._declared_external_deps(text):
        m = _AFTER_MARKER_RE.search(item)
        rest = m.group(1) if m else item
        name = _BLOCKS_SPLIT_RE.split(rest, maxsplit=1)[0].strip().strip("`'\"").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def proposed_rows(text: str) -> tuple[list[dict], bool]:
    """``[[doctor.checks]]`` rows from the fenced ```toml blocks in ``text``, plus
    whether ANY block failed to parse.

    A malformed block contributes no rows, so the dependency it would have resolved
    stays UNCONFIRMED — a broken proposal fails toward review, never toward skipping it.
    """
    rows: list[dict] = []
    malformed = False
    for block in _TOML_FENCE_RE.findall(text):
        try:
            data = tomllib.loads(block)
        except tomllib.TOMLDecodeError:
            malformed = True
            continue
        rows += [r for r in data.get("doctor", {}).get("checks", [])
                 if isinstance(r, dict)]
    return rows, malformed


def _resolve(name: str, registered: list[dict],
             proposed: list[dict]) -> tuple[dict | None, str]:
    """The ``[[doctor.checks]]`` row ``name`` resolves to, and where it came from.

    Registered rows win — they are human-blessed config; the builder's proposed row is
    consulted only when no registered row matches, which turns an existing prompt
    requirement into a load-bearing artifact (the builder supplies the detect command,
    the harness runs it, the exit code decides). Matching mirrors
    ``doctor.registered_ids``: the row's ``id`` (default: its ``cmd``),
    case-insensitive; a row with no ``cmd`` cannot detect anything so it never counts.
    """
    for rows, source in ((registered, "registered"), (proposed, "proposed")):
        for row in rows:
            cmd = str(row.get("cmd") or "").strip()
            if not cmd:
                continue
            rid = str(row.get("id") or cmd).strip()
            if rid.lower() == name.lower():
                return row, source
    return None, ""


def adjudicate(d: Path, cfg) -> list[Verdict] | None:
    """Adjudicate the builder's declarations deterministically, or ``None`` when there
    is nothing to adjudicate — the feature is off, or ``build-notes.md`` is absent /
    unreadable / carries no marker. ``None`` means the beat is byte-identical to today:
    no detect cmd is even spawned.

    Registered rows come from ``Config.current_doctor_checks`` — ``pdca.toml`` as it is
    on disk NOW, not the run's snapshot — for the same reason the Plan-exit probe reads
    it (#340): rows are registered mid-cycle. Each resolved row's cmd runs through
    :func:`doctor.probe`, the one probe implementation every consumer shares.
    """
    if getattr(cfg, "dependency_halt", False) is not True:
        return None
    notes = d / "build-notes.md"
    if not notes.exists():
        return None
    try:
        text = notes.read_text(encoding="utf-8")
    except OSError:
        return None
    names = declared_dependencies(text)
    if not names:
        return None
    proposed, malformed = proposed_rows(text)
    registered = cfg.current_doctor_checks()
    verdicts: list[Verdict] = []
    for name in names:
        row, source = _resolve(name, registered, proposed)
        if row is None:
            why = "no registered [[doctor.checks]] row and no parseable proposed row names it"
            if malformed:
                why += " (a proposed ```toml block failed to parse)"
            verdicts.append(Verdict(name, UNCONFIRMED, "", "", None, "", why))
            continue
        cmd = str(row.get("cmd")).strip()
        hint = str(row.get("hint") or "").strip()
        rc = doctor.probe(cmd, cfg)
        if rc != 0:
            verdicts.append(Verdict(name, CONFIRMED, source, cmd, rc, hint,
                                    f"detect cmd exited {rc} — absent on this host"))
        else:
            verdicts.append(Verdict(name, REFUTED, source, cmd, 0, hint,
                                    "detect cmd exited 0 — present on this host; the "
                                    "declaration does not excuse Check"))
    return verdicts


def confirmed(verdicts: list[Verdict] | None) -> bool:
    """True iff at least one declaration survived its probe: the bundle is genuinely
    blocked on that dependency however the other declarations fared, and spending the
    Check beat on the rest would still adjudicate an unverifiable patch."""
    return any(v.verdict == CONFIRMED for v in verdicts or [])


def record(d: Path, verdicts: list[Verdict]) -> None:
    """Persist the adjudication (``state.DEPENDENCY_ADJUDICATION``) — on BOTH outcomes,
    so a refuted or unconfirmed claim leaves the same audit trail a confirmed one does.
    Downstream of Do (``state.DOWNSTREAM_OF_BRIEF``), so an iterate archives it with its
    attempt and a rebuilt attempt is adjudicated fresh."""
    payload = {"halted": confirmed(verdicts),
               "verdicts": [v._asdict() for v in verdicts]}
    (d / state.DEPENDENCY_ADJUDICATION).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load(d: Path) -> dict | None:
    """The bundle's adjudication record, or ``None`` if absent/unreadable — the same
    tolerant contract as every other bundle-file read (testbed #3)."""
    p = d / state.DEPENDENCY_ADJUDICATION
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def recorded_verdicts(d: Path) -> list[Verdict]:
    """The recorded verdicts, reconstructed to the :class:`Verdict` shape — for a
    consumer that needs them again after the beat that produced them (#369: the
    CHECKED-resume rewrites :func:`blocked_review_note` when a death between the N/A
    gate write and the note left a halted bundle with no review artifact). Tolerant
    like :func:`load`: a malformed row is skipped, never a crash."""
    rec = load(d)
    return [Verdict(**{f: v.get(f) for f in Verdict._fields})
            for v in (rec or {}).get("verdicts", []) if isinstance(v, dict)]


def refuted_items(d: Path) -> list[str]:
    """§6 texts for every REFUTED declaration — how the refutation reaches the human and
    ``pdca act index`` (both read SUMMARY §6, not a bundle-local json). The caller tags
    them HUMAN, never IMPL: a rebuild cannot fix a mis-declaration."""
    rec = load(d)
    if not rec:
        return []
    return [
        f"builder-declared external dependency `{v.get('dependency', '?')}` was REFUTED "
        f"deterministically — its detect cmd (`{v.get('cmd', '')}`) exited 0, so the "
        "dependency is present and full Check ran (#341); weigh the builder's claim "
        "against the review before accept"
        for v in rec.get("verdicts", [])
        if isinstance(v, dict) and v.get("verdict") == REFUTED
    ]


def blocked_review_note(d: Path, verdicts: list[Verdict]) -> None:
    """Stand in for the reviewer leaf on a dependency-blocked bundle — the same shape as
    ``driver._close_review_note``: no honest review exists for a patch whose declared
    dependency is confirmed absent, but the human must still consciously disposition it.
    The ``- NEEDS-HUMAN —`` bullets parse into SUMMARY §6 (``assemble._needs_human``),
    so the C6 accept-guard blocks accept until the human decides: provide the dependency
    and iterate-do (the iterate archives this attempt and reruns the full band), or
    discontinue — both at sign-off, which alone sets a terminal state."""
    lines = [
        "# Advisory review — SKIPPED (builder-declared external dependency "
        "confirmed, #341)\n",
        "The builder declared an unmet external dependency in build-notes.md and the "
        "claim was CONFIRMED deterministically (the named [[doctor.checks]] row's "
        "detect cmd exited non-zero), so the Check beat was not spent adjudicating a "
        "patch already stated to be unverifiable. Gates are recorded N/A; no reviewer "
        "or adversary ran. The bundle is resumable.\n",
    ]
    for v in verdicts:
        if v.verdict != CONFIRMED:
            continue
        hint = f" Install hint: {v.hint}." if v.hint else ""
        lines.append(
            f"- NEEDS-HUMAN — External dependency `{v.dependency}` confirmed absent "
            f"(detect cmd `{v.cmd}` exited {v.exit_code}).{hint} Provide it and answer "
            "iterate-do to resume the full Do+Check band, or discontinue at sign-off.")
    (d / "check-review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
