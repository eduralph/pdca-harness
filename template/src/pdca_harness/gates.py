"""Deterministic Check gates → ``check-gates.json`` (docs 02 / 04, the gates path).

The gates are the *only* blocking path in Check — no model in the gating loop.
**Single-sourcing** (docs 04 §Single-sourcing) is the load-bearing property: the
gate *commands* live once in ``pdca.toml`` ``[[gates.checks]]``, and the same
``pdca gates`` entry point runs them for the local driver (over a bundle) and for
CI (over the PR working tree). There is no second implementation to drift.

Each configured check: ``{id, tier, label, cmd, gating, scope, target?}`` where
``scope`` is ``"repo"`` (runs against the working tree — what CI re-runs) or
``"bundle"`` (needs the bundle/patch context — local only), and the optional
``target`` (a project label or list of labels, e.g. ``"core"`` / ``["addon",
"frontend"]``) runs the check only when those labels are a SUBSET of the bundle's
label set (subset = AND). The bundle is classified from its brief: a primary axis
(``[gates] target_default`` + ``[gates.target_match]``) plus additive flags
(``[gates.target_flags]``); unset ⇒ no filtering. A check passes iff its ``cmd``
exits 0, fails on any other exit, and may instead declare itself **unverifiable**
when it genuinely cannot run its mechanical check (issue #46): exit
:data:`UNVERIFIABLE_RC` (77, the automake SKIP convention) **or** *declare* it by printing
a line that STARTS with :data:`UNVERIFIABLE_MARKER` (``PDCA-UNVERIFIABLE: <reason>``;
leading whitespace ignored) **while exiting 0 or 77** — the marker lets a gate that did NOT
fail defer to the human. It counts only on a non-failing exit, because a gate that failed
has failed whatever it printed (#329), and only at the start of a line, because a mid-line
occurrence is text the gate merely RELAYED (a child's log line, a quoted source comment),
not a verdict the gate declared (#428). When
``[[gates.checks]]`` is empty the driver falls back to all-PASS stub rows, so the
offline vertical slice still runs.

The row's **evidence line** follows the same declaration rule (issue #402): a gate states
its verdict summary by printing a line that STARTS with :data:`EVIDENCE_MARKER`
(``PDCA-EVIDENCE: <summary>``), and that summary — the LAST such line, a gate's final word
— becomes the row's ``path_line`` whatever the command relays afterwards. Without a
declaration the evidence falls back to the command's last output line, which is only ever
the gate's verdict by luck: the capture is one merged stdout+stderr stream, so a wrapper
that shells out to a suite files whatever that suite's children happened to flush last
(a scratch ``/tmp`` path from a since-deleted sandbox is not a reconstructable basis). The
marker declares evidence only — it never changes a verdict; the exit code alone decides
pass/fail, and only the ``PDCA-UNVERIFIABLE``/``PDCA-DEFERRED`` declarations can change
a ``result``.

A row: {check, result, oracle, rule_id, path_line, gating}. A row produced by a
bundle-scoped :func:`run_gates` additionally carries ``log`` (the bundle-relative path of
its full-output evidence log, ``gate-logs/<rule_id>.log``) and ``duration_secs`` (issue
#370) — additive keys, existing consumers unchanged. When that evidence log could NOT be
written, the row instead carries ``log_error`` (the reason) so a persistence failure is
never silent — the verdict itself is unaffected either way. ``result`` ∈
``pass`` / ``fail`` / ``unverifiable`` / ``deferred`` / ``none``. A ``none`` row is a
judgment cell decided by the reviewer + human (docs 04 §judgment cell); it is listed for
matrix alignment and never gates. An ``unverifiable`` row does **not** count toward
``overall`` (it is not a failure); the driver routes it into SUMMARY §6 NEEDS-HUMAN,
where the C6 accept-guard forces the human to clear it before sign-off.

A **``deferred``** row (issue #401) is the fourth member: the gate RAN and found its
subject **absent by design**, because the artifacts it audits are drafted later — the
Check-time run of a bundle-scoped T4 contribution row, whose ``commit-msg.txt`` /
``pr-description.md`` do not exist until publish. It is declared the same way
``unverifiable`` is — a line the gate STARTS with :data:`DEFERRED_MARKER`
(``PDCA-DEFERRED: <reason>``) while exiting 0 — and, like ``unverifiable``, it does not
count toward ``overall``. Unlike ``unverifiable`` it is **not** lifted into SUMMARY §6:
the condition is by-design and its substantive verdict is owed to a later gate, so a §6
checkbox on every cycle trains the human to tick §6 unread — the guard C6 depends on.
The deferral is honoured only for a row that is genuinely **re-gated later**
(:func:`_deferrable` → ``publish.publish_gates``); a row nothing re-runs has no later
verdict to defer to and keeps its pass/fail.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brief, lane, progress, state, worktree
from .config import Config

# A gate that cannot RUN its mechanical check (vs. running and failing) declares so:
# exit 77 (automake SKIP convention) or a marker line. The marker takes precedence —
# a gate may exit 0 and still defer to the human. Neither is a failure (see _finalize).
UNVERIFIABLE_RC = 77
UNVERIFIABLE_MARKER = "PDCA-UNVERIFIABLE:"

# A gate states the summary that goes into the row's `path_line` the same way it declares
# `unverifiable`: a line that STARTS with this marker (issue #402). Anything else in the
# capture is output the gate RELAYED from what it ran, and must not be filed as its verdict.
EVIDENCE_MARKER = "PDCA-EVIDENCE:"

# A gate that ran and found its subject ABSENT BY DESIGN — the audit it performs has no
# subject yet, because the artifacts it lints are drafted later — declares the deferral
# with this marker while exiting 0 (issue #401). Same declaration rule as the two above:
# only at the start of a line, never a mid-line quotation the gate relayed (#428). Neither
# a pass nor a failure (see _finalize), and NOT routed to §6 (see assemble): the verdict is
# owed to the later gate that re-runs the row (see _deferrable).
DEFERRED_MARKER = "PDCA-DEFERRED:"

# The bundle directory holding one full-output evidence log per gate rule (issue #370).
# Defined in `state` (next to the archive list that moves it per round) — re-exported
# here because gates is the writer.
GATE_LOGS_DIR = state.GATE_LOGS_DIR


# ----------------------------------------------------------------------------
# Gate-promotion lifecycle (issue #156): a check may carry ``promote_after = N``; once it
# has PASSED in its N most-recent frozen cycles it has earned promotion from advisory to
# gating. ``pdca gates --promotions`` lists the ready ones — hint-only, the human flips
# ``gating`` (nothing is auto-mutated). De-risks a new (often Act-proposed) gate, which
# should prove itself advisory before it is allowed to block.
# ----------------------------------------------------------------------------
GATES_JSON = "check-gates.json"
_PROMO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _gates_record(d: Path) -> dict | None:
    """A bundle's frozen ``check-gates.json``, or None if absent/unreadable."""
    p = d / GATES_JSON
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


_SIGNOFF_SECTION = re.compile(r"^##[^\n]*Check sign-off[^\n]*\n(.*?)(?=^##\s|\Z)",
                              re.MULTILINE | re.DOTALL)


def _signoff_date(d: Path) -> str:
    """A recency key for ordering frozen cycles — the ISO date in the §9 (``Check
    sign-off``) section of SUMMARY.md, or "" when none. Scoped to §9 so an ISO date in an
    earlier section (a brief/citation date) can't mis-order cycles and report a check ready
    whose most-recent sign-off run actually failed."""
    s = d / "SUMMARY.md"
    if not s.exists():
        return ""
    block = _SIGNOFF_SECTION.search(s.read_text(encoding="utf-8"))
    dm = _PROMO_DATE.search(block.group(1)) if block else None
    return dm.group(1) if dm else ""


def _check_result(rec: dict, check_id: str) -> str | None:
    """The result this gate record holds for ``check_id`` (``pass`` / ``fail`` /
    ``unverifiable``), or None when the check didn't run / isn't recorded."""
    for row in rec.get("rows", []):
        if row.get("rule_id") == check_id:
            res = row.get("result")
            return res if res in ("pass", "fail", "unverifiable") else None
    return None


def promotion_candidates(cfg: Config) -> list[dict]:
    """Advisory checks (``gating = false``) carrying ``promote_after = N`` that have PASSED
    in their N most-recent frozen runs — earned promotion to gating. Each:
    ``{id, label, threshold}``. Hint-only; the human flips ``gating``."""
    advisory = [c for c in cfg.gates_checks
                if c.get("promote_after") and not bool(c.get("gating", True))]
    if not advisory or not cfg.bundle_root.exists():
        return []
    frozen = sorted((d for d in cfg.bundle_root.glob("issue_*")
                     if d.is_dir() and state.state(d) == state.COMPLETE),
                    key=_signoff_date, reverse=True)  # newest first
    records = [rec for rec in (_gates_record(d) for d in frozen) if rec]
    out: list[dict] = []
    for chk in advisory:
        try:
            n = int(chk["promote_after"])
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        ran: list[str] = []
        for rec in records:
            res = _check_result(rec, chk.get("id", ""))
            if res is not None:
                ran.append(res)
            if len(ran) >= n:
                break
        if len(ran) >= n and all(r == "pass" for r in ran[:n]):
            out.append({"id": chk.get("id", ""), "label": chk.get("label", ""),
                        "threshold": n})
    return out


def run_gates(d: Path, cfg: Config) -> dict:
    """Run every gate for bundle ``d`` (both repo- and bundle-scoped); write JSON.

    The FULL output of each check is persisted to ``<d>/gate-logs/<rule_id>.log``
    (issue #370): the 120-char ``path_line`` is the right *summary*, but it must not be
    the entire *record* — a gating red that parks the bundle needs its whole basis
    reconstructable from bundle files alone (the state-is-files doctrine). One file per
    rule id, overwritten per Check run."""
    rows = _run_checks(cfg, cwd=cfg.root, bundle=d, scopes=("repo", "bundle"),
                       log_dir=d / GATE_LOGS_DIR)
    return _finalize(rows, name=d.name, write_to=d)


def _close_matrix_rows() -> list[dict]:
    """The 5/5/1 for a close-disposition bundle: every gate element N/A (no patch to
    verify). Each gate element is a non-gating ``none`` row, so ``overall`` = pass."""
    rows = _assemble_matrix([], stub=False)
    for r in rows:
        if r["oracle"] == "(no gate configured)":
            r["path_line"] = "N/A — close disposition (no patch to verify)"
    return rows


def run_close_gates(d: Path, cfg: Config) -> dict:
    """Write a Check matrix for a close-disposition bundle WITHOUT running any gate.

    A close / no-fix bundle (issue #60) has no patch.diff, so every gate element is
    N/A: there is nothing to verify. No gate command is executed — the gate
    *definitions* are unchanged (C4-verify is simply inapplicable). The human confirms
    the close at sign-off, not a gate.
    """
    return _finalize(_close_matrix_rows(), name=d.name, write_to=d)


def run_close_gates_dry(d: Path, cfg: Config) -> dict:
    """The close matrix WITHOUT writing the frozen file — the revalidate counterpart of
    :func:`run_close_gates` (so re-gating a frozen close bundle confirms, not drifts)."""
    return _finalize(_close_matrix_rows(), name=d.name, write_to=None)


def run_working_tree(cfg: Config) -> dict:
    """Run only repo-scoped gates against the working tree (the CI merge re-gate)."""
    rows = _run_checks(cfg, cwd=cfg.root, bundle=None, scopes=("repo",))
    return _finalize(rows, name="working-tree", write_to=None)


def run_integration(cfg: Config, worktree_path: Path, *, hold_lock: bool = True) -> dict:
    """Run the repo-scoped gates against a wave integration worktree (#wave-model re-gate).

    Like :func:`run_working_tree`, but targeted at an explicit tree — the folded
    integration tip the *next* wave will build on. The gate commands run from it and see it
    as ``$PDCA_WORKTREE``, so a project's repo-scoped gate validates the *combination* of
    the waves so far: a result that is red though each fix was green alone means the
    caller STOPs before building the next wave on it. Never writes a frozen record.

    Runs under the tree's lifecycle lock (#297 review round 6): a concurrent
    ``pdca sweep`` — or another flow's publish-boundary sweep — must not remove the
    worktree mid-gate and invalidate this re-gate's result. ``hold_lock=False`` is
    for a caller that ALREADY holds this tree's lock continuously across fold and
    re-gate (the flow's ``locks`` stack, #297 review round 10) — re-acquiring here
    would deadlock against our own held flock, and releasing between fold and
    re-gate was exactly the gap another flow's sweep could remove the tree in."""
    from . import integrate  # lazy: gates is imported by integrate's callers
    with contextlib.ExitStack() as scope:
        if hold_lock:
            held = scope.enter_context(integrate.integ_lock(worktree_path))
            if not held:
                # Fail CLOSED (#297 review round 7): an unserialized re-gate could
                # read a tree a concurrent fold is rewriting — it would attest nothing.
                raise integrate.IntegrationError(
                    f"could not take the integration lock next to {worktree_path.name} "
                    f"— the re-gate cannot attest an unserialized tree")
        rows = _run_checks(cfg, cwd=worktree_path, bundle=None, scopes=("repo",),
                           worktree_override=worktree_path)
    return _finalize(rows, name="integration", write_to=None)


def run_gates_dry(d: Path, cfg: Config) -> dict:
    """Run every gate for bundle ``d`` against the CURRENT engine WITHOUT writing the
    frozen ``check-gates.json`` — the gate runner behind ``pdca revalidate`` (issue #11).

    Same single-sourced ``_run_checks`` as :func:`run_gates`, but ``write_to=None`` so a
    re-gate of an already-COMPLETE bundle never mutates its frozen record. For the same
    reason no ``log_dir`` is passed (issue #370): ``gate-logs/`` is the frozen evidence
    behind the frozen verdict, and a later dry re-gate must not overwrite it either."""
    rows = _run_checks(cfg, cwd=cfg.root, bundle=d, scopes=("repo", "bundle"))
    return _finalize(rows, name=d.name, write_to=None)


# ----------------------------------------------------------------------------
def _bundle_target(
    bundle: Path | None,
    match: dict[str, str],
    default: str,
    flags: dict[str, dict[str, str]] | None = None,
) -> frozenset[str] | None:
    """The bundle's gate-target label SET, or ``None`` when filtering doesn't apply.

    Two config-driven axes, both keyed off the bundle's brief:
      * **primary** — ``match`` maps a label → substring matched case-insensitively
        against the "Repo + branch target" field; first hit wins, else ``default``.
        Mutually-exclusive (e.g. core vs addon).
      * **flags** — additive labels: ``flags`` maps a label → ``{field, substring}``
        matched against any brief field (e.g. ``frontend`` ← a "Surfaces" field). Each
        match adds its label.

    Returns ``None`` when there's no bundle (CI working-tree re-gate) or no config at all
    — so an unconfigured project keeps running every gate. Filtering only ever *removes*
    an inapplicable gate, never adds one.
    """
    flags = flags or {}
    if bundle is None or (not match and not flags):
        return None
    brief_path = bundle / "brief.md"
    labels: set[str] = set()

    primary = None
    if match:
        target_field = brief.field(brief_path, "repo + branch target", "repo + branch").lower()
        for label, needle in match.items():
            if needle and needle.lower() in target_field:
                primary = label
                break
        primary = primary or default
    if primary:
        labels.add(primary)

    for label, rule in flags.items():
        field_name = rule.get("field", "repo + branch target")
        needle = rule.get("substring", "")
        if needle and needle.lower() in brief.field(brief_path, field_name).lower():
            labels.add(label)

    return frozenset(labels) or None


def _applies(chk: dict, scopes: tuple[str, ...], labels: frozenset[str] | None) -> bool:
    """True iff ``chk`` should run for this scope set and bundle label set. A check with
    no ``target`` always applies; a ``target`` (a label or list of labels) runs iff its
    labels are a SUBSET of ``labels`` (subset = AND). ``labels is None`` ⇒ unknown ⇒ run,
    never over-skip."""
    if chk.get("scope", "repo") not in scopes:
        return False
    tgt = chk.get("target")
    if not tgt or labels is None:
        return True
    want = {tgt} if isinstance(tgt, str) else set(tgt)
    return want <= labels


def _run_checks(cfg: Config, *, cwd: Path, bundle: Path | None, scopes: tuple[str, ...],
                worktree_override: Path | None = None,
                log_dir: Path | None = None) -> list[dict]:
    # No configured gates → the offline stub: the full 5/5/1 with the mechanical
    # gate elements stub-passed (so the offline slice runs green). A declared
    # [gates] host_ci row counts as real configuration too (#311).
    if not cfg.gates_checks and not cfg.host_ci_checks:
        return _assemble_matrix([], stub=True)

    labels = _bundle_target(bundle, cfg.gate_target_match, cfg.gate_target_default, cfg.gate_target_flags)
    # Worktree isolation (issue #94): if Do ran in an isolated worktree, gates test THAT
    # tree — expose it as $PDCA_WORKTREE so a gate cmd targets it, not the host checkout.
    # ``worktree_override`` (the wave integration re-gate, #wave-model) points the
    # repo-scoped gates at an explicit tree (the folded integration tip) instead.
    # Resolve the tree the gates read. `for_gate` (issues #226/#296) RECONSTRUCTS the lane
    # as base + patch.diff on every gating read — the lane is a warm checkout cache, never a
    # trusted content cache — and, when a DIFFERENT bundle owns the lane, spills to an
    # ephemeral OVERFLOW tree (when `[driver].overflow` > 0) instead. An overflow tree
    # (`ovf_primary` not None) is torn down in the finally once the gates run. A tree that
    # cannot be made to match patch.diff raises WorktreeError: fail CLOSED with a synthetic
    # gating red — never run gates over mismatched content, never emit green for it (#296).
    # The lane lock (#296 review) is entered via `hold` and kept for the WHOLE gate run,
    # not just the reconstruction — so a concurrent reconstruction can't clean this run's
    # outputs mid-command; released in the finally. A busy lane (an in-flight Do, another
    # gate run) raises WorktreeError → the same fail-closed red as a mismatched tree.
    hold = contextlib.ExitStack()
    if worktree_override is not None:
        wt, ovf_primary = worktree_override, None
    elif bundle is not None:
        try:
            wt, ovf_primary = worktree.for_gate(bundle, cfg, hold=hold)
        except worktree.WorktreeError as exc:
            hold.close()
            print(f"gates: {exc} — failing closed, no gate was run", file=sys.stderr)
            return _assemble_matrix([_row(
                "C4 Verification (worktree mismatch)", "fail",
                oracle="worktree reconstruction (base + patch.diff)",
                rule_id="worktree-mismatch", path_line=str(exc).splitlines()[0][:160],
                gating=True, element="C4")], stub=False)
    else:
        wt, ovf_primary = None, None
    if log_dir is not None:
        # One evidence set per Check run (issue #370): clear the previous run's logs, so
        # gate-logs/ holds exactly THIS run's files — a check since removed from the
        # config leaves no stale log masquerading as current evidence. A non-directory
        # squatting on the path survives this (ignore_errors) and is surfaced per row as
        # ``log_error`` by _write_gate_log — visibly, never silently (#370 iteration 2).
        shutil.rmtree(log_dir, ignore_errors=True)
    configured: list[dict] = []
    try:
        for chk in cfg.gates_checks:
            if not _applies(chk, scopes, labels):
                if chk.get("scope", "repo") in scopes and chk.get("target") and labels is not None:
                    print(f"  · gate {chk.get('id', '')} skipped "
                          f"(target={chk.get('target')}, bundle labels {set(labels)})",
                          file=sys.stderr, flush=True)
                continue
            configured.append(_run_one(chk, cfg=cfg, cwd=cwd, bundle=bundle,
                                       runner=cfg.gates_runner,
                                       worktree_path=wt,
                                       default_timeout=cfg.gates_default_timeout_secs,
                                       log_dir=log_dir))
        # Host-only CI parity rows ([gates] host_ci, issue #311): commands the host's CI
        # runs on every PR but the delegated gate runner does not cover (a spell-checker,
        # a docs lint). Unlike the rows above (cwd=cfg.root; each command must target
        # $PDCA_WORKTREE itself), these run FROM the reconstructed base + patch.diff
        # tree: the point of the feature is that the HARNESS guarantees the tree under
        # test is the patched one — the T4 slot runs pre-apply, so it structurally cannot
        # see content that arrives in the patch. No bundle (the CI working-tree /
        # integration re-gate) ⇒ skipped: there the host's own CI runs these for real.
        # No patched tree (isolation off / target not a git checkout) ⇒ an UNVERIFIABLE
        # row (→ SUMMARY §6 NEEDS-HUMAN), never a run against the wrong tree — a green
        # over unpatched content is the exact lie this feature closes (#296 doctrine).
        if bundle is not None:
            for chk in cfg.host_ci_checks:
                if wt is None:
                    configured.append(_row(
                        f"{chk.get('tier', 'T4')} {chk.get('label', chk.get('id', ''))}",
                        "unverifiable",
                        oracle=chk.get("cmd", "") or chk.get("subcmd", ""),
                        rule_id=chk.get("id", ""),
                        path_line="host CI needs the patched tree — no worktree "
                                  "([driver].worktree off or target not a git checkout)",
                        gating=bool(chk.get("gating", True)),
                        element=chk.get("tier", "T4")))
                else:
                    configured.append(_run_one(chk, cfg=cfg, cwd=wt, bundle=bundle,
                                               runner=cfg.gates_runner, worktree_path=wt,
                                               default_timeout=cfg.gates_default_timeout_secs,
                                               log_dir=log_dir))
    finally:
        if ovf_primary is not None and wt is not None:
            worktree.overflow_remove(ovf_primary, wt)
        hold.close()
    # Overlay the configured gate results onto the complete 5/5/1 matrix.
    return _assemble_matrix(configured, stub=False)


def _delegated_cmd(chk: dict, runner: str) -> tuple[str, str]:
    """Resolve a check's command. A check may declare a bare ``subcmd`` (issue #67)
    delegated to the host's single-sourced ``[gates] runner`` (e.g. ``cargo xtask``),
    so PDCA orchestrates the host runner without re-declaring the gate; or a full ``cmd``
    (which may itself be ``cargo xtask ci`` — wholesale delegation). Returns
    ``(cmd, error)``: a non-empty ``error`` is a misconfiguration to surface as a fail
    row (a ``subcmd`` with no runner, or a runner binary missing from PATH)."""
    subcmd = chk.get("subcmd", "")
    if not subcmd:
        return chk.get("cmd", ""), ""
    if not runner:
        return "", "check declares 'subcmd' but [gates] runner is unset"
    first = shlex.split(runner)[0] if runner.strip() else ""
    # A clear error beats a cryptic shell failure when the host runner isn't installed.
    if first and not first.startswith((".", "/")) and shutil.which(first) is None:
        return "", f"delegated runner '{first}' not found on PATH — install it or fix [gates].runner"
    return f"{runner} {subcmd}", ""


def _gate_timeout(chk: dict, default: int | None) -> int | None:
    """The wall-clock bound (seconds) for one ``[[gates.checks]]`` row (issue #368).

    The row's own ``timeout_secs`` wins; else the ``[gates] default_timeout_secs``
    fallback; else ``None`` (unbounded — today's behaviour, unchanged). ``0`` or a
    negative value means "explicitly unbounded", so one long row can opt out of a
    configured default. A non-numeric value is treated as unconfigured rather than
    crashing the gate run.
    """
    raw = chk.get("timeout_secs", default)
    try:
        secs = int(raw)
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _run_one(chk: dict, *, cfg: Config, cwd: Path, bundle: Path | None, runner: str = "",
             worktree_path: Path | None = None,
             default_timeout: int | None = None,
             log_dir: Path | None = None) -> dict:
    # ``cfg`` is required (issue #387): the bundle-scoped base export resolves the brief's
    # own base as `<cfg.base_remote>/<branch or cfg.default_branch>` — the same ref publish
    # commits against — so it cannot be derived from the check row alone.
    cmd, cmd_error = _delegated_cmd(chk, runner)
    gating = bool(chk.get("gating", True))
    label = f"{chk.get('id', '')}: {chk.get('label', '')}".strip(": ")
    if cmd_error:
        # Misconfigured delegation — surface as a failing row with a fix hint, never crash.
        print(f"  · gate {label}: {cmd_error}", file=sys.stderr, flush=True)
        return _row(
            f"{chk.get('tier', '?')} {chk.get('label', chk.get('id', ''))}",
            "fail", oracle=chk.get("subcmd", "") or cmd, rule_id=chk.get("id", ""),
            path_line=cmd_error[:120], gating=gating, element=chk.get("tier", ""),
        )
    env = {"PDCA_BUNDLE": str(bundle)} if bundle is not None else None
    # Worktree isolation (issue #94): the tree Do edited; a gate cmd targets $PDCA_WORKTREE.
    if worktree_path is not None:
        env = {**(env or {}), "PDCA_WORKTREE": str(worktree_path)}
    # The base a per-fix verifier must reset to before applying patch.diff. The governing
    # invariant (issue #54): the TEST base and the DEPLOY base must not diverge — the gate has
    # to establish red→green on the very branch publish will commit to. So these three exports
    # are MUTUALLY EXCLUSIVE, resolved in the same order publish resolves its own base:
    #
    #   1. `Onto branch` (#54) → PDCA_BASE. The brief names an existing PR's head; publish
    #      appends a commit to THAT branch (`publish.publish` takes the Onto path and returns
    #      before it ever reads the stack-base marker), so it is also the test base.
    #   2. else the wave's folded integration branch (#273) → PDCA_VERIFY_BASE. A wave>0
    #      bundle's Do worktree is cut off the run-scoped integration branch (prior waves'
    #      folded patches, pushed to origin), and publish opens its PR against that branch. A
    #      verifier that instead reset to the brief's origin base would, for a dependent
    #      sharing a file with its prereq, either false-fail "patch does not apply — stale" or
    #      measure red→green against a tree LACKING the prereq.
    #   3. else the brief's own `Repo + branch target` base (#387) → PDCA_BRIEF_BASE, as
    #      `<base_remote>/<branch>` — the very ref publish checks the fix out against
    #      (`publish.publish`'s `checkout_base`: `f"{base_remote}/{base}"`), or
    #      `<base_remote>/<default>`
    #      when the brief names no target. This is the last rung of the ladder
    #      `engine/scripts/run-verify.sh` publishes to every instance, and it used to be the
    #      one the driver never supplied: a shell gate had to re-derive the ANCHORED parse
    #      (`brief._clean_ref`, got wrong and fixed twice in Python — #235, #262) from a
    #      comment, and the two implementations then disagreed on the very briefs that need
    #      the base most. Exported unconditionally at this rung so the gate reads a resolved
    #      ref rather than `brief.md`; a script composing `origin/$VAR` over it would double
    #      the remote, so the value is always fully qualified, like the other two.
    #
    # Exporting more than one would tell the gate to verify against the integration branch
    # while publish commits to the Onto branch — exactly the divergence #54 exists to prevent
    # (PR #282 review). Exactly one is set for every bundle-scoped gate invocation.
    if bundle is not None:
        onto = brief.onto_branch(bundle / "brief.md")
        if onto is not None:
            env = {**(env or {}), "PDCA_BASE": f"{onto[0]}/{onto[1]}"}
        else:
            from . import publish  # lazy: publish imports leaves→gates; avoid an import cycle
            stack_base = publish.read_stack_base(bundle)
            if stack_base:
                env = {**(env or {}), "PDCA_VERIFY_BASE": f"origin/{stack_base}"}
            else:
                base = brief.base_branch(bundle / "brief.md", cfg.default_branch)
                env = {**(env or {}), "PDCA_BRIEF_BASE": f"{cfg.base_remote}/{base}"}
    # Under in-driver lane concurrency, expose the worker-slot id so a gate command can
    # scope its checkout / container name / port / scratch per lane (docs 09). Absent
    # (serial driver) → no PDCA_LANE, so gates run exactly as before.
    lane_id = lane.current()
    if lane_id is not None:
        env = {**(env or {}), "PDCA_LANE": str(lane_id)}
    watch = bundle or cwd
    bound = _gate_timeout(chk, default_timeout)
    # May this row declare itself `deferred` (issue #401)? Only if a later gate re-runs it,
    # so the deferred verdict is genuinely owed rather than waived — resolved here, where
    # both the row and the config are in hand (`_classify` sees neither).
    deferrable = _deferrable(chk, cfg)
    print(f"  · gate {label} (a Docker-backed gate can take minutes)…", file=sys.stderr, flush=True)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    rc: int | None = None
    output = ""
    try:
        # Output is captured for the evidence line; the heartbeat ticks meanwhile so
        # a long, silent gate (e.g. a Docker-backed test suite) doesn't look hung.
        # `bound` (issue #368) caps the wall-clock when configured: on expiry the
        # process group is killed and TIMEOUT_RC comes back instead of an exit code.
        rc, output, _ = progress.run_with_heartbeat(
            cmd, cwd=cwd, shell=True, env=_merged_env(env), capture=True, label=label,
            timeout=bound, status=lambda: progress.bundle_activity(watch),
        )
        if rc == progress.TIMEOUT_RC:
            # The oracle did not answer (#368): a timed-out gate is `unverifiable`
            # (the #46 outcome — routed to SUMMARY §6 NEEDS-HUMAN, kept out of the
            # gating verdict), never a pass/fail verdict the command did not reach.
            result, evidence = "unverifiable", [f"gate exceeded its {bound}s timeout"]
        else:
            result, evidence = _classify(rc, output, deferrable=deferrable)
    except Exception as exc:  # command not found, etc. — a failing gate, surfaced
        result, evidence = "fail", [str(exc)]
        output = f"{exc}\n"  # the exception IS the run's whole output — log it (#370)
    duration = round(time.monotonic() - t0, 2)
    row = _row(
        f"{chk.get('tier', '?')} {chk.get('label', chk.get('id', ''))}",
        result, oracle=cmd, rule_id=chk.get("id", ""),
        path_line=evidence[0][:120], gating=gating, element=chk.get("tier", ""),
    )
    if log_dir is not None:
        # Persist the FULL evidence (issue #370): the truncated path_line above stays the
        # summary, but the verdict's whole basis — including the partial capture of a
        # timed-out gate — must be reconstructable from bundle files alone.
        rel, log_error = _write_gate_log(log_dir, chk, cmd=cmd, cwd=cwd,
                                         worktree_path=worktree_path, started=started,
                                         duration=duration, rc=rc, result=result,
                                         timeout=bound, output=output)
        row["duration_secs"] = duration  # additive keys — existing consumers unchanged
        if log_error is None:
            row["log"] = rel             # bundle-relative
        else:
            # A persistence failure must never break the gate run or alter the verdict —
            # but it must NOT be silent either (#370 iteration 2): the feature's promise
            # is "full basis reconstructable from bundle files alone", so a run where
            # that silently did not happen re-creates the original defect. Record the
            # reason in the row (additive) and say so on stderr.
            row["log_error"] = log_error
            print(f"  ! gate {label}: evidence log {GATE_LOGS_DIR}/ not written — "
                  f"{log_error}", file=sys.stderr, flush=True)
    return row


def _write_gate_log(log_dir: Path, chk: dict, *, cmd: str, cwd: Path,
                    worktree_path: Path | None, started: str, duration: float,
                    rc: int | None, result: str, timeout: int | None,
                    output: str) -> tuple[str | None, str | None]:
    """Write ``gate-logs/<rule_id>.log`` — a small header, then the combined
    stdout+stderr VERBATIM (issue #370). Returns ``(bundle_relative_path, None)`` on
    success, or ``(None, reason)`` on a write failure: evidence persistence is
    best-effort and must never break the gate run (the verdict itself is already in the
    row) — but the failure is returned, not swallowed, so the caller surfaces it as the
    row's ``log_error`` (#370 iteration 2)."""
    name = f"{re.sub(r'[^A-Za-z0-9._-]', '_', chk.get('id', '')) or 'gate'}.log"
    if rc == progress.TIMEOUT_RC:
        # (#368 × #370) the bound expired: attach what the gate DID say before the kill,
        # so a hung gate's log shows where it hung instead of nothing.
        exit_line = f"timeout — killed after its {timeout}s bound (partial output below)"
    elif rc is None:
        exit_line = "exception — the command could not be run"
    else:
        exit_line = str(rc)
    header = "\n".join([
        f"# gate: {chk.get('id', '')} — {chk.get('label', '')}",
        f"# cmd: {cmd}",
        f"# cwd: {cwd}",
        f"# PDCA_WORKTREE: {worktree_path if worktree_path is not None else '(none)'}",
        f"# start: {started}",
        f"# duration_secs: {duration}",
        f"# exit: {exit_line}",
        f"# outcome: {result}",
        "# ---- combined stdout+stderr (verbatim) ----",
        "",
    ])
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / name).write_text(header + output, encoding="utf-8")
    except OSError as exc:
        return None, f"could not write {GATE_LOGS_DIR}/{name}: {exc}"
    return f"{GATE_LOGS_DIR}/{name}", None


def _declarations(output: str, marker: str) -> list[str]:
    """Every line of ``output`` the gate **declared** with ``marker``, in order — the text
    after the marker (possibly empty).

    A **declaration** is a line whose first text is the marker (leading whitespace ignored)
    — how every documented emitter writes it: the shipped advisory check
    (``scripts/checks/test_exercises_production.py``) prints ``f"{UNVERIFIABLE} {reason}"``,
    and the gate wrappers ``echo`` the marker at the start of the line.

    A mid-line occurrence is NOT a declaration: it is text the gate merely **relayed** from
    something it ran (#428) — see :func:`_classify`. One notion of "the gate said this",
    shared by both markers (#402)."""
    out: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            out.append(stripped[len(marker):].strip())
    return out


def _declared_unverifiable(output: str) -> str | None:
    """The gate's OWN ``unverifiable`` declaration in ``output`` — its reason — or ``None``.

    The FIRST declaration wins: the reason a gate gives for deferring is the one it gave
    when it stopped being able to verify, and later output cannot retract it."""
    declared = _declarations(output, UNVERIFIABLE_MARKER)
    return declared[0] if declared else None


def _declared_evidence(output: str) -> str | None:
    """The gate's OWN verdict summary in ``output`` — the row's evidence line — or ``None``.

    The LAST non-empty declaration wins (issue #402): a wrapper with several legs declares
    per leg, and its final word is the summary of the run as a whole — where the last
    *output* line is merely whatever flushed last, usually a child's. A bare
    ``PDCA-EVIDENCE:`` with no text is no summary and falls back with the undeclared case."""
    declared = [text for text in _declarations(output, EVIDENCE_MARKER) if text]
    return declared[-1] if declared else None


def _declared_deferred(output: str) -> str | None:
    """The gate's OWN ``deferred`` declaration in ``output`` — the reason its substantive
    audit is owed later — or ``None`` (issue #401).

    The FIRST declaration wins, as for :func:`_declared_unverifiable`: the reason a gate
    gives when it finds its subject absent is the one it gave at that moment."""
    declared = _declarations(output, DEFERRED_MARKER)
    return declared[0] if declared else None


def _deferrable(chk: dict, cfg: Config) -> bool:
    """True iff ``chk`` is **re-gated later**, so a ``deferred`` row has a later verdict to
    defer to (issue #401).

    Deferral is legitimate only where the substantive audit actually happens afterwards:
    the row must be one ``publish`` re-runs before it pushes anything
    (:func:`publish.publish_gates` — a bundle-scoped T4 row, or an explicit
    ``at_publish = true``). A row nothing re-gates owes its verdict to nobody, so its
    declaration is ignored and it keeps today's ``pass``/``fail``. This is the guard that
    keeps ``PDCA-DEFERRED:`` from becoming a way for any gate to opt out of scrutiny: a
    deferral is a *hand-off* to a named later gate, not a waiver.
    """
    from . import publish  # lazy: publish imports leaves→gates; avoid an import cycle
    return any(c is chk or c == chk for c in publish.publish_gates(cfg))


def _classify(rc: int, output: str, *, deferrable: bool = False) -> tuple[str, list[str]]:
    """Map a gate command's exit code + output to (result, evidence-lines).

    ``unverifiable`` (issue #46) lets a gate that did NOT fail defer to the human: it may
    exit 0 and still declare the marker. The text after the marker is the reason; otherwise
    the evidence is the gate's declared summary, and failing that the command's last output
    line (as for pass/fail).

    The evidence line obeys the same declaration rule as the verdict (#402): the row records
    what the gate declared with :data:`EVIDENCE_MARKER`, and only where it declared nothing
    does it fall back to ``output``'s last line. That fallback is what made a GREEN gate's
    frozen record read like a failure path: the capture is one merged stdout+stderr stream
    (``progress.run_with_heartbeat``), so a wrapper that shells out to a test suite files
    whatever that suite's children flushed last — a ``/tmp`` scratch path from a sandbox
    that no longer exists was recorded as a passing C4's whole evidence, which is neither
    the *basis* of the verdict nor *reconstructable* (the invariant :func:`_write_gate_log`
    exists to keep, #370). Declaring is the only way a gate can be sure what gets filed, so
    the marker is the convention gate authors write to (docs 04 §Gate result vocabulary);
    the undeclared fallback stays defined, and the full basis stays in ``row["log"]``.
    The evidence marker never changes the verdict — a declaring gate that exits non-zero
    still FAILS, with its declaration as the evidence.

    The marker is honoured only for an exit code that is not a failure — 0, or the dedicated
    ``UNVERIFIABLE_RC``. A gate that exits non-zero FAILED, whatever its output happens to
    contain, and saying otherwise masked real red (#329): the marker is a plain substring, so
    a suite where one check deferred and a *different* test failed carried both the marker and
    a non-zero exit, and the whole gate read ``unverifiable`` — which is not a gating failure,
    so ``_finalize`` reported ``overall = "pass"``. Per bundle that still stops for a human
    (``assemble._unverifiable_items`` → §6 → C6), but three consumers read ``overall`` with no
    §6 in the path: the between-waves integration re-gate (``flow``) would not stop and later
    waves would build on a red tip, ``revalidate`` would not count it as a PASS→FAIL
    regression, and ``cli`` would exit 0. A gate with no possible verdict has its own channel;
    it must use it rather than piggy-backing on a failure.

    The same reason narrows *whose* marker counts (#428, the exit-0 half of #329). The
    verdict is the GATE's to declare, so only a line the gate started with the marker is one
    (:func:`_declared_unverifiable`); an occurrence anywhere else on a line is text the gate
    **relayed** from what it ran — a child's log, an assertion diff, a source comment a test
    read back — and it used to convert the gate's real verdict. It is structural for any project
    whose tests exercise this machinery: a green C4 whose captured output quoted the
    documented contract line (``... Emit `PDCA-UNVERIFIABLE: <reason>` and exit 77 ...``) was
    recorded ``unverifiable``, so a real green stopped counting toward ``overall`` and a real
    red would equally have been laundered into "defer to the human".

    ``deferred`` (issue #401) is the same family with a different addressee: the gate RAN,
    found its subject absent BY DESIGN, and owes its substantive verdict to a later gate —
    a bundle-scoped T4 contribution row at Check time, whose ``pr-description.md`` publish
    has not drafted yet. Recording that non-event as ``pass`` asserted a green no reviewer
    could reproduce (the artifacts the row names are not among its inputs), so every cycle
    escalated the by-design condition to §6 NEEDS-HUMAN; recording it ``unverifiable``
    would route it to §6 too. It is honoured only on exit **0** — 77 is the ``unverifiable``
    channel and a non-zero exit is a failure whatever the gate printed (#329) —
    ``unverifiable`` wins when both are declared (the safer channel: it stops for a human),
    and only when ``deferrable`` says a later gate actually re-runs this row
    (:func:`_deferrable`)."""
    if rc in (0, UNVERIFIABLE_RC):
        reason = _declared_unverifiable(output)
        if reason is not None:
            return "unverifiable", [reason or "gate declared itself unverifiable"]
        if rc == 0 and deferrable:
            owed = _declared_deferred(output)
            if owed is not None:
                return "deferred", [owed or "substantive audit runs at publish"]
    evidence = _declared_evidence(output)
    if evidence is None:
        evidence = (output.strip().splitlines()[-1:] or [""])[0]
    if rc == UNVERIFIABLE_RC:
        return "unverifiable", [evidence or f"gate exited unverifiable (rc {UNVERIFIABLE_RC})"]
    return ("pass" if rc == 0 else "fail"), [evidence]


def _merged_env(extra: dict | None) -> dict | None:
    if extra is None:
        return None
    import os
    return {**os.environ, **extra}


# ----------------------------------------------------------------------------
def _finalize(rows: list[dict], *, name: str, write_to: Path | None) -> dict:
    # Only a hard `fail` gates. `unverifiable` (#46) and `deferred` (#401) are verdicts the
    # gate did NOT reach — neither a green nor a gating red — so neither counts toward
    # `overall`; each has its own downstream route (§6 NEEDS-HUMAN / the later re-gate).
    gating_fail = any(r["gating"] and r["result"] == "fail" for r in rows)
    result = {"issue_dir": name, "overall": "fail" if gating_fail else "pass", "rows": rows}
    if write_to is not None:
        (write_to / "check-gates.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        (write_to / "check-gates.md").write_text(render_md(result), encoding="utf-8")
    return result


def _row(check, result, oracle, rule_id="", path_line="", gating=False, element="") -> dict:
    return {
        "check": check, "result": result, "oracle": oracle,
        "rule_id": rule_id, "path_line": path_line, "gating": gating, "element": element,
    }


# ----------------------------------------------------------------------------
# The Check 5/5/1 — 5 correctness + 5 conformance + 1 validation. Every
# validation output enumerates all eleven so the matrix is always complete:
# configured gates fill their element (matched by tier); the rest show as input
# (C1/C3), judgment (C5/T5/validation — reviewer + human), or not-configured.
# (docs 04 §The 5/5/1 × tooling-shape matrix)
# ----------------------------------------------------------------------------
_FIVE_FIVE_ONE = [
    # (element, label, kind, default-oracle)   kind ∈ input | gate | judgment
    ("C1", "C1 Spec",                         "input",    "brief.md"),
    ("C2", "C2 Reproduction (red pre-fix)",   "gate",     "fixture + repro runner"),
    ("C3", "C3 Change",                       "input",    "patch.diff"),
    ("C4", "C4 Verification (red→green)",     "gate",     "shipped test + regression suite"),
    ("C5", "C5 Causal adequacy",              "judgment", "reviewer + human sign-off"),
    ("T1", "T1 Structure",                    "gate",     "structural validator"),
    ("T2", "T2 Shape",                        "gate",     "semgrep / AST scanner"),
    ("T3", "T3 Runtime",                      "gate",     "dependency resolution + clean-env suite"),
    ("T4", "T4 Contribution",                 "gate",     "commit-msg / branch-target / version-bump"),
    ("T5", "T5 Judgment",                     "judgment", "reviewer + human sign-off"),
    ("V",  "Validation — fitness-to-purpose", "judgment", "human at sign-off"),
]


def canonical_elements() -> list[tuple[str, str, str, str]]:
    """The 11 elements of the 5/5/1 matrix — ``(element, label, kind, oracle)`` in
    canonical order. Public so the Check reviewer leaf can mandate a verdict table
    that mirrors exactly the matrix the gates assemble (single source of truth)."""
    return list(_FIVE_FIVE_ONE)


def _assemble_matrix(configured: list[dict], *, stub: bool) -> list[dict]:
    """Overlay configured gate rows onto the complete 5/5/1, in canonical order.

    A 5/5/1 element with one or more configured gates (matched by tier) shows
    those gate rows; an uncovered *gate* element shows a stub-pass row (offline
    slice) or a 'no gate configured' row; input and judgment elements always show
    their non-gating placeholder.
    """
    by_elem: dict[str, list[dict]] = {}
    for r in configured:
        by_elem.setdefault(r.get("element", ""), []).append(r)

    rows: list[dict] = []
    for elem, label, kind, oracle in _FIVE_FIVE_ONE:
        if elem in by_elem:
            rows.extend(by_elem[elem])
        elif kind in ("input", "judgment"):
            rows.append(_row(label, "none", oracle, element=elem))
        elif stub:
            rows.append(_row(f"{label} (stub)", "pass", f"{oracle} (stub)",
                             rule_id=f"{elem}-stub", gating=True, element=elem))
        else:
            rows.append(_row(label, "none", "(no gate configured)", element=elem))
    return rows


def render_md(result: dict) -> str:
    """Render the validation output as the Check 5/5/1 — Correctness, Conformance,
    Validation — so every element of the matrix is visible (docs 04)."""
    lines = [
        f"# Check gates — {result['issue_dir']}",
        "",
        f"**Overall (gating): {result['overall']}**",
        "",
        "The Check 5/5/1: 5 correctness · 5 conformance · 1 validation.",
    ]

    def section(title: str, keep) -> None:
        rows = [r for r in result["rows"] if keep(r["check"])]
        if not rows:
            return
        lines.extend(["", f"## {title}", "",
                      "| Check | Result | Oracle | Rule | Evidence | Gating |",
                      "|---|---|---|---|---|---|"])
        for r in rows:
            lines.append(
                f"| {r['check']} | {r['result']} | {r['oracle']} | "
                f"{r['rule_id'] or '—'} | {r['path_line'] or '—'} | "
                f"{'yes' if r['gating'] else 'no'} |"
            )

    section("Correctness (5)", lambda c: c.startswith("C"))
    section("Conformance (5)", lambda c: c.startswith("T"))
    section("Validation (1)", lambda c: not (c.startswith("C") or c.startswith("T")))
    return "\n".join(lines) + "\n"
