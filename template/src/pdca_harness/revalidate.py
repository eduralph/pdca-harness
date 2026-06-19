"""``pdca revalidate`` — re-gate a frozen bundle against the current engine (issue #11).

A bundle's ``check-gates.json`` is written once at Check time and frozen when the
bundle goes COMPLETE; that immutability is correct — the bundle is the record of what
was decided. But the gates run against a *moving* substrate (the engine code, the
conformance ruleset, the dependency repos under test). When those improve, a frozen
``FAIL`` the current engine would never reproduce becomes indistinguishable from a real
failure the human knowingly accepted.

``revalidate`` re-runs the **same single-sourced gate set** as ``pdca gates``
(:func:`gates.run_gates_dry` — which never writes the frozen file) against the current
engine and records an **additive, dated** stamp ``revalidation-<date>.json`` recording
each row's ``old → new`` result. It **never** mutates ``check-gates.json`` /
``check-gates.md`` or ``SUMMARY.md`` §9 — the original decision stands. A changed result
in *either* direction is a delta; a frozen ``PASS`` now ``FAIL`` is a real regression
signal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import gates, state
from .config import Config


def revalidate(cfg: Config, d: Path, date: str) -> dict:
    """Re-gate COMPLETE bundle ``d`` against the current engine; write a dated stamp.

    Returns the revalidation result and writes ``revalidation-<date>.json`` into the
    bundle — additive (one stamp per date, never overwriting a prior one) and never
    touching the frozen ``check-gates.json`` / ``check-gates.md`` / §9.
    """
    frozen = json.loads((d / "check-gates.json").read_text(encoding="utf-8"))
    # A close-disposition bundle (issue #60) was frozen with the N/A matrix and has no
    # patch — re-running the real gates would apply a nonexistent patch and report a
    # spurious delta. Re-gate it with the same close matrix so it confirms, not drifts.
    fresh = (gates.run_close_gates_dry(d, cfg) if (d / state.CLOSE_MARKER).exists()
             else gates.run_gates_dry(d, cfg))

    old_by = {_row_key(r): r for r in frozen.get("rows", [])}
    new_by = {_row_key(r): r for r in fresh.get("rows", [])}
    # Union, preserving the frozen order then any rows the current engine added.
    keys = list(old_by) + [k for k in new_by if k not in old_by]

    rows = []
    for key in keys:
        o, n = old_by.get(key), new_by.get(key)
        ref = o or n
        old_res = o["result"] if o else None
        new_res = n["result"] if n else None
        rows.append({
            "check": ref["check"],
            "element": ref.get("element", ""),
            "rule_id": ref.get("rule_id", ""),
            "gating": (n or o).get("gating", False),
            "old": old_res,
            "new": new_res,
            "changed": old_res != new_res,
        })

    result = {
        "date": date,
        "engine_rev": _engine_rev(cfg.root),
        "bundle": d.name,
        "frozen_overall": frozen.get("overall"),
        "current_overall": fresh.get("overall"),
        "changed": any(r["changed"] for r in rows),
        # A gating row that was PASS and is now FAIL is the load-bearing signal.
        "regression": any(r["gating"] and r["old"] == "pass" and r["new"] == "fail"
                          for r in rows),
        "rows": rows,
    }
    (d / f"revalidation-{date}.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def render_md(result: dict) -> str:
    """A compact old→new delta table; changed rows are the deltas, the rest confirm."""
    head = f"# Revalidation — {result['bundle']} — {result['date']}"
    rev = f" (engine {result['engine_rev']})" if result.get("engine_rev") else ""
    verdict = ("REGRESSION — a frozen PASS is now FAIL" if result["regression"]
               else "deltas found" if result["changed"]
               else "no change — frozen record confirmed against the current engine")
    lines = [head + rev, "", f"**{verdict}.** The frozen check-gates.json is unchanged.",
             "", "| Check | Old | New | Δ | Gating |", "|---|---|---|---|---|"]
    for r in result["rows"]:
        delta = "→" if r["changed"] else ""
        lines.append(f"| {r['check']} | {r['old'] or '—'} | {r['new'] or '—'} | "
                     f"{delta} | {'yes' if r['gating'] else 'no'} |")
    return "\n".join(lines) + "\n"


def deltas(d: Path) -> list[str]:
    """One-line summaries of every changed row across ``d``'s revalidation stamps.

    Read-only; for the Act bundle index to surface staleness where Act already looks.
    """
    out: list[str] = []
    for stamp in sorted(d.glob("revalidation-*.json")):
        try:
            res = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for r in res.get("rows", []):
            if r.get("changed"):
                out.append(f"{r['check']} {r['old']}→{r['new']} ({res.get('date', '?')})")
    return out


def _row_key(row: dict) -> tuple[str, str, str]:
    """Stable identity for a gate row across re-gates: matrix element + rule + label."""
    return (row.get("element", ""), row.get("rule_id", ""), row.get("check", ""))


def _engine_rev(root: Path) -> str:
    """Short git rev of the engine under test — best-effort provenance, never fatal."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 — provenance is a nicety, absence is not an error
        return ""
