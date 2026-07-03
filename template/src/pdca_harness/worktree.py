"""Per-cycle git worktree isolation for Do/Check (issue #94).

A cycle's Do (builder edits the target in place) and Check (gates run against the
working tree) otherwise mutate the host's **primary checkout**, leaving it dirty and
colliding with any human work there. Instead, the harness runs Do/Check in a
dedicated git **worktree** off the target's base branch, so the primary checkout is
never touched. The worktree path is exposed to the builder and gate commands as
``$PDCA_WORKTREE``.

On by default (``[driver].worktree``); **best-effort** — a target that is missing,
not a git checkout, or whose base can't be resolved silently falls back to in-place
(returns ``None``), so enabling it never breaks a cycle. The worktree is **reset and
reused** per cycle (reset to the base before each Do), keyed by lane slot so concurrent
lanes get private worktrees (never ``cp`` a worktree — its ``.git`` is an absolute
pointer; each is created in place by ``git worktree add``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import lane
from .config import Config


def _git(repo: Path, *args: str) -> int:
    """Run ``git -C repo args``, quietly; return the exit code (no raise)."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode


def _target(d: Path, cfg: Config) -> tuple[Path, str] | None:
    """``(primary_checkout, base_ref)`` for bundle ``d``, or None if it can't be resolved.

    Single-sourced from the brief's "Repo + branch target" via the same resolution
    publish uses; ``base_ref`` is ``<base_remote>/<base>`` (the remote-tracking base the
    worktree branches off), falling back to the bare base / default branch.
    """
    from . import publish  # lazy: publish imports leaves→worktree; avoid an import cycle
    try:
        repo_spec, base, _slug = publish._resolve_target(d)
    except Exception:  # noqa: BLE001 — resolution is best-effort
        return None
    if not repo_spec:
        return None
    primary = publish._checkout_path(cfg, repo_spec)
    if not (primary / ".git").exists():  # not a git checkout → can't worktree
        return None
    base_ref = f"{cfg.base_remote}/{base}" if base else cfg.default_branch
    # Auto-stacked chain (#123): base the dependent's Do worktree on the prereq's produced
    # branch (on origin), not the target base, so Do builds + verifies on top of its diff.
    stack_branch = publish._stack_base_branch(cfg, d)
    if stack_branch:
        base_ref = f"origin/{stack_branch}"
    return primary, base_ref


def _wt_dir(primary: Path) -> Path:
    """The worktree directory for the current lane slot — a sibling of the primary
    checkout (``<name>.pdca-wt`` / ``<name>.pdca-wt-l<lane>`` under concurrency)."""
    slot = lane.current()
    suffix = ".pdca-wt" + (f"-l{slot}" if slot is not None else "")
    return primary.parent / (primary.name + suffix)


def _owner_file(wt: Path) -> Path:
    """Sidecar recording which bundle's Do last populated worktree ``wt``.

    A sibling of the worktree (``<name>.pdca-wt.owner``), so it survives the worktree's
    own ``git clean`` and never shows up as an untracked file inside the tree.
    """
    return wt.with_name(wt.name + ".owner")


def owner_of(wt: Path) -> str | None:
    """The bundle dir name (e.g. ``issue_46``) stamped into ``wt`` by :func:`ensure`.

    None if the worktree carries no marker (created by an older run, or ``ensure`` never
    stamped it). Lets a reader tell whether a reused per-lane worktree still holds *this*
    bundle's build or a later bundle's (issue #94 worktrees are reset-and-reused).
    """
    f = _owner_file(wt)
    return (f.read_text(encoding="utf-8").strip() or None) if f.exists() else None


def path(d: Path, cfg: Config) -> Path | None:
    """The active worktree for this bundle/lane if one exists on disk, else None.

    Read-only (no git): Do calls :func:`ensure` to create/reset it; Check (gates) and
    the builder env read this. Returns None when worktree isolation is off or the target
    isn't resolvable, so callers fall back to the primary checkout.
    """
    if not cfg.worktree:
        return None
    tgt = _target(d, cfg)
    if tgt is None:
        return None
    wt = _wt_dir(tgt[0])
    return wt if (wt / ".git").exists() else None


def resync(d: Path, cfg: Config) -> Path | None:
    """The worktree for this bundle/lane, guaranteed to reflect *this* bundle — for a gate.

    The per-lane worktree is reset-and-reused across bundles but swept only before each Do
    (:func:`ensure`). So a gate that reads it OUTSIDE the Do→Check cadence — a standalone
    ``pdca gates`` re-run, or the next bundle's gate before its own Do in a shared lane —
    can see a *different* bundle's leftover net-new edits and false-red on an orphan file
    that this bundle never touched (issue #224). This heals that window: when the tree is
    owned by another bundle (or is unstamped, from an older run), reset it to the base,
    clean it, re-apply THIS bundle's ``patch.diff``, and take ownership — so the gate sees
    exactly this bundle's change over a clean base, never a foreign orphan.

    When the tree is already this bundle's (the normal Do→Check path, owner stamped by
    :func:`ensure`), it is returned untouched so Check still tests the tree Do built. Like
    :func:`path`, best-effort: isolation off / unresolved target / no worktree on disk / a
    git failure returns None, and the gate falls back to the primary checkout as it does
    when isolation is off. If this bundle's ``patch.diff`` no longer applies to the base,
    the tree can't be reconstructed as this bundle's build, so it clears the stamp and
    returns None rather than present a clean base as if it were patched. No fetch — the base
    ref is already present from Do's ensure.
    """
    if not cfg.worktree:
        return None
    tgt = _target(d, cfg)
    if tgt is None:
        return None
    primary, base_ref = tgt
    wt = _wt_dir(primary)
    if not (wt / ".git").exists():
        return None
    if owner_of(wt) == d.name:
        return wt  # already this bundle's build (normal Do→Check) — leave Do's tree intact
    # A foreign / unstamped tree: heal it to THIS bundle's state before the gate reads it.
    if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-fdq") != 0:
        print(f"worktree: could not resync {wt} to {base_ref} for {d.name}; the gate "
              "falls back to the primary checkout", file=sys.stderr)
        return None
    patch = d / "patch.diff"
    if patch.is_file() and patch.read_text(encoding="utf-8").strip():
        if _git(wt, "apply", str(patch.resolve())) != 0:
            # The patch no longer applies to the base (it drifted since Do, or is corrupt):
            # the tree is now clean base, NOT this bundle's build. Do NOT hand it to the gate
            # (it would test an unpatched tree and could pass), and do NOT claim ownership —
            # a later resync would then match the stamp and skip re-applying, silently
            # greening a clean base. Clear the stamp and fall back to in-place (best-effort).
            print(f"worktree: {d.name}'s patch.diff did not apply onto {base_ref} in {wt}; "
                  "not using the worktree (gates run in place)", file=sys.stderr)
            _owner_file(wt).unlink(missing_ok=True)
            return None
    _stamp_owner(wt, d)  # this bundle now owns the healed tree
    return wt


def ensure(d: Path, cfg: Config) -> Path | None:
    """Create or reset the per-cycle worktree off the target base; return its path.

    Reset-and-reused: an existing worktree is hard-reset to the base and cleaned; a new
    one is added off the base. Best-effort — disabled, unresolved target, non-git
    checkout, or any git failure returns None (the cycle then runs in place, unchanged).
    The primary checkout is never modified (worktrees are separate working trees).
    """
    if not cfg.worktree:
        return None
    tgt = _target(d, cfg)
    if tgt is None:
        return None
    primary, base_ref = tgt
    wt = _wt_dir(primary)
    try:
        _git(primary, "fetch", cfg.base_remote)  # refresh the base; best-effort
        if base_ref.startswith("origin/") and cfg.base_remote != "origin":
            _git(primary, "fetch", "origin")  # stacked base lives on origin (#123)
        if (wt / ".git").exists():
            # Reuse: drop the prior cycle's edits, return to a clean base.
            if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-fdq") != 0:
                print(f"worktree: could not reset {wt} to {base_ref}; running in place",
                      file=sys.stderr)
                return None
            _stamp_owner(wt, d)  # this bundle's Do now owns the reused tree
            return wt
        # Create off the base. --force tolerates the base branch being checked out elsewhere.
        if _git(primary, "worktree", "add", "--force", str(wt), base_ref) != 0:
            print(f"worktree: could not create {wt} off {base_ref}; running in place",
                  file=sys.stderr)
            return None
        _stamp_owner(wt, d)
        return wt
    except Exception as exc:  # noqa: BLE001 — isolation is best-effort, never fatal
        print(f"worktree: isolation unavailable for {d.name} ({exc}); running in place",
              file=sys.stderr)
        return None


def _stamp_owner(wt: Path, d: Path) -> None:
    """Record that bundle ``d``'s Do now owns ``wt`` (best-effort; never fatal)."""
    try:
        _owner_file(wt).write_text(d.name, encoding="utf-8")
    except OSError:  # a marker we couldn't write just reads back as None (unconfirmed)
        pass
