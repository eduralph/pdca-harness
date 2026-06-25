"""Integration-branch stacking — fold each wave's accepted work onto a run-scoped
branch the next wave builds on (the default, fork-safe wave sequencing).

After a wave's bundles are accepted, the *next* wave must build on a base that already
contains this wave's diffs — otherwise a dependent built off the untouched base misses
its prerequisite's change and conflicts. Rather than *merge* the wave's PRs (which needs
merge rights on the upstream base — impossible in a fork model — and relaxes the STOP
discipline), this folds every accepted patch onto a single run-scoped **integration
branch** on ``origin`` (push-only — a fork has push). The next wave's Do worktree and
its stacked PRs base off that branch, so a dependent batch completes in one run as a
reviewable PR stack the human merges bottom-up — generalising the single-chain
``Stacks on`` (#123) to whole waves, and fixing its multi-parent gap (the branch carries
*all* prerequisites, not just ``parents[0]``).

Idempotent + resumable: :func:`fold` rebuilds the branch from the target base every call,
applying the **cumulative** accepted patches (waves 0..k) in order — so a re-run
reproduces the same branch, and a patch that no longer applies (an undeclared cross-wave
overlap) is a loud :class:`IntegrationError` that stops the run before the next wave
builds on a broken base. Mechanics are deterministic ``git`` subprocesses (no model).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import publish
from .config import Config


class IntegrationError(RuntimeError):
    """A wave's accepted work could not be folded onto the integration branch — a patch
    no longer applies (undeclared overlap), or a git step failed. The caller STOPs rather
    than build the next wave on an incomplete base."""


def integration_branch(cfg: Config, base: str) -> str:
    """The run-scoped integration branch for a target ``base`` — deterministic, so a
    resumed run rebuilds the same branch (the ``/`` in a base like ``release/2`` is
    flattened so the ref is always a single segment under ``pdca-integration/``)."""
    return "pdca-integration/" + base.replace("/", "-")


def _has_patch(d: Path) -> bool:
    """True iff the bundle carries a non-empty ``patch.diff`` (something to integrate)."""
    p = d / "patch.diff"
    return p.is_file() and bool(p.read_text(encoding="utf-8").strip())


def _git(repo: Path, *args: str) -> int:
    """Run ``git -C repo args`` quietly; return the exit code (no raise)."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode


def _integ_worktree(primary: Path) -> Path:
    """The dedicated worktree the integration branch is assembled in — a sibling of the
    primary checkout, reused (reset) across folds, never the Do/Check lane worktrees."""
    return primary.parent / (primary.name + ".pdca-integ")


def _targeted(patched: list[Path]) -> list[tuple[Path, str, str]]:
    """``(bundle, repo_spec, base)`` for each patched bundle that resolves a usable
    upstream target; bundles with no target (non-contributing cycles) are dropped."""
    out: list[tuple[Path, str, str]] = []
    for d in patched:
        repo_spec, base, _slug = publish._resolve_target(d)
        if repo_spec and base:
            out.append((d, repo_spec, base))
    return out


def fold(cfg: Config, accepted: list[Path], *, dry_run: bool = False
         ) -> tuple[str | None, Path | None]:
    """Fold the cumulative accepted bundles' patches onto the integration branch.

    ``accepted`` is every accepted bundle (waves 0..k) in stack order (the caller passes
    them wave by wave, name-sorted within each). Returns ``(branch, worktree)`` — the
    integration branch and the worktree it was built in (for an optional re-gate) — or
    ``(None, None)`` when there is nothing to integrate (no patches, or none with a
    target). Raises :class:`IntegrationError` on a real failure (a patch no longer
    applies, or a git step fails) so the caller STOPs.

    Dry-run (offline rehearse / CI, where the publisher leaf is stubbed) prints the git
    plan and returns ``(branch, None)`` without a worktree or a push — the next wave's
    worktree then falls back to the target base, which is what an offline rehearse wants.
    """
    targeted = _targeted([d for d in accepted if _has_patch(d)])
    if not targeted:
        return (None, None)  # nothing to integrate — the next wave builds on the base

    # One integration line per (repo, base); a batch targeting one repo+base (the common
    # case) folds onto one branch. A bundle naming a different target shares no base to
    # stack on — exclude it, loudly, rather than misapply its patch.
    _, repo_spec, base = targeted[0]
    same = [d for (d, rs, b) in targeted if (rs, b) == (repo_spec, base)]
    other = [d.name for (d, rs, b) in targeted if (rs, b) != (repo_spec, base)]
    if other:
        print(f"integrate: {len(other)} accepted bundle(s) target a different repo/base "
              f"than {repo_spec}@{base} — excluded from the fold ({', '.join(other)}).",
              file=sys.stderr)

    branch = integration_branch(cfg, base)
    base_remote = cfg.base_remote
    repo = publish._checkout_path(cfg, repo_spec)

    if dry_run:
        print(f"integrate --dry-run — fold {len(same)} patch(es) onto {branch} "
              f"(off {base_remote}/{base} on {repo_spec}):")
        print(f"  git worktree → {_integ_worktree(repo)} @ {base_remote}/{base}; "
              f"checkout -B {branch}")
        for d in same:
            print(f"  git apply {(d / 'patch.diff')}  &&  git commit   ({d.name})")
        print(f"  git push --force origin {branch}")
        return (branch, None)

    wt = _prepare_worktree(repo, base_remote, base)
    if _git(wt, "checkout", "-B", branch, f"{base_remote}/{base}") != 0:
        raise IntegrationError(f"could not start {branch} off {base_remote}/{base}")
    for d in same:
        patch = (d / "patch.diff").resolve()
        if _git(wt, "apply", str(patch)) != 0:
            raise IntegrationError(
                f"{d.name}'s patch does not apply onto {branch} — an undeclared "
                f"cross-wave overlap; declare the conflict / re-order, then re-run")
        _git(wt, "add", "--all")
        if _git(wt, "commit", "-m", f"pdca-integrate: {d.name}") != 0:
            raise IntegrationError(f"could not commit {d.name} onto {branch}")
    # A harness-owned, rebuilt-each-run branch: a plain force is correct (every fold
    # rewrites it off the base), and it isn't a human PR branch needing lease safety.
    if _git(wt, "push", "--force", "origin", branch) != 0:
        raise IntegrationError(
            f"could not push {branch} to origin — the next wave cannot stack on it")
    return (branch, wt)


def _prepare_worktree(repo: Path, base_remote: str, base: str) -> Path:
    """Create (or reuse) the integration worktree off the freshly-fetched base; raise
    :class:`IntegrationError` if it can't be made (worktree isolation is required here —
    unlike Do/Check, there is no in-place fallback that would still produce the branch)."""
    if not (repo / ".git").exists():
        raise IntegrationError(f"checkout not found at {repo}")
    _git(repo, "fetch", base_remote)
    if base_remote != "origin":
        _git(repo, "fetch", "origin")
    wt = _integ_worktree(repo)
    if not (wt / ".git").exists() and _git(repo, "worktree", "add", "--force",
                                           str(wt), f"{base_remote}/{base}") != 0:
        raise IntegrationError(f"could not create the integration worktree at {wt}")
    return wt
