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
union of the enabled two        15       69%          73%
=========================  =======  ========  ===========

The rounds rule has no meaningful recall figure: every bundle that reached 3 rounds passed
through 2, so "recall" there is definitional, not evidence. Its 76% is the load-bearing
number — the probability that a bundle sitting at two rounds goes on to a third. It ships
**disabled** all the same, because enabling it would silently override
``[driver].max_auto_iters`` — see :func:`_thresholds`.

## Why this still does not gate

73% precision is better than the a-priori 62%, and it is not 100%: roughly one firing in
four is a coherent large change that would have converged. So the backstop raises a **§6 NEEDS-HUMAN
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

from . import waves

#: Written at Check, read by `assemble.collect_needs_human`. A file rather than a
#: recomputation so §6 and any later audit see the same numbers the decision was made on.
SIGNAL_FILE = "size-signal.json"

#: Calibrated against 86 settled bundles (see the module docstring for recall/precision of
#: each). In ``[driver.size_signal]`` so an instance retunes against its own corpus — the
#: same escape hatch ``[driver.sizing]`` gives the a-priori score.
DEFAULT_THRESHOLDS = {
    "patch_kb": 100,     # 62% recall / 71% precision
    "patch_files": 20,   # 38% recall / 86% precision
    # DISABLED by default (0), despite being the most precise rule of the three — see
    # `_thresholds` for why. Set it to 2 to enable.
    "rounds": 0,
}

#: A threshold of 0 (or less) switches its rule OFF. Needed because the rounds rule ships
#: disabled, and "0" is the natural way to say that in a TOML table an instance edits.
_DISABLED = 0


def _thresholds(cfg) -> dict[str, int]:
    """Defaults overlaid with ``[driver.size_signal]``, ignoring untidy values.

    A malformed threshold falls back to the default rather than raising: this runs inside
    the Check beat, and a typo in an optional tuning table must not cost the cycle.

    ## Why the rounds rule ships disabled

    It is the most precise of the three (76%), and it is also the one that would silently
    override a setting the operator already made. ``[driver].max_auto_iters`` defaults to
    **3**: with ``rounds`` at 2, the backstop raises a HUMAN item after the second archive,
    auto-iterate declines, and a budget of 3 can never be spent. The operator configured a
    number and would get a different one, with nothing naming the rule that changed it.

    The auto-iterate budget already answers "how many rebuild rounds is this worth?" —
    explicitly, and per instance. What the budget *cannot* see is how big the patch came
    out, and that is this module's actual contribution. So the round count is measured and
    recorded (it belongs in ``size-signal.json``, and #359 will retune against it), but by
    default it raises nothing. An instance that wants the stricter behaviour sets it, and
    then the interaction with ``max_auto_iters`` is a decision someone made on purpose.
    """
    out = dict(DEFAULT_THRESHOLDS)
    for key, value in (getattr(cfg, "size_signal", None) or {}).items():
        if key in out:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
    return out


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
    return {
        "patch_bytes": patch_bytes,
        "patch_files": patch_files,
        # Rounds ALREADY SPENT — one archive per rejected attempt. `iteration-v*` is the
        # same evidence `driver._next_iteration_no` counts, so the two cannot disagree.
        "rounds": len([p for p in d.glob("iteration-v*") if p.is_dir()]),
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
    try:
        return int(signal.get(key, 0))
    except (TypeError, ValueError):
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


def needs_human_text(reasons: list[str]) -> str:
    """The §6 line. Names the recommended answer explicitly, because the wrong one is the
    plausible one: findings on an oversized slice look implementation-shaped every round,
    so `iterate-do` reads as correct and never converges."""
    return ("size backstop — this slice is behaving oversized: "
            + "; ".join(reasons)
            + ". Recommend answering `iterate-plan` at sign-off and authoring the split in "
              "the re-plan (`pdca split`), rather than `iterate-do`: a slice that is too "
              "big yields implementation-shaped findings every round, and splitting "
              "authors briefs, which is Plan's beat.")
