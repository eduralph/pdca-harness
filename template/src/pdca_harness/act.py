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

import json
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


# ----------------------------------------------------------------------------
# Process-delta ledger (issue #149): make Act self-auditing. A recurring signal is
# REGISTERED (open); the human marks it APPLIED once a delta lands; a later Act flags it
# when the same signal RECURS after the applied date — a likely-ineffective delta. Stored
# as process/act-ledger.json: deterministic instrumentation; the human still authors the
# delta and runs `pdca act resolve`.
# ----------------------------------------------------------------------------
_LEDGER = "act-ledger.json"


def _ledger_path(cfg: Config) -> Path:
    """Where the process-delta ledger lives (``process/act-ledger.json``)."""
    return cfg.process_dir / _LEDGER


def load_ledger(cfg: Config) -> list[dict]:
    """The process-delta ledger, or ``[]`` if absent/unreadable."""
    p = _ledger_path(cfg)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _save_ledger(cfg: Config, entries: list[dict]) -> None:
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    _ledger_path(cfg).write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def _recurring(entries: list[ActEntry]) -> dict[str, str]:
    """Normalized-signal → a representative raw text, for each signal appearing in more
    than one cycle (the §10 Act-candidate + §6 NEEDS-HUMAN pool). A miss is "the same"
    across cycles by its normalized key, so a class showing once in §10 of one cycle and
    once in §6 of another still counts as recurring."""
    counts: Counter = Counter()
    raw_of: dict[str, str] = {}
    for e in entries:
        for s in e.act_candidates + e.needs_human:
            n = _norm(s)
            if not n:
                continue
            counts[n] += 1
            raw_of.setdefault(n, s)
    return {n: raw_of[n] for n, c in counts.items() if c > 1}


def register_signals(cfg: Config, entries: list[ActEntry], date: str) -> list[str]:
    """Track each recurring signal not already in the ledger as an ``open`` entry
    (idempotent, deduped by normalized signal). Returns the raw texts newly registered."""
    ledger = load_ledger(cfg)
    known = {e.get("signal") for e in ledger}
    added: list[str] = []
    for norm, raw in _recurring(entries).items():
        if norm not in known:
            ledger.append({"signal": norm, "raw": raw, "first_seen": date,
                           "status": "open", "applied_date": None, "location": ""})
            added.append(raw)
    if added:
        _save_ledger(cfg, ledger)
    return added


def resolve(cfg: Config, query: str, location: str, date: str) -> str | None:
    """Mark the first ``open`` ledger entry matching ``query`` (case-insensitive substring
    of its raw text or normalized signal) ``applied`` on ``date`` with ``location``.
    Returns the matched raw text, or ``None`` if nothing matched."""
    ledger = load_ledger(cfg)
    q = query.strip().lower()
    for e in ledger:
        if e.get("status") == "open" and (
                q in e.get("raw", "").lower() or q in e.get("signal", "")):
            e.update(status="applied", applied_date=date, location=location)
            _save_ledger(cfg, ledger)
            return e.get("raw", "")
    return None


def recurrences(cfg: Config, entries: list[ActEntry] | None = None) -> list[dict]:
    """``applied`` ledger entries whose signal reappears in a cycle frozen AFTER the
    applied date — the delta did not stop the miss, so it is likely ineffective. Each:
    ``{signal, applied, recurred_in: [ids]}``."""
    entries = index(cfg) if entries is None else entries
    out: list[dict] = []
    for led in load_ledger(cfg):
        if led.get("status") != "applied":
            continue
        applied = led.get("applied_date") or ""
        sig = led.get("signal", "")
        hits = [e.bundle.name.replace("issue_", "") for e in entries
                if e.date and (not applied or e.date > applied)
                and sig in {_norm(s) for s in (e.act_candidates + e.needs_human)}]
        if hits:
            out.append({"signal": led.get("raw", sig), "applied": applied,
                        "recurred_in": hits})
    return out


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
def render_index(entries: list[ActEntry], pats: dict[str, list[str]],
                 ledger: list[dict] | None = None, recs: list[dict] | None = None) -> str:
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
    # Process-delta ledger (#149): tracked signals + a loud flag for any applied delta
    # whose miss recurred (likely ineffective).
    if ledger is not None:
        lines += ["", "## Process-delta ledger"]
        if not ledger:
            lines.append("- (empty — no recurring signal tracked yet)")
        for e in ledger:
            tag = (f"applied {e.get('applied_date', '')}"
                   if e.get("status") == "applied" else "open")
            loc = f" → {e['location']}" if e.get("location") else ""
            lines.append(f"- [{tag}] {e.get('raw', '')}{loc}")
    if recs:
        lines += ["", "## ⚠ Ineffective deltas (recurred after applied)"]
        for r in recs:
            lines.append(f"- {r['signal']} — applied {r['applied']}, recurred in "
                         + ", ".join(r["recurred_in"]))
    return "\n".join(lines) + "\n"


def scaffold_entry(entries: list[ActEntry], pats: dict[str, list[str]], date: str,
                   recs: list[dict] | None = None) -> str:
    """A dated act-log entry with bundles + patterns filled, deltas left to the human.

    ``recs`` (issue #149) are applied process-deltas whose miss recurred — surfaced as a
    loud section so the review revisits the ineffective delta, not just new signals."""
    ids = ", ".join(e.bundle.name.replace("issue_", "") for e in entries) or "—"
    exposed = [f"- [{label}] {it}" for label, items in pats.items() for it in items] or [
        "- (no recurring signal surfaced — note any single-cycle observation worth a delta)"
    ]
    body = [
        f"# Act review — {date} — cycles considered: {ids}",
        "",
        "## What the cycles' records exposed",
        *exposed,
    ]
    if recs:
        body += ["", "## ⚠ Ineffective deltas (recurred after applied)"]
        body += [f"- {r['signal']} (applied {r['applied']}) recurred in "
                 f"{', '.join(r['recurred_in'])} — the delta may be ineffective; revisit it"
                 for r in recs]
    body += [
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
    return "\n".join(body)


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
