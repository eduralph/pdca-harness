"""The EMPIRICAL size backstop, measured at Check (issue #324).

``sizing`` guesses from the brief before a line is written. This module measures the patch
that actually arrived. They are the same question asked at two different times, and the
second one is much better at it — which is the whole reason this exists.

## Why an a-priori estimate is not enough

Structural features predict patch size well (ρ≈0.7) and churn weakly (best ρ 0.32), so some
oversized slices get through no matter how the thresholds are set. #321 declined to *gate*
on the a-priori score for exactly that reason: 62% precision is roughly one wrong hold per
right one, and a gate people learn to override is worse than no gate.

Measured at Check, the same corpus answers far better — each rule against "did this bundle
churn ≥3 rounds", over the 86 settled bundles of `getwyrd/wyrd-pdca` (base rate 19%):

=========================  =======  ========  ===========
rule                        fires    recall    precision
=========================  =======  ========  ===========
patch ≥ 100 KB                  14       62%          71%
patch touches ≥ 20 files         7       38%          86%
≥ 2 rounds already spent        21        —           76%
union of the three              23      100%          70%
=========================  =======  ========  ===========

The rounds rule has no meaningful recall figure: every bundle that reached 3 rounds passed
through 2, so "recall" there is definitional, not evidence. Its 76% is the load-bearing
number — the probability that a bundle sitting at two rounds goes on to a third. It fires
at 2 while ``[driver].max_auto_iters`` defaults to 3, so it deliberately stops the rebuild
loop with a round still nominally available; see :func:`_thresholds` for why a ceiling and
a stop signal are different things.

## Why this still does not gate

70% precision is better than the a-priori 62%, and it is not 100%: roughly three firings
in ten are a coherent large change that would have converged. So the backstop raises a **§6 NEEDS-HUMAN
item** and the human decides — the same disposition #321 reached, on better evidence.

## The tag is the mechanism, and getting it wrong inverts the feature

The item is **HUMAN**, never IMPL. ``autoiterate.eligible()`` requires every item to be
IMPL or STANDING, so a HUMAN item **disqualifies auto-iterate** — which is precisely what
should happen to a bundle that is behaving oversized. Tagged IMPL it would instead *count
as a reason to rebuild*, turning the backstop into an accelerator for the failure it exists
to stop: more rounds burned re-implementing a slice that needs splitting.

## It recommends iterate-plan, not iterate-do

By Check the bundle has a patch, and splitting authors briefs — which is Plan's beat. A
slice that is simply too big produces implementation-shaped findings every round, so
``iterate-do`` looks right and never converges. The doctrine 0.56 settled: the split is
authored in Plan, and late discovery routes there through sign-off answering
``iterate-plan``.
"""

from __future__ import annotations

import json
from pathlib import Path

import re

from . import waves

#: Written at Check, read by `assemble.collect_needs_human`. A file rather than a
#: recomputation so §6 and any later audit see the same numbers the decision was made on.
SIGNAL_FILE = "size-signal.json"

#: `iteration-v<N>` — the archive directory `driver._archive_iteration` writes.
_ITERATION_DIR = re.compile(r"^iteration-v(\d+)$")

#: Calibrated against 86 settled bundles (see the module docstring for recall/precision of
#: each). In ``[driver.size_signal]`` so an instance retunes against its own corpus — the
#: same escape hatch ``[driver.sizing]`` gives the a-priori score.
DEFAULT_THRESHOLDS = {
    "patch_kb": 100,     # 62% recall / 71% precision
    "patch_files": 20,   # 38% recall / 86% precision
    "rounds": 2,         # 76% precision that a third round follows
}

#: A threshold of 0 (or less) switches its rule OFF. Needed because the rounds rule ships
#: disabled, and "0" is the natural way to say that in a TOML table an instance edits.
_DISABLED = 0


def _thresholds(cfg) -> dict[str, int]:
    """Defaults overlaid with ``[driver.size_signal]``, ignoring untidy values.

    A malformed threshold falls back to the default rather than raising: this runs inside
    the Check beat, and a typo in an optional tuning table must not cost the cycle.
    ``OverflowError`` is caught alongside the obvious two because it is NOT one of them:
    `int(float("inf"))` raises it, and TOML writes `inf` as a bare literal, so
    `patch_kb = inf` — a plausible way to try to switch a rule off — aborted Check.

    ## The rounds rule DOES cut the auto-iterate budget short, on purpose

    ``[driver].max_auto_iters`` defaults to 3 and this rule fires at 2, so a bundle that
    reaches its second archive raises a HUMAN item and auto-iterate declines with a round
    still nominally available. That is the intended behaviour, not a collision to design
    around: the budget is a *ceiling* on how many rebuilds are worth attempting, and this
    is evidence that further rebuilds are the wrong move — 76% of bundles sitting at two
    rounds go on to a third, and a third round of implementation findings on a slice that
    needs splitting is the exact spiral the backstop exists to break. A ceiling and a stop
    signal are different things, and the stop signal should win.

    What must NOT happen is it winning *quietly*. An operator who set ``max_auto_iters = 3``
    and sees the loop halt at 2 has to be able to read why, or the number they configured
    just appears not to work. So :func:`is_size_item` lets the flow name this rule at the
    point auto-iterate declines, rather than returning a bare False.
    """
    out = dict(DEFAULT_THRESHOLDS)
    for key, value in (getattr(cfg, "size_signal", None) or {}).items():
        if key in out:
            try:
                out[key] = int(value)
            except (TypeError, ValueError, OverflowError):
                pass
    return out


def iteration_rounds(d: Path) -> tuple[int, int]:
    """``(rounds attributable to the CURRENT brief, replan count)``.

    An iterate-to-**Do** archives the attempt and keeps the brief; an iterate-to-**Plan**
    archives the brief too (``driver._archive_iteration(include_brief=True)``) and the
    bundle is re-planned from scratch. So an archive containing a ``brief.md`` marks a
    boundary: rounds before it were spent on a DIFFERENT brief and must not be charged to
    the one on disk now.

    Counting every archive is not a rounding error, it is the wrong measurement. A bundle
    that has just been re-planned — often *because* this backstop recommended it — starts
    its new spec already over the threshold, so its very first Check raises "2 rounds
    already spent" and recommends the re-plan that has only just happened.

    The same doctrine has a second boundary (issue #436): a round whose archived evidence
    shows an environment fault was the SOLE recorded driver of the iterate — a gating red
    the gate itself recorded ``unverifiable`` (a stale host CLI, an absent oracle), or a
    flaky ``fail→pass`` confirm-once record — is churn evidence about the HOST, not the
    slice, and is not charged to it either. See :func:`_environment_attributed` for the
    exact conditions; anything ambiguous, missing, or unreadable COUNTS the round, so
    the failure mode is over-counting (the backstop stays), never silent shrinkage.

    Shared with ``scripts/size-calibrate``, which defined it first: the thresholds were
    calibrated on THIS definition, so a runtime counting anything else is measuring a
    different quantity from the one the numbers describe.
    """
    archives = []
    for a in d.glob("iteration-v*"):
        m = _ITERATION_DIR.match(a.name)
        if m and a.is_dir():
            archives.append((int(m.group(1)), a))
    replans = [n for n, a in archives if (a / "brief.md").is_file()]
    boundary = max(replans, default=0)
    counted = [a for n, a in archives if n > boundary]
    return sum(1 for a in counted if not _environment_attributed(a)), len(replans)


def _environment_attributed(archive: Path) -> bool:
    """True iff the archive's own evidence shows an environment fault was the SOLE
    recorded driver of that round (issue #436).

    Presence of an environmental result alone is NOT attribution: a round can carry an
    ``unverifiable`` gating row AND an independent implementation finding, and that round
    is still slice churn. So all three must hold, each read from the files an iterate
    archives with the attempt (``state.DOWNSTREAM_OF_BRIEF`` moves ``check-gates.json``
    and ``check-review.md`` into every ``iteration-v<N>/``):

      (a) the gating rows contain NO plain gating ``fail`` — an un-flagged red IS a
          verdict on the patch, whatever else the round recorded;
      (b) at least one gating row is recorded ``unverifiable`` (the oracle could not
          answer — issue #46's channel) or bears a truthy ``flaky`` key (a fail→pass
          confirm-once record: the #371 contract, implemented here consumer-side and
          defensively — the recorder has not landed, so the key activates the day it
          does). A ``fail`` row flagged flaky is by construction not a verdict on the
          patch, so it neither trips (a) nor fails (b);
      (c) the archived review record drove nothing of its own
          (:func:`_review_drove_the_iterate`) — otherwise the environmental row merely
          accompanied a real finding.

    All-green gates fail (b): that iterate was reviewer-driven, which is slice churn.
    Fail-safe throughout: missing, unreadable, or malformed evidence returns False and
    the round counts — over-counting keeps the backstop, and silent shrinkage is the
    same failure mode :func:`current` refuses for the recorded signal.
    """
    rows = _archived_gating_rows(archive)
    if rows is None:
        return False
    if any(r.get("result") == "fail" and not r.get("flaky") for r in rows):
        return False
    if not any(r.get("result") == "unverifiable" or r.get("flaky") for r in rows):
        return False
    return not _review_drove_the_iterate(archive)


def _archived_gating_rows(archive: Path) -> list[dict] | None:
    """The GATING rows of the archive's own ``check-gates.json``, or ``None`` when the
    record is missing, unreadable, or not the shape ``gates._finalize`` writes.

    ``None`` (not ``[]``) so the caller can tell "no evidence" from "no gating rows":
    the former must count the round (fail-safe), and conflating them would let a bundle
    with garbled archives silently shrink the signal."""
    try:
        record = json.loads((archive / "check-gates.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = record.get("rows") if isinstance(record, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return None
    return [r for r in rows if r.get("gating")]


def _review_drove_the_iterate(archive: Path) -> bool:
    """Whether the archived review record shows a failing / implementation-shaped finding
    of its own driving the iterate — or is too ambiguous to say (both count the round).

    False only for a REAL review artifact whose findings are at most the standing
    Validation row — the one row the reviewer's prompt emits NEEDS-HUMAN on every cycle,
    which therefore carries no signal (the #293 doctrine). Everything else is True:

      * any other NEEDS-HUMAN finding, whatever its kind — a real objection the iterate
        may have been answering;
      * a FAIL verdict cell in a review table — a failing finding by name;
      * a leaf-status placeholder (``assemble.leaf_status``) — nothing reviewed the
        attempt, so the record cannot attest the review drove nothing;
      * a missing or unreadable file — no evidence, fail-safe.

    The findings are read through ``assemble._items_from_artifact`` — the same parser
    that feeds §6 and the auto-iterate decision — deliberately, rather than re-derived
    here: two parsers for the same artifact is what let a real objection wear the
    template's clothes once already (PR #294 review).
    """
    # Imported HERE, not at module scope, for the cycle `measure` documents: `assemble`
    # imports this module at its own top level.
    from . import assemble

    try:
        text = (archive / "check-review.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    if assemble.leaf_status(text):
        return True
    if any(it.kind != assemble.STANDING
           for it in assemble._items_from_artifact(text, allow_standing=True)):
        return True
    return _has_fail_verdict_cell(text)


def _has_fail_verdict_cell(text: str) -> bool:
    """A table cell reading exactly ``FAIL`` — the reviewer's failing verdict
    (``leaves``' mandated vocabulary: PASS / FAIL / NEEDS-HUMAN). Whole-cell match, so a
    Basis cell that merely *mentions* a failure does not trip it."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and any(c.strip().casefold() == "fail"
                                     for c in s.strip("|").split("|")):
            return True
    return False


def measure(d: Path) -> dict:
    """What the bundle has actually produced so far. Never raises.

    ``patch_files`` is counted through ``waves.diff_files`` rather than by splitting the
    diff here, so the backstop and the wave scheduler agree on what "a file this bundle
    touched" means.
    """
    # Imported HERE, not at module scope: `assemble` imports this module, `autoiterate`
    # imports `assemble`, and a top-level import closes that cycle — `assemble` then fails
    # at `from .assemble import IMPL` on a partially-initialised module. Reading
    # `auto-iterate.json` directly instead would be a second definition of the budget file.
    from . import autoiterate

    patch = d / "patch.diff"
    try:
        patch_bytes = patch.stat().st_size if patch.is_file() else 0
    except OSError:
        patch_bytes = 0
    try:
        patch_files = len(waves.diff_files(patch)) if patch.is_file() else 0
    except (OSError, UnicodeDecodeError, ValueError):
        # A diff this bundle produced can still be unparseable; an unmeasurable file count
        # is a missing signal, not a reason to abort Check.
        patch_files = 0
    rounds, replans = iteration_rounds(d)
    return {
        "patch_bytes": patch_bytes,
        "patch_files": patch_files,
        # Rounds spent ON THE BRIEF THAT IS THERE NOW, and attributable to the SLICE
        # rather than to a recorded environment fault — see `iteration_rounds`.
        "rounds": rounds,
        # Recorded but unweighted: a re-plan is a fact about the bundle's history that #359
        # will want, and it is the boundary `rounds` is measured from.
        "replans": replans,
        "auto_iters": autoiterate.count(d),
    }


def record(d: Path, cfg) -> dict:
    """Measure and persist. Returns the signal; a write failure is not fatal.

    The file is the artifact #324 asks for, but the caller gets the value back regardless
    so a read-only bundle directory degrades to "no record" rather than "no backstop".
    """
    signal = measure(d)
    try:
        (d / SIGNAL_FILE).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return signal


def current(d: Path, cfg=None) -> dict:
    """The signal to judge this bundle on: the recorded one, else a fresh measurement.

    The recorded file wins so §6 and any later audit read the numbers the decision was made
    on. But it is a RECORD, not the source of truth — :func:`record` is allowed to fail
    (a read-only bundle) and older bundles predate the file entirely. Reading it as the
    source of truth meant an unwritable ``size-signal.json`` made the backstop vanish
    silently: `_size_backstop` warned from the in-memory signal at Check while
    `collect_needs_human` found nothing, so an oversized bundle with an IMPL finding
    auto-iterated anyway — the exact failure this module exists to stop, and the direct
    opposite of the "degrades to no record, never no backstop" this file claimed.

    Measuring is a stat and a diff parse, so the fallback is cheap and deterministic.
    """
    return read(d) or measure(d)


def read(d: Path) -> dict | None:
    """The recorded signal, or None when absent or garbled.

    None means "not measured", which is different from "measured and small" — the caller
    must not read a missing file as evidence the bundle is fine.
    """
    try:
        loaded = json.loads((d / SIGNAL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _int(signal: dict, key: str) -> int:
    """A recorded value as an int, or 0. ``OverflowError`` for the same reason it is caught
    in :func:`_thresholds`, and it bites harder here: JSON parses ``1e309`` as ``inf``, so a
    single absurd number in a file this module wrote itself aborted summary assembly."""
    try:
        return int(signal.get(key, 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def oversize_reasons(signal: dict | None, cfg) -> list[str]:
    """Which empirical thresholds this bundle has crossed. Empty when none, or unmeasured.

    Every crossed rule is named, not just the first: "253 KB across 26 files after 2
    rounds" is a different conversation from "110 KB", and the human is being asked to
    decide whether to split.
    """
    if not signal:
        return []
    t = _thresholds(cfg)
    reasons: list[str] = []
    kb = _int(signal, "patch_bytes") / 1024
    if t["patch_kb"] > _DISABLED and kb >= t["patch_kb"]:
        reasons.append(f"patch is {kb:.0f} KB (threshold {t['patch_kb']} KB)")
    files = _int(signal, "patch_files")
    if t["patch_files"] > _DISABLED and files >= t["patch_files"]:
        reasons.append(f"patch touches {files} files (threshold {t['patch_files']})")
    rounds = _int(signal, "rounds")
    if t["rounds"] > _DISABLED and rounds >= t["rounds"]:
        reasons.append(f"{rounds} round(s) already spent (threshold {t['rounds']})")
    return reasons


#: Prefix of the §6 item this module raises. `flow` matches on it to explain WHY
#: auto-iterate declined — a heuristic that cuts an operator's configured budget short
#: must say so, or the number they set simply appears not to work.
SIZE_ITEM_PREFIX = "size backstop —"


def is_size_item(text: str) -> bool:
    """True for a §6 item raised by this module."""
    return str(text).lstrip().startswith(SIZE_ITEM_PREFIX)


def needs_human_text(reasons: list[str]) -> str:
    """The §6 line. Names the recommended answer explicitly, because the wrong one is the
    plausible one: findings on an oversized slice look implementation-shaped every round,
    so `iterate-do` reads as correct and never converges."""
    return (f"{SIZE_ITEM_PREFIX} this slice is behaving oversized: "
            + "; ".join(reasons)
            + ". Recommend answering `iterate-plan` at sign-off and authoring the split in "
              "the re-plan (`pdca split`), rather than `iterate-do`: a slice that is too "
              "big yields implementation-shaped findings every round, and splitting "
              "authors briefs, which is Plan's beat.")
