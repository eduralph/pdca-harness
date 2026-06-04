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
    if not cfg.gates_checks:
        return _stub_rows(bundle) if bundle is not None else _stub_repo_rows()

    rows: list[dict] = []
    for chk in cfg.gates_checks:
        scope = chk.get("scope", "repo")
        if scope not in scopes:
            continue
        rows.append(_run_one(chk, cwd=cwd, bundle=bundle))
    # Judgment cells are always recorded for matrix alignment (never gating).
    rows += _judgment_rows()
    return rows


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
        path_line=evidence[0][:120], gating=gating,
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


def _row(check, result, oracle, rule_id="", path_line="", gating=False) -> dict:
    return {
        "check": check, "result": result, "oracle": oracle,
        "rule_id": rule_id, "path_line": path_line, "gating": gating,
    }


def _judgment_rows() -> list[dict]:
    # C1/C3 inputs + C5/T5/validation judgment cells: recorded, never gating.
    return [
        _row("C5 causal adequacy", "none", "reviewer + human sign-off"),
        _row("T5 judgment", "none", "reviewer + human sign-off"),
        _row("Validation act", "none", "human at sign-off"),
    ]


# ----------------------------------------------------------------------------
# Stub rows — used only when no [[gates.checks]] are configured (offline slice).
# ----------------------------------------------------------------------------
def _stub_rows(bundle: Path) -> list[dict]:
    return [
        _row("C1 spec", "none", "brief.md"),
        _row("C2 repro (red pre-fix)", "pass", "fixture (stub)", path_line="examples/toy", gating=True),
        _row("C3 change", "none", "patch.diff"),
        _row("C4 verification (green post-fix)", "pass", "shipped test (stub)", gating=True),
        _row("C4 regression", "pass", "existing suite (stub)", gating=True),
        *(_row(f"T{n} {label}", "pass", f"{oracle} (stub)", rule_id=f"T{n}-stub", gating=True)
          for n, label, oracle in (
              (1, "structure", "structural validator"),
              (2, "shape", "semgrep"),
              (3, "runtime", "find_spec / clean-env suite"),
              (4, "contribution", "commit-msg / branch-target / version-bump"),
          )),
        *_judgment_rows(),
    ]


def _stub_repo_rows() -> list[dict]:
    return [r for r in _stub_rows(Path(".")) if r["gating"]] + _judgment_rows()


def render_md(result: dict) -> str:
    lines = [
        f"# Check gates — {result['issue_dir']}",
        "",
        f"**Overall (gating): {result['overall']}**",
        "",
        "| Check | Result | Oracle | Rule | Evidence | Gating |",
        "|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        lines.append(
            f"| {r['check']} | {r['result']} | {r['oracle']} | "
            f"{r['rule_id'] or '—'} | {r['path_line'] or '—'} | "
            f"{'yes' if r['gating'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"
