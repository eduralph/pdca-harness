"""``pdca doctor`` — report every prerequisite of a real run, fix nothing.

Most checks are DERIVED from the parsed config, so they track ``pdca.toml``
edits automatically: every distinct command-leaf ``argv[0]`` must be on PATH
(with a per-family auth probe where one exists), ``gh`` must be present and
authenticated for publish/merge, the bundle root must be writable, and the
tracker ``notes_cmd``'s tool must resolve. When the harness will actually seed a
**bounded leaf sandbox** — a `[leaves.sandbox]` exemption, on a leaf whose family
can be confined to it — its dependencies are checked too, because a sandbox that
cannot start does not fail: it silently does not confine (#289).
Instance-specific prerequisites
(a Docker engine image, sibling checkouts, a scraper browser, …) are declared
as data in ``pdca.toml``::

    [[doctor.checks]]
    group = "engine"      # optional section header to print the row under
    id = "docker"
    cmd = "docker info"   # run with cwd = project root; exit 0 ⇒ OK
    hint = "https://docs.docker.com/engine/install/ — the gates run in a container"
    level = "WARN"        # status when it FAILS (default MISSING); WARN for optional
    required = false      # a failing required row makes the doctor exit non-zero

    [[doctor.checks]]     # per_lane: one row per [driver].lane, {lane}/{lanes} filled
    group = "workspace"
    id = "lane worktrees lane{lane}"
    cmd = "test -e ../repo-lane{lane}/.git"
    hint = "make worktrees LANES={lanes}"
    per_lane = true       # expands 0..lanes-1; nothing when lanes ≤ 1 (serial)
    level = "WARN"

Output contract (shared with any instance wrapper script): one row per check,
``OK | MISSING | UNAUTH | WARN`` plus a fix hint; exit 0 iff every REQUIRED
check passes; ``--strict`` escalates every non-OK row (CI). Read-only and
idempotent — the doctor never installs or changes anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import Config, LeafConfig

OK, MISSING, UNAUTH, WARN = "OK", "MISSING", "UNAUTH", "WARN"


class _Report:
    def __init__(self) -> None:
        self.required_failed = False
        self.non_ok = False

    def row(self, status: str, check: str, hint: str = "", *, required: bool = False) -> None:
        print(f"{status:<7} {check:<34} {hint}")
        if status != OK:
            self.non_ok = True
            if required:
                self.required_failed = True


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _footprint_counts(cfg: Config) -> tuple[int, int, int]:
    """(lane, integration, orphaned-overflow) sibling-worktree counts (issue #297) — a
    cheap glob-count, never a size walk. Targets come from :func:`sweep.target_checkouts`
    so the common sibling-convention setup with NO ``[publisher.checkouts]`` entries is
    still covered (#297 review — the counts were permanently 0 there), and only overflow
    trees whose creating process is provably gone count as orphans (a live pid may be
    another process's in-flight gate read)."""
    from . import integrate, sweep, worktree  # lazy: doctor stays import-light
    lanes = integs = ovfs = 0
    for primary in sweep.target_checkouts(cfg):
        sibs = [p for p in primary.parent.glob(primary.name + worktree.WT_SUFFIX + "*")
                if p.is_dir()]
        ovfs += len(worktree.orphan_overflow_dirs(primary))
        lanes += sum(1 for p in sibs if worktree._OVF_SUFFIX not in p.name)
        integs += sum(1 for p in primary.parent.glob(
            primary.name + integrate.INTEG_INFIX + "*") if p.is_dir())
    return lanes, integs, ovfs


def _space_roots(cfg: Config, *, dev=lambda p: p.stat().st_dev) -> list[Path]:
    """One representative path per DISTINCT filesystem the harness writes on (#297
    review round 7): ``cfg.root`` plus each target checkout's PARENT directory. Lane,
    integration and overflow trees are created as SIBLINGS of the checkout — under
    ``checkout.parent`` — so when the checkout is itself a mount point, statting the
    checkout would measure the mounted filesystem while the sibling worktrees fill
    the parent's (#297 review round 9). Measuring the parent covers both: same
    filesystem in the common case, the right one when they differ. Deduped by
    ``st_dev``; an unstat-able path is kept so its row WARNs instead of vanishing.
    ``dev`` is injected for tests — real mount points can't be fabricated in a unit
    suite (the ``probe`` pattern from ``cli._suspend_inhibitor_argv``)."""
    from . import sweep  # lazy: doctor stays import-light
    roots: list[Path] = []
    seen: set[int] = set()
    for where in [cfg.root, *(p.parent for p in sweep.target_checkouts(cfg))]:
        try:
            d = dev(where)
        except OSError:
            roots.append(where)
            continue
        if d not in seen:
            seen.add(d)
            roots.append(where)
    return roots


def _quota_free_gb(where: Path, *, runner=subprocess.run) -> float | None:
    """Best-effort per-USER quota headroom (GiB) on the filesystem holding ``where``,
    or ``None`` when unknowable.

    ``shutil.disk_usage`` reports filesystem-wide free blocks — but the motivating
    #297 incident was ``EDQUOT``: a shared volume showing hundreds of free GiB while
    THIS user could no longer write (#297 review round 12). Parse linuxquota's
    ``quota -u -w --show-mntpoint --hide-device``: one row per quota'd filesystem
    (``<mountpoint> <blocks> <soft> <hard> …``, 1 KiB block units); the row whose
    mountpoint is the longest prefix of ``where`` supplies the headroom against its
    hard (else soft) limit. No ``quota`` binary, no matching row, a limit of 0 (no
    quota) or any exec/parse oddity ⇒ ``None`` — the fs-level number then stands and
    the row says quotas were not probed. ``runner`` is injected for tests (real
    quotas can't be fabricated in a unit suite)."""
    if shutil.which("quota") is None:
        return None
    try:
        proc = runner(["quota", "-u", "-w", "--show-mntpoint", "--hide-device"],
                      capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    # quota exits NON-ZERO when a limit is exceeded — the output still carries the
    # numbers, which is exactly the case this probe most needs to see.
    try:
        target = str(where.resolve())
    except OSError:
        return None
    best: tuple[int, float] | None = None  # (mountpoint length, headroom GiB)
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].startswith("/"):
            continue  # headers / device rows / continuation lines
        mnt = parts[0]
        try:
            blocks = float(parts[1].rstrip("*"))  # '*' marks an exceeded soft limit
            soft = float(parts[2])
            hard = float(parts[3])
        except ValueError:
            continue
        limit = hard if hard > 0 else soft
        if limit <= 0:
            continue  # no quota on this filesystem
        if target == mnt or target.startswith(mnt.rstrip("/") + "/"):
            headroom = max(0.0, limit - blocks) / (1024 ** 2)  # 1 KiB blocks → GiB
            if best is None or len(mnt) > best[0]:
                best = (len(mnt), headroom)
    return best[1] if best else None


# What Claude Code's Linux sandbox needs on PATH before it will actually engage. Missing any
# of these, it does not fail — it disables the sandbox and runs unconfined (#289).
# binary on PATH -> (what it is, the PACKAGE that provides it). The two differ for bubblewrap:
# the binary is `bwrap`, the package is `bubblewrap`, and `apt install bwrap` simply fails. On a
# REQUIRED row that is not a cosmetic slip — an operator who follows the hint stays blocked
# (PR #290 review). Probe the BINARY, name the PACKAGE.
_SANDBOX_DEPS = {
    "bwrap": ("bubblewrap — the leaf sandbox's jail", "bubblewrap"),
    "socat": ("the leaf sandbox's network proxy", "socat"),
}


def _sandbox_expected(cfg: Config) -> bool:
    """True iff the harness will actually SEED a bounded sandbox into a leaf — so a missing
    dependency is a false security claim, not merely an absent feature.

    That is exactly: a `[leaves.sandbox] unsandboxed_commands` exemption is configured, AND at
    least one **sandboxed** leaf (reviewer / advisory) runs in command mode on a family that can
    be confined to the harness's own settings (`settings_scope_argv` — claude). Those are the
    only runs where `leaves._seed_sandbox_settings` writes `sandbox.enabled` and the harness
    promises "only the named commands escape".

    Two earlier signals were WRONG, and both are gone (PR #290 review):

    * the project's own `.claude/settings.json` `sandbox.enabled` predicted nothing about a
      leaf. The leaf runs from a **temp cwd**, so it never loads the project's settings at all —
      only the file the harness seeds there. Reading it told the operator to install bwrap/socat
      for a leaf sandbox that was never going to exist.
    * ignoring the family made the rows fire for a **codex** reviewer, whose exemption
      `_seed_sandbox_settings` REFUSES (it cannot be bounded) and whose sandbox is its own —
      demanding claude's dependencies for a run that never uses them, as a REQUIRED failure.

    The operator's own ambient sandbox (their user-scope `~/.claude/settings.json`) is theirs,
    not the harness's claim, so it is not checked here.

    Plan-advisory leaves count UNCONDITIONALLY (#301 review round 8): their runner seeds
    a MINIMAL fail-closed sandbox (`_seed_plan_sandbox_settings` — enabled, no Check
    grants) for every confinable family, with no `[leaves.sandbox]` exemption involved —
    so a claude-family plan reviewer needs the dependencies even when no exemption is
    configured (the seeded `failIfUnavailable` makes the leaf REFUSE without them).
    """
    leaves_map = _command_leaves(cfg)
    if any(cfg.profile(leaf).settings_scope_argv
           for role, leaf in leaves_map.items() if role.startswith("plan-advisory:")):
        return True
    if not getattr(cfg, "leaf_unsandboxed_commands", None):
        return False
    return any(cfg.profile(leaf).settings_scope_argv
               for role, leaf in leaves_map.items()
               if role == "reviewer" or role.startswith("advisory:"))


def _auth_probe(family: str) -> tuple[str, str] | None:
    """A best-effort per-family credential probe: (status, hint), or ``None`` when
    the binary's presence is all that can be checked. Never spends a model call."""
    if family == "claude":
        home = Path.home()
        if (home / ".claude" / ".credentials.json").exists() or (home / ".claude.json").exists():
            return None
        return (WARN, "no claude credentials found — run 'claude' once interactively")
    if family == "codex":
        rc = subprocess.run(["codex", "login", "status"],
                            capture_output=True).returncode
        if rc != 0:
            return (UNAUTH, "run 'codex login' (or export OPENAI_API_KEY)")
        return None
    return None


def _command_leaves(cfg: Config) -> dict[str, LeafConfig]:
    """Every command-mode leaf by role name, including advisory/variant/escalation
    specs — the full set of CLIs a real run may spawn.

    Variant/escalation specs INHERIT mode/argv/family from ``[leaves.builder]`` when
    they omit them (``select_builder`` / ``_leaf_from_spec``): a spec that leaves
    ``mode`` unset is stored as ``""`` yet runs as a *command* if the default builder
    is one. Resolve the EFFECTIVE values here — otherwise a routed/escalated builder
    with its own ``argv`` (a different binary) is never checked, so ``--strict`` can
    pass while the real Do attempt later dies on that missing CLI. Advisory leaves
    have no builder inheritance (their stored default mode is ``stub``)."""
    named = {"builder": cfg.builder, "reviewer": cfg.reviewer, "planner": cfg.planner,
             "signoff": cfg.signoff, "publisher": cfg.publisher, "act": cfg.act,
             # The sizer (#320) is a real command leaf the Plan beat spawns; omitting it
             # let `--strict` pass while the advisory later died on a missing CLI.
             "sizer": getattr(cfg, "sizer", LeafConfig()),
             # `pdca split` spawns this like any other command leaf; omitting it let
             # --strict pass while the split later died on a CLI nobody had installed.
             "splitter": getattr(cfg, "splitter", LeafConfig())}
    out = {role: leaf for role, leaf in named.items()
           if leaf.mode == "command" and leaf.argv}
    # (kind, specs, base) — base is the leaf an omitted field inherits from (None ⇒
    # no inheritance: advisory's own mode/argv/family). Plan advisories (#301) are
    # command leaves a real run spawns too — omitting them let --strict pass while the
    # Plan beat later died on the missing CLI (#301 review).
    for kind, specs, base in (("advisory", cfg.advisory_leaves, None),
                              ("plan-advisory", getattr(cfg, "plan_advisory_leaves", []), None),
                              ("variant", cfg.builder_variants, cfg.builder),
                              ("escalation", cfg.builder_escalation, cfg.builder),
                              # Sizer escalations inherit from [leaves.sizer] the same way
                              # builder escalations inherit from [leaves.builder], so a
                              # spec naming a different binary must be resolved too.
                              ("sizer-escalation", getattr(cfg, "sizer_escalation", []),
                               getattr(cfg, "sizer", None))):
        for i, spec in enumerate(specs):
            mode = spec.get("mode") or (base.mode if base else "stub")
            argv = list(spec.get("argv") or (base.argv if base else []))
            family = spec.get("family") or (base.family if base else "")
            if mode == "command" and argv:
                label = f"{kind}:{spec.get('id') or spec.get('model') or i}"
                out[label] = LeafConfig(mode="command", family=family, argv=argv)
    return out


def _expand_checks(specs: list[dict], lanes: int) -> list[dict]:
    """Materialize the [[doctor.checks]] rows to run, in declared order.

    A row with ``per_lane = true`` is a TEMPLATE expanded once per driver lane —
    ``{lane}`` → 0..lanes-1 and ``{lanes}`` → the count — so ``[driver].lanes``
    drives the lane-worktree checks without the instance hardcoding a count (and
    it yields NOTHING when lanes ≤ 1, matching serial mode's base-only worktrees).
    ``{lanes}`` is substituted in every row (e.g. a hint's ``LANES={lanes}``).
    A row needs a non-empty ``cmd``; ``id`` defaults to the cmd."""
    def _sub(value, lane):
        if not isinstance(value, str):
            return value
        value = value.replace("{lanes}", str(lanes))
        return value.replace("{lane}", str(lane)) if lane is not None else value

    def _row(spec: dict, lane) -> dict:
        out = {k: _sub(v, lane) for k, v in spec.items()}
        out.setdefault("id", out.get("cmd", "?"))
        return out

    rows: list[dict] = []
    for spec in specs:
        if not spec.get("cmd"):
            continue
        if spec.get("per_lane"):
            rows.extend(_row(spec, k) for k in range(max(0, lanes) if lanes > 1 else 0))
        else:
            rows.append(_row(spec, None))
    return rows


def probe(cmd: str, cfg) -> int:
    """Run ONE detect ``cmd`` exactly the way :func:`run` runs a ``[[doctor.checks]]``
    row — shell, project root, exit 0 ⇒ present. The single probe implementation every
    consumer shares (:func:`run`, the Plan-exit guard's :func:`failing_dependencies`
    (#340), the Do-exit adjudication in :mod:`~pdca_harness.dependency_halt` (#341)),
    so "what it means to probe a row" cannot drift apart."""
    return subprocess.run(cmd, shell=True, capture_output=True, cwd=cfg.root).returncode


def registered_ids(cfg: Config) -> set[str]:
    """Lower-cased ids of ``[[doctor.checks]]`` rows that would actually **run** (issue #263).

    A row registers a dependency only if it can DETECT it. ``_expand_checks`` skips any row
    without a non-empty ``cmd``, so ``[[doctor.checks]] id = "protoc"`` alone runs no check —
    it must not be allowed to silence the unregistered-dependency §6 blocker while no
    preflight ever exists (PR #269 review). ``id`` defaults to ``cmd``, matching
    ``_expand_checks``'s own default.

    Rows are read from disk, not from the ``Config`` snapshot, so a row registered *during*
    the run (by the Plan beat, or by the human pasting the builder's proposal) counts.
    """
    return {
        str(row.get("id") or row["cmd"]).strip().lower()
        for row in cfg.current_doctor_checks()
        if str(row.get("cmd") or "").strip()
    }


def unregistered_dependencies(brief_path, cfg) -> list[str]:
    """Brief-declared external dependencies with no registered ``[[doctor.checks]]`` row.

    The principle (#263): when a change needs something a human must install or provide,
    the system must REGISTER it — a row with a detect ``cmd`` and an install ``hint`` —
    rather than let it surface mid-cycle as a cryptic build failure.

    A pure function of ``brief.md`` + ``pdca.toml``, and **not a judgment call**: it is set
    membership. That is what lets the Plan-exit policy block on it where the size advisory
    can only warn (#333/#321).

    :func:`registered_ids` owns what "registered" means — a row that would actually RUN
    (it has a detect ``cmd``), read from ``pdca.toml`` as it stands NOW rather than from
    the snapshot the run opened with, because Plan and Do both add rows mid-cycle (PR #269
    review). That is also what makes the Plan-exit hold self-clearing: register the row and
    the next beat proceeds, with no replan.

    Lives here rather than in ``assemble`` (#333) so the Plan-exit check and the Check-time
    backstop cannot drift apart; ``assemble`` imports this module already.
    """
    from . import brief as _brief  # local: brief has no dependency on doctor
    registered = registered_ids(cfg)
    return [
        f"external dependency `{token}` is declared in the brief but has no matching "
        f"[[doctor.checks]] row — register a detect cmd + install hint in pdca.toml, or "
        f"annotate it `(no-check: …)` if nothing can detect it"
        for token in _brief.external_dependency_tokens(brief_path)
        if token.strip().lower() not in registered
    ]


def failing_dependencies(brief_path, cfg) -> list[str]:
    """Brief-named registered dependencies whose detect ``cmd`` exits non-zero (#340).

    #333 forces *registration* — every checkable token must name a ``[[doctor.checks]]``
    row — but nothing executed the row: :func:`registered_ids` only requires a non-empty
    ``cmd``, so a planner could discharge every check on a machine where the dependency
    is absent, and the only remaining detector was the builder's own mid-cycle
    self-report — the actor most tempted to work around the gap silently. This runs the
    detect ``cmd`` of **exactly the rows the brief's backticked tokens name** — a
    registered row the brief does not name is never executed, so an instance's wider
    doctor inventory is not a tax on every bundle — and reports each non-zero exit
    together with the row's own ``hint``.

    Same sharing rationale as :func:`unregistered_dependencies`: the Plan-exit probe and
    any later consumer (#341 reuses it at Do exit) must not drift apart. Rows come from
    :meth:`~pdca_harness.config.Config.current_doctor_checks` — ``pdca.toml`` as it is on
    disk NOW, not the run's snapshot — so a row registered during the Plan beat is probed
    in the same pass, and each ``cmd`` runs exactly the way :func:`run` runs a row
    (shell, project root, exit 0 ⇒ present). Matching mirrors :func:`registered_ids`:
    the raw row's ``id`` (default: its ``cmd``), case-insensitive.

    Detect cmds therefore run on every beat the pre-dispatch policy is consulted, not
    just on an explicit ``pdca doctor`` — they must stay cheap and side-effect-free (the
    ``[[doctor.checks]]`` config comment carries that expectation). Probing is
    machine-scoped by design, and that is the correct scope, not a compromise: the
    builder runs on this same host.
    """
    from . import brief as _brief  # local: brief has no dependency on doctor
    wanted = {t.strip().lower() for t in _brief.external_dependency_tokens(brief_path)}
    if not wanted:
        return []
    failed: list[str] = []
    for row in cfg.current_doctor_checks():
        cmd = str(row.get("cmd") or "").strip()
        if not cmd:
            continue  # not registered — `registered_ids` skips it for the same reason
        rid = str(row.get("id") or cmd).strip()
        if rid.lower() not in wanted:
            continue  # ONLY the rows the brief names run (#340's definition of done)
        rc = probe(cmd, cfg)
        if rc != 0:
            hint = str(row.get("hint") or "").strip() or "the row has no hint — add one"
            failed.append(f"external dependency `{rid}` is registered but absent on this "
                          f"host — its detect cmd (`{cmd}`) exited {rc}; hint: {hint}")
    return failed


# The secret guard (.secret-guard/activate.sh, .pre-commit-config.yaml). The harness commits
# into the instance repo (records, reverts) and into each target checkout (publish), as the
# user running the driver, so the guard only protects those commits where that user activated
# it. Doctor does not re-implement the guard's rules: it runs the default branch's reviewed
# activation script in a throwaway shared copy of the clone and compares every artifact it
# writes, byte for byte, with the clone's own, so drift, a stale version or a modified hook,
# runner or policy all show up as a difference.
GUARD_SCRIPT = ".secret-guard/activate.sh"
GUARD_ACTIVATE = ('s=$(PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin git show '
                  'refs/remotes/origin/HEAD:.secret-guard/activate.sh) && /bin/sh -c "$s" activate')
_GUARD_REQ = 'exec "$(git rev-parse --path-format=absolute --git-common-dir)/secret-guard-run" "$@"'
# What activation writes into the git directory (the hook only without core.hooksPath).
_GUARD_ARTIFACTS = ("secret-guard-run", "secret-guard.yaml", "secret-guard-gitleaks.toml",
                    "secret-guard-pre-commit")
_GIT_CLEAN_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def _git_raw(repo: Path, *args: str, env: dict | None = None) -> str | None:
    """``git -C repo args`` stdout, unstripped, or None when git fails."""
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    return r.stdout if r.returncode == 0 else None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _in_work_tree(path: Path) -> bool:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_CLEAN_ENV}
    target = path if path.is_dir() else path.parent
    return (_git_raw(target, "rev-parse", "--is-inside-work-tree", env=env) or "").strip() == "true"


def _crosses_work_tree(path: Path) -> bool:
    """True when ``path`` passes through a work tree a branch controls. Symlinks are followed
    one hop at a time, as activation's ``chain_unsafe`` does: the place each link sits, every
    directory crossed (also on the way to ``..``) and the final target are checked, so a chain
    such as ``outside -> checkout/link -> outside`` is caught. Unfollowable: treated as inside."""
    p = path if path.is_absolute() else Path.cwd() / path
    for _ in range(40):
        prefix, link, rest = Path(p.anchor), None, ()
        parts = p.parts[1:]
        for i, part in enumerate(parts):
            prefix = prefix / part
            if prefix.is_symlink():
                if _in_work_tree(prefix.parent):
                    return True
                link, rest = prefix, parts[i + 1:]
                break
            if part != ".." and prefix.is_dir() and _in_work_tree(prefix):
                return True
        if link is None:
            return _in_work_tree(p.resolve())
        try:
            target = Path(os.readlink(link))
        except OSError:
            return True
        p = (target if target.is_absolute() else link.parent / target).joinpath(*rest)
    return True


def _repo_worktrees(repo: Path) -> list[Path]:
    """The physical paths of ``repo``'s worktrees (bare entries excluded)."""
    listing = _git_raw(repo, "worktree", "list", "--porcelain") or ""
    roots = []
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        path = next((ln[9:] for ln in lines if ln.startswith("worktree ")), None)
        if path and "bare" not in lines:
            roots.append(Path(path).resolve())
    return roots


def _in_repo_worktrees(path: Path, roots: list[Path], git_dir: Path) -> bool:
    """``path`` (as written, each prefix, or resolved) lies in one of ``roots`` outside the
    git directory: a checkout of this repository a branch can rewrite."""
    def inside(p: Path) -> bool:
        try:
            real = p.resolve()
        except OSError:
            return True
        if real == git_dir or git_dir in real.parents:
            return False
        return any(real == root or root in real.parents for root in roots)
    prefix = Path(path.anchor)
    for part in path.parts[1:]:
        prefix = prefix / part
        if prefix.is_symlink() and inside(prefix.parent):
            return True
    return inside(path)


def _worktree_overrides(repo: Path) -> list[str]:
    """Worktrees that set their own core.hooksPath (extensions.worktreeConfig): Git there
    ignores the hook activation installed. Activation refuses these; doctor reports them."""
    if (_git_raw(repo, "config", "--bool", "extensions.worktreeConfig") or "").strip() != "true":
        return []
    listing = _git_raw(repo, "worktree", "list", "--porcelain") or ""
    found = []
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        path = next((ln[9:] for ln in lines if ln.startswith("worktree ")), None)
        if path and "bare" not in lines and \
                _git_raw(Path(path), "config", "--worktree", "--get", "core.hooksPath") is not None:
            found.append(path)
    return found


def _remote_head_current(repo: Path, rev: str) -> bool | None:
    """Whether ``rev`` (the cached refs/remotes/origin/HEAD) is still the remote's HEAD:
    True/False, or None when the remote cannot be asked (offline, no access)."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-remote", "origin", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0] == rev


def _dry_activation(repo: Path, rev: str, script: str) -> tuple[Path, Path] | str:
    """Run the reviewed ``script`` in a throwaway shared copy of ``repo`` at ``rev`` (default
    hooks, no global or system git config). Returns ``(tmpdir, git_dir)`` holding the
    expected artifacts, or the failure text."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="pdca-guard-"))
    except OSError as exc:
        return f"no temporary directory ({exc})"
    copy = tmp / "copy"
    env = {k: v for k, v in os.environ.items() if k not in _GIT_CLEAN_ENV}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", SECRET_GUARD_REF="HEAD")
    steps = (["git", "clone", "-q", "--shared", "--no-checkout", str(repo), str(copy)],
             ["git", "-C", str(copy), "update-ref", "--no-deref", "HEAD", rev])
    try:
        for argv in steps:
            r = subprocess.run(argv, capture_output=True, text=True, env=env)
            if r.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                return (r.stderr or "git failed").strip()
        r = subprocess.run(["/bin/sh", "-c", script, "activate"], cwd=copy,
                           capture_output=True, text=True, env=env)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return str(exc)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return (r.stderr or r.stdout or "activation failed").strip().splitlines()[-1]
    return tmp, copy / ".git"


def secret_guard_status(repo: Path) -> tuple[str, str] | None:
    """``(status, hint)`` for the secret guard in the clone at ``repo``; None when ``repo`` is
    not a git checkout or ships no guard (nothing to activate)."""
    common = _git_raw(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    rev = (_git_raw(repo, "rev-parse", "--verify", "--quiet",
                    "refs/remotes/origin/HEAD^{commit}") or "").strip()
    if not rev:
        if _git_raw(repo, "cat-file", "-e", f"HEAD:{GUARD_SCRIPT}") is not None:
            return WARN, ("cannot read the default branch (refs/remotes/origin/HEAD); run there: "
                          "git remote set-head origin -a, then re-run doctor")
        return None
    script = _git_raw(repo, "show", f"{rev}:{GUARD_SCRIPT}")
    if script is None:
        # No guard as last fetched; a remote that moved since may have added one.
        if _remote_head_current(repo, rev) is False:
            return WARN, ("the remote default branch moved since the last fetch and may ship the "
                          "guard; run there: git fetch origin, then re-run doctor")
        return None
    d = Path(common.strip())
    again = f"run there: {GUARD_ACTIVATE}"
    overrides = _worktree_overrides(repo)
    if overrides:
        return WARN, (f"worktrees set their own core.hooksPath ({', '.join(overrides)}), so Git "
                      "there skips the guard; unset it in each (git -C <worktree> config "
                      f"--worktree --unset core.hooksPath), then {again}")

    # Where Git looks for the hook, and nothing a branch could replace on the way.
    hooks_set = _git_raw(repo, "config", "--get", "core.hooksPath") is not None
    hooks_path = (_git_raw(repo, "config", "--path", "core.hooksPath") or "").strip()
    if hooks_set:
        if not hooks_path:
            return WARN, ("core.hooksPath is set but empty, so Git runs no hooks; unset it "
                          f"(git config --unset core.hooksPath), then {again}")
        hp = Path(hooks_path)
        if not hp.is_absolute() or _crosses_work_tree(hp) or \
                _crosses_work_tree(hp / "pre-commit"):
            return WARN, (f"core.hooksPath ({hooks_path}) is relative or passes through a work "
                          f"tree, where a branch could replace the hook; {again}")
        hook_file = hp / "pre-commit"
        hook = (_read_bytes(hook_file) or b"").decode(errors="replace")
        wired = _GUARD_REQ in hook.splitlines()
    else:
        hook_file = d / "hooks" / "pre-commit"
        wired = hook_file.is_file()
    # .git/hooks matters only while Git uses it (no core.hooksPath).
    watched = (*_GUARD_ARTIFACTS, *(() if hooks_set else ("hooks", "hooks/pre-commit")))
    links = [n for n in watched if (d / n).is_symlink()]
    if links:
        return WARN, f"{', '.join(links)} in {d} is a symlink; {again}"
    if not (wired and os.access(hook_file, os.X_OK) and os.access(d / "secret-guard-run", os.X_OK)):
        return WARN, f"not active in this clone; {again}"

    # What the default branch's script writes, compared with what the clone has.
    on_path = shutil.which("pre-commit")
    if on_path is None:
        return WARN, ("cannot verify: pre-commit is not on PATH "
                      f"(pipx install pre-commit==4.7.0), then {again}")
    roots, git_dir = _repo_worktrees(repo), d.resolve()
    recorded = (_read_bytes(d / "secret-guard-pre-commit") or b"").decode(errors="replace").strip()
    for which, exe in (("on PATH", on_path), ("recorded", recorded)):
        if exe and _in_repo_worktrees(Path(exe), roots, git_dir):
            return WARN, (f"the pre-commit {which} ({exe}) is in a checkout of this repository, "
                          f"where a branch can replace it; put the pipx one first on PATH, then {again}")
    legacy = d / "hooks" / "pre-commit.legacy"
    if not hooks_set and (legacy.exists() or legacy.is_symlink()):
        approved = (_read_bytes(d / "secret-guard-legacy.approved") or b"").decode().strip()
        current = (_git_raw(repo, "hash-object", "--", str(legacy)) or "").strip()
        if legacy.is_symlink() or not current or approved != current:
            return WARN, ("hooks/pre-commit.legacy is not approved as it is now, so every commit "
                          f"stops before the guard; review it, then run there: {GUARD_ACTIVATE} "
                          "--approve-legacy (or delete it)")
    dry = _dry_activation(repo, rev, script)
    if isinstance(dry, str):
        return WARN, f"cannot verify (a dry activation failed: {dry}); {again}"
    tmp, expected = dry
    try:
        names = [*_GUARD_ARTIFACTS] + ([] if hooks_set else ["hooks/pre-commit"])
        differ = [n for n in names if _read_bytes(d / n) != _read_bytes(expected / n)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if differ:
        return WARN, (f"stale or modified ({', '.join(differ)} differ from what the default "
                      f"branch's script writes); {again}")
    current = _remote_head_current(repo, rev)
    if current is None:
        return WARN, ("matches the last fetched default branch, but the remote could not be "
                      "asked (git ls-remote origin HEAD) to confirm it is still current")
    if not current:
        return WARN, ("the remote default branch moved since the last fetch; run there: "
                      f"git fetch origin, then re-run doctor (and {GUARD_ACTIVATE} if it warns)")
    return OK, ""


def run(cfg: Config, *, strict: bool = False) -> int:
    r = _Report()

    print("== core ==")
    v = sys.version_info
    r.row(OK if v >= (3, 11) else MISSING, "python >= 3.11",
          f"{v[0]}.{v[1]}.{v[2]}", required=True)
    try:
        import ensurepip  # noqa: F401 — probe THIS interpreter, pre-venv (clean Ubuntu lacks it)
        r.row(OK, "python venv (ensurepip)")
    except ModuleNotFoundError:
        r.row(MISSING, "python venv (ensurepip)",
              "sudo apt-get install -y python3-venv", required=True)
    r.row(OK if _have("make") else MISSING, "make",
          "" if _have("make") else "install make (the front-door target runner)",
          required=True)
    if _have("git"):
        name = subprocess.run(["git", "config", "--get", "user.name"],
                              capture_output=True, text=True).stdout.strip()
        email = subprocess.run(["git", "config", "--get", "user.email"],
                               capture_output=True, text=True).stdout.strip()
        if name and email:
            r.row(OK, "git + identity")
        else:
            r.row(WARN, "git + identity",
                  "set git config user.name / user.email (sign-offs need them)")
    else:
        r.row(MISSING, "git", "install git", required=True)
    try:
        cfg.bundle_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cfg.bundle_root):
            pass
        r.row(OK, f"bundle root writable ({cfg.bundle_root.name}/)")
    except OSError as exc:
        r.row(MISSING, "bundle root writable", str(exc), required=True)

    if _have("git"):
        from . import sweep  # lazy: doctor stays import-light
        seen: set[Path] = set()
        for repo in [cfg.root, *sweep.target_checkouts(cfg)]:
            repo = repo.resolve()
            if repo in seen or not repo.is_dir():
                continue
            seen.add(repo)
            status = secret_guard_status(repo)
            if status is not None:
                label = "secret guard" if repo == cfg.root.resolve() else f"secret guard ({repo.name})"
                r.row(status[0], label, status[1])

    print()
    print("== model leaves (from pdca.toml) ==")
    leaves = _command_leaves(cfg)
    if not leaves:
        r.row(OK, "all leaves are stubs", "no model CLI needed (offline mode)")
    seen: set[str] = set()
    for role, leaf in leaves.items():
        binary = leaf.argv[0]
        if binary in seen:
            continue
        seen.add(binary)
        label = f"{binary} ({role}, family={leaf.family or 'generic'})"
        if not _have(binary):
            r.row(MISSING, label, f"install '{binary}' — [leaves.{role}] runs it")
            continue
        auth = _auth_probe(leaf.family)  # not `probe` — that name is the row-cmd runner
        if auth:
            r.row(auth[0], label, auth[1])
        else:
            r.row(OK, label)

    print()
    print("== contribution (gh) ==")
    if _have("gh"):
        if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0:
            r.row(OK, "gh CLI + auth")
        else:
            r.row(UNAUTH, "gh CLI", "run 'gh auth login' (publish/merge/revert need it)")
    else:
        r.row(MISSING, "gh CLI",
              "https://github.com/cli/cli — publish/merge/revert need it")
    if cfg.notes_cmd:
        tool = cfg.notes_cmd.split()[0]
        found = _have(tool) or (cfg.root / tool).exists()  # PATH or a repo-relative script
        r.row(OK if found else WARN, f"notes_cmd tool ({tool})",
              "" if found else "the Plan beat's tracker fetch will fail without it")

    if _sandbox_expected(cfg):
        print()
        print("== leaf sandbox ==")
        # The sandbox does not fail closed on its own: with `sandbox.enabled` true but a
        # dependency missing, Claude Code DISABLES the sandbox, warns, and runs every command
        # unconfined. A leaf would then run *everything* outside a sandbox that pdca.toml and
        # docs 05 both say bounds it to the named commands. So these are REQUIRED — the
        # consequence of a miss is not a degraded feature, it is a false security claim (#289).
        for tool, (why, package) in _SANDBOX_DEPS.items():
            r.row(OK if _have(tool) else MISSING, f"{tool} ({why})",
                  "" if _have(tool) else
                  f"sudo apt install {package} — without it the leaf sandbox silently does NOT "
                  "engage and the bounded exemption does not hold",
                  required=True)

    print()
    print("== workspace ==")
    # Footprint preflight (issue #297): quota exhaustion mid-`cargo test` produces an
    # arbitrary failing test name, so a gating red gets misattributed to the patch until a
    # human traces it. Surface it HERE, before a run — statvfs is O(1); never `du` a
    # multi-hundred-GB tree.
    if cfg.doctor_min_free_gb > 0:
        for where in _space_roots(cfg):
            label = ("free disk space" if where == cfg.root
                     else f"free disk space ({where.name})")
            try:
                fs_gb = shutil.disk_usage(where).free / (1024 ** 3)
                quota_gb = _quota_free_gb(where)
                # The EFFECTIVE headroom is the tighter of filesystem free and this
                # user's quota (#297 review round 12): the motivating incident was
                # EDQUOT on a shared volume that showed hundreds of fs-level GiB —
                # disk_usage alone cannot see per-user quotas.
                bound = quota_gb is not None and quota_gb < fs_gb
                eff = quota_gb if bound else fs_gb
                low = eff < cfg.doctor_min_free_gb
                what = "user-quota headroom" if bound else "free"
                caveat = ("" if quota_gb is not None or shutil.which("quota")
                          else " (fs-level; per-user quotas not visible — install "
                               "quota-tools to probe them)")
                r.row(WARN if low else OK, label,
                      f"{eff:.1f} GiB {what} < {cfg.doctor_min_free_gb:g} GiB "
                      "threshold — gate runs will false-red on quota; run "
                      "'pdca sweep' (or 'pdca sweep --remove')" if low
                      else f"{eff:.1f} GiB {what}{caveat}")
            except OSError as exc:
                r.row(WARN, label, f"could not stat {where}: {exc}")
    lanes_n, integs, ovfs = _footprint_counts(cfg)
    if ovfs:
        r.row(WARN, "harness worktree footprint",
              f"{ovfs} orphaned overflow tree(s) (crash leftovers) — run 'pdca sweep'")
    else:
        r.row(OK, "harness worktree footprint",
              f"{lanes_n} lane / {integs} integration worktree(s)")

    rows = _expand_checks(getattr(cfg, "doctor_checks", []), cfg.lanes)
    if rows:
        last_group = None
        for row in rows:
            group = row.get("group") or "project checks ([[doctor.checks]])"
            if group != last_group:
                print()
                print(f"== {group} ==")
                last_group = group
            rc = probe(row["cmd"], cfg)
            fail_status = row.get("level", MISSING)  # WARN for optional rows
            r.row(OK if rc == 0 else fail_status, row["id"],
                  "" if rc == 0 else row.get("hint", ""),
                  required=bool(row.get("required", False)))

    print()
    if r.required_failed:
        print("doctor: REQUIRED checks failed — fix the lines above first.")
        return 1
    if strict and r.non_ok:
        print("doctor (--strict): non-OK rows present.")
        return 1
    tail = " — some optional pieces need attention (see above)" if r.non_ok else ""
    print(f"doctor: required checks OK{tail}.")
    return 0
