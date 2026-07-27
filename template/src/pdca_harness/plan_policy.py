"""Pre-dispatch policy checks — evaluated before the driver spends anything (#321).

A pure function of ``brief.md`` + ``pdca.toml``, consulted by :func:`driver.advance`
before every work-dispatching beat. Nothing here decides *what* to build; it decides
whether the driver should spend a builder, a reviewer and an adversary on this bundle at
all, or hand it back to the human first.

## Why it is evaluated here and not at Plan exit

The obvious home is a hook at the end of the Plan beat. That covers two of the four ways a
bundle reaches Do, and the two it misses are not exotic:

* ``flow.flow`` (single id) calls ``_plan_if_unplanned`` and never touches
  ``waves.partition_schedulable``;
* the zero-id sweep DOES call it;
* ``flow.flow_ids`` (explicit ids — the documented way to drive a batch) reaches neither;
* ``pdca run <id>`` goes straight to ``driver.run_issue``.

Every one of them converges on :func:`driver.advance`. Evaluating there covers all four by
construction rather than by enumeration.

## Why the VERDICT is recomputed, never cached

A persisted hold marker becomes stale authority: once a bundle is PLANNED, resuming does
not re-run Plan, so a marker written at Plan exit would outlive whatever caused it and the
bundle would hold forever. The verdict is therefore derived fresh each beat from the
bundle's own files — edit the brief, or register the missing ``[[doctor.checks]]`` row
(``doctor.registered_ids`` deliberately reads ``pdca.toml`` from disk, PR #269 review), and
the next beat proceeds.

**The run's CONFIG is a snapshot, and that is deliberate.** ``Config.load()`` runs once per
invocation, so ``[driver].size_guard`` and ``[driver.sizing]`` are fixed for the whole run:
editing them mid-flight does not take effect until the next one. Re-reading them per beat
would let a single ``pdca flow`` score two bundles in the same batch against two different
thresholds, which is worse than the inconvenience it removes — a batch has to be
reproducible and explainable as one unit. The recompute guarantee is about the bundle, not
the settings.

## Why BUILT is checked too

A bundle with ``brief.md`` + ``patch.diff`` but no gate record derives as **BUILT** and
never re-enters PLANNED — a resumed bundle, or a builder that wrote a patch and then
exited non-zero (``do_build`` preserves the artifact and re-raises; ``flow._isolate``
contains it). Gating PLANNED alone would let Check run unpoliced on exactly those. It is
also the right semantics: an oversized slice should not buy a reviewer at ``xhigh`` plus
an adversary either.
"""

from __future__ import annotations

from typing import NamedTuple

from . import sizing

#: `[driver].size_guard` values. `hold` is NOT among them, deliberately — see
#: :func:`size_reasons`.
OFF, WARN = "off", "warn"


class HoldReason(NamedTuple):
    """One reason the driver should pause before spending on this bundle."""

    code: str      # stable, machine-readable: "oversized", …
    detail: str    # one line for the human


def size_reasons(d, cfg) -> list[HoldReason]:
    """Size advisories for a bundle, per ``[driver].size_guard``.

    **There is no `hold` mode, and that is an evidence-based decision.** Calibrated over
    86 settled bundles of a real instance, the best structural rule reaches 50% recall at
    62% precision against ≥3 rounds — nearly one wrong hold for every right one. A blocking
    gate at that precision costs a manual override every third flag, which is precisely how
    a guard is trained out of usefulness. #321's own definition of done anticipates this:

        If precision is poor, ship `warn` only and leave `hold` unimplemented rather than
        shipping a gate that trains people to override it.

    ``size_guard = "hold"`` is therefore accepted but treated as ``warn``, with a note —
    silently downgrading it would let an instance believe it is protected when it is not.
    """
    mode = str(getattr(cfg, "size_guard", OFF) or OFF).strip().lower()
    if mode == OFF:
        return []

    est = sizing.estimate(d / "brief.md", cfg)
    if est.band != sizing.OVERSIZED:
        return []

    detail = f"oversized — consider `pdca split` first ({'; '.join(est.reasons)})"
    if mode not in (OFF, WARN):
        detail += (f" [size_guard={mode!r} is treated as 'warn': a blocking mode is "
                   "unimplemented — the signal peaks at 62% precision, see #321]")
    return [HoldReason("oversized", detail)]


def evaluate(d, cfg) -> list[HoldReason]:
    """Every pre-dispatch reason to pause on this bundle. Empty ⇒ proceed.

    Advisory by construction today: the driver prints these and continues. The return
    shape is a list so a later blocking check (#333's unregistered dependency, whose
    verdict is set membership rather than a heuristic, and therefore *can* justify a
    block) slots in beside it without another mechanism.
    """
    return list(size_reasons(d, cfg))
