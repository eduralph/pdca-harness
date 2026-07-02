"""``pdca doctor`` — report every prerequisite of a real run, fix nothing.

Most checks are DERIVED from the parsed config, so they track ``pdca.toml``
edits automatically: every distinct command-leaf ``argv[0]`` must be on PATH
(with a per-family auth probe where one exists), ``gh`` must be present and
authenticated for publish/merge, the bundle root must be writable, and the
tracker ``notes_cmd``'s tool must resolve. Instance-specific prerequisites
(a Docker engine image, sibling checkouts, a scraper browser, …) are declared
as data in ``pdca.toml``::

    [[doctor.checks]]
    id = "docker"
    cmd = "docker info"
    hint = "https://docs.docker.com/engine/install/ — the gates run in a container"
    required = false

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
    specs — the full set of CLIs a real run may spawn."""
    named = {"builder": cfg.builder, "reviewer": cfg.reviewer, "planner": cfg.planner,
             "signoff": cfg.signoff, "publisher": cfg.publisher, "act": cfg.act}
    out = {role: leaf for role, leaf in named.items()
           if leaf.mode == "command" and leaf.argv}
    for kind, specs in (("advisory", cfg.advisory_leaves),
                        ("variant", cfg.builder_variants),
                        ("escalation", cfg.builder_escalation)):
        for i, spec in enumerate(specs):
            argv = list(spec.get("argv") or [])
            if spec.get("mode", "") == "command" and argv:
                label = f"{kind}:{spec.get('id') or spec.get('model') or i}"
                out[label] = LeafConfig(mode="command",
                                        family=spec.get("family", ""), argv=argv)
    return out


def run(cfg: Config, *, strict: bool = False) -> int:
    r = _Report()

    print("== core ==")
    v = sys.version_info
    r.row(OK if v >= (3, 11) else MISSING, "python >= 3.11",
          f"{v[0]}.{v[1]}.{v[2]}", required=True)
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

    checks = getattr(cfg, "doctor_checks", [])
    if checks:
        print()
        print("== project checks ([[doctor.checks]]) ==")
        for spec in checks:
            cid = spec.get("id") or spec.get("cmd", "?")
            cmd = spec.get("cmd", "")
            required = bool(spec.get("required", False))
            if not cmd:
                continue
            rc = subprocess.run(cmd, shell=True, capture_output=True,
                                cwd=cfg.root).returncode
            r.row(OK if rc == 0 else MISSING, cid,
                  "" if rc == 0 else spec.get("hint", ""), required=required)

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
