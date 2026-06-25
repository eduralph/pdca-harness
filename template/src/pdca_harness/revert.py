"""Revert — undo a published contribution (issue #158).

``pdca revert <id>`` reads the bundle's recorded ``publish.json`` and undoes the
contribution:

- the PR is **MERGED** → open a **revert PR** that reverse-applies the bundle's own
  ``patch.diff`` onto the base (``git apply --reverse``) — deterministic, no guessing the
  merge commit or the ``-m`` mainline — pushed as a fresh **draft** PR.
- the PR is **OPEN** (never landed) → **withdraw** it: ``gh pr close --delete-branch``.
- the PR is already **CLOSED** → nothing to do.

Records ``revert.json`` in the bundle. ``--dry-run`` prints the git/gh plan without
mutating anything (it still reads the PR state). Fail-closed and loud, like publish/merge;
STOP discipline holds — a revert PR opens as a draft for the human to merge. The mechanics
are deterministic ``git``/``gh`` subprocesses (no model), reusing the publish helpers.
"""

from __future__ import annotations

import datetime
import json
import shlex
import subprocess
import sys
from pathlib import Path

from . import publish
from .config import Config

REVERT_JSON = "revert.json"


def revert(cfg: Config, issue_id: str, *, dry_run: bool = False, by: str = "",
           today: str | None = None) -> int:
    """Undo the bundle's published contribution; return a process code."""
    d = cfg.bundle(issue_id)
    today = today or datetime.date.today().isoformat()
    rec = publish._publish_record(d)
    pr_url = rec.get("pr_url") if rec else None
    if not pr_url:
        print(f"revert: {d.name} has no recorded PR (nothing published to revert)",
              file=sys.stderr)
        return 1
    pr_state = _pr_state(pr_url)
    if pr_state is None:
        print(f"revert: could not read PR state for {pr_url}; aborting", file=sys.stderr)
        return 1
    if pr_state == "MERGED":
        return _revert_merged(cfg, d, issue_id, rec, pr_url, dry_run=dry_run, by=by, today=today)
    if pr_state == "OPEN":
        # ``mode: "stacked"`` (Onto branch, #54) means the harness appended a commit to a
        # PRE-EXISTING PR it did NOT create. Withdrawing it would `gh pr close
        # --delete-branch` that collaborator's whole PR branch — never do that. (The merged
        # path is still safe: it opens a *new* revert PR, leaving the original alone.)
        if rec.get("mode") == "stacked":
            print(f"revert: {d.name} was published as a commit onto an existing PR "
                  f"({pr_url}, mode=stacked) the harness did not create — refusing to close "
                  "it. Revert just that commit on the PR branch by hand.", file=sys.stderr)
            return 1
        return _withdraw(cfg, d, pr_url, dry_run=dry_run, by=by, today=today)
    print(f"revert: {d.name}'s PR is {pr_state} — nothing to revert ({pr_url}).")
    return 0


def _pr_state(pr_url: str) -> str | None:
    """The recorded PR's state via ``gh pr view`` (``MERGED`` / ``OPEN`` / ``CLOSED``), or
    None on a gh failure (the caller aborts — never reverts blind)."""
    r = subprocess.run(["gh", "pr", "view", str(pr_url), "--json", "state"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout or "{}").get("state")
    except ValueError:
        return None


def _commit_summary(d: Path, issue_id: str) -> str:
    """The contribution's commit subject (for the revert title), or a fallback."""
    msg = d / publish.COMMIT_MSG
    if msg.is_file():
        lines = msg.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()
    return f"contribution for {issue_id}"


def _record(d: Path, rec: dict) -> None:
    (d / REVERT_JSON).write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")


def _revert_merged(cfg: Config, d: Path, issue_id: str, pub: dict, pr_url: str, *,
                   dry_run: bool, by: str, today: str) -> int:
    """Open a draft revert PR that reverse-applies the bundle's patch.diff onto the base."""
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        print(f"revert: {d.name} has no patch.diff to reverse (close/no-fix had nothing to "
              "land) — nothing to revert.", file=sys.stderr)
        return 1
    repo_spec = pub.get("repo", "")
    base = pub.get("base", "")
    repo = publish._checkout_path(cfg, repo_spec)
    base_remote = cfg.base_remote
    rev_branch = f"revert/{issue_id}"
    summary = _commit_summary(d, issue_id)
    git = lambda *a: ["git", "-C", str(repo), *a]
    steps = [
        git("fetch", base_remote),
        git("checkout", "-B", rev_branch, f"{base_remote}/{base}"),
        git("apply", "--reverse", str(patch.resolve())),
        git("add", "--all"),
        git("commit", "-s", "-m",
            f"Revert: {summary}\n\nReverts the change contributed for {issue_id} ({pr_url})."),
        git("push", "--force-with-lease", "-u", "origin", rev_branch),
    ]
    if dry_run:
        print(f"revert --dry-run — {d.name}: open a draft revert PR on {repo_spec} "
              f"({rev_branch} → {base}):")
        for c in steps:
            print("  " + " ".join(shlex.quote(x) for x in c))
        print(f"  gh pr create --draft --repo {repo_spec} --base {base} "
              f"--head <fork-owner>:{rev_branch} --title {shlex.quote('Revert: ' + summary)}")
        return 0

    rc = publish._check_repo(repo, repo_spec, required_remotes={base_remote, "origin"})
    if rc != 0:
        return rc
    orig = publish._current_ref(repo)
    stashed = publish._stash_worktree(repo)
    try:
        for c in steps:
            print("→ " + " ".join(c[3:]))
            if subprocess.run(c).returncode != 0:
                print(f"revert: step failed: {' '.join(c)}", file=sys.stderr)
                return 1
    finally:
        publish._restore_worktree(repo, orig, stashed)

    head = f"{publish._fork_owner(repo) or repo_spec.split('/')[0]}:{rev_branch}"
    pr_cmd = ["gh", "pr", "create", "--draft", "--repo", repo_spec, "--base", base,
              "--head", head, "--title", f"Revert: {summary}",
              "--body", f"Reverts the contribution for {issue_id} ({pr_url}) by "
              f"reverse-applying the recorded patch onto `{base}`."]
    r = subprocess.run(pr_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        print("revert: branch pushed but `gh pr create` FAILED — open the revert PR by "
              "hand. This is NOT done.", file=sys.stderr)
        return 1
    revert_pr = ((r.stdout or "").strip().splitlines() or [""])[-1]
    _record(d, {"action": "revert-pr", "reverts": pr_url, "branch": rev_branch,
                "revert_pr": revert_pr, "base": base, "repo": repo_spec,
                "by": by or cfg.author or "unknown", "date": today})
    print(f"\nDraft revert PR opened on {repo_spec} ({rev_branch} → {base}).\n  {revert_pr}")
    print("  STOP: review CI, then mark it ready / merge yourself — the human's step.")
    return 0


def _withdraw(cfg: Config, d: Path, pr_url: str, *, dry_run: bool, by: str,
              today: str) -> int:
    """Withdraw an unmerged contribution: close the PR and delete its branch."""
    cmd = ["gh", "pr", "close", str(pr_url), "--delete-branch"]
    if dry_run:
        print(f"revert --dry-run — {d.name}: withdraw the unmerged PR: {' '.join(cmd)}")
        return 0
    print(f"→ gh pr close {pr_url} --delete-branch")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        print(f"revert: could not close {pr_url} — close it by hand.", file=sys.stderr)
        return 1
    _record(d, {"action": "withdraw", "reverts": pr_url,
                "by": by or cfg.author or "unknown", "date": today})
    print(f"Withdrew the unmerged PR {pr_url} (closed + branch deleted).")
    return 0
