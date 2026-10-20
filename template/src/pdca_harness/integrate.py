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
pushes anything (``carried``, #633). A rewrite after the run's last fold read the branch
(during that fold, or while the last wave builds, publishes, or Act runs) has no next fold
to stop: the run checks every branch it carried once more at its end, names each one
rewritten and each one it could not check, and exits 1 (:func:`rewritten_carried`,
#695). Both checks fetch while no integration lock is held, where another git process in
the same checkout (a held ref lock) can fail the fetch once: it is tried twice before it
counts as failed (#695). A branch deleted after its PR merged is judged by
the head commit the host says the PR merged with (``merged.merged_head``), never by a
commit message: a line that already has that commit skips it; one that does not (a fixup
pushed after the fold) takes the base in, once the base is checked to have that commit —
a PR merged somewhere else (its base edited on the host, a squash or rebase merge) stops
the run instead, and so does a base that would undo work already on the line (a revert of
a merged PR, #623). So does a bundle's own branch whose merge would undo it: one cut from,
or updated from, a base with such a revert brings the revert along (#673). A removal the
branch's own commits make is its change, never counted. Both checks judge each path
against where the line leaves the base, so they do not see a partial revert inside a file
the line also changed in other ways, nor a revert of work below that point, which the base
already has (a later wave's merged PR brings the line commits under it there): an accepted
limit. A branch that does not merge cleanly (an undeclared cross-wave overlap)
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
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path

from . import brief, merged, publish, waves
from .config import Config


class IntegrationError(RuntimeError):
    """A wave's accepted work could not be folded onto the integration branch — a published
    branch does not merge cleanly (undeclared overlap), is missing, or was rewritten after
    the run carried it (#633), another run moved the line, or a git step failed. The caller
    STOPs rather than build the next wave on an incomplete base.

    ``cause`` (#695): what failed, in a few words, without the step the message tells a fold
    to take (fix the checkout, then re-run) — for a report made outside a fold, where that
    step is wrong (:func:`rewritten_carried`, at the end of a run that already pushed its
    line). The whole message when the raiser gives none."""

    def __init__(self, *args: object, cause: str | None = None) -> None:
        super().__init__(*args)
        self.cause = str(self) if cause is None else cause


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
    conflict from any other failed merge. ``paths``: the files the merge left conflicting
    (#694: the carry names them)."""

    def __init__(self, message: str, paths: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.paths = list(paths)


class _Rewritten(IntegrationError):
    """What :func:`build_bundle_base` refuses a member's rewritten branch with (#727):
    bundle ``name``'s, every PR holding a head it lost named in the message. The flow holds
    the bundle whose base it is, not the run (#738); when a fold follows that bundle's wave,
    that hold's line says the next fold stops the run unless the branch is restored first
    (#633). The flow's end-of-run check (#695) still judges that branch: a hold does not
    take it out, so a rewrite the line still carries at the end of the run is reported
    again there, and fails the run."""

    def __init__(self, message: str, name: str) -> None:
        super().__init__(message)
        self.name = name


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


def bundle_base_branch(cfg: Config, base: str, batch: Iterable[str] | None, name: str) -> str:
    """The branch :func:`build_bundle_base` builds for bundle ``name`` (#723): the run's
    line for ``base`` (:func:`integration_branch`, scoped to ``batch``), then ``-b`` and the
    bundle's id (``issue_`` dropped, so ``12`` and ``issue_12`` are one bundle), escaped as a
    base is (:func:`_flatten_base`). A flattened base or id never holds ``-b`` (each ``-``
    there is ``-h`` or ``-s``), nor does a batch key (hex), so the first ``-b`` ends the
    line's part and the name decodes to one (base, batch, bundle). It has no ``/`` after
    ``pdca-integration/``, so it is never a ref-path prefix of the line or of another
    bundle's base, nor under one (git refuses a branch ``x`` beside ``x/y``)."""
    iid = str(name).removeprefix("issue_")
    return f"{integration_branch(cfg, base, batch)}-b{_flatten_base(iid)}"


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
         clashes: dict[str, tuple[str, dict[str, list[str]], list[str]]] | None = None,
         bases: Bases | None = None, line_prs: LinePrs | None = None,
         finished_heads: Mapping[str, str] | None = None,
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
    bundles of ``accepted`` and ``batch`` are what the message names. A bundle's branch
    whose merge would remove such work is refused the same way, unless the branch's own
    commits make the removal (#673, :func:`_keep_merge`): one cut from, or updated from, a
    base with a revert brings that revert along.

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

    ``bases`` / ``line_prs`` (#727, the flow's in-run folds: :data:`Bases`,
    :data:`LinePrs`): the heads the run's PRs hold. The same check holds each branch to
    them too, and the error names each PR holding a head the branch lost.

    ``held_out`` (#674, the flow's in-run folds) with ``wave``, the names of the bundles of
    the wave just published: a bundle of ``wave`` whose branch conflicts with the line is
    left out — recorded in place as ``held_out[name] = (partners, paths)`` — when at least
    one bundle of ``wave`` was merged before it on this line, and its branch merges cleanly
    onto the line as it was before the first of them but not onto that line plus them
    (:func:`_held_by`). ``partners`` are the ones of those it conflicts with, ``paths`` the
    conflicting files. So a bundle is judged against the bundles actually kept, in merge
    order, never against one already held; a branch that merges cleanly is never held,
    whatever files it shares. A conflict that is not with them is left out the same way
    when bundles already on the line explain it (#747, :func:`_held_by_line`): its branch
    merges cleanly onto ``<base_remote>/<base>`` alone, :func:`_clash` names the bundles on
    the line whose own change it conflicts with — of an earlier wave or of this one, or
    finished ids the carry put there (``finished_heads``) — and the line without them
    merges it cleanly (rebuilt on those of them it says it builds on, which stay). Any
    other conflict — with the base, with work on the line no bundle's own change accounts
    for, or one the named bundles do not explain alone — raises, as before. Nothing is
    pushed for a held bundle, and the caller leaves it out of its later folds.
    The trial merges run in the integration worktree under the fold's lock and leave it
    clean on the line. ``None`` (the carry, a direct caller): every conflict raises (or,
    under ``skipped``, is skipped) as before; a dry-run merges nothing, so holds nothing.

    ``clashes`` (#694, the flow's pre-wave carry), with ``skipped``: for each bundle
    skipped because its branch does not merge cleanly onto the line (a textual conflict),
    and not held for a skipped bundle's work, ``clashes[name] = (kind, partners, files)``
    says what it conflicts with (:func:`_clash`). It is judged once every pass is over, on
    the line the fold pushes, so never against a bundle a later pass took back off: the
    base (``"base"``); else the bundles the fold carries — merged before it or after, or on
    the line already — each with its files (``"batch"``); else other work on that line
    (``"line"``); else, when it merges onto that line, the bundles on the line when it was
    tried that a later pass took back off and that it conflicts with, maybe none
    (``"off"``), or, when none was taken off, nothing (``"moved"``: a later merge changed
    the line). Filled in place; its trial merges run in the integration worktree under the
    fold's lock and leave it clean on the line, as ``held_out``'s do. ``None`` records
    nothing and tries nothing.

    ``finished_heads`` (#747, the flow's in-run folds): each finished id the pre-wave carry
    (#646) put on the run's line → the head it put there. Used only as more bundles a
    ``held_out`` conflict may be pinned on, and only while that head is on the line being
    built: never merged, never put in ``carried``, never checked for a rewrite. ``None``:
    none.
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
    checked = (_carried_tips(cfg, groups, refs, carried, batch, bases=bases, line_prs=line_prs)
               if carried and not dry_run else {})
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
            # skipped for a conflict → (head, files, the bundles on the line then) (#694)
            conflicting: dict[str, tuple[str, list[str], list[str]]] = {}
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
                        if (held_out is not None and ours and head is not None
                                and pre is not None and isinstance(exc, _Conflict)):
                            # A same-wave conflict (#674), else one that bundles already
                            # on the line explain (#747): this bundle only.
                            clash = (_held_by(wt, branch, head, pre, kept) if kept else None)
                            if not clash:
                                clash = _held_by_line(
                                    cfg, wt, branch, d, head, f"{base_remote}/{base}",
                                    exc.paths, list(took.items()), finished_heads or {},
                                    {n: h for n, h in read.items() if h})
                            if clash:
                                held_out[d.name] = clash
                                dropped.add(d.name)
                                continue
                        if skipped is None or isinstance(exc, (IntegrationTreeError,
                                                                _LineBroken)):
                            raise
                        if (clashes is not None and head is not None
                                and isinstance(exc, _Conflict)):
                            conflicting[d.name] = (head, exc.paths, list(took))
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
            # What each bundle skipped for a conflict conflicts with (#694), judged once every
            # pass is over (a #672 hold may have taken bundles back off), on the line the
            # fold pushes: against the bundles it carries (``took``), else the ones on the
            # line when it was tried that a later pass took back off. One held for a skipped
            # bundle's work is not judged: that is its reason. Every tip was read in the
            # first pass, once (#633).
            judge = [n for n in conflicting if n not in (holds or {})]
            if clashes is not None and judge:
                tips = {d.name: t for d in bundles
                        if (t := read.get(d.name) or pr.get(d.name))
                        and _git(wt, "cat-file", "-e", f"{t}^{{commit}}") == 0}
                offs = [o for o in tips if o in dropped and o not in failed]  # taken back off
                try:
                    under: dict[str, list[str]] | None = _built_on(
                        wt, f"{base_remote}/{base}", tips, [*took, *offs])
                except IntegrationError:    # a git read failed (the tree is untouched):
                    under = None            # name no bundle, as a failed trial does
                for n in judge:
                    h, ps, then = conflicting[n]
                    off = [(o, tips[o]) for o in then if o in offs]
                    clashes[n] = (("line", {}, list(ps)) if under is None else
                                  _clash(wt, branch, h, f"{base_remote}/{base}", ps,
                                         list(took.items()), off, under))
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


def _bundle_name(name: str) -> str:
    """``name`` as a bundle's name, given as one or as its id: ``500`` and ``issue_500`` are
    one bundle, as :func:`batch_key` counts them."""
    return "issue_" + str(name).removeprefix("issue_")


def build_bundle_base(cfg: Config, d: Path, members: Sequence[Path], *,
                      batch: Collection[str] | None, carried: Mapping[str, str],
                      excluded: Mapping[str, str],
                      merged_heads: dict[str, str] | None = None,
                      bases: Bases | None = None,
                      line_prs: LinePrs | None = None) -> tuple[str, str]:
    """Build bundle ``d``'s own base (#723, for #463's dependency-scoped bases): a branch on
    ``origin`` holding exactly ``<base_remote>/<base>`` as just fetched plus the published
    branches of ``members`` (bundle paths, merged in that order). Returns ``(branch, pushed
    tip)``; the branch is :func:`bundle_base_branch` for ``d``'s target. The run's line, and
    what the caller recorded for it (``carried`` included), are never written.

    A sibling of :func:`fold` on the same path: the target's lock (:func:`integ_lock`) and
    worktree (:func:`_prepare_worktree`, which fetches), the tree reset
    (:func:`_reset_tree`), and the merge of each published branch (:func:`_merge_published`:
    a gone branch is judged by :func:`_gone_branch`, and a merge that would remove a
    member's work is refused, #623/#673). It always starts fresh from the base as fetched.
    A member with no patch adds nothing, as in a fold; one with a patch but no usable
    target, one on another target than ``d``, or one with no published branch raises before
    any git step.

    Refused with :class:`IntegrationError`, before anything is pushed:

    * ``carried`` (member name → the head a fold of this run put on its line, #633): a
      member whose branch no longer holds that head was rewritten
      (:func:`_carried_rewrite`). Each member's tip is read once, after the fetch, and that
      commit is the one checked and merged. Checked before anything is merged. So for a
      head ``bases`` / ``line_prs`` record (#727, as :func:`fold` takes them): the refusal
      (a :class:`_Rewritten`) names each PR holding a head the branch lost
      (:func:`_rewrite_stop`).
    * A member whose cut point cannot be trusted (:func:`_member_cut_point`). Checked before
      anything is merged.
    * A member that does not merge cleanly, naming both sides (:func:`_member_clash`): one
      that clashes with the fetched base itself names ``<base_remote>/<base>`` and is to be
      rebuilt on it.
    * A result holding work that is neither the base's nor a member's own
      (:func:`_unowned_work`). A member's own work is each non-merge commit its head (the one
      merged, or found on the branch already) reaches that neither the fetched base nor its
      cut point does: the commit its branch was cut from, as recorded when it was published,
      or nothing for a member never stacked. So work merged by hand into a member's branch
      above its cut point is its own, and ownership comes only from what was recorded for
      each member, never from which integration refs exist on origin: rebuilding or moving a
      ref cannot change who owns a member's unchanged history. The message names the first
      such commit, the first member whose head reaches it, and each bundle of ``excluded``
      (name → the published head of a bundle of the run that is not a member) whose head
      reaches it, or says no bundle of the run owns it (:func:`_foreign_refusal`).
      ``excluded`` only names: it never decides a refusal, so a head this clone does not
      have is no error.

    A name in ``carried`` or ``excluded`` is a bundle's with or without its ``issue_``
    prefix (:func:`_bundle_name`); one that is also a member's is a member.

    ``merged_heads`` (#727), when given, gets each member's name with the head this build
    put on the branch (merged, or found on it) once the push has succeeded: what the run's
    later checks hold that branch to for ``d``'s PR (:data:`Bases`).

    Push: a branch origin does not have is pushed plainly (no force, so an origin with
    ``receive.denyNonFastForwards`` takes it); one it has (``d``'s base built again) is
    force-pushed, leased on the tip just fetched. A refused push raises, naming the branch
    and the remedy."""
    repo_spec, base, _slug = publish._resolve_target(d)
    if not (repo_spec and base):
        raise IntegrationError(f"{d.name} has no usable target (Repo + branch target), so "
                               f"there is no base to build its own base on")
    branch = bundle_base_branch(cfg, base, batch, d.name)
    found = _fold_candidates(members)
    targeted = {c[0] for c in found}
    lost = [m.name for m in members if _has_patch(m) and m not in targeted]
    if lost:
        raise IntegrationError(
            f"{', '.join(lost)} has a patch but no usable target (Repo + branch target), so "
            f"none of its work can be part of {branch}: fix its brief, or leave it out of "
            f"{d.name}'s members, then re-run")
    mine: list[Path] = []                          # the members with work, in member order
    refs: dict[str, tuple[str, str, bool]] = {}    # member name → (remote, ref, onto)
    for m, m_repo, m_base, _s, ref in found:
        if (m_repo, m_base) != (repo_spec, base):
            raise IntegrationError(
                f"{m.name} targets {m_repo} @ {m_base}, but {d.name} targets {repo_spec} @ "
                f"{base}: one branch holds one target's work, so {m.name} cannot be part of "
                f"{branch}")
        if ref is None:
            raise IntegrationError(
                f"{m.name} accepted but not published (no publish.json branch) — there is "
                f"nothing of it to put on {branch}; publish it, then re-run")
        mine.append(m)
        refs[m.name] = ref
    names = {_bundle_name(m.name) for m in members}
    took_by = {_bundle_name(n): h for n, h in carried.items()}
    others: dict[str, str] = {}                    # the excluded bundles, by bundle name
    for n, h in excluded.items():
        if _bundle_name(n) not in names:
            others[_bundle_name(n)] = h
    base_remote = cfg.base_remote
    base_ref = f"{base_remote}/{base}"
    line = integration_branch(cfg, base, batch)   # where the run's folds recorded ``carried``
    repo = publish._checkout_path(cfg, repo_spec)
    with integ_lock(_integ_worktree(repo, base)) as held:
        if not held:
            raise IntegrationError(
                f"could not take the integration lock next to "
                f"{_integ_worktree(repo, base).name} — fix the checkout's parent "
                f"directory (permissions?), then re-run")
        wt = _prepare_worktree(repo, base_remote, base, remotes=[r[0] for r in refs.values()])
        heads = {m.name: _rev(wt, refs[m.name][1]) for m in mine}   # each read once (#633)
        takers = _takers(took_by, bases, line_prs)
        for m in mine:
            ref, tip = refs[m.name][1], heads[m.name]
            gone = (_lost(wt, m.name, ref, tip, line, takers.get(_bundle_name(m.name), []))
                    if tip else [])
            if gone:
                raise _Rewritten(_rewrite_stop(m.name, ref, tip, line, gone, onto=branch),
                                 m.name)
        cuts: dict[str, str | None] = {}           # member → its cut point (None: never stacked)
        for m in mine:
            head = heads[m.name]
            if head is not None:                   # a gone branch is judged by _gone_branch
                cuts[m.name] = _member_cut_point(wt, d, m, refs[m.name], head)
        existing = _rev(wt, f"origin/{branch}")    # origin's copy, as just fetched
        _reset_tree(wt, "HEAD")
        if _git(wt, "checkout", "-B", branch, base_ref) != 0:
            raise IntegrationError(f"could not start {branch} off {base_ref}")
        merged_in: list[tuple[str, str]] = []      # (member, head on the branch), in order
        for m in mine:
            head = heads[m.name]
            try:
                _merge_published(cfg, wt, m, refs[m.name], head, branch=branch, base=base,
                                 owners=tuple(mine))
            except _Conflict as exc:
                raise _member_clash(wt, d, branch, base_ref, m.name, refs[m.name][1], head,
                                    merged_in, exc) from exc
            if head is not None:                   # merged, or on the branch already
                merged_in.append((m.name, head))
        tip = pushed_tip(wt)
        own = [(n, refs[n][1], h, cuts[n]) for n, h in merged_in]
        foreign = _unowned_work(wt, tip, base_ref, own)
        if foreign:
            raise _foreign_refusal(wt, d, branch, base_ref, foreign, own, others)
        push = (("push", "origin", branch) if existing is None else
                ("push", f"--force-with-lease={branch}:{existing}", "origin", branch))
        if _git(wt, *push) != 0:
            why = (f"origin already has {branch} at {existing[:12]}, and a build always starts "
                   f"fresh from {base_ref}, so building {d.name}'s base again force-pushes "
                   f"over it: if origin refuses force-pushes, delete {branch} there or allow "
                   f"force-pushes on pdca-integration/*; or another run moved it since this "
                   f"build fetched it" if existing is not None else
                   f"a plain push creating it was refused: check origin's rules for "
                   f"pdca-integration/*")
            raise IntegrationError(f"could not push {branch} to origin ({why}) — {d.name} "
                                   f"cannot be built on it")
        if merged_heads is not None:
            merged_heads.update(merged_in)   # pushed: the branch now holds these (#727)
    return branch, tip


def _member_cut_point(wt: Path, d: Path, m: Path, published: tuple[str, str, bool],
                      head: str) -> str | None:
    """Where member ``m``'s published branch (``published``, its tip ``head``) was cut from,
    as recorded when it was published (#723): the commit its own work on ``d``'s base is
    counted above (:func:`build_bundle_base`). Its ``stack-base-tip``
    (``publish.read_stack_base_tip``) when it has a ``stack-base`` record
    (``publish.read_stack_base``); None, nothing below its own commits but the base, for a
    member never stacked. Read from the bundle's own files only, never from which
    integration refs exist on origin.

    Raises :class:`IntegrationError`, naming ``m`` and the fix (re-publish it), when the cut
    point cannot be trusted:

    1. a ``stack-base`` record with no tip (a dry-run fold records none, and so did a marker
       older than #593). ``read_stack_base_tip`` gives "" both for that and for no record,
       so the record itself is read;
    2. a tip that is not an ancestor of ``head``, or that this clone does not have: the
       branch was cut, or cut again, from somewhere else since it was recorded;
    3. an ``Onto branch`` member (``published``'s ``onto``): a commit on another PR's
       branch, for which no cut point is recorded;
    4. no ``stack-base`` record, but published stacked: its ``publish.json`` ``mode`` is
       ``"stacked-pr"`` (publish cut it from a stack branch: a wave's line, whose record a
       later run cleared, or a legacy ``Stacks on:`` parent's PR branch), or its brief
       declares ``Stacks on:``.

    A git failure raises."""
    _remote, ref, onto = published

    def refuse(why: str, how: str) -> IntegrationError:
        return IntegrationError(
            f"{why}, so which commits on {ref} are {m.name}'s own cannot be told, nor whether "
            f"{d.name}'s base would hold work of a bundle it does not build on. Nothing was "
            f"merged or pushed. Re-publish {m.name} {how}, then re-run")

    if onto:
        raise refuse(f"{m.name} was published as an `Onto branch` commit on another PR's "
                     f"branch {ref}, and no cut point is recorded for one",
                     "as a PR of its own (no `Onto branch` in its brief)")
    stack = publish.read_stack_base(m)
    if stack:
        tip = publish.read_stack_base_tip(m)
        if not tip:
            raise refuse(f"{m.name} has a recorded stack base ({stack}) but no recorded tip, "
                         f"the commit its branch was cut from",
                         "from a run that records that tip (not a dry-run)")
        if not _carries(wt, tip, f"{m.name}'s recorded tip {tip[:12]}", ref, of=head):
            raise refuse(f"{m.name}'s recorded cut point {tip[:12]} (its stack-base tip, on "
                         f"{stack}) is not in its branch {ref} at {head[:12]}: the branch was "
                         f"cut, or cut again, from somewhere else since that was recorded",
                         "(publish cuts its branch from its recorded tip)")
        return tip
    why: list[str] = []
    if (publish._publish_record(m) or {}).get("mode") == "stacked-pr":
        why.append('its publish.json records "mode": "stacked-pr"')
    parents = brief.stacks_on(m / "brief.md")
    if parents:
        why.append(f"its brief declares `Stacks on: {', '.join(parents)}`")
    if why:
        raise refuse(f"{m.name} was published stacked ({'; '.join(why)}) but has no "
                     f"stack-base record: the commit its branch {ref} was cut from is "
                     f"unknown",
                     "from a run that records where its branch is cut from"
                     + (" (plan its parent as a `Depends on`, not `Stacks on:`)"
                        if parents else ""))
    return None


def _member_clash(wt: Path, d: Path, branch: str, base_ref: str, name: str, ref: str,
                  head: str | None, merged_in: Sequence[tuple[str, str]],
                  exc: _Conflict) -> IntegrationError:
    """The refusal for member ``name`` (branch ``ref`` at ``head``) not merging cleanly onto
    ``d``'s base ``branch`` (#723), naming both sides. A head that does not merge cleanly
    onto ``base_ref`` alone clashes with the base itself (always so when no member was
    merged before it): no bundle is to blame, and the member's branch is to be rebuilt on
    the new base. Otherwise the members of ``merged_in`` (merged before it, ``(name, head)``
    in order) it clashes with, each tried on its own after the base, as a fold finds a
    same-wave clash (:func:`_held_by`), or all of them when only their combination clashes.
    The trials run on a detached HEAD under the caller's lock and leave ``branch`` where it
    was (:func:`_trial`)."""
    if head is None or not merged_in:
        on_base, files = True, list(exc.paths)
    else:
        at, files = _trial(wt, branch, base_ref, [head])
        on_base = at is not None
    if on_base or head is None:
        return IntegrationError(
            f"{name}'s branch {ref} does not merge cleanly onto {base_ref} itself (conflicts "
            f"in {', '.join(files)}): {base_ref} changed those files since {name}'s branch "
            f"was cut, so no base built from {base_ref} can hold {name} as it is. Rebuild "
            f"{name}'s branch on the new {base_ref} (update it from {base_ref} and resolve "
            f"the conflicts there), then re-run")
    clash = _held_by(wt, branch, head, base_ref, merged_in)
    partners, paths = clash if clash else ([n for n, _h in merged_in], list(exc.paths))
    return IntegrationError(
        f"{name}'s branch {ref} does not merge cleanly onto {branch} after "
        f"{', '.join(partners)} (conflicts in {', '.join(paths)}): {name} and "
        f"{', '.join(partners)} change the same lines and neither is built on the other, so "
        f"{d.name}'s base cannot hold both. Plan one of them to build on the other and "
        f"re-drive it, then re-run")


def _unowned_work(wt: Path, tip: str, base_ref: str,
                  own: Sequence[tuple[str, str, str, str | None]]) -> list[str]:
    """The non-merge commits on a bundle base at ``tip`` that are neither ``base_ref``'s nor
    any member's own work (#723), newest first in topological order. ``own``: for each
    member with a head, ``(name, ref, the head merged, its cut point or None)``
    (:func:`_member_cut_point`). A member's own work is each non-merge commit its head
    reaches that neither ``base_ref`` nor its cut point does: :func:`foreign_commit`'s
    ``rev-list --no-merges`` shape, with the cut point beside the base. A merge commit (the
    build's own, or a branch's update or topic merge) never counts. A git failure raises."""
    built = _out(wt, "rev-list", "--no-merges", "--topo-order", tip, "--not", base_ref,
                 what=f"the commits on {tip[:12]}").split()
    if not built:
        return []
    theirs: set[str] = set()
    for name, _ref, head, cut in own:
        theirs.update(_out(wt, "rev-list", "--no-merges", head, "--not", base_ref,
                           *((cut,) if cut else ()), what=f"{name}'s own work").split())
    return [c for c in built if c not in theirs]


def _foreign_refusal(wt: Path, d: Path, branch: str, base_ref: str, foreign: Sequence[str],
                     own: Sequence[tuple[str, str, str, str | None]],
                     others: Mapping[str, str]) -> IntegrationError:
    """The refusal of ``d``'s base ``branch`` for holding ``foreign``, work that is no
    member's own (#723, :func:`_unowned_work`; ``own`` as there). It names the first such
    commit, the first member (in member order) whose head reaches it, and each bundle of
    ``others`` (name → published head) whose head reaches it, or says no bundle of the run
    owns it. ``others`` only names: a head this clone does not have is left unjudged, and
    said so. A git failure raises."""
    commit = foreign[0]
    more = (f" (and {len(foreign) - 1} more commit{'s' if len(foreign) > 2 else ''} like it)"
            if len(foreign) > 1 else "")
    by = next(((n, ref, cut) for n, ref, head, cut in own
               if _in_line(wt, commit, commit[:12], f"{n}'s branch", of=head)), None)
    seen = {n: h for n, h in others.items()
            if h and _git(wt, "cat-file", "-e", f"{h}^{{commit}}") == 0}
    theirs = [n for n, h in seen.items()
              if _in_line(wt, commit, commit[:12], f"{n}'s published head", of=h)]
    unseen = [n for n in others if n not in seen]
    if by is None:
        how, member = "a member's branch brings it in", "that member"
    else:
        n, ref, cut = by
        how = f"{n}'s branch {ref} brings it in" + (
            f" from below {cut[:12]}, the cut point recorded when {n} was published"
            if cut else "")
        member = n
    if theirs:
        whose = (f"It is in the published head of {', '.join(theirs)}, which {d.name} does "
                 f"not build on")
        fix = f"add {', '.join(theirs)} to {d.name}'s members, or rebuild {member}'s branch"
    else:
        whose = "No bundle of the run owns it" + (
            f" (this clone does not have the published head of {', '.join(unseen)}, so "
            f"that was not checked)" if unseen else "")
        fix = f"rebuild {member}'s branch"
    return IntegrationError(
        f"{branch} would hold {commit[:12]}{more}, which is not {base_ref}'s and not any "
        f"member's own work: {how}. {whose}. Nothing was pushed. To build {d.name}'s base, "
        f"{fix} on a base without that work (re-publish it), then re-run")


#: Who took a head from a branch (#727): ``None``, the run's line; ``(owner, None)``, the PR
#: of ``owner``, cut from the line then; ``(owner, branch)``, ``owner``'s own base.
Taker = tuple[str, str | None] | None
#: Each bundle on a base of its own whose PR the run published → ``(that base's branch,
#: {member: the head it took})`` (#727).
Bases = Mapping[str, tuple[str, Mapping[str, str]]]
#: Each bundle the line took a head from → ``[(owner, head), ...]``, in the order cut: each
#: PR the run cut from the line while it held ``head`` of that branch (#727). A fold refuses
#: a rewrite, so a PR cut later holds that head or a newer one.
LinePrs = Mapping[str, Sequence[tuple[str, str]]]


def _takers(carried: Mapping[str, str], bases: Bases | None,
            line_prs: LinePrs | None) -> dict[str, list[tuple[str, Taker]]]:
    """Each branch's bundle name → ``(head, who took it)``: the line's head (``carried``),
    each line-cut PR's (``line_prs``, in the order cut), each own base's (``bases``)."""
    out: dict[str, list[tuple[str, Taker]]] = {}
    for name, took in carried.items():
        out.setdefault(_bundle_name(name), []).append((took, None))
    for name, prs in (line_prs or {}).items():
        out.setdefault(_bundle_name(name), []).extend((h, (o, None)) for o, h in prs)
    for owner, (branch, heads) in sorted((bases or {}).items()):
        for name, took in heads.items():
            out.setdefault(_bundle_name(name), []).append((took, (owner, branch)))
    return out


def _lost(repo: Path, name: str, ref: str, tip: str, line: str,
          takers: Sequence[tuple[str, Taker]]) -> list[tuple[str, Taker, str]]:
    """``(head, who took it, facts)`` for each of ``takers`` whose head bundle ``name``'s
    branch ``ref``, now at ``tip``, no longer holds: each head judged once, as the line's
    is (:func:`_carried_rewrite`, whose words ``facts`` are). A git failure raises."""
    facts: dict[str, str | None] = {}
    for took, _by in takers:
        if took not in facts:
            facts[took] = _carried_rewrite(repo, name, ref, took, tip, line)
    return [(took, by, f) for took, by in takers if (f := facts[took]) is not None]


def _carried_tips(cfg: Config, groups: Mapping[tuple[str, str], list[Path]],
                  refs: Mapping[str, tuple[str, str, bool]], carried: Mapping[str, str],
                  batch: Collection[str] | None, *, bases: Bases | None = None,
                  line_prs: LinePrs | None = None) -> dict[str, str | None]:
    """The tip of each published branch ``carried`` records a head for (#633), read once
    after fetching its checkout — None for a branch that no longer resolves (gone:
    :func:`_gone_branch` judges it). :func:`fold` checks every target this way before it
    merges or pushes anything, and merges exactly the tips returned. The fetch runs before
    the fold takes any integration lock, so a concurrent git process in the same checkout
    can make it fail for a reason that has nothing to do with the bundles (a ref lock):
    it is tried twice before it raises (``attempts``, :func:`_fetch`, #695).

    Raises :class:`IntegrationError` for a branch whose tip no longer holds the head a fold
    of this run put on the line (``carried[name]``), as itself or an ancestor
    (:func:`_carried_rewrite`): the branch was rewritten (re-published, force-pushed,
    updated with a rebase), and merging its tip would put the old and the rewritten commits
    on the line together. Decided by ancestry, never by message; a git failure raises.
    So for a head ``bases`` / ``line_prs`` record (#727, :func:`fold`'s): the error names
    each PR holding a head the branch lost (:func:`_rewrite_stop`)."""
    takers = _takers(carried, bases, line_prs)
    tips: dict[str, str | None] = {}
    for (repo_spec, base), bundles in sorted(groups.items()):
        mine = [d for d in bundles if d.name in carried]
        if not mine:
            continue
        repo = publish._checkout_path(cfg, repo_spec)
        _fetch(repo, cfg.base_remote, [refs[d.name][0] for d in mine], attempts=2)
        line = integration_branch(cfg, base, batch)
        for d in mine:
            ref = refs[d.name][1]
            tip = tips[d.name] = _rev(repo, ref)
            if tip is None:
                continue
            lost = _lost(repo, d.name, ref, tip, line, takers[d.name])
            if lost:
                raise IntegrationError(_rewrite_stop(d.name, ref, tip, line, lost))
    return tips


def _rewrite_stop(name: str, ref: str, tip: str, line: str,
                  lost: Sequence[tuple[str, Taker, str]], *, onto: str | None = None) -> str:
    """The error a fold — or, given ``onto``, the build of the bundle base ``onto`` — stops
    with for bundle ``name``'s branch ``ref``, now at ``tip``, which lost each head of
    ``lost`` (:func:`_lost`): the line's words as before #727, then each PR holding a lost
    head (#727), each to be rebuilt."""
    parts: list[str] = []
    facts = next((f for _h, by, f in lost if by is None), None)
    if facts is not None:
        parts.append(f"{facts} " + (
            f"Only commits that extend a carried branch reach the line (a fast-forward, such "
            f"as GitHub's merge-style \"Update branch\"): merging {tip[:12]} would put the old "
            f"and the rewritten commits on {line} together." if onto is None else
            f"A bundle's own base takes from a branch this run carried only commits that "
            f"extend what the run took (a fast-forward, such as GitHub's merge-style \"Update "
            f"branch\"): merging {tip[:12]} would put on {onto} commits no fold of this run "
            f"checked, beside the old ones the run's other branches hold."))
    prs = [(by[0], h) for h, by, _f in lost if by is not None and by[1] is None]
    if prs:
        parts.append(f"Every PR this run cut from {line} after it took {prs[0][1][:12]} "
                     f"({', '.join(o for o, _h in prs)}) holds those old commits too.")
    parts += [_base_rewrite(name, ref, h, tip, by[0], by[1]) for h, by, _f in lost
              if by is not None and by[1] is not None]
    rebuild = [by[0] for _h, by, _f in lost if by is not None]
    heads = list(dict.fromkeys(h[:12] for h, _by, _f in lost))
    to = (f"to {heads[0]} or a commit that contains it" if len(heads) == 1
          else f"to a commit that contains {' and '.join(heads)}")
    if onto is not None:
        return (f"{' '.join(parts)} Nothing was merged or pushed. Restore {ref} {to}, then "
                f"re-run" + (f"; or {_rebuild(rebuild)}, then re-run" if rebuild else ""))
    return (f"{' '.join(parts)} Nothing was merged or pushed. Restore {ref} {to}, then "
            f"re-issue the run; or let this run stop and re-issue it"
            + (f", and {_rebuild(rebuild)}" if rebuild else ""))


def _rebuild(names: Sequence[str]) -> str:
    """The step for the PRs of ``names``, which hold a head their branch lost (#727)."""
    names = list(dict.fromkeys(names))
    if len(names) == 1:
        return f"rebuild {names[0]}'s PR on the rewritten branch before it merges"
    return (f"rebuild the PRs of {', '.join(names)} on the rewritten branch before any of "
            f"them merges")


def _carried_rewrite(repo: Path, name: str, ref: str, took: str, tip: str,
                     line: str) -> str | None:
    """Whether bundle ``name``'s branch ``ref``, now at ``tip``, was rewritten after a fold
    of this run put ``took`` from it on ``line`` (#633): the facts, in words, or None when
    ``tip`` is ``took`` or a descendant of it. The one judgment both checks make — the
    fold's before it merges (:func:`_carried_tips`) and the run's at its end
    (:func:`rewritten_carried`, #695). Decided by ancestry, never by message; a git
    failure raises (:func:`_in_line`)."""
    if tip == took or _in_line(repo, took, f"{name}'s carried head {took[:12]}", ref, of=tip):
        return None
    return (f"{name}'s branch {ref} was rewritten after this run folded it onto {line}: it "
            f"is now at {tip[:12]}, which does not contain {took[:12]}, the commit the line "
            f"took from it. A re-publish (`pdca publish` force-pushes), a force-push or "
            f"\"Update with rebase\" does this.")


def _base_rewrite(name: str, ref: str, took: str, tip: str, owner: str, branch: str) -> str:
    """The facts of bundle ``name``'s branch ``ref``, now at ``tip``, rewritten after
    ``owner``'s own base ``branch`` took ``took`` from it (#727): ``owner``'s PR, built on
    that base, holds commits the branch no longer has."""
    return (f"{name}'s branch {ref} was rewritten after {owner}'s own base {branch} took "
            f"{took[:12]} from it: it is now at {tip[:12]}, which does not contain "
            f"{took[:12]}. A re-publish (`pdca publish` force-pushes), a force-push or "
            f"\"Update with rebase\" does this. {owner} was built on {branch}, so {owner}'s "
            f"PR holds commits {ref} no longer has.")


def rewritten_carried(cfg: Config, bundles: Iterable[Path], carried: Mapping[str, str],
                      batch: Collection[str] | None = None, *, bases: Bases | None = None,
                      line_prs: LinePrs | None = None
                      ) -> tuple[dict[str, list[str]],
                                 dict[str, list[tuple[str, str, str, Taker]]]]:
    """The end-of-run half of the #633 check (#695), over each bundle of ``bundles`` whose
    published branch a fold of this run put on its line (``carried``, the record
    :func:`fold` filled). A fold checks the branches it carries only before it merges, and
    a fold runs only between waves, so without this a rewrite during the run's last fold
    (after its check read the tip) or after it (while the last wave builds, publishes, or
    Act runs) went unseen.

    Returns ``(rewritten, unchecked)``. ``rewritten`` is ``{name: reports}`` for each branch
    rewritten since, judged and worded as a fold judges it (:func:`_carried_rewrite`), with
    what to do now that no fold will. A branch that no longer resolves is left alone (in a
    fold, :func:`_gone_branch` judges it; at the end of the run nothing does).
    ``unchecked`` is ``{name: items}`` for each branch that could not be judged: every
    one of a target whose checkout is missing or whose fetch failed twice (:func:`_fetch`),
    or one whose ancestry check failed (:func:`_in_line`). ``why`` is the error's
    ``cause`` — what failed, without the step its message tells a fold to take: re-running
    is no remedy once the run has pushed its line. Each target is fetched and each branch
    judged on its own, so one that cannot be checked never hides a rewrite found on
    another, nor stops the others being checked. Never raises :class:`IntegrationError`.

    ``bases`` / ``line_prs`` (#727, as :func:`fold` takes them): each head is judged once; a
    lost one gets a report, one not judged an item ``(ref, why, head, who took it)``. The
    line's report names the PRs cut from it holding a head lost or not judged; a bundle's
    own base gets one of its own. A line-cut PR has no item: the flow names those."""
    takers = _takers(carried, bases, line_prs)
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}   # (name, remote, ref)
    for d, repo_spec, base, _slug, published in _fold_candidates(bundles):
        if d.name in carried and published is not None:
            remote, ref, _onto = published
            groups.setdefault((repo_spec, base), []).append((d.name, remote, ref))
    rewritten: dict[str, list[str]] = {}
    unchecked: dict[str, list[tuple[str, str, str, Taker]]] = {}
    for (repo_spec, base), mine in sorted(groups.items()):
        repo = publish._checkout_path(cfg, repo_spec)
        try:
            _fetch(repo, cfg.base_remote, [remote for _name, remote, _ref in mine], attempts=2)
        except IntegrationError as exc:
            for name, _remote, ref in mine:
                unchecked[name] = [(ref, exc.cause, h, by) for h, by in takers[name]
                                   if by is None or by[1] is not None]
            continue
        line = integration_branch(cfg, base, batch)
        for name, _remote, ref in mine:
            tip = _rev(repo, ref)
            if tip is None:
                continue
            judged: dict[str, str | IntegrationError | None] = {}   # head → its facts
            for h, _by in takers[name]:
                if h not in judged:
                    try:
                        judged[h] = _carried_rewrite(repo, name, ref, h, tip, line)
                    except IntegrationError as exc:
                        judged[h] = exc
            reports: list[str] = []
            for h, by in takers[name]:
                facts = judged[h]
                if facts is None or (by is not None and by[1] is None):
                    continue                    # held; or a line-cut PR, named with the line
                if isinstance(facts, IntegrationError):
                    unchecked.setdefault(name, []).append((ref, facts.cause, h, by))
                elif by is None:
                    reports.append(_late_line_rewrite(facts, ref, h, line, [
                        (b[0], h2) for h2, b in takers[name]
                        if b is not None and b[1] is None and judged[h2] is not None]))
                else:
                    reports.append(f"{_base_rewrite(name, ref, h, tip, *by)} No fold of this "
                                   f"run ran after the rewrite. Restore {ref} to {h[:12]} or a "
                                   f"commit that contains it; or {_rebuild([by[0]])}")
            if reports:
                rewritten[name] = reports
    return rewritten, unchecked


def _late_line_rewrite(facts: str, ref: str, took: str, line: str,
                       prs: Sequence[tuple[str, str]]) -> str:
    """The end-of-run report (#695) of a branch rewritten after the line took ``took`` from
    it (``facts``): the line still carries the old commits, and so does each PR of ``prs``
    (``(owner, head)``, in the order cut, #727), cut after it took the first one's head."""
    if not prs:
        return (f"{facts} No fold of this run ran after the rewrite, so {line} still carries "
                f"the old commits, which {ref} no longer has; no PR this run cut from {line} "
                f"holds them. Restore {ref} to {took[:12]} or a commit that contains it")
    return (f"{facts} No fold of this run ran after the rewrite, so {line} still carries the "
            f"old commits, and so does every PR this run cut from {line} after it took "
            f"{prs[0][1][:12]} ({', '.join(o for o, _h in prs)}): they hold commits {ref} no "
            f"longer has. Restore {ref} to {took[:12]} or a commit that contains it; or "
            f"rebuild those PRs on the rewritten branch before any of them merges")


def _merge_published(cfg: Config, wt: Path, d: Path, published: tuple[str, str, bool],
                     head: str | None, *, branch: str, base: str,
                     owners: Sequence[Path] = ()) -> None:
    """Merge bundle ``d``'s published branch onto the line checked out in ``wt`` (#593):
    its own commits, joined by a signed-off merge commit — not a re-applied copy. ``head``
    is the branch's tip as :func:`fold` read it — None: it does not resolve (gone). It is
    the CURRENT tip, so commits pushed onto the branch after an earlier fold carried it
    reach the line too; for a branch the run carried, the tip checked to extend what the
    line took from it (:func:`_carried_tips`, #633). ``owners``: the bundles a refusal may
    name — to take the base in (:func:`_take_in_base`), or to keep this merge.

    A merge that would remove work from the line, by a change none of the branch's own
    commits makes (#673: the branch was cut from, or updated from, a base that reverted
    work the line carries), is undone and raises (:func:`_keep_merge`)."""
    _remote, ref, onto = published
    if head is None:
        _gone_branch(cfg, wt, d, ref, onto, branch=branch, base=base, owners=owners)
        return
    if _in_line(wt, head, f"{d.name}'s branch {ref}", branch):
        return  # already in the line (an earlier fold carried it) — never merged twice
    before = _rev(wt, "HEAD")
    if before is None:
        raise IntegrationTreeError(f"could not read the tip of {branch} in {wt} — check that "
                                   f"checkout, then re-run")
    rc, conflicts = _merge(wt, head, f"pdca-integrate: {d.name}")
    if rc == 0:
        _keep_merge(cfg, wt, d, ref, head, before, branch=branch,
                    base_ref=f"{cfg.base_remote}/{base}", owners=owners)
        return
    if conflicts:
        raise _Conflict(
            f"{d.name}'s branch {ref} does not merge cleanly onto {branch} (conflicts in "
            f"{', '.join(conflicts)}) — an undeclared cross-wave overlap; declare the "
            f"conflict / re-order, then re-run", conflicts)
    raise IntegrationError(
        f"could not merge {d.name}'s branch {ref} onto {branch} — `git merge` exited {rc} "
        f"with no conflicting path in {wt}, so a git step failed (not an overlap): check "
        f"that checkout and the branch, then re-run")


def _keep_merge(cfg: Config, wt: Path, d: Path, ref: str, head: str, before: str, *,
                branch: str, base_ref: str, owners: Sequence[Path]) -> None:
    """``d``'s branch ``ref`` at ``head`` just merged cleanly onto the line in ``wt``, whose
    tip was ``before`` (#673). Keep that merge unless it removes work from the line, judged
    as :func:`_take_in_base` judges the base merge (#623, :func:`_undone_paths`): a path
    the line changed since its cut point is back to its content there. A removal the
    branch's own commits make is the bundle's change and never counts
    (:func:`_own_removals`). Any other removal came in with the branch from a base that
    reverted the work (the branch was cut from it, or updated from it): the merge is undone
    (the line reset to ``before``) and this raises, naming each path, the bundles of
    ``owners`` whose work it is, and ``d`` (worded by :func:`_branch_removed`). A plain
    :class:`IntegrationError`, so under the carry's ``skipped`` only ``d`` is left out; a
    line that will not go back raises :class:`_LineBroken`, which stops even the carry."""
    merge = f"{d.name}'s branch {ref}"
    try:
        undone = _undone_paths(cfg, wt, before, base_ref, branch, owners, head=head)
    except IntegrationError:
        _reset_line(wt, before, branch, merge)
        raise
    if not undone:
        return
    _reset_line(wt, before, branch, merge)
    work, paths = _branch_removed(undone, branch, d.name)
    raise IntegrationError(
        f"merging {merge} would remove {work} from {branch}: {paths} would go back to how "
        f"{base_ref} had it before the line changed it, and that change is not one of "
        f"{d.name}'s own commits: its branch brings it in (cut from, or updated from, "
        f"{base_ref} after a revert there?). Not building the next wave on a line without "
        f"that work — put it back on {ref} (revert the revert there, or update it from "
        f"{base_ref} once {base_ref} has the work again), then re-run")


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
            _reset_line(wt, before, branch, base_ref)
            raise
        if undone:
            _reset_line(wt, before, branch, base_ref)
            work, paths = _removed(undone, branch)
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


def _removed(undone: Sequence[tuple[str, list[str], str, list[tuple[str, ...]]]],
             branch: str) -> tuple[str, str]:
    """How a refusal names what :func:`_undone_paths` found (#623, #673): ``(work, paths)``
    — whose work would go (``"issue_A's work"``; ``"folded work"`` when no bundle is known
    for it), and each path with the bundles that brought it, or else the line commit that
    last changed it. A bundle's branch merge words it as :func:`_branch_removed` does."""
    whose = sorted({n for _p, names, _note, _groups in undone for n in names})
    work = f"{', '.join(whose)}'s work" if whose else "folded work"
    paths = "; ".join(f"{p} ({', '.join(names) or f'{note} on {branch}'})"
                      for p, names, note, _groups in undone)
    return work, paths


def _branch_removed(undone: Sequence[tuple[str, list[str], str, list[tuple[str, ...]]]],
                    branch: str, incoming: str) -> tuple[str, str]:
    """``(work, paths)`` for the refusal to keep the merge of bundle ``incoming``'s branch
    (#673). While each commit that changed a path is the own commit of one bundle other
    than ``incoming``, it is worded as :func:`_removed` words it. Otherwise ``work`` is just
    "work" and each path says whose, never naming ``incoming`` as it names the others,
    which would read as if its merge removed its own work: a commit several bundles share
    (neither branch holds the other's tip) is work from one or the other, and git cannot
    tell which branch made it; a commit only ``incoming`` has is its earlier work, which
    its branch no longer has."""
    if all(len(g) == 1 and g[0] != incoming for _p, _n, _note, groups in undone
           for g in groups):
        return _removed(undone, branch)
    paths: list[str] = []
    for p, names, note, groups in undone:
        whose = [", ".join(n for n in names if n != incoming and (n,) in groups)]
        if (incoming,) in groups:
            whose.append(f"{incoming}'s earlier work, which its branch no longer has")
        whose += [f"work from {', '.join(g[:-1])} or {g[-1]} — git cannot tell which branch "
                  f"made it" for g in groups if len(g) > 1]
        paths.append(f"{p} ({', and '.join(w for w in whose if w) or f'{note} on {branch}'})")
    return "work", "; ".join(paths)


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


def _own_removals(wt: Path, head: str, before: str, skip: Collection[str],
                  paths: Sequence[str]) -> set[str]:
    """Which of ``paths`` the branch's OWN commits put back (#673): a removal that is the
    bundle's change. Each path is one the merge of branch tip ``head`` onto the line at
    ``before`` just put back to its content at the line's cut point (:func:`_undone_paths`).

    Judged by the commit that gave ``head`` its content at the path, as git's history
    simplification finds it (``git rev-list -n 1 <head> -- <path>``): through a merge that
    took the path from one parent, it follows that parent. The removal is the branch's own
    when that commit is one of the branch's own commits the merge brings
    (:func:`_own_commits` from ``head`` down to ``before``, which stops at the base's
    first-parent chain): a commit of the branch, or of a topic merged into it, that deletes
    the file or puts its old content back. It is not when that commit is the base's revert,
    which a branch cut from the base, or updated from it, carries: an "Update branch" merge
    takes the path from the base, so the walk goes past it to the revert. Nor is it when no
    commit of the branch changed the path. An earlier commit of the branch that changed the
    path counts for nothing when the content ``head`` has came from elsewhere."""
    own = _own_commits(wt, head, ("--not", before), skip,
                       what=f"the commits {head[:12]} brings onto the line")
    mine: set[str] = set()
    for p in paths:
        last = _out(wt, "rev-list", "-n", "1", head, "--", f":(literal){p}",
                    what=f"the commit that last changed {p} on {head[:12]}").strip()
        if last in own:
            mine.add(p)
    return mine


def _undone_paths(cfg: Config, wt: Path, before: str, base_ref: str, branch: str,
                  owners: Sequence[Path], *, head: str | None = None
                  ) -> list[tuple[str, list[str], str, list[tuple[str, ...]]]]:
    """``(path, bundles, note, groups)`` for each path the merge just made in ``wt`` undoes
    — of the base (#623), or of a bundle's branch at ``head`` (#673): one the line at
    ``before`` changed since its cut point (:func:`_cut_point`), which is back to its
    content there now (absent, where the cut point had none). An edit that leaves the path
    with NEW content removes nothing, and neither does a removal ``head``'s own commits
    make (:func:`_own_removals`): that is the bundle's change. ``bundles`` are the
    ``owners`` that brought the change: each one with a commit of its own
    (:func:`_own_commits`, from its published branch or, gone, the head its PR merged with)
    among the commits that changed the path on the line; ``note`` names the line commit
    that last changed it. A branch that holds another bundle's tip (cut from that bundle's
    branch, or updated from a base that merged it) holds that bundle's own commits, which
    are not its work: they are left out of its own, whatever path its walk reaches them by
    (#673). A commit two branches share while neither holds the other's tip counts for
    both: git cannot tell which branch it was made on, and naming both never leaves out the
    one whose work it is. ``groups`` keeps them apart for the bundle-branch refusal
    (:func:`_branch_removed`): for each of those commits a bundle has as its own, the
    bundles that have it — one, or the several that share it.

    Judged per path from the line's cut point, so it does not see a partial revert inside a
    file the line also changed in other ways, nor a revert of work below the cut point
    (already on the base): an accepted limit (#673)."""
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
    if head is not None:
        mine = _own_removals(wt, head, before, skip, undone)
        undone = [p for p in undone if p not in mine]
        if not undone:
            return []
    tips: dict[str, str] = {}                 # bundle name → its branch tip (or merged head)
    for o in owners:
        pub = _published_ref(o)
        if pub is None:
            continue
        tip = _rev(wt, pub[1]) or merged.merged_head(cfg, o.name.removeprefix("issue_"))
        if tip and _git(wt, "cat-file", "-e", f"{tip}^{{commit}}") == 0:
            tips[o.name] = tip
    walks = {name: _own_commits(wt, tip, since, skip, what=f"{name}'s commits")
             for name, tip in tips.items()}
    own: list[tuple[str, set[str]]] = []      # (bundle name, the commits it adds)
    for name, tip in tips.items():
        mine = walks[name]
        held = [walks[n] for n, t in tips.items() if t != tip and not walks[n].isdisjoint(mine)
                and _in_line(wt, t, f"{n}'s branch tip {t[:12]}", f"{name}'s branch", of=tip)]
        own.append((name, mine.difference(*held)))
    out: list[tuple[str, list[str], str, list[tuple[str, ...]]]] = []
    for path in undone:
        touched = _out(wt, "rev-list", before, *since, "--", f":(literal){path}",
                       what=f"the commits on {branch} that changed {path}").split()
        groups = [tuple(name for name, commits in own if c in commits) for c in touched]
        out.append((path, [name for name, commits in own if commits.intersection(touched)],
                    f"last changed by {touched[0][:12]}" if touched else "changed",
                    list(dict.fromkeys(g for g in groups if g))))
    return out


def _reset_line(wt: Path, tip: str, branch: str, merged_in: str) -> None:
    """Put the line in ``wt`` back at ``tip``, undoing the merge of ``merged_in`` — the base
    (#623) or a bundle's branch (#673) — that the fold refused to keep; one that will not
    go back stops the fold even under ``skipped`` (:class:`_LineBroken`) — never merged onto
    and pushed."""
    if _git(wt, "reset", "-q", "--hard", tip) != 0 or _rev(wt, "HEAD") != tip:
        raise _LineBroken(f"could not reset {branch} in {wt} back to {tip} to undo the merge "
                          f"of {merged_in} — check that checkout, then re-run")


def _in_line(wt: Path, rev: str, what: str, branch: str, *, of: str = "HEAD") -> bool:
    """Whether ``rev`` is an ancestor of ``of`` — by default the line checked out in ``wt``
    (``branch`` names where it looked, in an error). ``git merge-base --is-ancestor`` exits
    0 for yes, 1 for no; anything else (128) is git failing, so it raises — never read as
    "not in the line", or reported as an overlap."""
    rc = _git(wt, "merge-base", "--is-ancestor", rev, of)
    if rc in (0, 1):
        return rc == 0
    cause = f"`git merge-base --is-ancestor` exited {rc} in {wt}"
    raise IntegrationError(
        f"could not tell whether {what} is already in {branch} — {cause}; a git step failed "
        f"(not an overlap): check that checkout, then re-run", cause=cause)


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


def _merge_over(wt: Path, rev: str, message: str) -> None:
    """Merge ``rev`` onto the commit checked out in ``wt`` with ``rev``'s side winning
    every clash (#694, a trial only: :func:`_trial`'s ``wins``), so the trial always gets
    past it. Each hunk that clashes takes ``rev``'s side (``-X theirs``). A path the merge
    still leaves conflicting, which no hunk can settle (a file one side deleted, or that
    both renamed), is settled the same way: ``rev``'s version of it (git's stage 3), or
    none where ``rev`` has none. The result is committed (no commit hooks: a trial commit
    is never pushed) and the tree left clean on it. A merge that fails with no conflicting
    path, or a step settling one that fails, is a failed git step: the tree goes back to
    where it was, and this raises."""
    before = _rev(wt, "HEAD")
    if before is None:
        raise IntegrationTreeError(f"could not read the commit checked out in {wt} — a git "
                                   f"step failed: check that checkout, then re-run")
    rc = _git(wt, "merge", "--no-ff", "--no-edit", "--signoff", "-X", "theirs", "-m",
              message, rev)
    if rc == 0:
        return
    r = subprocess.run(["git", "-C", str(wt), "ls-files", "-u", "-z"],
                       capture_output=True, text=True)
    wins: dict[str, str] = {}   # each path left conflicting → its index lines: rev's, or gone
    for meta, path in (row.split("\t", 1) for row in r.stdout.split("\0") if "\t" in row):
        mode, sha, stage = meta.split()
        wins.setdefault(path, f"0 {'0' * len(sha)}\t{path}\0")    # drop every stage of it
        if stage == "3":
            wins[path] += f"{mode} {sha}\t{path}\0"                # then put rev's back
    settled = (r.returncode == 0 and bool(wins)
               and subprocess.run(["git", "-C", str(wt), "update-index", "-z", "--index-info"],
                                  input="".join(wins.values()), capture_output=True,
                                  text=True).returncode == 0
               and _git(wt, "commit", "-q", "--no-verify", "-m", message) == 0)
    if not settled:
        _reset_tree(wt, before)
        raise IntegrationError(
            f"could not try merging {rev} in {wt} with its side winning — `git merge` exited "
            f"{rc}{', and settling the paths it left conflicting failed' if wins else ''}, "
            f"so a git step failed: check that checkout, then re-run")
    _reset_tree(wt, "HEAD")    # the tree as committed: no copy of a path settled away


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


def _held_by_line(cfg: Config, wt: Path, branch: str, d: Path, head: str, base_ref: str,
                  paths: Sequence[str], took: Sequence[tuple[str, str]],
                  finished: Mapping[str, str], tips: Mapping[str, str]
                  ) -> tuple[list[str], list[str]] | None:
    """Whether bundle ``d``'s branch tip ``head``, which just failed to merge onto the line
    checked out in ``wt`` (``paths``: the files it left conflicting) and is not held for a
    same-wave conflict (:func:`_held_by`), is held for a conflict that bundles already on
    the line explain (#747), and if so ``(partners, paths)``, as :func:`_held_by` gives
    them. The bundles on the line: each finished id of ``finished`` (name → the head the
    pre-wave carry put on the line) whose head the line has, then ``took`` (``(name,
    head)``: the ones the fold took before ``head``, in merge order). ``tips``: the head of
    each bundle the fold has read, by name (:func:`_built_on`).

    Decided by trial merges, never by which branch touched a path. Held exactly when both
    of these hold; None otherwise, and the fold stops on it as before:

    * :func:`_clash` says ``"batch"``: ``head`` merges cleanly onto ``base_ref`` alone, and
      ``partners``, the bundles on the line whose own change it conflicts with
      (:func:`_partners`), are not empty. So each partner's own head merges cleanly onto
      ``base_ref`` alone: the trial that names it merges that head there first.
    * The partners explain the whole clash: ``head``, merged last, merges cleanly onto
      ``base_ref`` plus the line without them (every other bundle on it, in merge order).
      A partner ``d``'s brief says it builds on (:func:`_prerequisites`) is not taken off:
      ``d`` is to be rebuilt on it, so it stays on the line, and what is merged last is
      ``head`` as rebuilt on those partners — merged onto ``base_ref`` after them, its side
      winning every clash with them, as :func:`_partners` tries a bundle on what it was
      built on. Last, never first: a clash the rest of the line makes only together (a
      rename and an edit of the file it renames, say) shows only in that order. A bundle
      left on the line, such a partner too, built on a partner taken off (what
      :func:`_built_on` says it was built on holds that partner's head) would bring that
      partner back, so the clash is not explained: the fold stops.

    A git step that fails in a trial gives None; a tree that will not go back raises. The
    trials never move the line (:func:`_trial`)."""
    try:
        taken = dict(took)
        on = [*((n, h) for n, h in finished.items()
                if n not in taken and _carries(wt, h, f"{n}'s head {h}", branch)), *took]
        under = _built_on(wt, base_ref, {**dict(on), **tips, d.name: head},
                          [n for n, _h in on])
        kind, partners, _files = _clash(wt, branch, head, base_ref, paths, on, (), under)
        if kind != "batch":
            return None
        own = _prerequisites(cfg, d)
        off = [(n, h) for n, h in on if n in partners and n not in own]   # taken off the line
        left = [(n, h) for n, h in on if n not in partners or n in own]
        if any(_in_line(wt, ph, f"{p}'s head", f"what {n} was built on", of=u)
               for n, _h in left for u in under.get(n, ()) for p, ph in off):
            return None     # a bundle left on the line brings a partner back
        last = head
        pre = [h for n, h in left if n in partners]     # its prerequisites: rebuilt on them
        if pre:
            made: list[str] = []
            if _trial(wt, branch, base_ref, [*pre, head], wins=len(pre),
                      made=made)[0] is not None:
                return None
            last = made[0]
        if _trial(wt, branch, base_ref, [*(h for _n, h in left), last])[0] is not None:
            return None     # the partners alone do not explain the clash
    except (IntegrationTreeError, _LineBroken):
        raise
    except IntegrationError:
        return None
    return list(partners), list(paths)


def _prerequisites(cfg: Config, d: Path) -> set[str]:
    """The bundles that bundle ``d``'s brief says it builds on (#747), by name: each one
    its ``Depends on``, ``Depends on (merged)`` or ``Stacks on`` names
    (:func:`waves.declared_deps`), found as the flow finds a prerequisite, an archived one
    too (``find_bundle``). None for a bundle with no brief."""
    bp = d / "brief.md"
    return ({cfg.find_bundle(i).name for i in waves.declared_deps(bp)} if bp.exists()
            else set())


def _clash(wt: Path, branch: str, head: str, base_ref: str, paths: Sequence[str],
           on: Sequence[tuple[str, str]], off: Sequence[tuple[str, str]],
           under: Mapping[str, Sequence[str]]) -> tuple[str, dict[str, list[str]], list[str]]:
    """What branch tip ``head`` conflicts with (#694, the flow's pre-wave carry): the fold
    skipped it for a textual conflict (``paths``: the files its merge left conflicting),
    and the line is checked out in ``wt`` as the fold pushes it, every pass over.
    ``(kind, partners, files)``, so the carry can name it as the run names a same-wave
    conflict hold (#674), never with a bundle that line does not carry:

    * ``"base"`` — ``head`` does not merge cleanly onto ``base_ref`` alone: the clash is
      with the base, so no bundle is named. ``files``: that trial's.
    * ``"batch"`` — else, the bundles of ``on`` whose own change ``head`` conflicts with
      (:func:`_partners`), name → the files. ``on``: the bundles the fold carries, each
      ``(name, head merged)`` in merge order — merged by it, before ``head`` was tried or
      after, or on the line already.
    * ``"line"`` — else, when ``head`` does not merge cleanly onto the line: the clash is
      with other work on it. No bundle is named. ``files``: that trial's.
    * ``"off"`` — else ``head`` merges onto the line, and ``off`` names the bundles that
      were on the line when it was tried and that a later pass took back off (#672): its
      merge failed on work they brought. ``partners``: the ones whose own change ``head``
      conflicts with — maybe none. ``files``: ``paths``.
    * ``"moved"`` — else (none of those was taken off): a merge after it changed the line
      so that ``head`` merges onto it. No bundle is named. ``files``: ``paths``.

    ``under``: what each bundle was built on beyond the base (:func:`_built_on`). A trial
    git step that fails (a merge with no conflicting path, or one :func:`_merge_over`
    cannot settle) gives ``"line"`` with ``paths``, never a stop. The trials never move the
    line (:func:`_trial`); a tree that will not go back raises."""
    try:
        at, found = _trial(wt, branch, base_ref, [head])
        if at is not None:
            return "base", {}, found
        partners = _partners(wt, branch, head, base_ref, on, under)
        if partners:
            return "batch", partners, list(paths)
        at, found = _trial(wt, branch, "HEAD", [head])
        if at is not None:
            return "line", {}, found
        if not off:
            return "moved", {}, list(paths)
        return "off", _partners(wt, branch, head, base_ref, off, under), list(paths)
    except (IntegrationTreeError, _LineBroken):
        raise
    except IntegrationError:
        return "line", {}, list(paths)


def _partners(wt: Path, branch: str, head: str, base_ref: str,
              bundles: Sequence[tuple[str, str]], under: Mapping[str, Sequence[str]]
              ) -> dict[str, list[str]]:
    """The bundles of ``bundles`` (``(name, head)``) whose own change branch tip ``head``
    conflicts with (#694), each with the files: ``head`` does not merge cleanly onto
    ``base_ref`` plus its head. One built on more than the base (``under``, :func:`_built_on`:
    cut from the line, or from another bundle's branch) is named only for its own change:
    it is tried on ``base_ref`` plus what it was built on, then ``head`` with its side
    winning every clash with that (:func:`_merge_over`: each hunk, and each file one side
    deleted, say), then its own head, and named when that last merge conflicts, with that
    merge's files. So what it was built on never decides it, either way: a clash ``head``
    has only with that is not its own, and one it has with its own change is found however
    ``head`` clashes with that. What it was built on not merging onto ``base_ref`` proves
    nothing against it: it is not named. Raises as :func:`_trial` does."""
    named: dict[str, list[str]] = {}
    for name, its in bundles:
        at, found = _trial(wt, branch, base_ref, [its, head])
        if at != 1:
            continue
        below = list(under.get(name, ()))
        if below:
            at, found = _trial(wt, branch, base_ref, [*below, head, its], wins=len(below))
            if at != len(below) + 1:
                continue    # its own change merges with head's (or what it was built on clashes)
        named[name] = found
    return named


def _built_on(wt: Path, base_ref: str, tips: Mapping[str, str],
              names: Iterable[str]) -> dict[str, list[str]]:
    """What each bundle of ``names`` was built on that ``base_ref`` does not have (#694):
    the parents of its own commits that are neither its own nor on ``base_ref`` — the line
    commit it was cut from, or the other bundle's commit it was cut from. None for one cut
    from the base, or updated from it. ``tips``: every bundle of the fold with a head in the
    clone, name → that head; ``names`` are some of them (the ones a partner is looked for
    among).

    Its own commits are those its head reaches without passing through the first-parent
    chain of the line checked out in ``wt``, or of ``base_ref``, as #673 judges a branch's
    own commits (:func:`_own_commits`), less the own commits of each other bundle of
    ``tips`` whose head it holds. So one cut from the line, or from another bundle's
    branch, does not own what it was cut from, and one whose branch moved on since owns its
    commits, old and new. A commit two of them share while neither holds the other's head
    counts for both: git cannot tell which branch made it. A git failure raises."""
    cut, chain = _cut_point(wt, "HEAD", base_ref)
    since = ("--not", cut) if cut else ()
    skip = {*chain, *_out(wt, "rev-list", "--first-parent", base_ref, *since,
                          what=f"the first-parent chain of {base_ref}").split()}
    walks = {name: _own_commits(wt, tip, since, skip, what=f"{name}'s commits")
             for name, tip in tips.items()}
    out: dict[str, list[str]] = {}
    for name in dict.fromkeys(n for n in names if n in tips):
        tip = tips[name]
        mine = walks[name].difference(*(
            walks[n] for n, t in tips.items()
            if t != tip and _in_line(wt, t, f"{n}'s head", f"{name}'s head", of=tip)))
        rows = (_out(wt, "rev-list", "--parents", "--no-walk", *sorted(mine),
                     what=f"the parents of {name}'s commits").splitlines() if mine else [])
        out[name] = [p for p in dict.fromkeys(p for row in rows for p in row.split()[1:])
                     if p not in mine and not _in_line(wt, p, p[:12], base_ref, of=base_ref)]
    return out


def _trial(wt: Path, branch: str, onto: str, revs: Sequence[str], *,
           wins: int | None = None, made: list[str] | None = None
           ) -> tuple[int | None, list[str]]:
    """Merge ``revs`` one after another onto commit ``onto`` in ``wt``, as a trial (#674):
    ``(i, paths)`` for the first that does not merge cleanly — its index and the files it
    left conflicting — or ``(None, [])`` when all do. It runs on a detached HEAD, so
    ``branch`` never moves, and ends with the tree back on ``branch`` at the tip it had,
    clean (:func:`_reset_tree`): one that will not go back raises :class:`_LineBroken`. A
    merge that fails with no conflicting path is a failed git step and raises. The caller
    holds the worktree's lock (:func:`fold`). ``wins`` (#694): the index of the rev merged
    with its side winning every clash with what came before it (:func:`_merge_over`), so it
    never stops the trial. ``made`` (#747): when all merge cleanly, gets the commit the
    trial ended on — on no branch, for a later trial to merge."""
    tip = _rev(wt, "HEAD")
    if tip is None:
        raise IntegrationTreeError(f"could not read the tip of {branch} in {wt} — check that "
                                   f"checkout, then re-run")
    try:
        if _git(wt, "checkout", "-q", "--detach", onto) != 0:
            raise IntegrationTreeError(f"could not check out {onto} in {wt} to try a merge "
                                       f"— check that checkout, then re-run")
        for i, rev in enumerate(revs):
            message = f"pdca-integrate: trial merge of {rev}"
            if i == wins:
                _merge_over(wt, rev, message)
                continue
            rc, conflicts = _merge(wt, rev, message)
            if rc != 0 and not conflicts:
                raise IntegrationError(
                    f"could not try merging {rev} in {wt} — `git merge` exited {rc} with no "
                    f"conflicting path, so a git step failed: check that checkout, then re-run")
            if rc != 0:
                return i, conflicts
        if made is not None:
            made.append(_out(wt, "rev-parse", "--verify", "HEAD",
                             what="the commit a trial merge made").strip())
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


#: Seconds :func:`_fetch` waits before it tries a failed fetch again (``attempts``, #695).
_FETCH_RETRY_PAUSE = 1.0


def _fetch(repo: Path, base_remote: str, remotes: Iterable[str] = (), *,
           attempts: int = 1) -> None:
    """Fetch what a fold reads into ``repo``: ``origin`` (the line and the PR branches),
    every remote a ``"stacked"`` record's branch lives on (#593), and the base remote. The
    remotes holding PR branches are fetched with ``--prune``: a published branch deleted on
    its remote must stop resolving here, and a stale remote-tracking ref would hide that.
    The base remote is fetched LAST: a merged PR's branch is deleted after its merge, so
    when these fetches show a branch as gone, the base snapshot already has that merge — the
    check that the base has the merged head (:func:`_gone_branch`) never runs on a snapshot
    taken too early. A missing checkout, or a fetch that fails, is a failed git step (the
    run stops). ``attempts`` (#695): how many times each fetch is tried, with a short pause
    between, before it counts as failed — 2 for the carried-branch checks, which fetch
    while no integration lock is held (:func:`_carried_tips` before a fold takes any,
    :func:`rewritten_carried` at the end of the run), where a concurrent git process
    holding a ref lock in the same checkout can fail it once; 1 (no retry) everywhere
    else."""
    if not (repo / ".git").exists():
        raise IntegrationError(f"checkout not found at {repo}")
    branch_remotes = list(dict.fromkeys(("origin", *remotes)))
    for remote in [r for r in branch_remotes if r != base_remote] + [base_remote]:
        prune = ("--prune",) if remote in branch_remotes else ()
        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(_FETCH_RETRY_PAUSE)
            if _git(repo, "fetch", *prune, remote) == 0:
                break
        else:
            cause = f"could not fetch {remote} in {repo}"
            raise IntegrationError(f"{cause} — the fold cannot see the branches it merges",
                                   cause=cause)


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
