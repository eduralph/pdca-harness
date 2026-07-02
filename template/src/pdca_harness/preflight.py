"""Per-lane resource preflight (issue #213) — verify a ``lanes > 1`` fan-out's per-lane
resources exist BEFORE driving, so a batch never runs against missing lane worktrees /
containers / ports and silently produces a pile of false-red bundles.

Deterministic, opt-in, and resource-agnostic: it runs the instance's OWN declared checks and
never learns what a "lane" resource is. Two declarations, either or both:

  1. the **REQUIRED** ``per_lane`` ``[[doctor.checks]]`` — reused and expanded over
     ``[driver].lanes`` (the same ``{lane}`` / ``{lanes}`` rows ``pdca doctor`` runs), so an
     instance that already declares its lane resources as doctor rows needs no new config;
  2. ``[driver].lane_preflight = "<cmd>"`` — a single shell command (``{lanes}``
     interpolated) run once, the escape hatch for resources not expressed as doctor rows.

A serial (``lanes <= 1``) run never preflights; nothing declared ⇒ a clean pass (no-op), so
today's behaviour is unchanged.
"""

from __future__ import annotations

import subprocess

from . import doctor
from .config import Config


def lane_preflight(cfg: Config) -> tuple[bool, list[str]]:
    """``(ok, messages)`` — ``ok`` is False iff a declared per-lane check/command failed;
    ``messages`` are the failure hints to print. A no-op ``(True, [])`` when ``lanes <= 1``
    or nothing is declared."""
    if cfg.lanes <= 1:
        return True, []
    ok = True
    messages: list[str] = []

    # 1. REQUIRED per_lane doctor rows, expanded 0..lanes-1 (reuse doctor's expansion).
    per_lane_required = [c for c in getattr(cfg, "doctor_checks", [])
                         if c.get("per_lane") and c.get("required")]
    for row in doctor._expand_checks(per_lane_required, cfg.lanes):
        rc = subprocess.run(row["cmd"], shell=True, capture_output=True,
                            cwd=cfg.root).returncode
        if rc != 0:
            ok = False
            hint = row.get("hint", "")
            messages.append(f"lane check '{row['id']}' failed"
                            + (f" — {hint}" if hint else ""))

    # 2. The generic [driver].lane_preflight command (its own output streams to the user).
    if cfg.lane_preflight:
        cmd = cfg.lane_preflight.replace("{lanes}", str(cfg.lanes))
        rc = subprocess.run(cmd, shell=True, cwd=cfg.root).returncode
        if rc != 0:
            ok = False
            messages.append(f"[driver].lane_preflight failed (rc {rc}): {cmd}")

    return ok, messages
