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
from pathlib import Path

from . import publish
from .config import Config


class IntegrationError(RuntimeError):
    """A wave's accepted work could not be folded onto the integration branch — a patch
    no longer applies (undeclared overlap), or a git step failed. The caller STOPs rather
    than build the next wave on an incomplete base."""


def integration_branch(cfg: Config, base: str) -> str:
    """The run-scoped integration branch for a target ``base`` — deterministic (a resumed run
    rebuilds the same branch) and **injective in the base** (#187): the base is flattened to a
    single ref segment under ``pdca-integration/`` via :func:`_flatten_base`, so two bases that
    differ only by ``/`` vs ``-`` (``release/2.0`` vs ``release-2.0``) never collide onto one
    branch and force-push over each other's fold."""
    return "pdca-integration/" + _flatten_base(base)


def _flatten_base(base: str) -> str:
    """Map a base ref to a single, **injective** branch segment: double every existing ``-``
    first, then map ``/`` → ``-``. So ``release/2.0`` → ``release-2.0`` while ``release-2.0``
    → ``release--2.0`` — distinct (a single ``-`` in the output can only come from a ``/``,
    a ``--`` only from a ``-``), and the result has no ``/`` so there's no branch dir/file
    conflict either (#187)."""
    return base.replace("-", "--").replace("/", "-")


def _has_patch(d: Path) -> bool:
    """True iff the bundle carries a non-empty ``patch.diff`` (something to integrate)."""
    p = d / "patch.diff"
    return p.is_file() and bool(p.read_text(encoding="utf-8").strip())


def _git(repo: Path, *args: str) -> int:
    """Run ``git -C repo args`` quietly; return the exit code (no raise)."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode


def _integ_worktree(primary: Path, base: str) -> Path:
    """The dedicated worktree a target's integration branch is assembled in — a sibling of
    the primary checkout, keyed by ``base`` (injective, like the branch) so two bases on the
    same repo don't share one worktree (#187), reused (reset) across folds, never the Do/Check
    lane worktrees."""
    return primary.parent / (primary.name + ".pdca-integ-" + _flatten_base(base))


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
         ) -> dict[tuple[str, str], tuple[str, Path | None]]:
    """Fold the cumulative accepted bundles' patches onto a per-target integration branch.

    ``accepted`` is every accepted bundle (waves 0..k) in stack order (the caller passes
    them wave by wave, name-sorted within each). Bundles are grouped by their upstream
    ``(repo, base)`` target and **each group folds onto its own integration branch** — a
    batch spanning several targets (two repos, or two base branches on one repo) keeps one
    integration line per target, so a later wave's bundle stacks on the branch for *its* own
    target, never a sibling target's (#187). Returns ``{(repo, base): (branch, worktree)}``
    — each target's integration branch and the worktree it was built in (for an optional
    re-gate) — or ``{}`` when there is nothing to integrate (no patches, or none with a
    target). Raises :class:`IntegrationError` on a real failure (a patch no longer applies,
    or a git step fails) so the caller STOPs.

    Dry-run (offline rehearse / CI, where the publisher leaf is stubbed) prints each group's
    git plan and returns the branches with ``None`` worktrees — no worktree, no push — so the
    next wave falls back to the target base, which is what an offline rehearse wants.
    """
    targeted = _targeted([d for d in accepted if _has_patch(d)])
    if not targeted:
        return {}  # nothing to integrate — the next wave builds on the base

    # One integration line per (repo, base): group the accepted bundles by target so a
    # multi-target batch folds each onto its own branch (the common single-target batch is
    # just one group). Preserve stack order within a group (targeted keeps accepted's order).
    groups: dict[tuple[str, str], list[Path]] = {}
    for d, repo_spec, base in targeted:
        groups.setdefault((repo_spec, base), []).append(d)

    base_remote = cfg.base_remote
    result: dict[tuple[str, str], tuple[str, Path | None]] = {}
    for (repo_spec, base), bundles in groups.items():
        branch = integration_branch(cfg, base)
        repo = publish._checkout_path(cfg, repo_spec)
        if dry_run:
            print(f"integrate --dry-run — fold {len(bundles)} patch(es) onto {branch} "
                  f"(off {base_remote}/{base} on {repo_spec}):")
            print(f"  git worktree → {_integ_worktree(repo, base)} @ {base_remote}/{base}; "
                  f"checkout -B {branch}")
            for d in bundles:
                print(f"  git apply {(d / 'patch.diff')}  &&  git commit   ({d.name})")
            print(f"  git push --force origin {branch}")
            result[(repo_spec, base)] = (branch, None)
            continue

        wt = _prepare_worktree(repo, base_remote, base)
        if _git(wt, "checkout", "-B", branch, f"{base_remote}/{base}") != 0:
            raise IntegrationError(f"could not start {branch} off {base_remote}/{base}")
        for d in bundles:
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
        result[(repo_spec, base)] = (branch, wt)
    return result


def _prepare_worktree(repo: Path, base_remote: str, base: str) -> Path:
    """Create (or reuse) the integration worktree off the freshly-fetched base; raise
    :class:`IntegrationError` if it can't be made (worktree isolation is required here —
    unlike Do/Check, there is no in-place fallback that would still produce the branch)."""
    if not (repo / ".git").exists():
        raise IntegrationError(f"checkout not found at {repo}")
    _git(repo, "fetch", base_remote)
    if base_remote != "origin":
        _git(repo, "fetch", "origin")
    wt = _integ_worktree(repo, base)
    if not (wt / ".git").exists() and _git(repo, "worktree", "add", "--force",
                                           str(wt), f"{base_remote}/{base}") != 0:
        raise IntegrationError(f"could not create the integration worktree at {wt}")
    return wt
