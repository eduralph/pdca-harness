"""Integration-branch stacking — fold each wave's accepted work onto a run-scoped
branch the next wave builds on (the default, fork-safe wave sequencing).

After a wave's bundles are accepted and published, the *next* wave must build on a base
that already contains this wave's work — otherwise a dependent built off the untouched
base misses its prerequisite's change and conflicts. Rather than *merge* the wave's PRs
into the target (which needs merge rights on the upstream base — impossible in a fork
model — and relaxes the STOP discipline), this merges every accepted bundle's **published
PR branch** onto a single run-scoped **integration branch** on ``origin`` (push-only — a
fork has push). The branch belongs to one batch (:func:`integration_branch`): two runs
driving different batches against the same base fold onto different lines, so neither
replaces the line the other builds and verifies on (#591). The next wave's Do worktree and
PR branches are cut from that branch, so a dependent batch completes in one run as a
reviewable PR stack the human merges bottom-up — generalising the single-chain ``Stacks
on`` (#123) to whole waves, and fixing its multi-parent gap (the branch carries *all*
prerequisites, not just ``parents[0]``).

Every PR the stack opens targets the real base (publish), and the line carries the PR
branches' own commits (same SHAs), joined by signed-off merge commits — never re-applied
as new commits (#593). So merging the stack bottom-up with merge commits lands every wave
on the target. The fold puts every published branch of a target on its line, so a later
wave's PR, cut from the line, carries every earlier-wave branch on the line — not only its
prerequisites' — and shows their changes until they merge. Once they have all merged, its
diff against the base is its own change when the line is a plain chain: one branch per
earlier wave, and no base newer than a branch on it taken in (the base did not move
between the wave-0 publishes and the run's first fold, and no fold merged the base in —
see below). Otherwise a fold merge commit joined histories the base got separately; that
commit never reaches the base, so the PR has several merge bases with it and can keep
showing an already-merged change. Updating its branch from the base (GitHub's "Update
branch") then leaves only its own change. Within one run the line is **append-only**: the
run's first fold of a target starts fresh from the base (a force-push that replaces only
an earlier run of the SAME batch's line — a different batch has a line of its own); every
later fold continues from the tip this run pushed, pushed without force, and refuses a
line another run moved (``folded_this_run``, #591). The one exception is a line the flow's
pre-wave carry puts to use (#646): a re-issued run that carries finished prerequisites
onto its batch's line continues origin's line from its tip — only when every commit on it
is accounted for (:func:`foreign_commit`) — and stays append-only from there.

Commits pushed straight onto a stack PR branch after a fold carried it reach the line only
when they extend it (a fast-forward; GitHub's merge-style "Update branch" is one): the next
fold merges the branch's new tip. A branch a fold of the run carried that is REWRITTEN
since — re-published (``publish`` force-pushes), force-pushed, or updated with a rebase —
no longer holds what the line took from it, so its new tip would put the old and the
rewritten commits on the line together: the run's next fold stops before it merges or
pushes anything (``carried``, #633). A branch deleted after its PR merged is judged by
the head commit the host says the PR merged with (``merged.merged_head``), never by a
commit message: a line that already has that commit skips it; one that does not (a fixup
pushed after the fold) takes the base in, once the base is checked to have that commit —
a PR merged somewhere else (its base edited on the host, a squash or rebase merge) stops
the run instead, and so does a base that would undo work already on the line (a revert of
a merged PR, #623). A branch that does not merge cleanly (an undeclared cross-wave overlap)
is a loud :class:`IntegrationError` that stops the run before the next wave builds on a
broken base — unless its conflict is with bundles of its own wave merged before it (it
merges cleanly onto the line as the wave found it, but not onto that line plus them): the
flow's fold then holds just that bundle out of the line and names the bundles it conflicts
with (``held_out``, #674). Mechanics are deterministic ``git`` subprocesses (no model).
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path

from . import merged, publish
from .config import Config


class IntegrationError(RuntimeError):
    """A wave's accepted work could not be folded onto the integration branch — a published
    branch does not merge cleanly (undeclared overlap), is missing, or was rewritten after
    the run carried it (#633), another run moved the line, or a git step failed. The caller
    STOPs rather than build the next wave on an incomplete base."""


class IntegrationTreeError(IntegrationError):
    """The integration worktree could not be put back to a known commit (#631) — a failed
    ``reset --hard`` or ``clean``, or a tip that could not be read to reset to. Unlike a
    bundle that does not merge, this is not about one bundle: every later merge would run
    in an unknown tree, so the carry's per-bundle ``skipped`` mode never absorbs it — the
    fold stops. An :class:`IntegrationError`, so every caller that catches that one (the
    flow's fold and carry) catches this too."""


class _LineBroken(IntegrationError):
    """The fold could not put the line back the way it was before a failed step (#623), so
    it must not go on merging and push: raised even under ``skipped``."""


class _Conflict(IntegrationError):
    """A published branch that does not merge cleanly onto the line. Raised by
    :func:`_merge_published` so :func:`fold`'s ``held_out`` mode (#674) can tell a textual
    conflict from any other failed merge."""


def integration_branch(cfg: Config, base: str, batch: Iterable[str] | None = None) -> str:
    """The run-scoped integration branch for a target ``base`` — deterministic (a resumed run
    rebuilds the same branch) and **injective in the base** (#187): the base is flattened to a
    single ref segment under ``pdca-integration/`` via :func:`_flatten_base`, so two bases that
    differ only by ``/`` vs ``-`` (``release/2.0`` → ``release-s2.0`` vs ``release-2.0`` →
    ``release-h2.0``) never collide onto one branch and force-push over each other's fold.

    ``batch`` (#591) is the bundles the run was ASKED to drive. It scopes the branch to that
    batch — ``pdca-integration/<flattened base>-r<batch key>`` (:func:`batch_key`) — so two
    runs on one base that drive different batches never share, replace or add to each
    other's line, while the same batch asked for again gets the same branch back. ``-r``
    never appears in :func:`_flatten_base`'s output (each ``-`` it emits is ``-h`` or
    ``-s``), so the base part ends unambiguously and the name stays injective in the base,
    and a batch-scoped name can never equal an unscoped one. ``None`` (a direct caller with
    no batch) keeps the unscoped ``pdca-integration/<flattened base>``."""
    name = "pdca-integration/" + _flatten_base(base)
    return name if batch is None else f"{name}-r{batch_key(batch)}"


def batch_key(names: Iterable[str]) -> str:
    """A short, deterministic key for a batch of bundles (#591): the names normalised to
    bundle names (``500`` and ``issue_500`` are one bundle), de-duplicated and sorted — so
    the order they were asked for in does not matter — then hashed. Lowercase hex: a valid
    ref-name fragment."""
    norm = sorted({"issue_" + str(n).removeprefix("issue_") for n in names})
    return hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()[:12]


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


def _rev(repo: Path, ref: str) -> str | None:
    """The commit SHA ``ref`` resolves to in ``repo``, or None when it does not resolve."""
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                        ref + "^{commit}"], capture_output=True, text=True)
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else None


def pushed_tip(wt: Path) -> str:
    """The tip a fold just pushed from integration worktree ``wt`` (``git rev-parse HEAD``),
    for the caller's ``folded_this_run`` (#593); raises when unreadable."""
    sha = _rev(wt, "HEAD")
    if not sha:
        raise IntegrationError(f"could not read the folded tip in {wt}")
    return sha


def _published_ref(d: Path) -> tuple[str, str, bool] | None:
    """``(remote, ref, onto)`` of the branch bundle ``d`` published, from its
    ``publish.json`` (read the way ``publish._stack_base_branch`` reads a parent's) — None
    when it has none.

    A ``"stacked"`` record (``Onto branch``, #54; ``onto`` True) committed onto an existing
    PR branch that may live on another remote: its own ``base`` (``<remote>/<branch>``,
    ``publish._publish_stacked``) IS that branch — never a same-named one on ``origin``; a
    ``base`` of any other shape names no remote, so no branch is on record. Every other
    record (``"new-pr"`` / ``"stacked-pr"``) pushed ``branch`` to ``origin``."""
    rec = publish._publish_record(d)
    branch = rec.get("branch") if isinstance(rec, dict) else None
    if not branch or not isinstance(branch, str):
        return None
    if rec.get("mode") == "stacked":
        ref, suffix = rec.get("base"), "/" + branch
        if isinstance(ref, str) and ref.endswith(suffix) and len(ref) > len(suffix):
            return ref[:-len(suffix)], ref, True
        return None
    return "origin", f"origin/{branch}", False


def _fold_candidates(bundles: Iterable[Path]
                     ) -> list[tuple[Path, str, str, str, tuple[str, str, bool] | None]]:
    """``(bundle, repo_spec, base, slug, published)`` for each bundle the fold carries — a
    non-empty ``patch.diff`` and a usable target — where ``published`` is its
    :func:`_published_ref` (None: no branch on record). The ONE definition :func:`fold` and
    :func:`unpublished` share (#593), so what the fold merges and what the flow holds back
    cannot drift apart."""
    return [(d, repo_spec, base, slug, _published_ref(d))
            for d, repo_spec, base, slug in _targeted([d for d in bundles if _has_patch(d)])]


def unpublished(bundles: Iterable[Path], *,
                pushed: Collection[str] | None = None) -> list[Path]:
    """The bundles the fold has no branch of to carry (#593): each one it would merge
    (:func:`_fold_candidates`) whose ``publish.json`` names no branch — and, when the caller
    passes ``pushed`` (the names of the bundles that pushed a branch THIS run), each one not
    in it. ``publish.json`` survives an iterate, so a re-publish that failed before its push
    leaves the old, rejected branch on record. Outside a dry-run :func:`fold` refuses the
    first kind itself; the flow holds both, and what depends on them, out of the fold. A
    patched bundle with no target is neither."""
    return [d for d, _repo, _base, _slug, ref in _fold_candidates(bundles)
            if ref is None or (pushed is not None and d.name not in pushed)]


def carry_view(cfg: Config, repo_spec: str, line: str,
               remotes: Iterable[str] = ()) -> tuple[Path, str | None]:
    """What the flow's pre-wave carry (#646) judges finished bundles against, before it
    folds: ``repo_spec``'s checkout, fetched as a fold fetches it (:func:`_fetch`), and the
    tip of origin's ``line`` there — None when origin has no such line. Raises
    :class:`IntegrationError` when the checkout is missing or a fetch fails."""
    repo = publish._checkout_path(cfg, repo_spec)
    _fetch(repo, cfg.base_remote, remotes)
    return repo, _rev(repo, f"origin/{line}")


def foreign_commit(repo: Path, tip: str, base_ref: str, heads: Iterable[str]) -> str | None:
    """The first commit on the line at ``tip`` the carry cannot account for (#646): one that
    is not a merge and is reachable neither from ``base_ref`` nor from any of ``heads`` (the
    PR heads of the finished bundles it would carry) — an old, rejected commit, say, or a
    closed PR's. None: every commit is accounted for. A fold's own commits are merges, so
    they never count. A git failure raises :class:`IntegrationError`."""
    r = subprocess.run(["git", "-C", str(repo), "rev-list", "--no-merges", tip, "--not",
                        base_ref, *heads], capture_output=True, text=True)
    if r.returncode != 0:
        raise IntegrationError(f"could not list the commits on {tip} in {repo} — `git "
                               f"rev-list` exited {r.returncode}; check that checkout")
    return next(iter(r.stdout.split()), None)


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


def _targeted(patched: list[Path]) -> list[tuple[Path, str, str, str]]:
    """``(bundle, repo_spec, base, slug)`` for each patched bundle that resolves a usable
    upstream target; bundles with no target (non-contributing cycles) are dropped."""
    out: list[tuple[Path, str, str, str]] = []
    for d in patched:
        repo_spec, base, slug = publish._resolve_target(d)
        if repo_spec and base:
            out.append((d, repo_spec, base, slug))
    return out


def fold(cfg: Config, accepted: list[Path], *, dry_run: bool = False,
         locks: contextlib.ExitStack | None = None,
         folded_this_run: Mapping[tuple[str, str], str | None] | None = None,
         batch: Collection[str] | None = None,
         skipped: dict[str, str] | None = None,
         carried: dict[str, str] | None = None,
         held_out: dict[str, tuple[list[str], list[str]]] | None = None,
         wave: Collection[str] = (),
         pr_heads: Mapping[str, str] | None = None,
         holds: dict[str, str] | None = None,
         ) -> dict[tuple[str, str], tuple[str, Path | None]]:
    """Fold the cumulative accepted bundles' published branches onto a per-target
    integration branch.

    ``accepted`` is every accepted bundle (waves 0..k) in stack order (the caller passes
    them wave by wave, name-sorted within each). Bundles are grouped by their upstream
    ``(repo, base)`` target and **each group folds onto its own integration branch** — a
    batch spanning several targets (two repos, or two base branches on one repo) keeps one
    integration line per target, so a later wave's bundle stacks on the branch for *its* own
    target, never a sibling target's (#187). Each bundle's published branch
    (:func:`_published_ref`) is merged onto the line with a signed-off merge commit, so the
    line carries the PR's own commits rather than a re-applied copy (#593); a branch already
    in the line is not merged again. Returns ``{(repo, base): (branch, worktree)}`` — each
    target's integration branch and the worktree it was built in (for an optional re-gate)
    — or ``{}`` when there is nothing to integrate (no patches, or none with a target).
    Raises :class:`IntegrationError` on a real failure (a branch does not merge cleanly, the
    line was moved by another run, or a git step fails) so the caller STOPs.

    ``folded_this_run`` (#593) maps each target THIS run has already folded to the tip its
    last fold pushed (``None`` in a dry-run, where nothing is pushed). A **listed** target
    continues from that tip and pushes WITHOUT force (append-only, so no push rewrites a
    commit an open PR's base or head depends on), and refuses when origin's line is not that
    SHA — something else moved it (#591). A target **not listed** is the run's first fold
    of it: it starts fresh from ``<base_remote>/<base>`` and force-pushes, replacing an
    earlier run's line of the same name. ``None`` (a direct caller) continues the line if
    origin has it, else starts fresh.

    ``batch`` (#591) is the bundles the run was asked to drive; each target's line is
    :func:`integration_branch` scoped to it, so a run's fresh first fold replaces only an
    earlier run of the SAME batch's line, never a concurrent run's on the same base. The
    flow always passes it. ``None`` (a direct caller) folds onto the unscoped name.

    Fail-closed outside a dry-run: a targeted, patched bundle with no published branch
    raises before any git step (the flow holds such bundles, and failed publishes, out:
    :func:`unpublished`). A branch that moved since the last fold (commits pushed onto the
    PR branch after the fold carried it) has its new tip merged — when it only moved
    forward; a carried one rewritten since raises (``carried``, below). A published branch gone
    after the (pruning) fetch is judged by commit, never by message (:func:`_gone_branch`):
    unless the host reports its PR merged, with the head commit it merged, the fold raises;
    a line that already has that head skips it; otherwise the line takes the base in
    (:func:`_take_in_base`), so the work still reaches the next wave — but only once the
    base is checked to have that head: a PR merged somewhere else (its base edited on the
    host, a squash or rebase merge) raises, and so does an ``Onto branch`` record, whose
    PR may have merged into another base. So does a base that would remove work a bundle
    put on the line (#623): the line is reset to its tip before the base merge, and the
    bundles of ``accepted`` and ``batch`` are what the message names.

    Dry-run (offline rehearse / CI, where the publisher leaf is stubbed) prints each group's
    git plan and returns the branches with ``None`` worktrees — no worktree, no push — so the
    next wave falls back to the target base, which is what an offline rehearse wants. A
    bundle with no ``publish.json`` (the stub records none) is planned as merging
    ``origin/<branch>`` for the branch a real publish would push.

    ``locks`` (#297 review round 10): when the caller passes an ``ExitStack``, each
    target's :func:`integ_lock` is entered on IT and stays held after fold returns —
    covering the caller's re-gate window, so no gap exists in which another flow's
    publish-boundary sweep could remove the tree (or another fold rewrite it) between
    the fold and ``gates.run_integration`` attesting it. The caller releases every
    lock by exiting the stack; ``None`` keeps the per-group scope (lock released when
    the group's build finishes).

    ``skipped`` (#646, the flow's pre-wave carry): when given, an :class:`IntegrationError`
    raised while merging ONE bundle (:func:`_merge_published`, its :func:`_gone_branch`
    included) is recorded as ``skipped[name] = message`` and the next bundle is merged — a
    failed merge is undone (the tree reset to the line tip it had before it, #631), so the
    tree is clean for it. None of a skipped bundle's work reaches the line the fold pushes
    (#672). Its work is every commit, merge commits included, reachable from its branch tip
    as read here or from its ``pr_heads`` entry (the PR head the caller read), and from
    neither ``<base_remote>/<base>`` nor the commit the line started from (:func:`_work`);
    a commit it shares with another branch counts, whoever wrote it. So a bundle whose
    merged head reaches any of it — merged before the skip or after — is left out too
    (:func:`_hold_work`): recorded in ``skipped`` with that cause and in ``holds`` as
    ``holds[name] = <the bundle whose work it holds>``, its own work then left out the same
    way. The line is then started again from the same commit with only the bundles kept,
    before the fold's one push; a bundle that fails on that line is skipped the same way,
    and nothing left out is tried again. A bundle whose merge failed, and whose branch holds
    any of the work of one before it in fold order whose merge failed too, is recorded for
    holding that one's work, not for its own failure (:func:`_built_on_skipped`); a commit
    two of them share is the work of both, so the earlier one is the cause.
    A target whose every bundle was skipped pushes nothing and has no entry in the result.
    Every other failure still raises: the lock, the worktree (one that cannot be made, or
    reset — :class:`IntegrationTreeError`), a moved line, the push, and the up-front
    refusal of a bundle with no published branch (the carry passes only bundles that have
    one). ``None`` (every other caller) raises on the first failure, as before, and
    computes no work.

    ``carried`` (#633, the flow's in-run folds): the run's record, filled in place like
    ``skipped``, of the head each bundle's branch had when a fold of this run put it on its
    line (merged it, or found it there already) — written once that target has pushed.
    Before any target merges or pushes, each bundle with a recorded head whose branch still
    resolves must still hold that head (its tip, or a descendant of it); a branch rewritten
    since raises (:func:`_carried_tips`), so the line never carries commits no PR holds any
    more. The tip read for that check is the commit merged: the branch is not read again,
    so a fetch for another target in the same checkout, or a push from it, cannot swap in
    a commit that was never checked. A gone branch is left to :func:`_gone_branch`.
    ``None`` (the pre-wave carry, a direct caller) checks and records nothing; a dry-run
    does neither.

    ``held_out`` (#674, the flow's in-run folds) with ``wave``, the names of the bundles of
    the wave just published: a bundle of ``wave`` whose branch conflicts with the line is
    left out — recorded in place as ``held_out[name] = (partners, paths)`` — when at least
    one bundle of ``wave`` was merged before it on this line, and its branch merges cleanly
    onto the line as it was before the first of them but not onto that line plus them
    (:func:`_held_by`). ``partners`` are the ones of those it conflicts with, ``paths`` the
    conflicting files. So a bundle is judged against the bundles actually kept, in merge
    order, never against one already held; a branch that merges cleanly is never held,
    whatever files it shares; and one whose conflict is not with them — with earlier-wave
    work, or with a base the line took in for a gone branch — raises, as before. Nothing
    is pushed for a held bundle, and the caller leaves it out of its later folds.
    The trial merges run in the integration worktree under the fold's lock and leave it
    clean on the line. ``None`` (the carry, a direct caller): every conflict raises (or,
    under ``skipped``, is skipped) as before; a dry-run merges nothing, so holds nothing.
    """
    candidates = _fold_candidates(accepted)
    if not candidates:
        return {}  # nothing to integrate — the next wave builds on the base

    # What each bundle merges — resolved for EVERY bundle before any git step, so a fold
    # that cannot carry one of them fails before it touches anything.
    refs: dict[str, tuple[str, str, bool]] = {}      # bundle name → (remote, ref, onto)
    missing: list[str] = []
    for d, _repo_spec, _base, slug, ref in candidates:
        if ref is None and dry_run:
            ref = ("origin", "origin/" + publish._branch_name(cfg, d, slug), False)
        if ref is None:
            missing.append(d.name)
        else:
            refs[d.name] = ref
    if missing:
        raise IntegrationError(
            f"{', '.join(missing)} accepted but not published (no publish.json branch) — "
            f"there is nothing of it to fold; publish it, or leave it out of the fold, then "
            f"re-run")

    # One integration line per (repo, base): group the accepted bundles by target so a
    # multi-target batch folds each onto its own branch (the common single-target batch is
    # just one group). Preserve stack order within a group (candidates keep accepted's order).
    groups: dict[tuple[str, str], list[Path]] = {}
    for d, repo_spec, base, _slug, _ref in candidates:
        groups.setdefault((repo_spec, base), []).append(d)

    base_remote = cfg.base_remote
    # A branch this run carried that was rewritten since stops the fold before ANY target
    # merges or pushes (#633). Each carried branch's tip is read once, here, and that commit
    # is the one merged below — never the branch read again after a later fetch (another
    # target in the same checkout) or a push from that checkout has moved it.
    checked = _carried_tips(cfg, groups, refs, carried, batch) if carried and not dry_run else {}
    result: dict[tuple[str, str], tuple[str, Path | None]] = {}
    # Groups are processed in SORTED (repo, base) order (#297 review round 11): with a
    # caller-held ``locks`` stack the per-target locks accumulate, and two concurrent
    # multi-target flows encountering their groups in opposite bundle order would
    # otherwise deadlock (A holds target-1 waiting on target-2 while B holds target-2
    # waiting on target-1). A globally consistent acquisition order makes them
    # serialize instead. Stack order WITHIN each group is untouched.
    for (repo_spec, base), bundles in sorted(groups.items()):
        branch = integration_branch(cfg, base, batch)
        repo = publish._checkout_path(cfg, repo_spec)
        start = ("auto" if folded_this_run is None
                 else "continue" if (repo_spec, base) in folded_this_run else "fresh")
        if dry_run:
            print(f"integrate --dry-run — fold {len(bundles)} patch(es) onto {branch} "
                  f"(off {base_remote}/{base} on {repo_spec}):")
            plan = {"fresh": f"start {branch} fresh from {base_remote}/{base}",
                    "continue": f"continue {branch} from this run's tip",
                    "auto": f"continue {branch} if origin has it, else start fresh "
                            f"from {base_remote}/{base}"}[start]
            print(f"  git worktree → {_integ_worktree(repo, base)}; {plan}")
            for d in bundles:
                print(f"  git merge --no-ff --signoff {refs[d.name][1]}   ({d.name})")
            print({"fresh": f"  git push --force origin {branch}",
                   "continue": f"  git push origin {branch}",
                   "auto": f"  git push origin {branch}   (--force only if it started "
                           f"fresh)"}[start])
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
            wt = _prepare_worktree(repo, base_remote, base,
                                   remotes=[refs[d.name][0] for d in bundles])
            line = _rev(wt, f"origin/{branch}")  # origin's line, as just fetched
            if start == "continue":
                pushed = folded_this_run[(repo_spec, base)]
                if line is None or line != pushed:
                    raise IntegrationError(
                        f"origin's {branch} is at {line or '(absent)'}, not at "
                        f"{pushed or '(no tip recorded)'}, the tip this run last pushed (or "
                        f"checked, for a carried line) — another run of the same batch, or "
                        f"someone outside the harness, has moved it (#591). Not building on "
                        f"a line this run did not push or check; let the other run finish, "
                        f"then re-run")
            fresh = start == "fresh" or (start == "auto" and line is None)
            start_ref = f"{base_remote}/{base}" if fresh else line
            # The tree is shared by every fold of the run, the #646 carry and the re-gate
            # (#631): drop whatever its last user left — a mid-merge state, edited tracked
            # files, untracked build output — so the fold depends only on start_ref and the
            # branches it merges. Ignored files stay (git overwrites them without refusing).
            _reset_tree(wt, "HEAD")
            if _git(wt, "checkout", "-B", branch, start_ref) != 0:
                raise IntegrationError(f"could not start {branch} off {start_ref}")
            # Who a path the base would undo belongs to (#623): this fold's bundles, then
            # the rest of the run's batch (folded earlier, absent from this call), found by
            # id as the flow finds them — an archived one too (`find_bundle`) — and each
            # listed once, by name.
            owners: dict[str, Path] = {}
            for o in [*bundles, *(cfg.find_bundle(str(n).removeprefix("issue_"))
                                  for n in batch or ())]:
                owners.setdefault(o.name, o)
            read: dict[str, str | None] = {}   # each branch's tip, read once (#633)
            dropped: set[str] = set()          # left out of the line: never tried again (#672)
            failed: list[str] = []             # the bundles skipped: their merge raised (#672)
            work: dict[str, list[str]] = {}    # each bundle left out → its work (#672)
            ends = (f"{base_remote}/{base}", start_ref)   # a bundle's work: what neither has
            pr = pr_heads or {}
            while True:
                took: dict[str, str] = {}   # bundle name → the head this fold put on the line
                pre: str | None = None      # the line before the wave's first merge (#674)
                kept: list[tuple[str, str]] = []   # the wave's bundles merged so far: (name, head)
                new: list[str] = []         # the bundles this pass skipped
                for d in bundles:
                    if d.name in dropped:
                        continue
                    # Its branch's tip, read once — a carried branch's is the one checked
                    # (#633) — and kept for a pass that starts the line again (#672).
                    if d.name not in read:
                        read[d.name] = (checked[d.name] if d.name in checked
                                        else _rev(wt, refs[d.name][1]))
                    head = read[d.name]
                    ours = held_out is not None and d.name in wave
                    if ours and pre is None:
                        pre = _rev(wt, "HEAD")
                        if pre is None:
                            raise IntegrationTreeError(f"could not read the tip of {branch} "
                                                       f"in {wt} — check that checkout, then "
                                                       f"re-run")
                    try:
                        _merge_published(cfg, wt, d, refs[d.name], head, branch=branch,
                                         base=base, owners=tuple(owners.values()))
                    except IntegrationError as exc:
                        if (held_out is not None and ours and kept and head is not None
                                and pre is not None and isinstance(exc, _Conflict)):
                            clash = _held_by(wt, branch, head, pre, kept)
                            if clash:   # a same-wave conflict: this bundle only (#674)
                                held_out[d.name] = clash
                                dropped.add(d.name)
                                continue
                        if skipped is None or isinstance(exc, (IntegrationTreeError,
                                                                _LineBroken)):
                            raise
                        skipped[d.name] = str(exc)   # this bundle only; its merge was undone
                        dropped.add(d.name)
                        new.append(d.name)
                    else:
                        if head is not None:        # merged, or in the line already
                            took[d.name] = head
                            if ours:
                                kept.append((d.name, head))
                if not new:     # none skipped (never, without ``skipped``): the line stands
                    break
                # A skipped bundle's work stays off the line (#672). A merged bundle that
                # holds any of it, merged before the skip or after, comes off too; then the
                # line starts again from the same commit with only the bundles kept. Each
                # pass leaves out at least one more bundle, so this ends.
                failed += new
                for n in new:
                    work[n] = _work(wt, (read[n], pr.get(n)), ends, what=f"{n}'s work")
                held = _hold_work(wt, [d.name for d in bundles], took, work, ends, pr,
                                  refs=refs, branch=branch, skipped=skipped, holds=holds)
                if not held:
                    break
                dropped.update(held)
                _reset_tree(wt, "HEAD")
                if _git(wt, "checkout", "-B", branch, start_ref) != 0:
                    raise IntegrationError(f"could not start {branch} off {start_ref}")
            if len(failed) > 1:
                _built_on_skipped(wt, [d.name for d in bundles if d.name in failed],
                                  {n: read[n] or pr.get(n) for n in failed}, work, ends,
                                  refs=refs, branch=branch, skipped=skipped, holds=holds)
            if skipped is not None and all(d.name in skipped for d in bundles):
                continue   # nothing of this target merged: push nothing, report nothing
            # Fresh: the run's first fold of this target replaces an earlier run of the same
            # batch's line (the name is batch-scoped, #591), so it is forced. Continuing: a
            # fast-forward of this run's own tip, pushed WITHOUT force, so it lands on an
            # origin with receive.denyNonFastForwards too, and can never rewrite a commit an
            # open PR's base or head depends on (#593).
            push = ("push", "--force", "origin", branch) if fresh else ("push", "origin", branch)
            if _git(wt, *push) != 0:
                why = (" (the run's first fold force-pushes over an earlier run of the same "
                       f"batch's line: if origin refuses force-pushes, delete the old {branch} "
                       "there or allow force-pushes on pdca-integration/*)" if fresh else
                       " (an unforced fast-forward was refused — did another run move it? "
                       "#591)")
                raise IntegrationError(
                    f"could not push {branch} to origin{why} — the next wave cannot stack "
                    f"on it")
            if carried is not None:
                carried.update(took)   # pushed: the line now holds these heads (#633)
        result[(repo_spec, base)] = (branch, wt)
    return result


def _carried_tips(cfg: Config, groups: Mapping[tuple[str, str], list[Path]],
                  refs: Mapping[str, tuple[str, str, bool]], carried: Mapping[str, str],
                  batch: Collection[str] | None) -> dict[str, str | None]:
    """The tip of each published branch ``carried`` records a head for (#633), read once
    after fetching its checkout — None for a branch that no longer resolves (gone:
    :func:`_gone_branch` judges it). :func:`fold` checks every target this way before it
    merges or pushes anything, and merges exactly the tips returned.

    Raises :class:`IntegrationError` for a branch whose tip no longer holds the head a fold
    of this run put on the line (``carried[name]``), as itself or an ancestor: the branch
    was rewritten (re-published, force-pushed, updated with a rebase), and merging its tip
    would put the old and the rewritten commits on the line together. Decided by ancestry,
    never by message; a git failure raises (:func:`_in_line`)."""
    tips: dict[str, str | None] = {}
    for (repo_spec, base), bundles in sorted(groups.items()):
        mine = [d for d in bundles if d.name in carried]
        if not mine:
            continue
        repo = publish._checkout_path(cfg, repo_spec)
        _fetch(repo, cfg.base_remote, [refs[d.name][0] for d in mine])
        line = integration_branch(cfg, base, batch)
        for d in mine:
            ref, took = refs[d.name][1], carried[d.name]
            tip = tips[d.name] = _rev(repo, ref)
            if tip is None or tip == took or _in_line(
                    repo, took, f"{d.name}'s carried head {took[:12]}", ref, of=tip):
                continue
            raise IntegrationError(
                f"{d.name}'s branch {ref} was rewritten after this run folded it onto "
                f"{line}: it is now at {tip[:12]}, which does not contain {took[:12]}, the "
                f"commit the line took from it. A re-publish (`pdca publish` force-pushes), "
                f"a force-push or \"Update with rebase\" does this. Only commits that extend "
                f"a carried branch reach the line (a fast-forward, such as GitHub's "
                f"merge-style \"Update branch\"): merging {tip[:12]} would put the old and "
                f"the rewritten commits on {line} together. Nothing was merged or pushed. "
                f"Restore {ref} to {took[:12]} or a commit that contains it, then re-issue "
                f"the run; or let this run stop and re-issue it")
    return tips


def _merge_published(cfg: Config, wt: Path, d: Path, published: tuple[str, str, bool],
                     head: str | None, *, branch: str, base: str,
                     owners: Sequence[Path] = ()) -> None:
    """Merge bundle ``d``'s published branch onto the line checked out in ``wt`` (#593):
    its own commits, joined by a signed-off merge commit — not a re-applied copy. ``head``
    is the branch's tip as :func:`fold` read it — None: it does not resolve (gone). It is
    the CURRENT tip, so commits pushed onto the branch after an earlier fold carried it
    reach the line too; for a branch the run carried, the tip checked to extend what the
    line took from it (:func:`_carried_tips`, #633). ``owners``: the bundles a refusal to
    take the base in may name (:func:`_take_in_base`)."""
    _remote, ref, onto = published
    if head is None:
        _gone_branch(cfg, wt, d, ref, onto, branch=branch, base=base, owners=owners)
        return
    if _in_line(wt, head, f"{d.name}'s branch {ref}", branch):
        return  # already in the line (an earlier fold carried it) — never merged twice
    rc, conflicts = _merge(wt, head, f"pdca-integrate: {d.name}")
    if rc == 0:
        return
    if conflicts:
        raise _Conflict(
            f"{d.name}'s branch {ref} does not merge cleanly onto {branch} (conflicts in "
            f"{', '.join(conflicts)}) — an undeclared cross-wave overlap; declare the "
            f"conflict / re-order, then re-run")
    raise IntegrationError(
        f"could not merge {d.name}'s branch {ref} onto {branch} — `git merge` exited {rc} "
        f"with no conflicting path in {wt}, so a git step failed (not an overlap): check "
        f"that checkout and the branch, then re-run")


def _gone_branch(cfg: Config, wt: Path, d: Path, ref: str, onto: bool, *, branch: str,
                 base: str, owners: Sequence[Path] = ()) -> None:
    """``d``'s published branch ``ref`` does not resolve after the pruning fetch — a merged
    PR's branch is commonly deleted on merge (#593). Decided by commit, never by message:

    1. ask the host which head commit the bundle's PR merged with
       (:func:`merged.merged_head`); not merged, or unknown, raises, naming the ref;
    2. the line being built already has that commit: it carries all of the PR's work, so
       skip it and do NOT take the base in — however the base has moved since;
    3. otherwise (the fold never carried that head — a fixup pushed after the fold — or the
       commit is not in this clone at all) the line can get that work only from the base.
       An ``Onto branch`` record (``onto``) is a commit on another PR, and a legacy ``Stacks
       on:`` PR targets its parent's branch, so either may have merged elsewhere, and that
       raises. Any other PR targeted this base, but it may still have merged into another
       branch (its base edited on the host), so the fold first checks that the base has the
       head: then the line takes the base in (:func:`_take_in_base`), unless that would
       remove work a bundle put on the line (#623), which raises. A base without it —
       or a clone without it, which, having fetched the base, means the same; a squash or
       rebase merge never puts it on the base either — raises, rather than build the next
       wave on a line that lacks the PR's work."""
    base_ref = f"{cfg.base_remote}/{base}"
    head = merged.merged_head(cfg, d.name.removeprefix("issue_"))
    if not head:
        raise IntegrationError(
            f"{d.name}'s published branch {ref} does not resolve after fetching (deleted?) "
            f"and its PR is not known to be merged (not merged, or its state could not be "
            f"read) — restore or re-publish the branch, then re-run")
    if _carries(wt, head, f"{d.name}'s merged head {head}", branch):
        print(f"integrate: {d.name}'s branch {ref} is gone and its PR merged at "
              f"{head[:12]} — {branch} already carries that commit; skipped.",
              file=sys.stderr)
        return
    rec = publish._publish_record(d) or {}
    if onto or rec.get("base") != base:
        what = ("an `Onto branch` commit on another PR" if onto else
                f"a PR against {rec.get('base') or '(no recorded base)'}, not {base}")
        raise IntegrationError(
            f"{d.name}'s branch {ref} is gone and its PR merged at {head[:12]}, a commit "
            f"{branch} does not have, but it is {what}, which may have merged into a base "
            f"other than {base_ref} — the fold cannot tell its work reaches {branch}; "
            f"restore the branch, then re-run")
    if not _carries(wt, head, f"{d.name}'s merged head {head}", base_ref, of=base_ref):
        raise IntegrationError(
            f"{d.name}'s branch {ref} is gone and its PR "
            f"{rec.get('pr_url') or '(no recorded PR)'} merged at {head}, a commit neither "
            f"{branch} nor {base_ref} has: it merged somewhere other than {base_ref} (its "
            f"base edited on the host, or a squash or rebase merge), so taking {base_ref} "
            f"in would not carry that work to the next wave — restore the branch, or get "
            f"that work into {base_ref}, then re-run")
    _take_in_base(cfg, wt, d, ref, branch, base_ref, head, owners)


def _carries(wt: Path, commit: str, what: str, branch: str, *, of: str = "HEAD") -> bool:
    """Whether ``of`` — by default the line checked out in ``wt`` — has ``commit``. A commit
    this clone does not have is not in it — never an error (:func:`_in_line` would read
    git's exit 128 for it as a failure)."""
    if _git(wt, "cat-file", "-e", f"{commit}^{{commit}}") != 0:
        return False
    return _in_line(wt, commit, what, branch, of=of)


def _take_in_base(cfg: Config, wt: Path, d: Path, ref: str, branch: str, base_ref: str,
                  head: str, owners: Sequence[Path] = ()) -> None:
    """``d``'s branch ``ref`` is gone and its PR merged at ``head``, a commit the line does
    not have but the base does (the caller checked), so the base carries the merged work.
    Merge the base in (signed off, on top of the line: a fast-forward for the push, never a
    rewrite — #593); a line that already has the base needs nothing.

    A base merge that would remove work from the line (#623) — the base reverted a merged
    PR, ``d``'s or one already on the line — is undone (the line is reset to its tip before
    the merge, so under the carry's ``skipped`` the other bundles merge onto a line without
    it) and raises, naming each path it would undo and the bundle of ``owners`` that
    brought it (:func:`_undone_paths`). Commit ancestry alone cannot see this: the base
    still has the merged head after the revert."""
    if not _in_line(wt, base_ref, base_ref, branch):
        before = _rev(wt, "HEAD")
        if before is None:
            raise IntegrationError(f"could not read the tip of {branch} in {wt}")
        rc, conflicts = _merge(wt, base_ref, f"pdca-integrate: {base_ref} (carries merged "
                                             f"{d.name})")
        if rc != 0:
            why = (f"conflicts in {', '.join(conflicts)}; update the clashing PR's branch "
                   f"from the base" if conflicts else
                   f"`git merge` exited {rc} with no conflicting path: a git step failed")
            raise IntegrationError(
                f"{d.name}'s branch {ref} is gone and its PR merged at {head[:12]}, which "
                f"{branch} does not have, but {base_ref}, which carries that work, does not "
                f"merge onto {branch} ({why}), then re-run")
        try:
            undone = _undone_paths(cfg, wt, before, base_ref, branch, owners)
        except IntegrationError:
            _reset_line(wt, before, branch)
            raise
        if undone:
            _reset_line(wt, before, branch)
            whose = sorted({n for _p, names, _note in undone for n in names})
            work = f"{', '.join(whose)}'s work" if whose else "folded work"
            paths = "; ".join(f"{p} ({', '.join(names) or f'{note} on {branch}'})"
                              for p, names, note in undone)
            raise IntegrationError(
                f"{d.name}'s branch {ref} is gone and its PR merged at {head[:12]}, which "
                f"{branch} does not have, but taking {base_ref} in would remove {work} from "
                f"{branch}: {paths} would go back to how {base_ref} had it before the line "
                f"changed it (a revert on {base_ref}?). Not building the next wave on a line "
                f"without that work — get it back into {base_ref}, or restore {ref}, then "
                f"re-run")
    print(f"integrate: {d.name}'s branch {ref} is gone and its PR merged at {head[:12]}, "
          f"which {branch} did not have — the merged work reaches {branch} through "
          f"{base_ref}.", file=sys.stderr)


def _out(wt: Path, *args: str, what: str, stdin: str | None = None) -> str:
    """``git -C wt args``' output; a failing git step raises (``what`` says what it read)."""
    r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True,
                       input=stdin)
    if r.returncode != 0:
        raise IntegrationError(f"could not read {what} in {wt} — `git {args[0]}` exited "
                               f"{r.returncode}; a git step failed: check that checkout, "
                               f"then re-run")
    return r.stdout


def _cut_point(wt: Path, tip: str, base_ref: str) -> tuple[str | None, list[str]]:
    """Where the line at ``tip`` leaves the base, and the line's own commits since (#623):
    ``(cut, chain)``. ``chain`` is the line's first-parent chain, newest first, down to the
    first commit on it that ``base_ref`` has, which is ``cut`` — decided by commit, never by
    message. A base that has ``tip`` itself gives ``(tip, [])``. A chain that never meets
    the base (its oldest commit has no parent) gives a ``cut`` of None: the line starts from
    nothing."""
    chain = _out(wt, "rev-list", "--first-parent", "--topo-order", tip, "--not", base_ref,
                 what=f"the first-parent chain of {tip}").split()
    if not chain:
        return tip, chain
    parents = _out(wt, "rev-list", "--parents", "-n", "1", chain[-1],
                   what=f"the parents of {chain[-1]}").split()[1:]
    return (parents[0] if parents else None), chain


def _changed(wt: Path, old: str, new: str) -> list[str]:
    """The paths whose content differs between ``old`` and ``new`` (no rename pairing)."""
    out = _out(wt, "diff", "--no-renames", "--name-only", "-z", old, new,
               what=f"the paths changed between {old} and {new}")
    return [p for p in out.split("\0") if p]


def _own_commits(wt: Path, tip: str, since: Sequence[str], skip: Collection[str], *,
                 what: str) -> set[str]:
    """The commits a branch at ``tip`` adds of its own since the cut point (#623): each one
    reachable from ``tip`` without passing through a commit of ``skip`` (the line's and the
    base's first-parent commits; ``since`` leaves out the cut point's history). A branch cut
    from the line or from the base, or updated from the base, has those in its history, but
    they are not its work. A commit merged INTO the branch (say a topic branch merged into
    the PR branch) is its work, though it is not on the branch's first-parent chain."""
    out = _out(wt, "rev-list", "--parents", tip, *since, what=what)
    parents = {c: ps for c, *ps in (row.split() for row in out.splitlines() if row)}
    own: set[str] = set()
    todo = [tip]
    while todo:
        c = todo.pop()
        if c in own or c in skip or c not in parents:
            continue
        own.add(c)
        todo.extend(parents[c])
    return own


def _undone_paths(cfg: Config, wt: Path, before: str, base_ref: str, branch: str,
                  owners: Sequence[Path]) -> list[tuple[str, list[str], str]]:
    """``(path, bundles, note)`` for each path the base merge just made in ``wt`` undoes
    (#623): one the line at ``before`` changed since its cut point (:func:`_cut_point`),
    which is back to its content there now (absent, where the cut point had none). An edit
    that leaves the path with NEW content removes nothing. ``bundles`` are the ``owners``
    that brought the change: each one with a commit of its own (:func:`_own_commits`, from
    its published branch or, gone, the head its PR merged with) among the commits that
    changed the path on the line; ``note`` names the line commit that last changed it."""
    cut, chain = _cut_point(wt, before, base_ref)
    since = ("--not", cut) if cut else ()
    old = cut or _out(wt, "hash-object", "-t", "tree", "--stdin", stdin="",
                      what="the empty tree").strip()
    after = set(_changed(wt, old, "HEAD"))
    undone = [p for p in _changed(wt, old, before) if p not in after]
    if not undone:
        return []
    skip = {*chain, *_out(wt, "rev-list", "--first-parent", base_ref, *since,
                          what=f"the first-parent chain of {base_ref}").split()}
    own: list[tuple[str, set[str]]] = []      # (bundle name, the commits it adds)
    for o in owners:
        pub = _published_ref(o)
        if pub is None:
            continue
        tip = _rev(wt, pub[1]) or merged.merged_head(cfg, o.name.removeprefix("issue_"))
        if tip and _git(wt, "cat-file", "-e", f"{tip}^{{commit}}") == 0:
            own.append((o.name, _own_commits(wt, tip, since, skip,
                                             what=f"{o.name}'s commits")))
    out: list[tuple[str, list[str], str]] = []
    for path in undone:
        touched = _out(wt, "rev-list", before, *since, "--", f":(literal){path}",
                       what=f"the commits on {branch} that changed {path}").split()
        out.append((path, [name for name, commits in own if commits.intersection(touched)],
                    f"last changed by {touched[0][:12]}" if touched else "changed"))
    return out


def _reset_line(wt: Path, tip: str, branch: str) -> None:
    """Put the line in ``wt`` back at ``tip`` (#623); one that will not go back stops the
    fold even under ``skipped`` (:class:`_LineBroken`) — never merged onto and pushed."""
    if _git(wt, "reset", "-q", "--hard", tip) != 0 or _rev(wt, "HEAD") != tip:
        raise _LineBroken(f"could not reset {branch} in {wt} back to {tip} after refusing "
                          f"to take the base in — check that checkout, then re-run")


def _in_line(wt: Path, rev: str, what: str, branch: str, *, of: str = "HEAD") -> bool:
    """Whether ``rev`` is an ancestor of ``of`` — by default the line checked out in ``wt``
    (``branch`` names where it looked, in an error). ``git merge-base --is-ancestor`` exits
    0 for yes, 1 for no; anything else (128) is git failing, so it raises — never read as
    "not in the line", or reported as an overlap."""
    rc = _git(wt, "merge-base", "--is-ancestor", rev, of)
    if rc in (0, 1):
        return rc == 0
    raise IntegrationError(
        f"could not tell whether {what} is already in {branch} — `git merge-base "
        f"--is-ancestor` exited {rc} in {wt}; a git step failed (not an overlap): check that "
        f"checkout, then re-run")


def _merge(wt: Path, rev: str, message: str) -> tuple[int, list[str]]:
    """Merge ``rev`` onto the line in ``wt``; return the exit code and, on a failure, the
    paths left conflicting (none: it failed for another reason). A failed merge is undone by
    resetting the tree to the line tip it had before the merge (#631) — not a ``merge
    --abort`` whose failure would go unseen and leave a mid-merge tree for the next merge.
    ``--signoff`` (DCO, as publish #81): later waves' PRs carry these merge commits outside
    the base's ancestry, where a DCO-gated host inspects them (#405)."""
    before = _rev(wt, "HEAD")
    if before is None:
        raise IntegrationTreeError(f"could not read the line tip checked out in {wt} — a "
                                   f"git step failed: check that checkout, then re-run")
    rc = _git(wt, "merge", "--no-ff", "--no-edit", "--signoff", "-m", message, rev)
    if rc == 0:
        return 0, []
    r = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"],
                       capture_output=True, text=True)
    conflicts = r.stdout.splitlines() if r.returncode == 0 else []
    _reset_tree(wt, before)   # drops MERGE_HEAD too; a no-op when the merge never started
    return rc, conflicts


def _held_by(wt: Path, branch: str, head: str, pre: str,
             kept: Sequence[tuple[str, str]]) -> tuple[list[str], list[str]] | None:
    """Whether branch tip ``head``, which just failed to merge onto the line checked out in
    ``wt``, is held for a conflict with ``kept`` — the wave's bundles already merged onto
    ``branch``, ``(name, head)`` in merge order (#674) — and if so ``(partners, paths)``:
    the bundles of ``kept`` it conflicts with and the files it conflicts in.

    Decided on ``pre``, the line as the wave found it (before the first of them), never on
    the line as it is now: held exactly when ``head`` merges cleanly onto ``pre`` but not
    onto ``pre`` plus ``kept``, merged in order. None otherwise, and the fold stops on it
    as before: a ``head`` that does not merge onto ``pre`` conflicts with earlier-wave
    work, and one that merges onto ``pre`` plus ``kept`` conflicts with something else the
    line took in after ``pre`` — the base, merged in for a bundle of the wave whose PR
    merged and whose branch is gone (:func:`_take_in_base`). Neither is a conflict with
    the bundles kept, so none of them is named. ``partners``: each bundle of ``kept`` tried
    on its own (``pre``, then it, then ``head``) that ``head`` conflicts with; when it
    conflicts only with several of them together, all of them. ``paths``: the files left
    conflicting on ``pre`` plus ``kept``. The trials never move the line (:func:`_trial`)."""
    if _trial(wt, branch, pre, [head])[0] is not None:
        return None
    at, paths = _trial(wt, branch, pre, [*(its for _name, its in kept), head])
    if at != len(kept):
        return None
    partners = [name for name, its in kept if _trial(wt, branch, pre, [its, head])[0] == 1]
    return (partners or [name for name, _its in kept]), paths


def _trial(wt: Path, branch: str, onto: str,
           revs: Sequence[str]) -> tuple[int | None, list[str]]:
    """Merge ``revs`` one after another onto commit ``onto`` in ``wt``, as a trial (#674):
    ``(i, paths)`` for the first that does not merge cleanly — its index and the files it
    left conflicting — or ``(None, [])`` when all do. It runs on a detached HEAD, so
    ``branch`` never moves, and ends with the tree back on ``branch`` at the tip it had,
    clean (:func:`_reset_tree`): one that will not go back raises :class:`_LineBroken`. A
    merge that fails with no conflicting path is a failed git step and raises. The caller
    holds the worktree's lock (:func:`fold`)."""
    tip = _rev(wt, "HEAD")
    if tip is None:
        raise IntegrationTreeError(f"could not read the tip of {branch} in {wt} — check that "
                                   f"checkout, then re-run")
    try:
        if _git(wt, "checkout", "-q", "--detach", onto) != 0:
            raise IntegrationTreeError(f"could not check out {onto} in {wt} to try a merge "
                                       f"— check that checkout, then re-run")
        for i, rev in enumerate(revs):
            rc, conflicts = _merge(wt, rev, f"pdca-integrate: trial merge of {rev}")
            if rc != 0 and not conflicts:
                raise IntegrationError(
                    f"could not try merging {rev} in {wt} — `git merge` exited {rc} with no "
                    f"conflicting path, so a git step failed: check that checkout, then re-run")
            if rc != 0:
                return i, conflicts
        return None, []
    finally:
        _reset_tree(wt, "HEAD")
        if _git(wt, "checkout", "-q", branch) != 0 or _rev(wt, "HEAD") != tip:
            raise _LineBroken(f"could not put {branch} back at {tip} in {wt} after a trial "
                              f"merge — check that checkout, then re-run")


def _work(wt: Path, heads: Iterable[str | None], ends: Sequence[str], *,
          what: str) -> list[str]:
    """The commits reachable from any of ``heads`` and from none of ``ends``, newest first,
    merge commits included (#672): a bundle's work, measured against the base and the
    commit its line started from. A head that is None, or that this clone does not have,
    adds nothing. A git failure raises (:func:`_out`)."""
    have = [h for h in dict.fromkeys(heads)
            if h and _git(wt, "cat-file", "-e", f"{h}^{{commit}}") == 0]
    if not have:
        return []
    return _out(wt, "rev-list", *have, "--not", *ends, what=what).split()


def _holding(name: str, ref: str, commit: str, cause: str, branch: str) -> str:
    """Why ``name`` is left out for holding ``cause``'s work (#672)."""
    return (f"{name}'s branch {ref} holds {commit[:12]}, which is also {cause}'s work, and "
            f"this fold leaves {cause} off {branch}")


def _hold_work(wt: Path, order: Sequence[str], took: Mapping[str, str],
               work: dict[str, list[str]], ends: Sequence[str], pr_heads: Mapping[str, str],
               *, refs: Mapping[str, tuple[str, str, bool]], branch: str,
               skipped: dict[str, str], holds: dict[str, str] | None) -> set[str]:
    """Leave out each bundle merged onto the line whose head reaches work the fold left out
    (#672). ``took``: name → the head merged; ``work``: name → the work (:func:`_work`) of
    each bundle left out so far, filled in place. A merged bundle whose head reaches a
    commit of one of them — the first in fold order ``order`` — is recorded in ``skipped``
    and ``holds`` with that one as its cause, and its own work, from its head and its PR
    head, joins ``work``: a bundle that reaches only that is left out too, to a fixed point.
    A cause is always one left out before, so no two name each other. Returns the names
    left out."""
    reach = {n: set(_work(wt, (h,), ends, what=f"{n}'s commits")) for n, h in took.items()}
    held: set[str] = set()
    grew = True
    while grew:
        grew = False
        for n in order:
            if n not in reach or n in work:
                continue
            hit = next(((c, x) for c in order if c in work
                        for x in work[c] if x in reach[n]), None)
            if hit is None:
                continue
            cause, commit = hit
            work[n] = _work(wt, (took[n], pr_heads.get(n)), ends, what=f"{n}'s work")
            skipped[n] = _holding(n, refs[n][1], commit, cause, branch)
            if holds is not None:
                holds[n] = cause
            held.add(n)
            grew = True
    return held


def _built_on_skipped(wt: Path, failed: Sequence[str], tips: Mapping[str, str | None],
                      work: Mapping[str, Sequence[str]], ends: Sequence[str], *,
                      refs: Mapping[str, tuple[str, str, bool]], branch: str,
                      skipped: dict[str, str], holds: dict[str, str] | None) -> None:
    """Of the bundles whose merge failed (``failed``, in fold order), record each one whose
    branch holds any of the work of one before it in fold order (``work``, :func:`_work`)
    for holding that one's work, not for its own failure (#672): the first such one is its
    cause. Its branch is read at one tip (``tips``: the branch tip the fold read, else the
    PR head). A commit two of them share is the work of both, so the earlier one in fold
    order is the cause: a cause always comes before the bundle it names, and no two name
    each other."""
    for i, n in enumerate(failed):
        if not tips.get(n):
            continue
        mine = set(_work(wt, (tips[n],), ends, what=f"{n}'s commits"))
        hit = next(((c, x) for c in failed[:i] for x in work.get(c, ()) if x in mine), None)
        if hit:
            cause, commit = hit
            skipped[n] = _holding(n, refs[n][1], commit, cause, branch)
            if holds is not None:
                holds[n] = cause


def _reset_tree(wt: Path, rev: str) -> None:
    """Put ``wt`` back to exactly ``rev`` (#631): ``reset --hard`` (drops a merge in
    progress, restores tracked files) then ``clean -ffd`` (removes untracked files and
    directories — with ``-f`` twice, also one that is a git repository of its own, such as a
    gate's clone, which a single ``-f`` skips and which blocks a later merge the same way).
    No ``-x``: an ignored file never makes a checkout or merge refuse — git overwrites it —
    so it cannot change a fold's result, and keeping it lets the re-gate reuse its build
    caches. Raises :class:`IntegrationTreeError` when either step fails."""
    for step in (("reset", "-q", "--hard", rev), ("clean", "-q", "-ffd")):
        if _git(wt, *step) != 0:
            raise IntegrationTreeError(
                f"could not reset the integration worktree {wt} to {rev} — `git "
                f"{' '.join(step)}` failed, so the tree's state is unknown: check that "
                f"checkout (or remove it to have it recreated), then re-run")


def _fetch(repo: Path, base_remote: str, remotes: Iterable[str] = ()) -> None:
    """Fetch what a fold reads into ``repo``: ``origin`` (the line and the PR branches),
    every remote a ``"stacked"`` record's branch lives on (#593), and the base remote. The
    remotes holding PR branches are fetched with ``--prune``: a published branch deleted on
    its remote must stop resolving here, and a stale remote-tracking ref would hide that.
    The base remote is fetched LAST: a merged PR's branch is deleted after its merge, so
    when these fetches show a branch as gone, the base snapshot already has that merge — the
    check that the base has the merged head (:func:`_gone_branch`) never runs on a snapshot
    taken too early. A missing checkout, or a fetch that fails, is a failed git step (the
    run stops)."""
    if not (repo / ".git").exists():
        raise IntegrationError(f"checkout not found at {repo}")
    branch_remotes = list(dict.fromkeys(("origin", *remotes)))
    for remote in [r for r in branch_remotes if r != base_remote] + [base_remote]:
        prune = ("--prune",) if remote in branch_remotes else ()
        if _git(repo, "fetch", *prune, remote) != 0:
            raise IntegrationError(f"could not fetch {remote} in {repo} — the fold cannot "
                                   f"see the branches it merges")


def _prepare_worktree(repo: Path, base_remote: str, base: str,
                      remotes: Iterable[str] = ()) -> Path:
    """Create (or reuse) the integration worktree off the freshly-fetched base (:func:`_fetch`);
    raise :class:`IntegrationError` if it can't be made (worktree isolation is required here
    — unlike Do/Check, there is no in-place fallback that would still produce the branch)."""
    _fetch(repo, base_remote, remotes)
    wt = _integ_worktree(repo, base)
    if not (wt / ".git").exists() and _git(repo, "worktree", "add", "--force",
                                           str(wt), f"{base_remote}/{base}") != 0:
        raise IntegrationError(f"could not create the integration worktree at {wt}")
    return wt
