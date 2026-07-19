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

import re
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


def target_checkouts(cfg: Config, bundles: list[Path] | None = None) -> list[Path]:
    """Every target checkout the harness may have left siblings next to: all configured
    ``[publisher.checkouts]`` entries plus each bundle's resolved target — which covers
    the sibling-convention fallback, the common setup with NO explicit checkout map
    (#297 review). ``bundles=None`` ⇒ every persisted ``issue_*`` bundle, so the manual
    ``pdca sweep`` and the doctor discover targets without an active flow. Only real
    git checkouts qualify."""
    from . import publish  # lazy: publish imports leaves→worktree; avoid an import cycle
    if bundles is None:
        # Archived completed/ bundles (#171) count too (#297 review round 2): an
        # installation that archived everything still has the worktrees on disk, and
        # they may be the ONLY record of the sibling-convention targets.
        roots = (cfg.bundle_root, cfg.bundle_root / "completed")
        bundles = sorted(d for root in roots if root.exists()
                         for d in root.glob("issue_*") if d.is_dir())
    candidates: dict[Path, None] = {}
    for spec in cfg.repo_checkouts:
        candidates.setdefault(publish._checkout_path(cfg, spec), None)
    for d in bundles:
        try:
            repo_spec, _base, _slug = publish._resolve_target(d)
        except Exception:  # noqa: BLE001 — resolution is best-effort here
            continue
        if repo_spec:
            candidates.setdefault(publish._checkout_path(cfg, repo_spec), None)
    return [p for p in candidates if (p / ".git").exists()]


def _lane_dirs(primary: Path) -> list[Path]:
    """The per-lane Do/Check worktrees for ``primary`` — EXACTLY the names the harness
    creates (``<name>.pdca-wt`` / ``<name>.pdca-wt-l<slot>``), never a loose prefix
    match (#297 review): a sibling like ``<name>.pdca-wt-backup`` is not ours and must
    never be touched, let alone rmtree'd by the removal fallback."""
    exact = re.compile(re.escape(primary.name + worktree.WT_SUFFIX) + r"(-l\d+)?$")
    return sorted(p for p in primary.parent.glob(primary.name + worktree.WT_SUFFIX + "*")
                  if p.is_dir() and exact.fullmatch(p.name))


def _integ_dirs(primary: Path) -> list[Path]:
    """The integration worktrees for ``primary`` (``<name>.pdca-integ-<base>``)."""
    return sorted(p for p in primary.parent.glob(primary.name + integrate.INTEG_INFIX + "*")
                  if p.is_dir())


def _registered_worktree(primary: Path, wt: Path) -> bool:
    """True iff ``wt`` is a worktree REGISTERED to ``primary`` (`git worktree list
    --porcelain`). A ``.git`` entry alone proves nothing (#297 review round 2): a
    standalone clone that merely matches our sibling naming has a ``.git`` DIRECTORY,
    fails ``git worktree remove``, and the rmtree fallback would eat an unrelated
    repository. Registration is the authoritative "the harness created this here"."""
    proc = subprocess.run(["git", "-C", str(primary), "worktree", "list", "--porcelain"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    registered = {line[len("worktree "):].strip()
                  for line in proc.stdout.splitlines() if line.startswith("worktree ")}
    try:
        target = str(wt.resolve())
    except OSError:
        return False
    return any(target == str(Path(p).resolve()) for p in registered)


def _remove_tree(primary: Path, wt: Path) -> bool:
    """``git worktree remove`` with the rmtree + prune fallback (the drift.py pattern),
    plus the owner sidecar. Refuses (False) anything not REGISTERED as a worktree of
    ``primary`` — whatever it is (a plain dir, a standalone clone that matches our
    naming), the harness did not create it and the rmtree fallback must never eat it
    (#297 review). Best-effort otherwise."""
    if not _registered_worktree(primary, wt):
        return False
    if _git(primary, "worktree", "remove", "--force", str(wt)) != 0:
        shutil.rmtree(wt, ignore_errors=True)
        _git(primary, "worktree", "prune")
    worktree._owner_file(wt).unlink(missing_ok=True)
    return True


def sweep(cfg: Config, bundles: list[Path] | None = None, *,
          mode: str | None = None, dry_run: bool = False) -> list[str]:
    """Reclaim harness worktree/build footprint; return human-readable report lines.

    ``bundles=None`` discovers targets from every persisted ``issue_*`` bundle (the
    manual command / sibling-convention setups, #297 review); the flow passes its
    run's bundles. ``mode`` overrides ``cfg.sweep_worktrees`` (the CLI passes it
    explicitly, so the manual command works even under ``"off"``). ``dry_run``
    reports without touching. Never raises: a failing target is reported and skipped
    (teardown must not fail a run); sizes are deliberately not computed (no ``du``
    over a 200 GB tree).
    """
    mode = mode or cfg.sweep_worktrees
    if mode not in MODES:  # defensive: config.load already normalizes
        mode = "clean"
    if mode == "off":
        return []
    lines: list[str] = []
    verb = "would " if dry_run else ""
    for primary in target_checkouts(cfg, bundles):
        try:
            # Overflow trees: reclaim only PROVEN orphans (creator pid gone, #297
            # review) — a live pid may be another process's in-flight gate read, and
            # deleting its working directory mid-command invalidates that gate. And
            # only REGISTERED worktrees (#297 review round 4): overflow_remove's
            # rmtree fallback would otherwise eat an unrelated dir that merely
            # matches the `…-ovf-<pid>-*` pattern — the same guard lanes/integs get.
            candidates = worktree.orphan_overflow_dirs(primary)
            orphans = [o for o in candidates if _registered_worktree(primary, o)]
            for unowned in (o for o in candidates if o not in orphans):
                lines.append(f"sweep: left {unowned.name} (not a worktree registered "
                             f"to {primary.name} — not ours to remove)")
            live = len(worktree._overflow_dirs(primary)) - len(candidates)
            if orphans:
                lines.append(f"sweep: {verb}remove {len(orphans)} orphaned overflow "
                             f"tree(s) next to {primary.name}")
            if live:
                lines.append(f"sweep: left {live} overflow tree(s) next to "
                             f"{primary.name} (owner process still alive)")
            if not dry_run:
                for ovf in orphans:
                    worktree.overflow_remove(primary, ovf)
            for integ in _integ_dirs(primary):
                if not _registered_worktree(primary, integ):
                    lines.append(f"sweep: left {integ.name} (not a worktree registered "
                                 f"to {primary.name} — not ours to remove)")
                    continue
                lines.append(f"sweep: {verb}remove integration tree {integ.name}")
                if not dry_run:
                    _remove_tree(primary, integ)
            for lane_wt in _lane_dirs(primary):
                if mode == "remove":
                    if not _registered_worktree(primary, lane_wt):
                        lines.append(f"sweep: left {lane_wt.name} (not a worktree "
                                     f"registered to {primary.name} — not ours to remove)")
                        continue
                    lines.append(f"sweep: {verb}remove lane worktree {lane_wt.name}")
                    if not dry_run:
                        _remove_tree(primary, lane_wt)
                else:
                    # The registration guard applies to CLEAN too (#297 review round 3):
                    # `clean -fdxq` + `reset --hard` are just as destructive as removal,
                    # and an unrelated clone/symlink squatting on the exact lane path
                    # must never have its work stripped by them.
                    if not _registered_worktree(primary, lane_wt):
                        lines.append(f"sweep: left {lane_wt.name} (not a worktree "
                                     f"registered to {primary.name} — not ours to clean)")
                        continue
                    lines.append(f"sweep: {verb}clean lane worktree {lane_wt.name} "
                                 "(build artifacts dropped, checkout kept)")
                    if not dry_run:
                        if (_git(lane_wt, "clean", "-fdxq") != 0
                                or _git(lane_wt, "reset", "--hard") != 0):
                            lines.append(f"sweep: {lane_wt.name}: clean/reset failed "
                                         "(left as is)")
                        else:
                            # The reset stripped the bundle's patch, so the owner stamp
                            # no longer describes the tree's CONTENT — a later gate read
                            # trusting it would false-green against the unpatched base
                            # (#297 review round 2). Clear it; the next read re-populates.
                            worktree._owner_file(lane_wt).unlink(missing_ok=True)
            if not dry_run:
                _git(primary, "worktree", "prune")
        except Exception as exc:  # noqa: BLE001 — teardown must never fail a run
            lines.append(f"sweep: {primary.name}: {type(exc).__name__}: {exc} (skipped)")
    return lines
