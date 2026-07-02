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

from . import brief, publish, state
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


def _resolve_base(cfg: Config, d: Path, base: str) -> tuple[str, str, str]:
    """``(fetch_remote, fetch_ref, base_ref)`` — the branch the PR was ACTUALLY applied onto,
    resolved exactly as :mod:`publish` does so drift checks the same base publish committed to:
      * an ``Onto branch`` (stack-on-an-existing-PR, #54) → ``<remote>/<branch>``;
      * else the wave / ``Stacks on`` integration branch (#wave-model / #123) on ``origin``;
      * else the target base ``<base_remote>/<base>``.
    Checking the brief's target base for a *stacked* PR would report false clean/stale (#211
    review) — the PR really depends on the branch above, not on upstream ``main``."""
    onto = brief.onto_branch(d / "brief.md")
    if onto is not None:
        remote, branch = onto
        return remote, branch, f"{remote}/{branch}"
    stack_branch = publish._stack_base_branch(cfg, d)
    if stack_branch:
        return "origin", stack_branch, f"origin/{stack_branch}"
    return cfg.base_remote, base, f"{cfg.base_remote}/{base}"


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
    fetch_remote, fetch_ref, base_ref = _resolve_base(cfg, d, base)
    repo = publish._checkout_path(cfg, repo_spec)
    if not (repo / ".git").exists():
        return {"bundle": d.name, "pr_url": pr_url, "base": base_ref,
                "status": "error", "detail": f"no checkout at {repo}"}
    if fetch:
        f = _git(repo, "fetch", fetch_remote, fetch_ref)
        if f.returncode != 0:
            # A failed fetch (expired creds, deleted/renamed base, network) must NOT fall
            # through to a stale remote-tracking ref and mis-report apply-clean (#211 review).
            tail = (f.stderr.strip().splitlines()[-1:] or ["git fetch failed"])[0]
            return {"bundle": d.name, "pr_url": pr_url, "base": base_ref, "status": "error",
                    "detail": f"fetch {fetch_remote} {fetch_ref} failed: {tail}"[:200]}
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
