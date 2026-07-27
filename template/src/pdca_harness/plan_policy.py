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

## Why it is recomputed, never cached

A persisted hold marker becomes stale authority: once a bundle is PLANNED, resuming does
not re-run Plan, so registering the missing ``[[doctor.checks]]`` row or retuning
``[driver.sizing]`` would never clear the marker and the bundle would hold forever. This
runs each beat and reads config from disk, so the fix always takes effect immediately.

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

from . import doctor, sizing

#: `[driver].size_guard` values. `hold` is NOT among them, deliberately — see
#: :func:`size_reasons`.
OFF, WARN, HOLD = "off", "warn", "hold"

#: Reason codes that STOP the beat. Deterministic verdicts only — a heuristic never earns
#: a block (see :func:`size_reasons`).
_BLOCKING = frozenset({"unregistered-dependency"})


class HoldReason(NamedTuple):
    """One reason the driver should pause before spending on this bundle."""

    code: str      # stable, machine-readable: "oversized", …
    detail: str    # one line for the human


def size_reasons(d, cfg) -> list[HoldReason]:
    """Size advisories for a bundle, per ``[driver].size_guard``.

    **There is no `hold` mode, and that is an evidence-based decision.** Calibrated over
    86 settled bundles of a real instance, the best structural rule reaches 50% recall at
    67% precision against ≥3 rounds — one wrong hold for every two right ones. A blocking
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
                   "unimplemented — the signal peaks at 67% precision, see #321]")
    return [HoldReason("oversized", detail)]


def dependency_reasons(d, cfg) -> list[HoldReason]:
    """Brief-declared external dependencies with no registered row (#333).

    **This one blocks**, where the size advisory only warns, and the difference is not a
    matter of taste: it is set membership, not a heuristic. There is no false-positive
    class to trade against — a backticked token either names a registered row or it does
    not — so the precision argument that keeps `size_guard` advisory does not apply.

    It also does not add a new block, it moves an existing one earlier: the same condition
    already refuses `signoff --accept` through the C6 guard. Catching it at Plan spends a
    human minute; catching it at Check spends an `opus`/`max` builder, a codex reviewer at
    `xhigh` and the adversary first — for a verdict that was knowable before Do ever
    dispatched.

    The escape hatch is unchanged: a dependency nothing can detect is written in prose or
    annotated ``(no-check: …)`` and yields no token, so this can never become a reason to
    stop declaring dependencies.
    """
    mode = str(getattr(cfg, "dependency_guard", HOLD) or HOLD).strip().lower()
    if mode == OFF:
        return []
    return [HoldReason("unregistered-dependency", item)
            for item in doctor.unregistered_dependencies(d / "brief.md", cfg)]


def blocking(reasons) -> list[HoldReason]:
    """The subset that should stop the beat. Advisory reasons are reported and passed."""
    return [r for r in reasons if r.code in _BLOCKING]


def evaluate(d, cfg) -> list[HoldReason]:
    """Every pre-dispatch reason to pause on this bundle. Empty ⇒ proceed.

    Advisory by construction today: the driver prints these and continues. The return
    shape is a list so a later blocking check (#333's unregistered dependency, whose
    verdict is set membership rather than a heuristic, and therefore *can* justify a
    block) slots in beside it without another mechanism.
    """
    return list(size_reasons(d, cfg)) + list(dependency_reasons(d, cfg))
