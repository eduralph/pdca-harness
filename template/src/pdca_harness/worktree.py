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

import itertools
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import lane
from .config import Config


class WorktreeError(Exception):
    """Isolation was requested against a real git checkout but its base can't be
    materialized (issue #235). Raised instead of silently falling back to in-place, which
    would run Do/Check in the operator's primary checkout and violate never-mutate. The
    driver aborts the beat; the operator fixes the brief's base (or fetches it) and retries.
    """


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
    # ``-x`` also removes IGNORED files, so a prior bundle's ignored build outputs (dist/,
    # caches, generated assets) can't linger and contaminate this bundle's gate (issue #237).
    if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-fdxq") != 0:
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


def stage(d: Path, cfg: Config) -> Path | None:
    """Materialize bundle ``d``'s patched tree from its ``patch.diff`` — for ``pdca try``.

    Unlike :func:`path` (read-only; hands back a tree only if Do already left it there),
    ``stage`` RECONSTRUCTS the tree on demand: create the per-lane worktree off the base if
    it doesn't exist, reset + clean an existing one, apply THIS bundle's ``patch.diff``, and
    take ownership. So a human reviewing a *batch* can ``pdca try <id>`` any built/parked
    bundle in turn — not just the last one Do populated in the shared, reset-reused per-cycle
    worktree. Deterministic: ``patch.diff`` is Do's canonical output. Best-effort — isolation
    off / unresolved target / non-git checkout / a ``patch.diff`` that no longer applies onto
    the base returns None (``pdca try`` then reports no launchable tree). Mirrors the gate's
    :func:`resync` reconstruction (issue #224), extended to create the tree when it is absent.

    NB: like Do's :func:`ensure`, it hard-resets the shared per-lane worktree — so it must not
    run while a lane's Do is mid-build on the same tree (``pdca try`` is a between-cycles,
    human-paced action; the normal batch-then-review flow has all Do already done).
    """
    if not cfg.worktree:
        return None
    tgt = _target(d, cfg)
    if tgt is None:
        return None
    primary, base_ref = tgt
    wt = _wt_dir(primary)
    if not (wt / ".git").exists():
        _git(primary, "fetch", cfg.base_remote)  # best-effort refresh of the base
        if base_ref.startswith("origin/") and cfg.base_remote != "origin":
            _git(primary, "fetch", "origin")  # a stacked base lives on origin (#123)
        if _git(primary, "worktree", "add", "--force", str(wt), base_ref) != 0:
            print(f"worktree: could not create {wt} off {base_ref} for {d.name}; nothing to try",
                  file=sys.stderr)
            return None
    # Reconstruct THIS bundle's build: clean base, then its patch. ``-x`` also removes IGNORED
    # files, so another bundle's ignored build outputs (dist/, caches, generated assets) can't
    # survive and get launched alongside this bundle's source patch — which would defeat the
    # owner check and let a reviewer sign off the wrong build (issue #237).
    if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-fdxq") != 0:
        print(f"worktree: could not reset {wt} to {base_ref} for {d.name}; nothing to try",
              file=sys.stderr)
        return None
    patch = d / "patch.diff"
    if patch.is_file() and patch.read_text(encoding="utf-8").strip():
        if _git(wt, "apply", str(patch.resolve())) != 0:
            print(f"worktree: {d.name}'s patch.diff did not apply onto {base_ref} in {wt}; "
                  "nothing to try", file=sys.stderr)
            _owner_file(wt).unlink(missing_ok=True)
            return None
    _stamp_owner(wt, d)
    return wt


def ensure(d: Path, cfg: Config) -> Path | None:
    """Create or reset the per-cycle worktree off the target base; return its path.

    Reset-and-reused: an existing worktree is hard-reset to the base and cleaned; a new
    one is added off the base. Best-effort for the cases where isolation legitimately can't
    apply — disabled, unresolved / non-git target — which return None (the cycle then runs
    in place, unchanged). But a real git checkout whose **base ref can't be resolved** is
    NOT one of those: it raises :class:`WorktreeError` rather than fall back to in-place
    (issue #235), because running Do/Check in the operator's primary checkout would violate
    never-mutate. The primary checkout is never modified (worktrees are separate trees).
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
        # Fail closed (#235): the target IS a git checkout, so if its intended base doesn't
        # resolve (a mis-parsed / nonexistent base), refuse — never silently run in place and
        # mutate the primary. A resolvable base that then fails to check out is a rarer infra
        # hiccup left as best-effort below.
        if _git(primary, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}") != 0:
            raise WorktreeError(
                f"{d.name}: base ref '{base_ref}' does not resolve in {primary}. Worktree "
                "isolation refuses to run Do/Check in the operator's primary checkout "
                "(never-mutate). Fix the brief's 'Repo + branch target' base (or fetch it), "
                "then retry.")
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
    except WorktreeError:
        raise  # fail closed (#235) — do NOT degrade an unresolvable base to in-place
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


# ----------------------------------------------------------------------------
# Overflow worktrees (issue #226) — ephemeral spillover for a gate read whose cached
# lane is owned by a DIFFERENT bundle. Rather than mutate that lane (the resync heal),
# hand the read its OWN throwaway tree, off the base + this bundle's patch, removed after.
# Config-gated by ``[driver].overflow`` (0 ⇒ disabled, heal in place as before).
# ----------------------------------------------------------------------------
_OVF_SUFFIX = ".pdca-wt-ovf-"
_ovf_seq = itertools.count()
_ovf_seq_lock = threading.Lock()   # guards the token counter (name uniqueness)
# Serializes the overflow cap RESERVATION — the count-check and the create must be atomic so
# two concurrent Check threads can't both slip past the cap before either dir exists (#241).
# A separate lock from the counter's so `_overflow_create` → `_overflow_path` never re-enters.
_ovf_cap_lock = threading.Lock()


def _overflow_dirs(primary: Path) -> list[Path]:
    """Existing overflow trees for ``primary`` (sibling DIRS ``<name>.pdca-wt-ovf-*``)."""
    return sorted(p for p in primary.parent.glob(primary.name + _OVF_SUFFIX + "*")
                  if p.is_dir())


def _overflow_path(primary: Path) -> Path:
    """A fresh, unique overflow path — pid + a process-local counter, so concurrent lanes
    (threads) never collide and a name is never reused within a run."""
    with _ovf_seq_lock:
        token = f"{os.getpid()}-{next(_ovf_seq)}"
    return primary.parent / (primary.name + _OVF_SUFFIX + token)


def overflow_remove(primary: Path, ovf: Path) -> None:
    """Tear down one overflow tree — ``git worktree remove`` then a hard rmtree + prune
    fallback. Best-effort: teardown must never fail a gate run (issue #226)."""
    if _git(primary, "worktree", "remove", "--force", str(ovf)) != 0:
        shutil.rmtree(ovf, ignore_errors=True)
        _git(primary, "worktree", "prune")
    _owner_file(ovf).unlink(missing_ok=True)  # drop any owner sidecar too


def sweep_overflow(primary: Path) -> None:
    """Reclaim crash-orphaned overflow trees for ``primary``: prune git's admin entries,
    then rmtree any leftover ``*-ovf-*`` dirs. Best-effort; safe to call before a run."""
    _git(primary, "worktree", "prune")
    for d in _overflow_dirs(primary):
        overflow_remove(primary, d)


def _overflow_create(d: Path, primary: Path, base_ref: str) -> Path | None:
    """Add a fresh overflow worktree off ``base_ref`` and apply this bundle's patch — the
    exceptional-read equivalent of :func:`resync`, but on its own tree (never a shared lane).
    Best-effort: a git-add / patch-apply failure returns None (the caller falls back to the
    in-place heal) and cleans up any partial tree. No fetch — the lane's Do already refreshed
    the base."""
    ovf = _overflow_path(primary)
    try:
        _git(primary, "worktree", "prune")  # drop admin entries for any vanished trees first
        if _git(primary, "worktree", "add", "--force", str(ovf), base_ref) != 0:
            return None
        patch = d / "patch.diff"
        if patch.is_file() and patch.read_text(encoding="utf-8").strip():
            if _git(ovf, "apply", str(patch.resolve())) != 0:
                overflow_remove(primary, ovf)  # patch didn't apply → not this bundle's build
                return None
        return ovf
    except Exception:  # noqa: BLE001 — overflow is best-effort; fall back to the heal
        overflow_remove(primary, ovf)
        return None


def for_gate(d: Path, cfg: Config) -> tuple[Path | None, Path | None]:
    """Resolve the worktree a gate should read for bundle ``d`` (issue #226).

    Returns ``(worktree_path, overflow_primary)``:
    * ``worktree_path`` is the tree to run the gate in (or None → run in the primary
      checkout, as when isolation is off — same fallback :func:`resync` uses); and
    * ``overflow_primary`` is non-None ONLY when ``worktree_path`` is an ephemeral overflow
      tree the caller MUST tear down (``overflow_remove(overflow_primary, worktree_path)``)
      once the gate has run.

    The cached lane (owned by this bundle — the normal Do→Check path) is returned warm and
    untouched. A lane owned by a DIFFERENT bundle is the exceptional read: with overflow
    enabled and under the cap it gets its own throwaway tree; otherwise it falls back to the
    in-place :func:`resync` heal (``overflow_primary`` None)."""
    if not cfg.worktree:
        return None, None
    tgt = _target(d, cfg)
    if tgt is None:
        return None, None
    primary, base_ref = tgt
    wt = _wt_dir(primary)
    if not (wt / ".git").exists():
        return resync(d, cfg), None  # no cached lane yet → resync's own fallback (None here)
    if owner_of(wt) == d.name:
        return wt, None  # cached lane, warm — leave Do's tree intact
    # Foreign / unstamped lane: prefer an overflow tree over mutating a lane another bundle
    # may still want — but only up to the configured cap; else heal the lane in place. The
    # count-check and the create are held under one lock so concurrent Check threads can't
    # both pass the cap before either dir exists (#241); the gate itself runs after this
    # returns, so gates still execute concurrently — only the brief reservation serializes.
    if cfg.overflow > 0:
        with _ovf_cap_lock:
            ovf = (_overflow_create(d, primary, base_ref)
                   if len(_overflow_dirs(primary)) < cfg.overflow else None)
        if ovf is not None:
            print(f"worktree: {d.name} gate read spilled to an overflow tree {ovf.name} "
                  f"(lane owned by {owner_of(wt)})", file=sys.stderr)
            return ovf, primary
    return resync(d, cfg), None
