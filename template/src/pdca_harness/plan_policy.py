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

import sys
from typing import NamedTuple

from . import doctor, sizing

#: `[driver].size_guard` values. `hold` is NOT among them, deliberately — see
#: :func:`size_reasons`.
OFF, WARN, HOLD = "off", "warn", "hold"

#: Reason codes that STOP the beat. Deterministic verdicts only — a heuristic never earns
#: a block (see :func:`size_reasons`).
_BLOCKING = frozenset({"unregistered-dependency"})


class PolicyHold(Exception):
    """A blocking pre-dispatch reason. Raised by the driver, caught by its callers.

    An EXCEPTION rather than a quiet return, because a quiet return is a hang: `run_issue`
    loops `while state not in HALTED: advance(...)`, and a hold leaves the bundle in
    PLANNED or BUILT — neither halted — so `advance` returning without progress spins
    forever printing the same warning. Signalling out of band is the only shape that
    cannot be accidentally ignored by a caller's loop.
    """

    def __init__(self, reasons: list["HoldReason"]):
        self.reasons = reasons
        super().__init__("; ".join(r.detail for r in reasons))


class HoldReason(NamedTuple):
    """One reason the driver should pause before spending on this bundle."""

    code: str      # stable, machine-readable: "oversized", …
    detail: str    # one line for the human


def size_reasons(d, cfg, *, before_do: bool = True) -> list[HoldReason]:
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
    # Fold in the configured sizer's verdict (#320's 1b). Without this the model half is
    # unreachable: `estimate` alone never sees the one question structure cannot answer —
    # how many independently shippable outcomes the brief describes — and every configured
    # `[leaves.sizer]` escalation is dead config. `combine` escalates only, so a stub, a
    # missing verdict or a malformed one leaves the structural estimate byte-identical.
    # `before_do` is False before CHECK. The paid leaf answers "how many independently
    # shippable outcomes?" — advice that can prevent a build, and therefore worth buying
    # only while one can still be prevented. At BUILT the patch already exists, the
    # advisory does not block, and nothing persists it, so a second call would buy a log
    # line about work already paid for. A verdict the Plan beat already produced is read
    # for free.
    from . import leaves
    est = sizing.combine(est, leaves.run_sizer(d, cfg) if before_do
                         else leaves.current_sizing(d, cfg))
    if est.band != sizing.OVERSIZED:
        return []

    # The remediation follows the READOUT that fired, not the combined band. A brief that
    # is high-difficulty and large scores `patch_band=oversized` with `churn_band=watch`,
    # and the sizing contract is explicit that a large COHERENT patch is not a slice that
    # needs splitting — recommending a split there is advice the estimator's own model
    # contradicts.
    # The MODEL's verdict counts here even when it did not raise the band: "two
    # independently shippable outcomes" is the evidence that justifies a split, and
    # telling the human "large but coherent" over the top of it contradicts the one
    # signal that can actually see decomposability.
    splittable = (est.churn_band == sizing.OVERSIZED
                  or est.model_band == sizing.OVERSIZED
                  or est.patch_band != sizing.OVERSIZED)
    if not splittable:
        remedy = ("expect a large patch — worth a look before Do, but a large COHERENT "
                  "change is not a split candidate")
    elif before_do:
        remedy = "consider `pdca split` first"
    else:
        # A split authors BRIEFS, and authoring briefs is what Plan does — so the route
        # back is `iterate-plan`, which archives this brief and returns the bundle to
        # Plan, where the split belongs. Telling the human to run `pdca split` on a bundle
        # that already has a patch would decompose it AFTER the build the children will
        # not inherit.
        remedy = ("if this will not converge, answer `iterate-plan` at sign-off and split "
                  "in the re-plan — a split authors briefs, which is Plan's beat")
    detail = f"oversized — {remedy} ({'; '.join(est.reasons)})"
    if mode not in (OFF, WARN):
        detail += (f" [size_guard={mode!r} is treated as 'warn': a blocking mode is "
                   "unimplemented — the signal peaks at 62% precision, see #321]")
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
    if mode not in (OFF, WARN, HOLD):
        # A typo must fail SAFE. Falling through to the warn branch let
        # `dependency_guard = "hld"` silently dispatch Do past an unregistered dependency,
        # with nothing on screen to say the setting had not been understood.
        print(f"plan-policy: [driver].dependency_guard = {mode!r} is not one of "
              f"{OFF!r}/{WARN!r}/{HOLD!r} — treating it as {HOLD!r}", file=sys.stderr)
        mode = HOLD
    if mode == OFF:
        return []
    # Only `hold` blocks. `warn` reports the same item and lets Do proceed — the code
    # carries the mode, because `blocking()` decides on the code alone and a shared code
    # would make the documented warn option silently behave as hold.
    code = "unregistered-dependency" if mode == HOLD else "unregistered-dependency-warn"
    return [HoldReason(code, item)
            for item in doctor.unregistered_dependencies(d / "brief.md", cfg)]


def blocking(reasons) -> list[HoldReason]:
    """The subset that should stop the beat. Advisory reasons are reported and passed."""
    return [r for r in reasons if r.code in _BLOCKING]


def evaluate(d, cfg, *, before_do: bool = True) -> list[HoldReason]:
    """Every pre-dispatch reason to pause on this bundle. Empty ⇒ proceed.

    Advisory by construction today: the driver prints these and continues. The return
    shape is a list so a later blocking check (#333's unregistered dependency, whose
    verdict is set membership rather than a heuristic, and therefore *can* justify a
    block) slots in beside it without another mechanism.
    """
    # Deterministic checks FIRST. The size advisory may invoke a paid model leaf, and
    # there is no sense buying an advisory for a bundle that is about to be held on set
    # membership — the human would pay for it again on the retry after registering the row.
    deps = list(dependency_reasons(d, cfg))
    if blocking(deps):
        return deps
    return list(size_reasons(d, cfg, before_do=before_do)) + deps
