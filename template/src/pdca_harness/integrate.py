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

import contextlib
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
    differ only by ``/`` vs ``-`` (``release/2.0`` → ``release-s2.0`` vs ``release-2.0`` →
    ``release-h2.0``) never collide onto one branch and force-push over each other's fold."""
    return "pdca-integration/" + _flatten_base(base)


def _flatten_base(base: str) -> str:
    """Map a base ref to a single, **injective** branch segment via a prefix-free escape: ``-``
    → ``-h`` first (escape the escape char), then ``/`` → ``-s``. So ``release/2.0`` →
    ``release-s2.0`` while ``release-2.0`` → ``release-h2.0`` — distinct. Every output ``-``
    unambiguously introduces one escape (``-h`` decodes to ``-``, ``-s`` to ``/``), so unlike
    the old ``-``→``--`` / ``/``→``-`` scheme this stays injective even when the base puts ``-``
    and ``/`` adjacent (``release-/2`` → ``release-h-s2`` ≠ ``release/-2`` → ``release-s-h2``,
    which both collapsed to ``release---2`` before, #199). The result has no ``/`` so there's no
    branch dir/file conflict either (#187)."""
    return base.replace("-", "-h").replace("/", "-s")


def _has_patch(d: Path) -> bool:
    """True iff the bundle carries a non-empty ``patch.diff`` (something to integrate)."""
    p = d / "patch.diff"
    return p.is_file() and bool(p.read_text(encoding="utf-8").strip())


def _git(repo: Path, *args: str) -> int:
    """Run ``git -C repo args`` quietly; return the exit code (no raise)."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).returncode


# The harness-owned sibling-dir infix for integration worktrees; single-sourced so the
# footprint sweeper (issue #297) globs exactly what this module creates.
INTEG_INFIX = ".pdca-integ-"


def _integ_worktree(primary: Path, base: str) -> Path:
    """The dedicated worktree a target's integration branch is assembled in — a sibling of
    the primary checkout, keyed by ``base`` (injective, like the branch) so two bases on the
    same repo don't share one worktree (#187), reused (reset) across folds, never the Do/Check
    lane worktrees."""
    return primary.parent / (primary.name + INTEG_INFIX + _flatten_base(base))


@contextlib.contextmanager
def integ_lock(wt: Path, *, wait: bool = True):
    """Advisory exclusive lock on an integration worktree's LIFECYCLE (#297 review
    round 6); yields whether it was acquired. :func:`fold`'s build (prepare →
    apply/commit → push) and the between-waves re-gate hold it for their whole
    critical section, and the footprint sweeper tries it non-blocking — without it, a
    flow finishing on one base could force-remove the worktree where ANOTHER process
    is mid-fold or mid-re-gate, failing that run or invalidating its re-gate result
    (`_sweep_quietly` only joins its own lane threads). The ``.lock`` sidecar lives
    NEXT TO the worktree (same convention as the lane lock), so it survives worktree
    removal and two processes racing over a recreated tree still serialize. Blocking
    for users (concurrent folds of the same target serialize instead of clobbering
    each other's ``checkout -B``); never raises itself — an unopenable/untakeable
    lock yields False and the CALLER decides (#297 review round 7): fold and the
    re-gate fail CLOSED with :class:`IntegrationError`, the sweeper leaves the tree
    alone."""
    from . import worktree  # lazy: keep integrate importable without the lock helpers
    try:
        fh = wt.with_name(wt.name + ".lock").open("w")
    except OSError:
        yield False
        return
    try:
        try:
            worktree._lock_file(fh, wait=wait)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                worktree._unlock_file(fh)
    finally:
        fh.close()


def _targeted(patched: list[Path]) -> list[tuple[Path, str, str]]:
    """``(bundle, repo_spec, base)`` for each patched bundle that resolves a usable
    upstream target; bundles with no target (non-contributing cycles) are dropped."""
    out: list[tuple[Path, str, str]] = []
    for d in patched:
        repo_spec, base, _slug = publish._resolve_target(d)
        if repo_spec and base:
            out.append((d, repo_spec, base))
    return out


def fold(cfg: Config, accepted: list[Path], *, dry_run: bool = False,
         locks: contextlib.ExitStack | None = None
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

    ``locks`` (#297 review round 10): when the caller passes an ``ExitStack``, each
    target's :func:`integ_lock` is entered on IT and stays held after fold returns —
    covering the caller's re-gate window, so no gap exists in which another flow's
    publish-boundary sweep could remove the tree (or another fold rewrite it) between
    the fold and ``gates.run_integration`` attesting it. The caller releases every
    lock by exiting the stack; ``None`` keeps the per-group scope (lock released when
    the group's build finishes).
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
    # Groups are processed in SORTED (repo, base) order (#297 review round 11): with a
    # caller-held ``locks`` stack the per-target locks accumulate, and two concurrent
    # multi-target flows encountering their groups in opposite bundle order would
    # otherwise deadlock (A holds target-1 waiting on target-2 while B holds target-2
    # waiting on target-1). A globally consistent acquisition order makes them
    # serialize instead. Stack order WITHIN each group is untouched.
    for (repo_spec, base), bundles in sorted(groups.items()):
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

        # The whole build holds the worktree's lifecycle lock (#297 review round 6):
        # a concurrent sweep must not remove the tree mid-fold, and two concurrent
        # folds of the same target serialize instead of fighting over `checkout -B`.
        # With a caller-supplied ``locks`` stack the lock OUTLIVES this block and
        # keeps covering the caller's re-gate (#297 review round 10).
        with contextlib.ExitStack() as scope:
            holder = locks if locks is not None else scope
            held = holder.enter_context(integ_lock(_integ_worktree(repo, base)))
            if not held:
                # Fail CLOSED (#297 review round 7): proceeding unserialized could
                # apply/push a mixed stack interleaved with another fold's commits.
                raise IntegrationError(
                    f"could not take the integration lock next to "
                    f"{_integ_worktree(repo, base).name} — fix the checkout's parent "
                    f"directory (permissions?), then re-run")
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
                # `-s` adds the Signed-off-by trailer (DCO) — same as publish (#81): the
                # branch is rebuilt each fold, so a stacked PR cut from an EARLIER fold
                # carries these commits outside the base's ancestry, where a DCO-gated
                # host inspects them (#405).
                if _git(wt, "commit", "-s", "-m", f"pdca-integrate: {d.name}") != 0:
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
