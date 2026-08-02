"""The driver — a thin, deterministic loop over a bundle's file-state (docs 03).

No model in the control path: ``state`` / ``advance`` / ``run_issue`` are pure
code, and the two model leaves are reached only inside :mod:`pdca_harness.leaves`.
The driver advances an issue beat by beat, writing each artifact, and STOPS at
AWAITING_SIGNOFF (the human touch point). The iterate transitions deliberately
**archive** the previous attempt into ``iteration-v<N>/`` (never delete it) so a
rebuild starts clean while the rejected attempt is preserved; on iterate-to-Plan
the ``brief.md`` is archived with it (state → UNPLANNED) for the re-authoring human.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from . import (assemble, brief, dependency_halt, gates, leaves, plan_policy, signoff,
               size_signal, state)
from .config import Config


def _say(msg: str) -> None:
    """Per-beat progress to stderr, so a headless leaf or a slow gate never looks hung."""
    print(msg, file=sys.stderr, flush=True)


def _headless_note(leaf) -> str:
    return " (headless Claude — no live output, may take minutes)" if leaf.mode == "command" else ""

# Moved to `state` (#334) so `is_resolved` can read it without a circular import.
# Re-exported here because this is where the archive step and its callers expect it.
DOWNSTREAM_OF_BRIEF = state.DOWNSTREAM_OF_BRIEF
DOWNSTREAM_GLOBS = state.DOWNSTREAM_GLOBS


def advance(d: Path, cfg: Config) -> None:
    """Run the one beat the bundle's current state calls for."""
    s = state.state(d)
    close = _close_class(d, cfg)
    # Pre-dispatch policy (#321). Evaluated here because every path into Do converges on
    # this function — single flow, the zero-id sweep, explicit ids, and `pdca run` — and
    # recomputed each beat so a fix (registering a doctor row, retuning [driver.sizing])
    # takes effect immediately instead of being pinned by a stale marker.
    #
    # AFTER `_close_class`: a close-disposition bundle skips builder and reviewer
    # entirely, so advising a split on a duplicate/wontfix/split parent would be noise
    # about work that never enters Do. BUILT is covered as well as PLANNED: a partial
    # build lands there and never re-enters PLANNED, and Check is a real spend too.
    if not close and s in (state.PLANNED, state.BUILT):
        # The paid sizer runs only before Do. The blocking dependency check and the free
        # structural estimate still run at BUILT — a resumed or partially-built bundle
        # must not buy a reviewer at xhigh plus an adversary to discover something two
        # files already answer.
        reasons = plan_policy.evaluate(d, cfg, before_do=(s == state.PLANNED))
        for reason in reasons:
            _say(f"⚠ {d.name}: {reason.detail}")
        # A BLOCKING reason stops the beat; advisories are reported and passed. Only a
        # deterministic verdict earns a block — the unregistered dependency is set
        # membership (#333), where the size band is a heuristic that peaks at 62%
        # precision (#321). The bundle stays in-flight, so registering the row and
        # re-running is all it takes: the policy is recomputed every beat.
        blockers = plan_policy.blocking(reasons)
        if blockers:
            _say(f"→ {d.name}: held before {'Do' if s == state.PLANNED else 'Check'} — "
                 f"{len(blockers)} blocking item(s) above; resolve, then re-run.")
            raise plan_policy.PolicyHold(blockers)
    if s == state.PLANNED:
        if close:
            _say(f"→ {d.name}: close disposition '{close}' — skipping builder leaf (no patch to build)…")
            _do_close(d, cfg, close)  # write the close marker + breadcrumb instead of leaf 1
        else:
            _say(f"→ {d.name}: Do — builder writing patch.diff + test{_headless_note(cfg.builder)}…")
            leaves.do_build(d, cfg)  # leaf 1 — Do
    elif s == state.BUILT:
        # Do-exit adjudication (#341): a builder that honestly declared an unmet external
        # dependency (`NEEDS-HUMAN external dependency:` in build-notes.md, the builder-
        # contract marker) must not buy the full Check beat — gates, cross-vendor
        # reviewer, adversary — for a patch it already STATED is unverifiable; and a
        # dishonest claim must not skip review. `adjudicate` is None unless the opt-in
        # `[driver].dependency_halt` is on AND the marker is present; only a claim its
        # detect-cmd probe CONFIRMS (non-zero exit) reroutes the beat. The verdict is
        # recorded on BOTH outcomes, so a refutation reaches §6 / `pdca act index` too.
        verdicts = None if close else dependency_halt.adjudicate(d, cfg)
        if verdicts is not None:
            dependency_halt.record(d, verdicts)
        if close:
            _say(f"→ {d.name}: close disposition — recording N/A gates, skipping reviewer leaf…")
            gates.run_close_gates(d, cfg)  # deterministic N/A matrix, no gate subprocess
            _close_review_note(d, close)   # stand-in for leaf 2 — close-confirm → §6
        elif dependency_halt.confirmed(verdicts):
            # The existing close fast path, one beat later: N/A matrix + a review
            # stand-in whose NEEDS-HUMAN bullets route to §6, then CHECKED → assemble →
            # AWAITING_SIGNOFF. Never a terminal state — sign-off owns those — and the
            # bundle is resumable: iterate-do archives this attempt (the adjudication
            # record and this note are DOWNSTREAM_OF_BRIEF) and reruns the full band.
            _say(f"→ {d.name}: builder-declared external dependency confirmed absent — "
                 "recording N/A gates, skipping reviewer leaf (provide it, then "
                 "iterate-do to resume)…")
            gates.run_close_gates(d, cfg)  # deterministic N/A matrix, no gate subprocess
            dependency_halt.blocked_review_note(d, verdicts)  # stand-in for leaf 2 → §6
        else:
            _say(f"→ {d.name}: Check — running gates…")
            gates.run_gates(d, cfg)  # deterministic gates
            # Measure what the patch ACTUALLY came to, before the reviewer runs (#324).
            # Recorded here rather than recomputed at assembly so §6 and any later audit
            # read the same numbers the decision was made on. Advisory only: it raises a
            # §6 HUMAN item, and the human decides whether to split.
            _size_backstop(d, cfg)
            _say(f"→ {d.name}: Check — advisory reviewer{_headless_note(cfg.reviewer)}…")
            leaves.run_review(d, cfg)  # leaf 2 — reviewer (advisory)
            if cfg.advisory_leaves:  # optional extra advisory reviewers (issue #64)
                _say(f"→ {d.name}: Check — advisory reviewers ({len(cfg.advisory_leaves)})…")
                leaves.run_advisory_leaves(d, cfg)
    elif s == state.CHECKED:
        # Trap-door recovery (#369): `check-gates.json` IS the CHECKED marker
        # (state.state), but the BUILT branch above runs gates → reviewer → advisory
        # leaves as ONE indivisible step. A death in the window between the gate write
        # and a model leaf (Ctrl-C, OOM, a killed session) landed the bundle HERE with
        # that leaf never run — and nothing ever ran it again: assemble filled the
        # missing-review placeholder and the only escape was hand-deleting
        # check-gates.json, re-paying the entire gate run. Recover the never-ran leaf
        # (artifact AND error log both absent — the #138 failed-leaf discriminator)
        # before assembly, preserving the paid gate record; a leaf that ran and FAILED
        # left its error log and is NOT re-run. An uninterrupted cycle has every
        # artifact in place, so this is a no-op there.
        _resume_interrupted_check(d, cfg, close)
        _say(f"→ {d.name}: assembling SUMMARY…")
        assemble.assemble_summary(d, cfg)  # pure code → SUMMARY.md §1–8
    elif s == state.ITERATE_DO:
        n = _next_iteration_no(d)
        _say(f"→ {d.name}: iterate-to-Do — archiving the attempt to iteration-v{n}/, rebuilding…")
        _carry_forward_into_brief(d, n)  # fold prior insight into the surviving brief
        _archive_iteration(d, n, include_brief=False)  # rebuild against the annotated brief
    elif s == state.ITERATE_PLAN:
        n = _next_iteration_no(d)
        _say(f"→ {d.name}: iterate-to-Plan — archiving the attempt to iteration-v{n}/, re-planning…")
        _carry_forward_into_brief(d, n)  # appended to the brief, archived with it
        _archive_iteration(d, n, include_brief=True)  # brief archived too → UNPLANNED
    # UNPLANNED / AWAITING_SIGNOFF / COMPLETE / DISCONTINUED: nothing for the driver to do.


def _resume_interrupted_check(d: Path, cfg: Config, close: str) -> None:
    """Recover a Check leaf the interrupted BUILT beat never ran (#369) — see the
    CHECKED dispatch in :func:`advance`.

    Three shapes, matching the three BUILT branches. A close-class or
    dependency-halted bundle never runs model leaves — its reviewer stand-in note is
    deterministic (``_close_review_note`` / ``dependency_halt.blocked_review_note``),
    so a note the death window swallowed is simply rewritten. A normal bundle gets the
    reviewer leaf back iff it NEVER RAN (``leaves.review_never_ran`` — no artifact,
    no error log) and each configured advisory leaf back under the same discriminator
    (``only_missing``); a leaf that ran and failed left its error log (#138) and is
    left alone — today's behaviour for a failed leaf is unchanged.
    """
    if close:
        if not (d / "check-review.md").exists():
            _say(f"→ {d.name}: close-disposition review note missing (beat was "
                 "interrupted before it was written) — rewriting it…")
            _close_review_note(d, close)
        return
    rec = dependency_halt.load(d)
    if rec is not None and rec.get("halted") is True:
        if not (d / "check-review.md").exists():
            _say(f"→ {d.name}: dependency-halt review note missing (beat was "
                 "interrupted before it was written) — rewriting it…")
            dependency_halt.blocked_review_note(d, dependency_halt.recorded_verdicts(d))
        return
    if leaves.review_never_ran(d):
        _say(f"→ {d.name}: Check — reviewer never ran (beat was interrupted after the "
             f"gate write); recovering it{_headless_note(cfg.reviewer)}…")
        leaves.run_review(d, cfg)
    if cfg.advisory_leaves:
        leaves.run_advisory_leaves(d, cfg, only_missing=True)


def run_issue(d: Path, cfg: Config) -> str:
    """Advance until the bundle halts OR a pre-dispatch policy holds it; return its state.

    Two exits, not one. The ordinary exit is a state in :data:`state.HALTED`. The other is
    a :class:`plan_policy.PolicyHold` — an unregistered external dependency, say — which
    leaves the bundle in-flight at PLANNED or BUILT: nothing about a hold changes the state
    it is held in, so the caller gets a NON-halted state back and must not read it as
    completion. :func:`held` answers that question for callers that care (``pdca run``
    exits non-zero on it, so automation does not read a blocked run as a success).
    """
    while state.state(d) not in state.HALTED:
        try:
            advance(d, cfg)
        except plan_policy.PolicyHold:
            # The bundle stays in-flight in a NON-halted state, so the loop must exit here
            # or spin forever: nothing about a hold changes the state it is held in.
            break
    return state.state(d)


def held(final: str) -> bool:
    """True if ``final`` came back from :func:`run_issue` without the bundle finishing.

    A non-halted state can ONLY mean a policy hold — the loop has no other early exit — so
    this is the one predicate a caller needs to tell "stopped for a human" from "done".
    """
    return final not in state.HALTED


# ----------------------------------------------------------------------------
# Close-disposition fast path (issue #60) — skip the speculative build for a bundle
# whose Plan already concluded a close / no-fix outcome. It elides the two model
# leaves (the engine's only token spend); it does NOT decide the disposition — the
# human confirms or overrides the close at sign-off (C6 forces a conscious confirm).
# ----------------------------------------------------------------------------
def _close_class(d: Path, cfg: Config) -> str:
    """The close class for a bundle taking the fast path, or "" for the normal Do path.

    Active iff the brief's Disposition hint matches a configured close class AND this is
    the FIRST attempt (no ``iteration-v*`` archive). The first-attempt guard keeps it a
    hint, not a gate: reopening to a fix path (iterate-do/-plan) archives the close marker
    and leaves an iteration behind, so the next pass returns "" and runs the real build.
    """
    # An EXISTING close marker wins outright (#323). The first-attempt guard below applies
    # to the brief's *hint*, which is advisory — but the marker is written by the driver
    # (or by `pdca split --accept`) and is a decision already taken. Without this, a split
    # parent could never take the close path: the realistic one has an `iteration-v*`
    # archive, because it failed an attempt before anyone concluded it was too large.
    #
    # Reopening still works: an iterate archives the marker (it is in DOWNSTREAM_OF_BRIEF),
    # so the next pass falls through to the hint path and runs a real build.
    marker = d / state.CLOSE_MARKER
    if marker.exists():
        try:
            recorded = marker.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        if recorded:
            return recorded
    bp = d / "brief.md"
    if not bp.exists() or list(d.glob("iteration-v*")):
        return ""
    return cfg.close_class(brief.disposition_hint(bp))


def _do_close(d: Path, cfg: Config, close_class: str) -> None:
    """Stand in for the Do builder leaf: write the close marker + an audit breadcrumb.

    The marker is the bundle's Do artifact (state reads it as past Do). build-notes.md
    records WHY no patch exists, so a frozen close bundle never looks like an incomplete
    Do. A manual-verification close also seeds MANUAL-VERIFICATION.md for the human.
    """
    (d / state.CLOSE_MARKER).write_text(close_class + "\n", encoding="utf-8")
    (d / "build-notes.md").write_text(
        "# Build notes — NO PATCH (close disposition)\n\n"
        f"Leaves skipped: disposition={close_class}. The Plan concluded a close / no-fix "
        "outcome, so the builder and reviewer model leaves were NOT run — there is nothing "
        "to build. The human confirms or overrides the close at sign-off; reopening to a "
        "fix path (iterate-to-Do) re-enables the full Do+Check band.\n",
        encoding="utf-8",
    )
    if _is_manual_verification(close_class):
        tpl = cfg.templates_dir / "MANUAL-VERIFICATION.md.tpl"
        dst = d / "MANUAL-VERIFICATION.md"
        if tpl.exists() and not dst.exists():
            shutil.copyfile(tpl, dst)


def _close_review_note(d: Path, close_class: str) -> None:
    """Stand in for the advisory reviewer leaf on a close bundle: write check-review.md.

    No patch means nothing to review, but the human must still consciously confirm the
    close. The ``- NEEDS-HUMAN —`` bullets parse into SUMMARY §6 (assemble._needs_human),
    so the C6 accept-guard blocks accept until the human ticks them — confirming the close
    or overriding it via iterate-to-Do.
    """
    lines = [
        "# Advisory review — SKIPPED (close disposition)\n",
        f"The reviewer leaf was skipped: this bundle's Plan concluded a close / no-fix "
        f"disposition ({close_class}), so there is no patch to review.\n",
        f"- NEEDS-HUMAN — Confirm the close disposition '{close_class}' (no patch was "
        "built). Override to a fix path (iterate-to-Do) if the close is wrong.",
    ]
    if _is_manual_verification(close_class):
        lines.append(
            "- NEEDS-HUMAN — Complete MANUAL-VERIFICATION.md and record the verdict "
            "(the manual check the gates cannot run).")
    (d / "check-review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_manual_verification(close_class: str) -> bool:
    return "manual" in close_class.lower()


# ----------------------------------------------------------------------------
# Iterate transitions — a deliberate ARCHIVE, not a delete: the previous attempt is
# moved into iteration-v<N>/ so a rejected attempt is preserved, never lost.
# ----------------------------------------------------------------------------
def _size_backstop(d: Path, cfg: Config) -> None:
    """Record the empirical size signal and say so when it has crossed a threshold.

    The §6 item is raised by `assemble.collect_needs_human`, not here — one classifier, so
    the rendered §6 and the auto-iterate decision can never disagree. This writes the
    evidence and gives the operator the same warning at the point it is measured.
    """
    reasons = size_signal.oversize_reasons(size_signal.record(d, cfg), cfg)
    if reasons:
        _say(f"→ {d.name}: size backstop — {'; '.join(reasons)}. "
             "Raising a §6 NEEDS-HUMAN item; auto-iterate will decline.")


def _next_iteration_no(d: Path) -> int:
    """Next iteration number = (count of existing iteration-v* archives) + 1."""
    return len(list(d.glob("iteration-v*"))) + 1


# ----------------------------------------------------------------------------
# Iterate carry-forward — persist the WHY into the one input the next beat reads.
# ----------------------------------------------------------------------------
def _carry_forward_into_brief(d: Path, n: int) -> None:
    """Fold the previous iteration's insight into ``brief.md`` BEFORE the attempt is
    archived — so the next Do/Plan isn't blind. On iterate-do the brief stays at the
    top level (the rebuild reads it); on iterate-plan the annotated brief is archived
    with the attempt for the re-authoring human.

    Captures whatever is available — the §9 sign-off rationale AND the failing gates
    (gating *and* advisory, since an iterate is often driven by an advisory red), so
    an iterate with no recorded rationale still carries context. Since #331 it also
    MERGES the session-captured carry-forward (``state.SESSION_CARRY`` — the FULL
    multi-line rationale the sign-off session wrote below its decision token, captured
    live by ``flow._apply_decision`` before the decision file is unlinked): §9's
    "Iteration delta" is a single flattened line, so reading recorded artifacts only
    lost every structure the session put into its rationale. Best-effort: it must
    never break the transition, so any failure is swallowed.
    """
    brief_path = d / "brief.md"
    if not brief_path.exists():
        return
    try:
        delta = signoff.iteration_delta(d / "SUMMARY.md")
        session_notes = _session_carry_forward(d)
        fails = _failing_gate_lines(d / "check-gates.json")
        if not delta and not fails and not session_notes:
            return
        out = [f"\n## Iteration {n} — carry-forward (from the previous attempt)\n"]
        if delta:
            out.append(f"- Sign-off rationale: {delta}\n")
        # The session capture is merged, not duplicated: a single-line rationale is
        # byte-identical to the flattened §9 delta, so only a capture carrying MORE
        # (extra lines / structure §9 flattened away) earns its own block.
        if session_notes and ("\n" in session_notes
                              or " ".join(session_notes.split()) != delta):
            out.append("- Sign-off session carry-forward (captured live, before §9 "
                       "flattened it):\n")
            out += [f"  {ln}\n" if ln.strip() else "\n"
                    for ln in session_notes.splitlines()]
        for f in fails:
            out.append(f"- Failing gate: {f}\n")
        out.append(f"- Full previous attempt preserved in `iteration-v{n}/` "
                   "(patch.diff, build-notes.md, SUMMARY.md, check-*).\n")
        out.append("- Address the above; do NOT re-attempt the rejected approach "
                   "unchanged. Satisfy the brief's Success criterion (the end result).\n")
        with brief_path.open("a", encoding="utf-8") as fh:
            fh.write("".join(out))
    except Exception:  # noqa: BLE001 — carry-forward is advisory; never break the iterate
        pass


def _session_carry_forward(d: Path) -> str:
    """The session-captured sign-off carry-forward (``state.SESSION_CARRY``), or "".

    The file is written by ``flow._apply_decision`` at the moment the sign-off session's
    decision is consumed (issue #331 — the registering side of the channel; this is the
    consuming side, shipped together). Best-effort like every carry-forward input."""
    p = d / state.SESSION_CARRY
    try:
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except OSError:
        return ""


def _failing_gate_lines(gates_json: Path) -> list[str]:
    """``"check — evidence"`` for each failing row in ``check-gates.json`` — gating AND
    advisory, since an iterate is often driven by an advisory red. Best-effort."""
    if not gates_json.exists():
        return []
    try:
        data = json.loads(gates_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    out: list[str] = []
    for r in data.get("rows", []):
        if r.get("result") == "fail":
            ev = r.get("path_line") or r.get("oracle") or ""
            tag = "" if r.get("gating") else " (advisory)"
            out.append(f"{r.get('check', '?')}{tag} — {ev}".strip(" —"))
    return out


def _within(p: Path, parent: Path) -> bool:
    try:
        p.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _archive_iteration(d: Path, n: int, *, include_brief: bool) -> None:
    """Move the previous attempt's artifacts into ``d/iteration-v<N>/`` rather than
    deleting them: the Do+Check downstream always, plus ``brief.md`` on iterate-plan
    (so state() → UNPLANNED and the human re-authors a fresh brief). Most tests ride
    in patch.diff; a test file written *into the bundle* is archived too. External
    paths (e.g. a sibling repo's test) are left untouched, never deleted.
    """
    arch = d / f"iteration-v{n}"
    names = list(DOWNSTREAM_OF_BRIEF)
    # Pattern-matched cycle artifacts: the advisory files (#64) and every leaf's captured
    # error tail — `build.error.log` (Do), `check-review.error.log` /
    # `check-advisory-*.error.log` (Check). Each tail is cleared at the start of the NEXT
    # run of its leaf, so one left at the top level here would be deleted rather than kept,
    # destroying the only on-disk record of why the attempt failed (#280 review). Archive
    # them with their attempt.
    #
    # The patterns come from `state.DOWNSTREAM_GLOBS`, the same constant `is_resolved`
    # reads (#334), so "what an iterate archives" and "what proves a cycle ran" cannot
    # drift apart.
    for pattern in DOWNSTREAM_GLOBS:
        names += [p.name for p in d.glob(pattern)]
    if include_brief:
        names.append("brief.md")
        # The plan-advisory artifacts + benefit record reviewed THAT brief (#301) —
        # archive them with it so the re-plan (and its fresh review) starts clean.
        names += [p.name for p in d.glob("plan-advisory-*")]
    if (d / "brief.md").exists():
        for tf in brief.test_files(d / "brief.md"):
            p = d / tf
            if p.is_file() and _within(p, d):
                names.append(str(tf))
    for name in names:
        src = d / name
        # A directory entry (state.GATE_LOGS_DIR, #370 — the round's gate evidence logs)
        # is moved whole: each iteration-v<N>/ keeps the full basis of its own verdict.
        if src.is_file() or src.is_dir():
            arch.mkdir(parents=True, exist_ok=True)
            src.rename(arch / Path(name).name)
