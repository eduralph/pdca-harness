"""Act tooling (L4) — cross-cycle process review (docs 01/02/03 §Act).

Act runs *out of band*: not inside the per-issue state machine, but periodically
across **frozen** (COMPLETE) bundles. This module is the instrumentation, not the
judgment — it surfaces what the cycles' records expose (a read-only bundle index
+ recurring-signal scan) and scaffolds a dated act-log entry with the considered
bundles and detected patterns pre-filled. *Which* rule to add, *which* template
field to clarify — the irreducible Act work — stays the human's, left as TODO in
the scaffold.

What Act never does (enforced by this module doing none of it): re-decide a
contribution's disposition, run the validator/suite, or author the next brief.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import revalidate, state
from .config import Config

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass
class ActEntry:
    """The Act-relevant extract of one frozen bundle's SUMMARY.md."""

    bundle: Path
    date: str = ""
    outcome: str = ""
    needs_human: list[str] = field(default_factory=list)  # §6 items (cleared or not)
    unproven: list[str] = field(default_factory=list)  # §7 unproven lines
    act_candidates: list[str] = field(default_factory=list)  # §10 hints
    reval_deltas: list[str] = field(default_factory=list)  # revalidation stamps (#11)


def frozen_bundles(cfg: Config) -> list[Path]:
    """COMPLETE bundles, sorted by name — the only material Act reads."""
    if not cfg.bundle_root.exists():
        return []
    return sorted(
        d for d in cfg.bundle_root.glob("issue_*")
        if d.is_dir() and state.state(d) == state.COMPLETE
    )


# ----------------------------------------------------------------------------
# Cadence (issue #109): Act yields a real delta only once enough cycles have frozen to
# show a pattern. The flow auto-runs it only when this many cycles have frozen SINCE the
# last Act — counted from a durable marker (the frozen count at the last review) so it
# holds across flow invocations, and works even when a command-mode Act writes no
# act-log entry (the model judged "no delta"). Frozen bundles are monotonic (COMPLETE is
# terminal), so current-minus-marker is the count of unreviewed cycles.
# ----------------------------------------------------------------------------
_CADENCE_MARKER = ".act-reviewed"  # holds the frozen-bundle count at the last Act


def mark_reviewed(cfg: Config) -> None:
    """Record that Act just ran: stamp the current frozen-bundle count (issue #109)."""
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    (cfg.process_dir / _CADENCE_MARKER).write_text(
        f"{len(frozen_bundles(cfg))}\n", encoding="utf-8")


def cycles_since_review(cfg: Config) -> int:
    """How many cycles have frozen since the last Act (issue #109).

    ``current frozen count − marker`` (no marker ⇒ all frozen cycles count). Never
    negative, so a deleted bundle can't wedge the cadence.
    """
    marker = cfg.process_dir / _CADENCE_MARKER
    last = 0
    if marker.exists():
        try:
            last = int(marker.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            last = 0
    return max(0, len(frozen_bundles(cfg)) - last)


def act_due(cfg: Config) -> bool:
    """True iff enough cycles have frozen since the last Act to warrant a review (#109)."""
    return cycles_since_review(cfg) >= cfg.act_cadence


def index(cfg: Config, since: str | None = None) -> list[ActEntry]:
    """Extract §6/§7/§9/§10 from each frozen bundle, newest filtering via §9 date."""
    entries = [_extract(d / "SUMMARY.md", d) for d in frozen_bundles(cfg)]
    if since:
        entries = [e for e in entries if e.date and e.date >= since]
    return entries


def patterns(entries: list[ActEntry]) -> dict[str, list[str]]:
    """Recurring signals across cycles — the same hint/class in more than one."""
    cand = Counter(_norm(c) for e in entries for c in e.act_candidates)
    nh = Counter(_norm(c) for e in entries for c in e.needs_human)
    return {
        "act_candidates": [f"{n}× {t}" for t, n in cand.most_common() if n > 1],
        "needs_human_classes": [f"{n}× {t}" for t, n in nh.most_common() if n > 1],
    }


# ----------------------------------------------------------------------------
def render_index(entries: list[ActEntry], pats: dict[str, list[str]]) -> str:
    lines = [f"# Act bundle index — {len(entries)} frozen cycle(s)", ""]
    if not entries:
        lines.append("(no frozen bundles — nothing to review yet)")
        return "\n".join(lines) + "\n"
    for e in entries:
        lines += [
            f"## {e.bundle.name}  ({e.date or 'no date'}) — {e.outcome or 'no outcome'}",
            f"- §6 NEEDS-HUMAN ({len(e.needs_human)}): " + ("; ".join(e.needs_human) or "—"),
            f"- §7 unproven ({len(e.unproven)}): " + ("; ".join(e.unproven) or "—"),
            f"- §10 Act candidates ({len(e.act_candidates)}): " + ("; ".join(e.act_candidates) or "—"),
        ]
        # Only when present — a frozen gate result the current engine now contradicts
        # (esp. a frozen FAIL now PASS = stale artifact, or a frozen PASS now FAIL =
        # regression). Surfaced here so Act can tell stale records from real failures.
        if e.reval_deltas:
            lines.append(f"- revalidation deltas ({len(e.reval_deltas)}): "
                         + "; ".join(e.reval_deltas))
        lines.append("")
    lines += ["## Recurring signals (appear in >1 cycle)"]
    any_pat = False
    for label, items in pats.items():
        for it in items:
            lines.append(f"- [{label}] {it}")
            any_pat = True
    if not any_pat:
        lines.append("- (none yet)")
    return "\n".join(lines) + "\n"


def scaffold_entry(entries: list[ActEntry], pats: dict[str, list[str]], date: str) -> str:
    """A dated act-log entry with bundles + patterns filled, deltas left to the human."""
    ids = ", ".join(e.bundle.name.replace("issue_", "") for e in entries) or "—"
    exposed = [f"- [{label}] {it}" for label, items in pats.items() for it in items] or [
        "- (no recurring signal surfaced — note any single-cycle observation worth a delta)"
    ]
    return "\n".join(
        [
            f"# Act review — {date} — cycles considered: {ids}",
            "",
            "## What the cycles' records exposed",
            *exposed,
            "",
            "## Process deltas  (TODO — the human decides these; each must be located)",
            "- Spec template: <field added/clarified/removed>            (path)",
            "- Ruleset: <rule added/retired/relaxed/tightened>           (path:line)",
            "- Gates: <check added/promoted/moved>                       (path:line)",
            "- Agent skills: <SKILL.md / AGENTS.md adjustment>           (path:line)",
            "",
            "## How effectiveness will be judged",
            "- The next Do phases should not recreate <specific issue>. Watch the next K cycles.",
            "",
        ]
    )


def append_entry(cfg: Config, entry_text: str) -> Path:
    """Append a scaffolded entry to process/act-log.md (creating it if needed)."""
    log = cfg.process_dir / "act-log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if log.exists() else "# Act log\n\n"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(prefix + "\n" + entry_text + "\n")
    return log


# ----------------------------------------------------------------------------
def _extract(summary: Path, bundle: Path) -> ActEntry:
    if not summary.exists():
        return ActEntry(bundle=bundle)
    secs = _sections(summary.read_text(encoding="utf-8"))
    s9 = _find(secs, "9. Check sign-off")
    s6 = _find(secs, "6. NEEDS-HUMAN")
    s7 = _find(secs, "7. Proven")
    s10 = _find(secs, "10. Act candidates")
    date_m = _DATE_RE.search(s9)
    out_m = re.search(r"^- Outcome:\s*(.+?)\s*$", s9, re.MULTILINE)
    return ActEntry(
        bundle=bundle,
        date=date_m.group(1) if date_m else "",
        outcome=(out_m.group(1).strip() if out_m else ""),
        needs_human=_checkitems(s6),
        unproven=_unproven(s7),
        act_candidates=_candidates(s10),
        reval_deltas=revalidate.deltas(bundle),  # frozen-gate staleness surfaced (#11)
    )


def _sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur, buf = None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur, buf = line[3:].strip(), []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf)
    return out


def _find(secs: dict[str, str], substr: str) -> str:
    for k, v in secs.items():
        if substr in k:
            return v
    return ""


def _checkitems(body: str) -> list[str]:
    items = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("- [ ]") or s.startswith("- [x]"):
            items.append(s[5:].strip())
    return items


def _unproven(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.lower().startswith("- unproven") and ":" in s:
            val = s.split(":", 1)[1].strip()
            if val and not val.startswith("anything flagged"):
                out.append(val)
    return out


def _candidates(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("- [ ]") or s.startswith("- [x]"):
            out.append(s[5:].strip())
        elif s.startswith("- ") and not s.startswith("- (") and "Examples:" not in s:
            out.append(s[2:].strip())
    return out


def _norm(text: str) -> str:
    words = re.sub(r"\s+", " ", text.strip().lower()).split()
    return " ".join(words[:8])
