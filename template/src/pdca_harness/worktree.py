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

The lane is a warm *checkout* cache, never a trusted *content* cache: a gating read
(:func:`for_gate`) always reconstructs ``base + patch.diff`` rather than trust what Do
left behind, and fails closed on any mismatch (issue #296).
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import lane
from .config import Config

# Cross-platform advisory file lock (#296 review round 2, same rationale as act.py):
# ``fcntl`` is Unix-only and cli.py imports this module at load time, so a hard import
# would break every installed command on Windows (scripts/install.ps1 is a supported
# bootstrap). ``msvcrt.locking`` is the Windows equivalent; LK_NBLCK raises at once
# when contended, matching flock's LOCK_NB.
if os.name == "nt":  # pragma: no cover — exercised only on Windows
    import msvcrt

    def _lock_file(fh, *, wait: bool) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh, *, wait: bool) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))

    def _unlock_file(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


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


# The harness-owned sibling-dir suffix for lane worktrees; single-sourced so the
# footprint sweeper (issue #297) globs exactly what this module creates.
WT_SUFFIX = ".pdca-wt"


def _wt_dir(primary: Path) -> Path:
    """The worktree directory for the current lane slot — a sibling of the primary
    checkout (``<name>.pdca-wt`` / ``<name>.pdca-wt-l<lane>`` under concurrency)."""
    slot = lane.current()
    suffix = WT_SUFFIX + (f"-l{slot}" if slot is not None else "")
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


# A patch that changes a submodule gitlink (mode 160000). Plain `git apply` exits 0
# for these while leaving the submodule checkout untouched, so a reconstruction that
# "succeeded" would still not be base + patch.diff — the exact lie #296 forbids.
# Keyed on the DIFF HEADERS that declare the 160000 mode (index/old mode/new mode/new
# file mode/deleted file mode lines), never on hunk text alone (#296 review round 2):
# an ordinary text file may legitimately contain a `Subproject commit …` line, and
# misclassifying it would fail-close a patch `git apply` materializes correctly.
_GITLINK_RE = re.compile(
    r"^(?:index [0-9a-f]+\.\.[0-9a-f]+|old mode|new mode|new file mode|deleted file mode)"
    r" 160000$",
    re.MULTILINE)


@contextlib.contextmanager
def lane_lock(d: Path, cfg: Config, *, wait: bool):
    """Advisory per-lane exclusive lock (#296 review) — the lane lifecycle guard.

    The owner stamp names which bundle's Do last populated a lane; it cannot say
    whether that Do (or a gate) is STILL RUNNING there. Without a lifecycle guard, an
    out-of-band gate read (``pdca gates <id>``) overlapping an in-flight Do for the
    same bundle would reconstruct the lane under the builder — destroying its
    uncommitted work — and two concurrent gate reads could clean each other's outputs
    mid-run. Both the Do band (ensure → builder invocation) and the gate read
    (reconstruction → gate commands) therefore hold this ``flock`` on a per-lane
    ``.lock`` sidecar for their whole critical section.

    ``wait=True`` blocks (Do waits out a transient gate read); ``wait=False`` raises
    :class:`WorktreeError` when the lane is busy — the gate then fails CLOSED with a
    reason instead of clobbering a live tree. No-op (yields) where isolation doesn't
    apply. flock serializes across processes AND across same-process file handles.
    """
    if not cfg.worktree:
        yield
        return
    tgt = _target(d, cfg)
    if tgt is None:
        yield
        return
    wt = _wt_dir(tgt[0])
    try:
        # The open itself can fail (read-only parent, fd exhaustion) — that too must
        # surface as WorktreeError, not a raw OSError: gates only catch WorktreeError,
        # and a raw escape would abort the run instead of the fail-closed red
        # (#296 review round 3).
        fh = wt.with_name(wt.name + ".lock").open("w")
    except OSError as exc:
        raise WorktreeError(
            f"{d.name}: cannot create the lane lock next to {wt.name} ({exc}) — "
            "failing closed; fix the checkout's parent directory, then retry.") from exc
    try:
        _lock_file(fh, wait=wait)
    except OSError as exc:
        fh.close()
        raise WorktreeError(
            f"{d.name}: lane {wt.name} is busy (another Do or gate run holds it) — "
            "refusing to reconstruct under a live run; retry when it finishes.") from exc
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            _unlock_file(fh)
        fh.close()


def rebuild_for_gate(d: Path, cfg: Config) -> Path | None:
    """Reconstruct the lane worktree as ``base + patch.diff`` — the tree a gate may trust.

    The per-lane worktree is reset-and-reused, historically swept only before each Do
    (:func:`ensure`) and trusted by content between Do and Check. Issue #296 showed why
    that trust is misplaced: an interrupted re-populate (host standby mid-Do) left the
    PREVIOUS iteration's tree in the lane with a correct-looking owner stamp — the stamp
    names the bundle, and iterate-do reuses the same bundle dir — and a *gating* C4 green
    then attested code that was not the patch under review. There is also no robust
    equality predicate to *verify* the tree against ``patch.diff`` (the builder authors
    the diff directly; context width / hunk order / whitespace legitimately differ from
    ``git diff`` output). So the gate read does not verify — it RECONSTRUCTS: reset to
    the base, clean (``-x`` — ignored build outputs too, issue #237), re-apply THIS
    bundle's ``patch.diff``, re-stamp. By construction a green then attests exactly
    ``base + patch.diff`` — the same artifact publish ships (issue #224 subsumed).

    Fail CLOSED (issue #235 precedent): once the target is a real git checkout, any step
    that fails — reset, clean, a ``patch.diff`` that no longer applies — raises
    :class:`WorktreeError` (stamp cleared first) rather than degrade to running the gate
    in the primary checkout or over a mismatched tree; a red with a reason always beats
    a green for the wrong content. Returns ``None`` only where isolation legitimately
    doesn't apply (disabled / unresolved target / non-git checkout — unchanged
    best-effort contract). A missing lane is created off the base (crash/standby
    self-heal: the next gate run rebuilds from scratch). No fetch on the warm path —
    the base ref must stay the one Do built against.
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
            raise WorktreeError(
                f"{d.name}: could not create worktree {wt} off '{base_ref}' for the gate "
                "read. Failing closed — a gate must attest base + patch.diff, never the "
                "primary checkout. Fix the brief's base (or fetch it), then retry.")
    # ``-ff``: git-clean(1) preserves an untracked NESTED REPOSITORY under a single -f,
    # so a stale lane carrying one would survive the sweep and the reconstructed tree
    # would still hold files outside base + patch.diff (#296 review round 3).
    if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-ffdxq") != 0:
        _owner_file(wt).unlink(missing_ok=True)
        raise WorktreeError(
            f"{d.name}: could not reset {wt} to '{base_ref}' before the gate read. "
            "Failing closed — the tree cannot be shown to match patch.diff.")
    patch = d / "patch.diff"
    patch_text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    if patch_text.strip():
        if _GITLINK_RE.search(patch_text):
            # A submodule gitlink hunk: plain `git apply` exits 0 while ignoring it, so
            # the "reconstructed" tree would carry the wrong submodule revision under a
            # valid owner stamp — precisely the mismatched-green this path exists to
            # prevent. The reconstruction cannot materialize submodule state; fail
            # CLOSED rather than certify it (#296 review).
            _owner_file(wt).unlink(missing_ok=True)
            raise WorktreeError(
                f"{d.name}: patch.diff changes a submodule gitlink (mode 160000), which "
                "this reconstruction cannot materialize — `git apply` would silently "
                "skip it and a green would attest the wrong submodule revision (#296). "
                "Gate this bundle against a checkout with the submodule updated by hand.")
        if _git(wt, "apply", str(patch.resolve())) != 0:
            # The patch no longer applies to the base (drifted since Do, or corrupt): the
            # tree is clean base, NOT this bundle's build. Do NOT hand it to the gate (it
            # would test an unpatched tree and could pass) and do NOT claim ownership.
            _owner_file(wt).unlink(missing_ok=True)
            raise WorktreeError(
                f"{d.name}: patch.diff did not apply onto '{base_ref}' in {wt}. Failing "
                "closed — a green over a tree that does not match patch.diff would attest "
                "the wrong code (#296). Rebase/regenerate the patch, then retry.")
    _stamp_owner(wt, d)  # this bundle owns the reconstructed tree
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
    :func:`rebuild_for_gate` reconstruction (issues #224/#296), but stays best-effort — a
    human-paced ``pdca try`` can afford a "nothing to try" where a gating read cannot.

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
    if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-ffdxq") != 0:
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
            if _git(wt, "reset", "--hard", base_ref) != 0 or _git(wt, "clean", "-ffdq") != 0:
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
# lane is owned by a DIFFERENT bundle. Rather than mutate that lane (the in-lane
# reconstruction), hand the read its OWN throwaway tree, off the base + this bundle's
# patch, removed after.
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
    then rmtree any leftover ``*-ovf-*`` dirs. Best-effort; safe to call before a run.
    NB: removes ALL overflow trees — a caller that may overlap other live processes
    (the footprint sweep, #297) must use :func:`orphan_overflow_dirs` instead."""
    _git(primary, "worktree", "prune")
    for d in _overflow_dirs(primary):
        overflow_remove(primary, d)


def _pid_alive(pid: int) -> bool:
    """Non-destructive process-existence probe (#297 review round 5).

    On Windows ``os.kill(pid, 0)`` is NOT the harmless POSIX probe — CPython routes
    non-console signals through ``TerminateProcess``, so probing a live gate process
    would KILL it. ``OpenProcess`` with query-limited rights is the safe equivalent.
    Anything unknowable reads as alive (never reclaim what can't be classified)."""
    if os.name == "nt":  # pragma: no cover — exercised only on Windows
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87  # "no such process" for OpenProcess
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # A null handle does not prove absence (#297 review round 10): OpenProcess
        # also fails ACCESS_DENIED for a live protected / other-user process — the
        # exact mistake the POSIX branch's PermissionError arm avoids. Only the
        # nonexistent-pid error proves death; every other failure reads alive, so
        # the sweep leaves that overflow tree for its (possibly live) owner.
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists (another user's) / unknowable → treat as live
    return True


def orphan_overflow_dirs(primary: Path) -> list[Path]:
    """Overflow trees whose creating process is provably gone (#297 review).

    The overflow name embeds the creator's pid (``…-ovf-<pid>-<seq>``). A live pid —
    including a process we lack permission to signal — means the tree may be mid-gate
    in another process, and reclaiming it would invalidate that gate's results; only a
    pid that positively no longer exists marks an orphan. An unparseable name proves
    nothing, so it is skipped too (never delete what can't be classified)."""
    out: list[Path] = []
    for p in _overflow_dirs(primary):
        pid_s = p.name.split(_OVF_SUFFIX, 1)[1].split("-", 1)[0]
        if pid_s.isdigit() and not _pid_alive(int(pid_s)):
            out.append(p)  # provably dead → a crash leftover
    return out


def _overflow_create(d: Path, primary: Path, base_ref: str) -> Path | None:
    """Add a fresh overflow worktree off ``base_ref`` and apply this bundle's patch — the
    exceptional-read equivalent of :func:`rebuild_for_gate`, but on its own tree (never a
    shared lane). Best-effort: a git-add / patch-apply failure returns None (the caller
    falls through to the in-lane reconstruction, which fails closed) and cleans up any
    partial tree. No fetch — the lane's Do already refreshed the base."""
    ovf = _overflow_path(primary)
    try:
        _git(primary, "worktree", "prune")  # drop admin entries for any vanished trees first
        if _git(primary, "worktree", "add", "--force", str(ovf), base_ref) != 0:
            return None
        patch = d / "patch.diff"
        patch_text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
        if patch_text.strip():
            if _GITLINK_RE.search(patch_text):
                # Same gitlink fail-closed as rebuild_for_gate (#296 review round 2):
                # plain `git apply` exits 0 while skipping the gitlink, so this tree
                # would carry the wrong submodule revision. Decline the overflow; the
                # caller falls through to rebuild_for_gate, which raises the loud
                # WorktreeError → the fail-closed gating red.
                overflow_remove(primary, ovf)
                return None
            if _git(ovf, "apply", str(patch.resolve())) != 0:
                overflow_remove(primary, ovf)  # patch didn't apply → not this bundle's build
                return None
        return ovf
    except Exception:  # noqa: BLE001 — overflow is best-effort; fall back to the heal
        overflow_remove(primary, ovf)
        return None


def for_gate(d: Path, cfg: Config,
             hold: contextlib.ExitStack | None = None) -> tuple[Path | None, Path | None]:
    """Resolve the worktree a gate should read for bundle ``d`` (issues #226, #296).

    Returns ``(worktree_path, overflow_primary)``:
    * ``worktree_path`` is the tree to run the gate in (or None → run in the primary
      checkout, as when isolation is off — same fallback :func:`rebuild_for_gate` uses); and
    * ``overflow_primary`` is non-None ONLY when ``worktree_path`` is an ephemeral overflow
      tree the caller MUST tear down (``overflow_remove(overflow_primary, worktree_path)``)
      once the gate has run.

    The lane is a warm *checkout* cache, never a trusted *content* cache (#296): even when
    this bundle owns it, the gate read reconstructs ``base + patch.diff`` via
    :func:`rebuild_for_gate` — an owner stamp attests what Do intended, not what the tree
    is, and a standby-interrupted re-populate left a stale tree under a correct-looking
    stamp. A lane owned by a DIFFERENT bundle still prefers an overflow tree (with overflow
    enabled and under the cap) over clobbering a lane whose Do may be mid-flight; a failed
    overflow create falls through to the reconstruction, which fails closed
    (:class:`WorktreeError`) rather than hand the gate a mismatched tree.

    Touching the lane requires the :func:`lane_lock` (#296 review) — an in-flight Do (or
    another gate run) holding it means the tree is LIVE, and reconstruction would destroy
    its work; the lock is tried non-blocking and a busy lane raises. Pass ``hold`` (an
    ``ExitStack``) to keep the lock for the whole gate run — gates do, so a concurrent
    reconstruction can't clean their outputs mid-command; without it the lock guards only
    the reconstruction itself (direct/test callers). The overflow spill path never locks
    the lane — it never touches it."""
    if not cfg.worktree:
        return None, None
    tgt = _target(d, cfg)
    if tgt is None:
        return None, None
    primary, base_ref = tgt
    wt = _wt_dir(primary)
    # Foreign-owned lane: prefer an overflow tree over mutating a lane another bundle may
    # still want — but only up to the configured cap. The count-check and the create are
    # held under one lock so concurrent Check threads can't both pass the cap before either
    # dir exists (#241); the gate itself runs after this returns, so gates still execute
    # concurrently — only the brief reservation serializes.
    lane_owner = owner_of(wt) if (wt / ".git").exists() else None
    if lane_owner is not None and lane_owner != d.name and cfg.overflow > 0:
        with _ovf_cap_lock:
            ovf = (_overflow_create(d, primary, base_ref)
                   if len(_overflow_dirs(primary)) < cfg.overflow else None)
        if ovf is not None:
            print(f"worktree: {d.name} gate read spilled to an overflow tree {ovf.name} "
                  f"(lane owned by {lane_owner})", file=sys.stderr)
            return ovf, primary
    if hold is not None:
        hold.enter_context(lane_lock(d, cfg, wait=False))
        return rebuild_for_gate(d, cfg), None
    with lane_lock(d, cfg, wait=False):
        return rebuild_for_gate(d, cfg), None
