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

import contextlib
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import revalidate, signoff, state
from .config import Config

# Cross-platform advisory file lock (#299 review): ``fcntl`` is Unix-only, and cli.py
# imports this module at load time — a hard ``import fcntl`` would break EVERY installed
# command on Windows (scripts/install.ps1 is a supported bootstrap path) before argument
# parsing. ``msvcrt.locking`` is the Windows equivalent (LK_LOCK retries, then raises).
if os.name == "nt":  # pragma: no cover — exercised only on Windows
    import msvcrt

    def _lock_exclusive(fh, *, wait: bool = True) -> None:
        fh.seek(0)
        if not wait:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        # LK_LOCK is NOT flock's indefinite block: it retries ten times at 1 s
        # intervals and then raises (#299 review round 15). A concurrent interactive
        # Act easily outlives ten seconds, and giving up would skip the waiter's
        # promised review — loop until the lock is actually held.
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue  # LK_LOCK slept ~10 s itself; retry until acquired

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_exclusive(fh, *, wait: bool = True) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))

    def _unlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)

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
    fingerprint: str = ""  # SUMMARY.md hash AT EXTRACTION time (#299 review round 17)


def frozen_bundles(cfg: Config) -> list[Path]:
    """COMPLETE bundles, sorted by name — the only material Act reads."""
    if not cfg.bundle_root.exists():
        return []
    return sorted(
        d for d in cfg.bundle_root.glob("issue_*")
        if d.is_dir() and state.state(d) == state.COMPLETE
    )


# ----------------------------------------------------------------------------
# Cadence + review frontier (issues #109, #299). Act yields a real delta only once
# enough cycles have frozen to show a pattern; the flow auto-runs it only when enough
# have frozen SINCE the last Act. The durable marker used to hold just the frozen
# COUNT at the last review — enough for the cadence trigger, useless for resume: a
# count identifies neither WHICH bundles were covered nor when, so overlapping
# sessions re-reviewed each other's cycles (real merge conflicts in act-log.md and
# the marker) and out-of-order freezes slipped through uncovered. The marker is now a
# JSON object recording the reviewed bundle NAMES (the frontier):
#
#     {"count": 14, "reviewed": ["issue_101", "issue_58"], "last_review_date": "…"}
#
# ``reviewed`` is authoritative (sorted → deterministic file, mergeable-by-union in
# git); ``count`` is derived/informational. A legacy bare-int marker (valid JSON!)
# keeps the old cadence ARITHMETIC but carries NO name information — inferring names
# from it (e.g. a sorted prefix) misassigns whenever a new bundle sorts before
# reviewed ones (issue_10 freezing after 20/30 were reviewed; numeric ids at digit
# boundaries like 99→100), and a misassignment there is a PERMANENT silent skip
# (#299 review round 3). So under a legacy marker the default scope is the FULL
# frozen history, once, loudly; the first --append then records a real frontier.
# An older engine reading the JSON object hits int() → ValueError → 0: degrades
# toward re-review, never toward silent skips — the same failure direction.
# ----------------------------------------------------------------------------
_CADENCE_MARKER = ".act-reviewed"
_SESSION_LOCK = ".act-session.lock"


@contextlib.contextmanager
def act_session(cfg: Config, *, wait: bool = False):
    """The cross-process Act SESSION lock (#299 review rounds 11/12); yields whether
    it was acquired. Non-blocking by default (a manual append's loser reports and
    the human retries); ``wait=True`` BLOCKS until the active session finishes —
    the auto-Act path uses it (#299 review round 14): a flow that merely skipped
    would leave its newly frozen bundles without their promised automatic review
    until some unrelated later flow happened to complete.

    EVERY writing Act path holds it: the flow's auto-Act (``leaves.run_act``) for its
    whole review, and ``act log --append`` for its transaction — otherwise a manual
    append overlapping an automatic review could log-and-mark the same snapshot the
    leaf is still reviewing, and the leaf would then append a duplicate entry the
    frontier union cannot undo. Deliberately a SEPARATE sidecar from the marker lock:
    ``mark_reviewed``/``append_reviewed`` re-acquire that inside the session, and a
    concurrent ``pdca revalidate`` must not block behind a whole interactive review.
    An unopenable lock file yields False (skip, never crash an Act path)."""
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    try:
        fh = (cfg.process_dir / _SESSION_LOCK).open("w")
    except OSError:
        yield False
        return
    try:
        try:
            _lock_exclusive(fh, wait=wait)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                _unlock(fh)
    finally:
        fh.close()


def _load_marker(cfg: Config) -> dict:
    """The parsed review marker:
    ``{"count": int, "reviewed": set[str] | None, "fingerprints": dict[str, str]}``.

    ``reviewed is None`` ⇒ legacy count-only or absent/garbage marker (callers apply
    the name-sorted-prefix heuristic). ``fingerprints`` maps a reviewed name to the
    SUMMARY.md hash the review covered (#299 review round 16) — tolerant: absent or
    malformed reads as ``{}`` (name-only semantics). Defensive like
    :func:`load_ledger` — any unreadable/malformed content is "nothing reviewed",
    never a crash.
    """
    nothing = {"count": 0, "reviewed": None, "fingerprints": {}}
    marker = cfg.process_dir / _CADENCE_MARKER
    if not marker.exists():
        return dict(nothing)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return dict(nothing)
    if isinstance(data, bool):  # bool is an int subclass; a `true` marker means nothing
        return dict(nothing)
    if isinstance(data, int):  # legacy bare-int marker (pre-#299)
        return {"count": max(0, data), "reviewed": None, "fingerprints": {}}
    if isinstance(data, dict):
        reviewed = data.get("reviewed")
        if isinstance(reviewed, list) and all(isinstance(n, str) for n in reviewed):
            fps_raw = data.get("fingerprints")
            fps = ({k: v for k, v in fps_raw.items()
                    if isinstance(k, str) and isinstance(v, str)}
                   if isinstance(fps_raw, dict) else {})
            return {"count": len(reviewed), "reviewed": set(reviewed),
                    "fingerprints": fps}
        count = data.get("count")
        # bool is an int subclass here too (#299 review): a malformed `{"count": true}`
        # must read as "nothing reviewed", not as one covered bundle.
        if isinstance(count, int) and not isinstance(count, bool):
            return {"count": max(0, count), "reviewed": None, "fingerprints": {}}
        return dict(nothing)
    return dict(nothing)


def _fingerprint(d: Path) -> str:
    """Identity of a frozen bundle's reviewed CONTENT — sha256 of its SUMMARY.md
    (#299 review round 16). The documented redo path (``rm -rf`` + rerun) recreates
    a bundle under the SAME name; a name-only frontier would treat the new
    generation as already reviewed and silently omit it from every default scope
    and from the cadence. "" when the summary is unreadable.

    Hashed over a CANONICAL representation (#299 review round 19): tolerantly
    decoded text with newlines normalized to ``\n`` — a frontier shared between
    checkouts with different line-ending settings (``core.autocrlf``) must not read
    unchanged content as a new generation and trigger duplicate reviews."""
    try:
        text = (d / "SUMMARY.md").read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    canon = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _covered_names(m: dict, bundles: list[Path]) -> set[str]:
    """Which of ``bundles`` the marker proves reviewed: name membership AND, when a
    fingerprint was recorded, an unchanged SUMMARY.md hash (#299 review round 16).
    A recorded-but-mismatching fingerprint reads UNREVIEWED (the recreated
    generation was never seen); a name with no recorded fingerprint (a transitional
    marker) counts by name alone."""
    if m["reviewed"] is None:
        return set()
    fps = m["fingerprints"]
    return {d.name for d in bundles
            if d.name in m["reviewed"]
            and (d.name not in fps or fps[d.name] == _fingerprint(d))}


def has_frontier(cfg: Config) -> bool:
    """True iff the marker records WHICH bundles were reviewed (the #299 format);
    False for a legacy count-only / absent / garbage marker — callers then treat the
    full frozen history as the scope, loudly, until the first frontier write."""
    return _load_marker(cfg)["reviewed"] is not None


def unreviewed_bundles(cfg: Config, frozen: list[Path] | None = None) -> list[Path]:
    """Frozen bundles not covered by the last review — the default Act scope (#299).

    Set difference on bundle names, so a bundle frozen out of name order around a
    past review (the observed coverage-gap case) still surfaces as unreviewed. Pass
    ``frozen`` to reuse one snapshot across scope computations (#299 review round 3 —
    two globs can disagree when a bundle freezes between them).
    """
    if frozen is None:
        frozen = frozen_bundles(cfg)
    covered = _covered_names(_load_marker(cfg), frozen)
    return [d for d in frozen if d.name not in covered]


def mark_reviewed(cfg: Config, reviewed: list[Path] | None = None, date: str = "",
                  delta_guard: float | None = None,
                  fingerprints: dict[str, str] | None = None) -> list[str]:
    """Advance the review frontier: union the covered bundles into the marker (#109/#299).

    ``reviewed=None`` ⇒ every currently frozen bundle (what a full auto-Act covers).
    The union is intersected with the current frozen set so a deleted bundle can't
    wedge the counts. Concurrency-safe (#299 review): the whole read-union-write runs
    under an exclusive ``flock`` on a lock sidecar — two overlapping Act sessions
    serialize instead of one overwriting the other's reviewed set — and the temp file
    is per-writer (pid-suffixed) so one writer's ``os.replace`` can never consume
    another's. Each write stays crash-atomic (temp sibling + ``os.replace``).

    ``delta_guard`` (the review's start instant) applies the in-session delta
    protection INSIDE this same critical section (#299 review round 7): a bundle with
    a :func:`delta_since` real delta is dropped from the union. Scanning outside the
    lock would race ``unmark_reviewed`` — revalidate could finish its removal between
    a caller's stale scan and this write, and the stale union would re-hide the
    delta. Revalidate writes the stamp BEFORE calling ``unmark_reviewed`` (which
    takes this same lock), so whichever side enters the critical section second sees
    the other's effect. Returns the withheld bundle names (callers report them).

    ``fingerprints`` are the hashes captured WITH the caller's snapshot (#299 review
    round 17): a bundle recreated while the review ran must not be attested by a
    hash computed from the NEW generation's file after the fact — the marker records
    what the review actually read, and the recreated generation stays unreviewed.
    Names without a snapshot hash fall back to hashing now (direct callers whose
    review IS the present content)."""
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    marker = cfg.process_dir / _CADENCE_MARKER
    lock = marker.with_name(marker.name + ".lock")
    with lock.open("w") as fh:
        _lock_exclusive(fh)  # blocking: the critical section is tiny
        try:
            frozen = frozen_bundles(cfg)
            frozen_names = {d.name for d in frozen}
            # A legacy marker contributes NO prior names (#299 review round 3): the count
            # can't say WHICH bundles it covered, and inferring would risk a permanent
            # skip. Older cycles stay in the default scope until reviewed once more —
            # fail toward re-review, never toward skipping.
            m = _load_marker(cfg)
            prior = m["reviewed"] if m["reviewed"] is not None else set()
            prior_fps = m["fingerprints"]
            src = list(reviewed if reviewed is not None else frozen)
            withheld: list[str] = []
            if delta_guard is not None:
                withheld = sorted(d.name for d in src if delta_since(d, delta_guard))
                src = [d for d in src if d.name not in withheld]
            new = {d.name for d in src}
            # Withheld names leave the PRIOR frontier too (#299 review round 20): a
            # re-review (auto-Act, --all) of an already-covered bundle whose delta
            # landed mid-session must not be re-added by the union — revalidate's
            # own unmark_reviewed can be interrupted after durably writing the
            # stamp, and the unchanged SUMMARY fingerprint would then keep the new
            # delta (a PASS→FAIL regression included) covered indefinitely.
            covered = sorted(((prior - set(withheld)) | new) & frozen_names)
            # Fingerprints (#299 review round 16): newly covered names get the hash
            # THIS review saw; retained names keep the hash THEIR review saw (a
            # recreated bundle must not be re-attested by a review that never read
            # it). A retained name without one (transitional marker) stays name-only.
            by_name = {d.name: d for d in frozen}
            snap = fingerprints or {}
            fps = {nm: ((snap.get(nm) or _fingerprint(by_name[nm])) if nm in new
                        else prior_fps[nm])
                   for nm in covered if nm in new or nm in prior_fps}
            payload = {"count": len(covered), "reviewed": covered,
                       "fingerprints": fps, "last_review_date": date}
            tmp = marker.with_name(f"{marker.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, marker)
            return withheld
        finally:
            _unlock(fh)


def unmark_reviewed(cfg: Config, d: Path) -> None:
    """Drop ONE bundle from the review frontier (#299 review round 4).

    Called when revalidation records a DELTA on a frozen cycle: that delta is new Act
    signal (a frozen PASS→FAIL regression especially), so the bundle must re-enter the
    default ``act index``/``act log`` scope instead of hiding behind the frontier
    until another bundle happens to freeze. Same lock + atomic-write discipline as
    :func:`mark_reviewed`; a legacy/absent marker (no frontier) is a no-op — the
    bundle is already in scope there.

    The lock is taken even when the marker is ABSENT (#299 review round 9): during
    the FIRST Act review the marker only appears at ``mark_reviewed``'s final
    ``os.replace`` — an unlocked absent-check could no-op while that writer, whose
    in-lock delta scan predates this call's stamp, then publishes a frontier
    containing the changed bundle. Under the lock the interleavings are safe: either
    this runs first (still-absent marker → no-op, and the writer's in-lock
    ``delta_guard`` scan then sees the already-written stamp — revalidate stamps
    BEFORE calling here), or the writer runs first and the marker exists on the
    locked re-read, so the entry is removed."""
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    marker = cfg.process_dir / _CADENCE_MARKER
    lock = marker.with_name(marker.name + ".lock")
    with lock.open("w") as fh:
        _lock_exclusive(fh)
        try:
            if not marker.exists():
                return
            m = _load_marker(cfg)
            if m["reviewed"] is None or d.name not in m["reviewed"]:
                return
            covered = sorted(m["reviewed"] - {d.name})
            payload = {"count": len(covered), "reviewed": covered,
                       "fingerprints": {k: v for k, v in m["fingerprints"].items()
                                        if k in covered},
                       "last_review_date": _load_marker_date(cfg)}
            tmp = marker.with_name(f"{marker.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, marker)
        finally:
            _unlock(fh)


def _load_marker_date(cfg: Config) -> str:
    """The marker's recorded last_review_date, or "" (tolerant read)."""
    try:
        data = json.loads((cfg.process_dir / _CADENCE_MARKER).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ""
    return str(data.get("last_review_date", "") or "") if isinstance(data, dict) else ""


def cycles_since_review(cfg: Config) -> int:
    """How many frozen cycles the last Act did not cover (issue #109).

    Exact under the frontier marker (name-set difference); the legacy count
    arithmetic (``current − marker``, never negative) for a pre-#299 marker.
    """
    frozen = frozen_bundles(cfg)
    m = _load_marker(cfg)
    if m["reviewed"] is None:
        return max(0, len(frozen) - m["count"])
    return len(frozen) - len(_covered_names(m, frozen))


def act_due(cfg: Config) -> bool:
    """True iff enough cycles have frozen since the last Act to warrant a review (#109)."""
    return cycles_since_review(cfg) >= cfg.act_cadence


# File mtimes and time.time() live in different clock domains: filesystems truncate
# or round timestamps (ext4 nanosecond fields still disagree with the Python clock by
# milliseconds; FAT rounds to 2 s), so a stamp written moments AFTER `started` can
# carry an mtime just BEFORE it (#299 review round 7). The slack errs toward
# re-review: a stamp from just before the review started is re-examined, never a
# fresh one skipped.
_MTIME_SLACK = 2.0


def delta_since(d: Path, started: float) -> bool:
    """True iff a revalidation stamp recording a REAL delta (``changed: true``) landed
    on ``d`` at/after ``started`` — the "did new Act signal arrive while this review
    ran?" predicate shared by auto-Act and ``act log --append`` (#299 review rounds
    5/6). Such a bundle must stay OUT of the review frontier: the review's index was
    built before the delta, so re-marking it would hide even a frozen PASS→FAIL
    regression behind the frontier (it would undo ``unmark_reviewed``'s effect).

    The stamp's recorded verdict decides, never its mtime alone (#299 review round
    6): a concurrent revalidation that CONFIRMED the frozen record (``changed:
    false``) is not new signal, and withholding its bundle would inflate
    ``cycles_since_review`` into a redundant extra Act. An unreadable stamp counts
    as a delta — re-review over skip. A bundle deleted mid-review globs empty:
    nothing left to protect. Mtimes are compared with :data:`_MTIME_SLACK` (#299
    review round 7) — filesystem and wall-clock timestamps are not the same clock."""
    for p in d.glob("revalidation-*.json"):
        try:
            if p.stat().st_mtime < started - _MTIME_SLACK:
                continue
            if json.loads(p.read_text(encoding="utf-8")).get("changed"):
                return True
        except (ValueError, OSError):
            return True  # fail toward re-review, never toward skipping the signal
    return False


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


def index(cfg: Config, since: str | None = None,
          bundles: list[Path] | None = None) -> list[ActEntry]:
    """Extract §6/§7/§9/§10 from each frozen bundle, newest filtering via §9 date.

    ``bundles`` (#299) restricts the extraction to an explicit list (e.g. the
    unreviewed set); default is every frozen bundle."""
    entries = [_extract(d / "SUMMARY.md", d)
               for d in (frozen_bundles(cfg) if bundles is None else bundles)]
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
        # NOT an early return (#299 review): with the frontier default, an empty scoped
        # set is common ("everything reviewed") while the FULL history still carries the
        # recurring signals, the ledger and the recurrence warnings computed below —
        # discarding them made the command misread as "no frozen bundles".
        lines.append("(no cycles in scope)")
        lines.append("")
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
        "- Agent role prompts: <agents/*.md / skill adjustment>      (path:line)",
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


def append_reviewed(cfg: Config, entries: list[ActEntry], render, *, date: str,
                    delta_guard: float | None = None,
                    ) -> tuple[Path | None, list[ActEntry], list[str]]:
    """The default-scope ``act log --append`` TRANSACTION (#299 review round 10):
    re-check the frontier, append the entry, and advance the frontier under ONE
    marker critical section.

    Two overlapping default-scope appends both scaffold the same unreviewed set
    outside any lock; ``mark_reviewed``'s union keeps the FRONTIER correct, but the
    loser would still append a duplicate log entry for cycles the winner already
    recorded. Here the loser re-scopes INSIDE the lock: entries whose bundles are
    now reviewed are dropped, ``render(kept)`` re-scaffolds the entry for exactly
    the surviving cycles, and nothing at all is appended when none survive
    (``(None, [], [])`` — the caller reports "already covered").

    Ordering inside the critical section: log first, marker second — a crash
    between the two re-reviews the cycles next time, never silently skips them
    (the ``mark_reviewed`` contract). ``delta_guard`` applies the in-session
    delta protection to the same write (see :func:`mark_reviewed`); withheld names
    are returned for reporting. The ``--all``/``--since`` full-scope append stays
    on the plain ``append_entry`` + ``mark_reviewed`` path — an explicit re-review
    deliberately duplicates coverage."""
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    marker = cfg.process_dir / _CADENCE_MARKER
    lock = marker.with_name(marker.name + ".lock")
    with lock.open("w") as fh:
        _lock_exclusive(fh)
        try:
            m = _load_marker(cfg)
            prior = m["reviewed"] if m["reviewed"] is not None else set()
            prior_fps = m["fingerprints"]
            covered_now = _covered_names(m, [e.bundle for e in entries])
            kept = [e for e in entries if e.bundle.name not in covered_now]
            if not kept:
                return None, [], []
            log = append_entry(cfg, render(kept))
            withheld = sorted(e.bundle.name for e in kept
                              if delta_guard is not None
                              and delta_since(e.bundle, delta_guard))
            new = {e.bundle.name for e in kept} - set(withheld)
            frozen = frozen_bundles(cfg)
            frozen_names = {d.name for d in frozen}
            covered = sorted(((prior - set(withheld)) | new) & frozen_names)  # (#299 r20)
            by_name = {d.name: d for d in frozen}
            # The entries carry the hash captured when their SUMMARY was extracted
            # (#299 review round 17) — attest the logged content, never whatever a
            # concurrent redo left on disk after the append.
            ent_fps = {e.bundle.name: e.fingerprint for e in kept if e.fingerprint}
            fps = {nm: ((ent_fps.get(nm) or _fingerprint(by_name[nm])) if nm in new
                        else prior_fps[nm])
                   for nm in covered if nm in new or nm in prior_fps}
            payload = {"count": len(covered), "reviewed": covered,
                       "fingerprints": fps, "last_review_date": date}
            tmp = marker.with_name(f"{marker.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, marker)
            return log, kept, withheld
        finally:
            _unlock(fh)


# ----------------------------------------------------------------------------
def _extract(summary: Path, bundle: Path) -> ActEntry:
    if not summary.exists():
        return ActEntry(bundle=bundle)
    # The fingerprint is captured HERE, at extraction time (#299 review round 17):
    # the entry's hash must attest the content the review/scaffold actually read,
    # not whatever a concurrent redo leaves on disk by the time the frontier writes.
    fingerprint = _fingerprint(bundle)
    secs = _sections(summary.read_text(encoding="utf-8"))
    s9 = _find(secs, "9. Check sign-off")
    s6 = _find(secs, "6. NEEDS-HUMAN")
    s7 = _find(secs, "7. Proven")
    s10 = _find(secs, "10. Act candidates")
    date_m = _DATE_RE.search(s9)
    # [ \t] not \s: `\s` matches `\n`, so an EMPTY `- Outcome:` captured the following line
    # and the ledger recorded "- Iteration delta (if iterating):" as the bundle's outcome.
    # Same defect as signoff._OUTCOME_RE (#328) — fixed in both, or the next reader fixes one
    # and leaves the other, which is how it survived this long.
    out_m = re.search(r"^- Outcome:[ \t]*(.+?)[ \t]*$", s9, re.MULTILINE)
    return ActEntry(
        bundle=bundle,
        date=date_m.group(1) if date_m else "",
        outcome=(out_m.group(1).strip() if out_m else ""),
        needs_human=_checkitems(s6),
        unproven=_unproven(s7),
        act_candidates=_candidates(s10),
        reval_deltas=revalidate.deltas(bundle),  # frozen-gate staleness surfaced (#11)
        fingerprint=fingerprint,
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
    """The body of the section whose heading names ``substr``, or "".

    Delegates to ``signoff.heading_is`` rather than testing here: containment matched
    ``## 19. Check sign-off``, and a bare prefix matched ``## 9. Check sign-off-not-
    authoritative``, so the ledger could read a bundle's outcome out of a lookalike section.
    Sharing the predicate is the point — this rule has come back twice from being fixed on
    one side only (#330 review). Keys are already ``## ``-stripped by :func:`_sections`.
    """
    for k, v in secs.items():
        if signoff.heading_is(k, substr):
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
