"""Deterministic Check gates → ``check-gates.json`` (docs 02 / 04, the gates path).

The gates are the *only* blocking path in Check — no model in the gating loop.
**Single-sourcing** (docs 04 §Single-sourcing) is the load-bearing property: the
gate *commands* live once in ``pdca.toml`` ``[[gates.checks]]``, and the same
``pdca gates`` entry point runs them for the local driver (over a bundle) and for
CI (over the PR working tree). There is no second implementation to drift.

Each configured check: ``{id, tier, label, cmd, gating, scope}`` where ``scope``
is ``"repo"`` (runs against the working tree — what CI re-runs) or ``"bundle"``
(needs the bundle/patch context — local only). A check passes iff its ``cmd``
exits 0. When ``[[gates.checks]]`` is empty the driver falls back to all-PASS
stub rows, so the offline vertical slice still runs.

A row: {check, result, oracle, rule_id, path_line, gating}. ``result`` ∈
``pass`` / ``fail`` / ``none``. A ``none`` row is a judgment cell decided by the
reviewer + human (docs 04 §judgment cell); it is listed for matrix alignment and
never gates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import progress
from .config import Config


def run_gates(d: Path, cfg: Config) -> dict:
    """Run every gate for bundle ``d`` (both repo- and bundle-scoped); write JSON."""
    rows = _run_checks(cfg, cwd=cfg.root, bundle=d, scopes=("repo", "bundle"))
    return _finalize(rows, name=d.name, write_to=d)


def run_working_tree(cfg: Config) -> dict:
    """Run only repo-scoped gates against the working tree (the CI merge re-gate)."""
    rows = _run_checks(cfg, cwd=cfg.root, bundle=None, scopes=("repo",))
    return _finalize(rows, name="working-tree", write_to=None)


# ----------------------------------------------------------------------------
def _run_checks(cfg: Config, *, cwd: Path, bundle: Path | None, scopes: tuple[str, ...]) -> list[dict]:
    # No configured gates → the offline stub: the full 5/5/1 with the mechanical
    # gate elements stub-passed (so the offline slice runs green).
    if not cfg.gates_checks:
        return _assemble_matrix([], stub=True)

    configured: list[dict] = []
    for chk in cfg.gates_checks:
        if chk.get("scope", "repo") not in scopes:
            continue
        configured.append(_run_one(chk, cwd=cwd, bundle=bundle))
    # Overlay the configured gate results onto the complete 5/5/1 matrix.
    return _assemble_matrix(configured, stub=False)


def _run_one(chk: dict, *, cwd: Path, bundle: Path | None) -> dict:
    cmd = chk.get("cmd", "")
    gating = bool(chk.get("gating", True))
    label = f"{chk.get('id', '')}: {chk.get('label', '')}".strip(": ")
    env = {"PDCA_BUNDLE": str(bundle)} if bundle is not None else None
    print(f"  · gate {label} (a Docker-backed gate can take minutes)…", file=sys.stderr, flush=True)
    try:
        # Output is captured for the evidence line; the heartbeat ticks meanwhile so
        # a long, silent gate (e.g. a Docker-backed test suite) doesn't look hung.
        rc, output = progress.run_with_heartbeat(
            cmd, cwd=cwd, shell=True, env=_merged_env(env), capture=True, label=label,
        )
        result = "pass" if rc == 0 else "fail"
        evidence = output.strip().splitlines()[-1:] or [""]
    except Exception as exc:  # command not found, etc. — a failing gate, surfaced
        result, evidence = "fail", [str(exc)]
    return _row(
        f"{chk.get('tier', '?')} {chk.get('label', chk.get('id', ''))}",
        result, oracle=cmd, rule_id=chk.get("id", ""),
        path_line=evidence[0][:120], gating=gating, element=chk.get("tier", ""),
    )


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
