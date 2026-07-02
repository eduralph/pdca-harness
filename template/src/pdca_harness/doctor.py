"""``pdca doctor`` — report every prerequisite of a real run, fix nothing.

Most checks are DERIVED from the parsed config, so they track ``pdca.toml``
edits automatically: every distinct command-leaf ``argv[0]`` must be on PATH
(with a per-family auth probe where one exists), ``gh`` must be present and
authenticated for publish/merge, the bundle root must be writable, and the
tracker ``notes_cmd``'s tool must resolve. Instance-specific prerequisites
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
             "signoff": cfg.signoff, "publisher": cfg.publisher, "act": cfg.act}
    out = {role: leaf for role, leaf in named.items()
           if leaf.mode == "command" and leaf.argv}
    # (kind, specs, base) — base is the leaf an omitted field inherits from (None ⇒
    # no inheritance: advisory's own mode/argv/family).
    for kind, specs, base in (("advisory", cfg.advisory_leaves, None),
                              ("variant", cfg.builder_variants, cfg.builder),
                              ("escalation", cfg.builder_escalation, cfg.builder)):
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
        probe = _auth_probe(leaf.family)
        if probe:
            r.row(probe[0], label, probe[1])
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

    rows = _expand_checks(getattr(cfg, "doctor_checks", []), cfg.lanes)
    if rows:
        last_group = None
        for row in rows:
            group = row.get("group") or "project checks ([[doctor.checks]])"
            if group != last_group:
                print()
                print(f"== {group} ==")
                last_group = group
            rc = subprocess.run(row["cmd"], shell=True, capture_output=True,
                                cwd=cfg.root).returncode
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
