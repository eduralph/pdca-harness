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
:data:`UNVERIFIABLE_RC` (77, the automake SKIP convention) **or** print a line
containing :data:`UNVERIFIABLE_MARKER` (``PDCA-UNVERIFIABLE: <reason>``; the marker
wins over the exit code, so a gate may exit 0 and still defer). When
``[[gates.checks]]`` is empty the driver falls back to all-PASS stub rows, so the
offline vertical slice still runs.

A row: {check, result, oracle, rule_id, path_line, gating}. ``result`` ∈
``pass`` / ``fail`` / ``unverifiable`` / ``none``. A ``none`` row is a judgment cell
decided by the reviewer + human (docs 04 §judgment cell); it is listed for matrix
alignment and never gates. An ``unverifiable`` row does **not** count toward
``overall`` (it is not a failure); the driver routes it into SUMMARY §6 NEEDS-HUMAN,
where the C6 accept-guard forces the human to clear it before sign-off.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from pathlib import Path

from . import brief, lane, progress, state, worktree
from .config import Config

# A gate that cannot RUN its mechanical check (vs. running and failing) declares so:
# exit 77 (automake SKIP convention) or a marker line. The marker takes precedence —
# a gate may exit 0 and still defer to the human. Neither is a failure (see _finalize).
UNVERIFIABLE_RC = 77
UNVERIFIABLE_MARKER = "PDCA-UNVERIFIABLE:"


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
    """Run every gate for bundle ``d`` (both repo- and bundle-scoped); write JSON."""
    rows = _run_checks(cfg, cwd=cfg.root, bundle=d, scopes=("repo", "bundle"))
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


def run_integration(cfg: Config, worktree_path: Path) -> dict:
    """Run the repo-scoped gates against a wave integration worktree (#wave-model re-gate).

    Like :func:`run_working_tree`, but targeted at an explicit tree — the folded
    integration tip the *next* wave will build on. The gate commands run from it and see it
    as ``$PDCA_WORKTREE``, so a project's repo-scoped gate validates the *combination* of
    the waves so far: a result that is red though each fix was green alone means the
    caller STOPs before building the next wave on it. Never writes a frozen record."""
    rows = _run_checks(cfg, cwd=worktree_path, bundle=None, scopes=("repo",),
                       worktree_override=worktree_path)
    return _finalize(rows, name="integration", write_to=None)


def run_gates_dry(d: Path, cfg: Config) -> dict:
    """Run every gate for bundle ``d`` against the CURRENT engine WITHOUT writing the
    frozen ``check-gates.json`` — the gate runner behind ``pdca revalidate`` (issue #11).

    Same single-sourced ``_run_checks`` as :func:`run_gates`, but ``write_to=None`` so a
    re-gate of an already-COMPLETE bundle never mutates its frozen record."""
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
                worktree_override: Path | None = None) -> list[dict]:
    # No configured gates → the offline stub: the full 5/5/1 with the mechanical
    # gate elements stub-passed (so the offline slice runs green).
    if not cfg.gates_checks:
        return _assemble_matrix([], stub=True)

    labels = _bundle_target(bundle, cfg.gate_target_match, cfg.gate_target_default, cfg.gate_target_flags)
    # Worktree isolation (issue #94): if Do ran in an isolated worktree, gates test THAT
    # tree — expose it as $PDCA_WORKTREE so a gate cmd targets it, not the host checkout.
    # ``worktree_override`` (the wave integration re-gate, #wave-model) points the
    # repo-scoped gates at an explicit tree (the folded integration tip) instead.
    wt = worktree_override if worktree_override is not None else (
        worktree.path(bundle, cfg) if bundle is not None else None)
    configured: list[dict] = []
    for chk in cfg.gates_checks:
        if not _applies(chk, scopes, labels):
            if chk.get("scope", "repo") in scopes and chk.get("target") and labels is not None:
                print(f"  · gate {chk.get('id', '')} skipped "
                      f"(target={chk.get('target')}, bundle labels {set(labels)})",
                      file=sys.stderr, flush=True)
            continue
        configured.append(_run_one(chk, cwd=cwd, bundle=bundle, runner=cfg.gates_runner,
                                   worktree_path=wt))
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


def _run_one(chk: dict, *, cwd: Path, bundle: Path | None, runner: str = "",
             worktree_path: Path | None = None) -> dict:
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
    # Stack mode (issue #54): when the brief names an existing PR's head to stack onto,
    # expose it as PDCA_BASE so the verify/repro gate establishes red→green on THAT branch
    # — the same branch publish commits onto and pushes to. Single-sourced from the brief,
    # so the test base and the deploy base can't diverge. Absent ⇒ no PDCA_BASE, unchanged.
    if bundle is not None:
        onto = brief.onto_branch(bundle / "brief.md")
        if onto is not None:
            env = {**(env or {}), "PDCA_BASE": f"{onto[0]}/{onto[1]}"}
    # Under in-driver lane concurrency, expose the worker-slot id so a gate command can
    # scope its checkout / container name / port / scratch per lane (docs 09). Absent
    # (serial driver) → no PDCA_LANE, so gates run exactly as before.
    lane_id = lane.current()
    if lane_id is not None:
        env = {**(env or {}), "PDCA_LANE": str(lane_id)}
    watch = bundle or cwd
    print(f"  · gate {label} (a Docker-backed gate can take minutes)…", file=sys.stderr, flush=True)
    try:
        # Output is captured for the evidence line; the heartbeat ticks meanwhile so
        # a long, silent gate (e.g. a Docker-backed test suite) doesn't look hung.
        rc, output, _ = progress.run_with_heartbeat(
            cmd, cwd=cwd, shell=True, env=_merged_env(env), capture=True, label=label,
            status=lambda: progress.bundle_activity(watch),
        )
        result, evidence = _classify(rc, output)
    except Exception as exc:  # command not found, etc. — a failing gate, surfaced
        result, evidence = "fail", [str(exc)]
    return _row(
        f"{chk.get('tier', '?')} {chk.get('label', chk.get('id', ''))}",
        result, oracle=cmd, rule_id=chk.get("id", ""),
        path_line=evidence[0][:120], gating=gating, element=chk.get("tier", ""),
    )


def _classify(rc: int, output: str) -> tuple[str, list[str]]:
    """Map a gate command's exit code + output to (result, evidence-lines).

    ``unverifiable`` (issue #46) wins over the exit code: a gate may exit 0 and still
    print the marker to defer to the human. The text after the marker is the reason;
    otherwise the evidence is the command's last output line (as for pass/fail)."""
    for line in output.splitlines():
        if UNVERIFIABLE_MARKER in line:
            reason = line.split(UNVERIFIABLE_MARKER, 1)[1].strip()
            return "unverifiable", [reason or "gate declared itself unverifiable"]
    last = output.strip().splitlines()[-1:] or [""]
    if rc == UNVERIFIABLE_RC:
        return "unverifiable", [last[0] or f"gate exited unverifiable (rc {UNVERIFIABLE_RC})"]
    return ("pass" if rc == 0 else "fail"), last


def _merged_env(extra: dict | None) -> dict | None:
    if extra is None:
        return None
    import os
    return {**os.environ, **extra}


# ----------------------------------------------------------------------------
def _finalize(rows: list[dict], *, name: str, write_to: Path | None) -> dict:
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
