"""Reclaim the harness's own worktree/build footprint (issue #297).

The isolation model leaves durable siblings next to every target checkout: per-lane
Do/Check worktrees (``<name>.pdca-wt[-l<slot>]``, reset-and-reused but never removed),
per-base integration worktrees (``<name>.pdca-integ-<base>``, reused across folds,
never removed), and — after a crash — orphaned overflow trees (``<name>.pdca-wt-ovf-*``,
whose sweeper existed but had no caller). Their build dirs (``target/``, ``node_modules``,
…) dominate: a long-running instance accumulated >200 GB and its *gating* gates started
false-redding with ``Disk quota exceeded`` — an environment fault misattributed to the
patch until a human traced it.

``sweep()`` reclaims that footprint at the publish/freeze boundary (the flow calls it
after a run's waves complete, when nothing reuses the trees) and on demand via
``pdca sweep``. What it does per target checkout is set by ``[driver].sweep_worktrees``:

* ``"clean"`` (default) — lane worktrees are kept as warm checkouts but stripped of
  build state (``git clean -fdxq`` + ``reset --hard``, the bulk of the footprint);
  integration and overflow trees are removed outright (folds rebuild from the base
  every call, so their reuse value is nil).
* ``"remove"`` — lane worktrees are removed too (Do/``pdca try`` recreate on demand).
* ``"off"`` — the flow never sweeps; ``pdca sweep`` still works (explicit mode).

Best-effort throughout: teardown must never fail a run (the ``overflow_remove``
contract). Only harness-named siblings of target checkouts are touched — never the
primary checkout, never bundle artifacts. Must not run while a flow is mid-Do on the
same lanes (the flow's own call sites run after all lane threads join).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import integrate, worktree
from .config import Config

MODES = ("clean", "remove", "off")


def _git(repo: Path, *args: str) -> int:
    """Run ``git -C repo args`` quietly; return the exit code (no raise)."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode


def _primaries(cfg: Config, bundles: list[Path] | None) -> list[Path]:
    """Every target checkout the harness may have left siblings next to: all configured
    ``[publisher.checkouts]`` entries plus each driven bundle's resolved target (covers
    the sibling-convention fallback). Only real git checkouts qualify."""
    from . import publish  # lazy: publish imports leaves→worktree; avoid an import cycle
    candidates: dict[Path, None] = {}
    for spec in cfg.repo_checkouts:
        candidates.setdefault(publish._checkout_path(cfg, spec), None)
    for d in bundles or []:
        try:
            repo_spec, _base, _slug = publish._resolve_target(d)
        except Exception:  # noqa: BLE001 — resolution is best-effort here
            continue
        if repo_spec:
            candidates.setdefault(publish._checkout_path(cfg, repo_spec), None)
    return [p for p in candidates if (p / ".git").exists()]


def _lane_dirs(primary: Path) -> list[Path]:
    """The per-lane Do/Check worktrees for ``primary`` (``<name>.pdca-wt[-l<slot>]``) —
    dirs only, excluding overflow trees and the ``.owner`` sidecar files."""
    return sorted(p for p in primary.parent.glob(primary.name + worktree.WT_SUFFIX + "*")
                  if p.is_dir() and worktree._OVF_SUFFIX not in p.name)


def _integ_dirs(primary: Path) -> list[Path]:
    """The integration worktrees for ``primary`` (``<name>.pdca-integ-<base>``)."""
    return sorted(p for p in primary.parent.glob(primary.name + integrate.INTEG_INFIX + "*")
                  if p.is_dir())


def _remove_tree(primary: Path, wt: Path) -> None:
    """``git worktree remove`` with the rmtree + prune fallback (the drift.py pattern),
    plus the owner sidecar. Best-effort."""
    if _git(primary, "worktree", "remove", "--force", str(wt)) != 0:
        shutil.rmtree(wt, ignore_errors=True)
        _git(primary, "worktree", "prune")
    worktree._owner_file(wt).unlink(missing_ok=True)


def sweep(cfg: Config, bundles: list[Path] | None = None, *,
          mode: str | None = None, dry_run: bool = False) -> list[str]:
    """Reclaim harness worktree/build footprint; return human-readable report lines.

    ``mode`` overrides ``cfg.sweep_worktrees`` (the CLI passes it explicitly, so the
    manual command works even under ``"off"``). ``dry_run`` reports without touching.
    Never raises: a failing target is reported and skipped (teardown must not fail a
    run); sizes are deliberately not computed (no ``du`` over a 200 GB tree).
    """
    mode = mode or cfg.sweep_worktrees
    if mode not in MODES:  # defensive: config.load already normalizes
        mode = "clean"
    if mode == "off":
        return []
    lines: list[str] = []
    verb = "would " if dry_run else ""
    for primary in _primaries(cfg, bundles):
        try:
            ovf = worktree._overflow_dirs(primary)
            if ovf:
                lines.append(f"sweep: {verb}remove {len(ovf)} orphaned overflow tree(s) "
                             f"next to {primary.name}")
            if not dry_run:
                worktree.sweep_overflow(primary)  # prunes git admin state too
            for integ in _integ_dirs(primary):
                lines.append(f"sweep: {verb}remove integration tree {integ.name}")
                if not dry_run:
                    _remove_tree(primary, integ)
            for lane_wt in _lane_dirs(primary):
                if mode == "remove":
                    lines.append(f"sweep: {verb}remove lane worktree {lane_wt.name}")
                    if not dry_run:
                        _remove_tree(primary, lane_wt)
                else:
                    lines.append(f"sweep: {verb}clean lane worktree {lane_wt.name} "
                                 "(build artifacts dropped, checkout kept)")
                    if not dry_run:
                        if (_git(lane_wt, "clean", "-fdxq") != 0
                                or _git(lane_wt, "reset", "--hard") != 0):
                            lines.append(f"sweep: {lane_wt.name}: clean/reset failed "
                                         "(left as is)")
            if not dry_run:
                _git(primary, "worktree", "prune")
        except Exception as exc:  # noqa: BLE001 — teardown must never fail a run
            lines.append(f"sweep: {primary.name}: {type(exc).__name__}: {exc} (skipped)")
    return lines
