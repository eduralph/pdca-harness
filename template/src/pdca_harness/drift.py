"""Drift sweep (issue #206): re-check each COMPLETE-with-open-PR bundle's patch against the
**current** pristine publish base and flag non-appliers as needs-rebase.

A bundle's ``patch.diff`` is validated against the upstream tip **at build time**, but
upstream keeps moving. Nothing else re-checks an already-published bundle against the
current base, so drift is invisible until a maintainer hits the merge conflict at review
time. This sweep ``git apply --check``s each published patch against a freshly-fetched base
in a **throwaway detached worktree** (the primary checkout is never touched) and reports the
stale ones so their PRs can be rebased proactively.

**Report-only.** It never mutates a bundle, never re-decides §9, and never fails the run —
it is a signal for the human, the same contract as :mod:`revalidate` (which re-checks the
*engine* substrate; this re-checks the *upstream base* substrate).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import publish, state
from .config import Config


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _applies_to_base(repo: Path, base_ref: str, patch: Path) -> tuple[str, str]:
    """``('ok' | 'needs-rebase' | 'error', detail)`` for ``patch`` vs ``base_ref``. Uses a
    throwaway detached worktree at ``base_ref`` so the primary checkout is untouched."""
    with tempfile.TemporaryDirectory(prefix="pdca-drift-") as tmp:
        wt = Path(tmp) / "wt"
        add = _git(repo, "worktree", "add", "--detach", str(wt), base_ref)
        if add.returncode != 0:
            tail = (add.stderr.strip().splitlines()[-1:] or ["worktree add failed"])[0]
            return "error", tail[:200]
        try:
            chk = _git(wt, "apply", "--check", str(patch))
            if chk.returncode == 0:
                return "ok", ""
            tail = (chk.stderr.strip().splitlines()[-1:] or ["patch does not apply"])[0]
            return "needs-rebase", tail[:200]
        finally:
            _git(repo, "worktree", "remove", "--force", str(wt))


def check_bundle(cfg: Config, d: Path, *, fetch: bool = True) -> dict | None:
    """Drift status for one bundle, or ``None`` if it isn't a published contribution to
    check (no patch, or accepted-but-unpublished — the latter is #206's part 2, not drift).
    Returns ``{bundle, pr_url, base, status, detail}``."""
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return None  # close/no-fix disposition — nothing to apply
    rec = publish._publish_record(d)
    pr_url = rec.get("pr_url") if rec else None
    if not pr_url:
        return None  # accepted but no PR yet — not a drift case
    repo_spec, base, _ = publish._resolve_target(d)
    if not repo_spec or not base:
        return None  # no resolvable upstream target
    base_ref = f"{cfg.base_remote}/{base}"
    repo = publish._checkout_path(cfg, repo_spec)
    if not (repo / ".git").exists():
        return {"bundle": d.name, "pr_url": pr_url, "base": base_ref,
                "status": "error", "detail": f"no checkout at {repo}"}
    if fetch:
        _git(repo, "fetch", cfg.base_remote, base)  # best-effort — refresh the base tip
    status, detail = _applies_to_base(repo, base_ref, patch.resolve())
    return {"bundle": d.name, "pr_url": pr_url, "base": base_ref,
            "status": status, "detail": detail}


def sweep(cfg: Config, *, fetch: bool = True) -> list[dict]:
    """Drift status for every COMPLETE, published bundle carrying a patch (report-only)."""
    if not cfg.bundle_root.exists():
        return []
    rows: list[dict] = []
    for d in sorted(cfg.bundle_root.glob("issue_*")):
        if not d.is_dir() or state.state(d) != state.COMPLETE:
            continue
        row = check_bundle(cfg, d, fetch=fetch)
        if row is not None:
            rows.append(row)
    return rows
