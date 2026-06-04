"""Deterministic Check gates → ``check-gates.json`` (docs 02 / 04, the gates path).

The gates are the *only* blocking path in Check — no model in the gating loop.
Each gate is a callable that returns rows; the driver and CI invoke the **same**
single-sourced implementation (docs 04 §Single-sourcing). This module ships the
5/5/1 row skeleton and stub tier implementations that all PASS, so the vertical
slice runs offline. Replace each ``_stub_*`` with the real validator / semgrep /
suite / hooks for your project; the row contract stays identical.

A row: {check, result, oracle, rule_id, path_line, gating}. ``result`` is one of
``pass`` / ``fail`` / ``none``. A ``none`` row is a judgment cell decided by the
reviewer + human (docs 04 §Inside the judgment cell), listed here only so the
table aligns 1-for-1 with the matrix — it never gates.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config


def run_gates(d: Path, cfg: Config) -> dict:
    """Run every gate over bundle ``d``; write ``check-gates.{json,md}``.

    Returns the result dict (also written to disk). ``overall`` is ``fail`` iff
    any *gating* row failed — that is the merge-blocking signal.
    """
    rows: list[dict] = []
    rows += _correctness(d, cfg)
    rows += _conformance(d, cfg)

    gating_fail = any(r["gating"] and r["result"] == "fail" for r in rows)
    result = {
        "issue_dir": d.name,
        "overall": "fail" if gating_fail else "pass",
        "rows": rows,
    }
    (d / "check-gates.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (d / "check-gates.md").write_text(_render_md(result), encoding="utf-8")
    return result


# ----------------------------------------------------------------------------
# Row builders. Gating rows carry an oracle and a path:line; judgment cells are
# recorded as result="none" (decided in check-review.md + SUMMARY §6 / §9).
# ----------------------------------------------------------------------------
def _row(check, result, oracle, rule_id="", path_line="", gating=False) -> dict:
    return {
        "check": check,
        "result": result,
        "oracle": oracle,
        "rule_id": rule_id,
        "path_line": path_line,
        "gating": gating,
    }


def _correctness(d: Path, cfg: Config) -> list[dict]:
    # C1 spec and C3 change are inputs to Check, not gates (docs 04 obs. #1).
    return [
        _row("C1 spec", "none", "brief.md"),
        _stub_repro(d, cfg),
        _row("C3 change", "none", "patch.diff"),
        _stub_verify(d, cfg),
        _stub_regression(d, cfg),
        _row(
            "C5 causal adequacy",
            "none",
            "reviewer + human sign-off",
        ),
    ]


def _conformance(d: Path, cfg: Config) -> list[dict]:
    return [
        _stub_tier(d, cfg, 1, "structure", "structural validator"),
        _stub_tier(d, cfg, 2, "shape", "semgrep"),
        _stub_tier(d, cfg, 3, "runtime", "find_spec / clean-env suite"),
        _stub_tier(d, cfg, 4, "contribution", "commit-msg / branch-target / version-bump"),
        _row("T5 judgment", "none", "reviewer + human sign-off"),
        _row("Validation act", "none", "human at sign-off"),
    ]


# ----------------------------------------------------------------------------
# Stub implementations — all PASS. Swap for real, single-sourced gates.
# Each returns a gating row so the slice exercises the blocking path shape.
# ----------------------------------------------------------------------------
def _stub_repro(d: Path, cfg: Config) -> dict:
    return _row("C2 repro (red pre-fix)", "pass", "fixture (stub)", path_line="examples/toy", gating=True)


def _stub_verify(d: Path, cfg: Config) -> dict:
    return _row("C4 verification (green post-fix)", "pass", "shipped test (stub)", gating=True)


def _stub_regression(d: Path, cfg: Config) -> dict:
    return _row("C4 regression", "pass", "existing suite (stub)", gating=True)


def _stub_tier(d: Path, cfg: Config, n: int, name: str, oracle: str) -> dict:
    return _row(f"T{n} {name}", "pass", f"{oracle} (stub)", rule_id=f"T{n}-stub", gating=True)


def _render_md(result: dict) -> str:
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
