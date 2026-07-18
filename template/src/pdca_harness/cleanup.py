"""Reconcile bundle state with the issue tracker — ``pdca cleanup`` (issue #300).

A long-running instance drifts out of sync with its tracker: an issue gets closed
by decision in-thread while its bundle still sits in the pending list, a bundle
freezes COMPLETE while its issue stays open, a PR merges while the bundle is still
awaiting sign-off. This module is the deterministic reconciler: one read-only pass
computes a row per discrepancy with a planned action, and ``--apply`` executes the
narrow, auditable action set below. **Dry-run is the default.**

Reconciliation matrix (local state × remote state):

* issue CLOSED, bundle briefless (a notes-only tracker) → write the ``resolved``
  object into ``notes.json`` — the bundle reads RESOLVED (#302). An unparseable
  existing ``notes.json`` is skipped with a note (never clobber what we can't read).
* issue CLOSED, bundle AWAITING_SIGNOFF → record §9 ``discontinue`` (the same
  primitive as ``pdca signoff --discontinue``) → DISCONTINUED.
* issue CLOSED, bundle mid-flight (PLANNED/BUILT/CHECKED/ITERATE_*) → report only:
  fabricating a SUMMARY §9 for in-flight work is not auditable — finish or
  discontinue by hand.
* PR MERGED, bundle not COMPLETE → report only, always: auto-writing an accept
  would forge the human verdict past the C6 guard.
* issue OPEN, bundle COMPLETE with a merged PR → comment (the bundle's
  ``tracker-comment.md`` if present) + ``gh issue close --reason completed``.
* issue OPEN, bundle COMPLETE close/no-fix (empty patch) or DISCONTINUED →
  comment + ``gh issue close --reason "not planned"``.
* issue OPEN, bundle COMPLETE with an unmerged PR → report only (the issue stays
  open until the PR merges).

Fail-closed: ``gh`` missing/unauthenticated aborts before any write (rc 2); a
per-issue ``gh`` failure reports ``remote: unknown`` and never acts. GitHub-only
for the issue-side classes (a GitLab/other tracker gets a loud skip; the PR-side
merged-check still runs — it reads the recorded ``pr_url`` like merged.py does).
Exactly three write primitives exist: the ``notes.json`` merge, ``signoff.record``
+ ``driver.run_issue``, and ``gh issue comment``/``close``.

Do not run mid-flow: the discontinue path races a live sign-off session in theory;
this is a human-invoked maintenance command (same caveat as ``pdca sweep``).
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import driver, publish, signoff, state
from .config import Config

_MID_FLIGHT = (state.PLANNED, state.BUILT, state.CHECKED,
               state.ITERATE_DO, state.ITERATE_PLAN)


@dataclass
class _Row:
    bundle: str
    local: str
    remote: str
    plan: str                       # human-readable planned action ("-" = in sync note)
    apply: list = field(default_factory=list)   # zero or more thunks run under --apply


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _issue_state(number: str, repo: str) -> dict | None:
    """``{state, stateReason, closedAt}`` for the tracker issue, or None (unknown).

    Fail-closed like ``merged.is_merged``: any failure — gh error, unparseable
    JSON — is "unknown", and unknown never acts."""
    args = ["issue", "view", number, "--json", "state,stateReason,closedAt"]
    if repo:
        args += ["--repo", repo]
    proc = _gh(args)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("state") else None


def _pr_state(url: str) -> str:
    """``MERGED`` / ``OPEN`` / ``CLOSED`` for a recorded PR url, or ``""`` (unknown).
    The same probe revert.py uses; fail-closed."""
    proc = _gh(["pr", "view", url, "--json", "state"])
    if proc.returncode != 0:
        return ""
    try:
        return str(json.loads(proc.stdout).get("state", "") or "")
    except ValueError:
        return ""


def _github_tracker(cfg: Config) -> tuple[bool, str]:
    """(issue-side reconciliation possible, default --repo).

    Only a github ``[[plan.source]]`` that DECLARES ``role = "tracker"`` is canonical
    (#300 review) — that is exactly the meaning ``sources._is_tracker`` reserves for the
    role. A github source WITHOUT it is supplementary reading material (a linked spec
    repo, a mirror), and reconciling against it could comment on or close an unrelated
    issue that merely shares the numeric id. Fallback: the legacy ``[tracker].system``
    setting."""
    for src in cfg.plan_sources:
        if (isinstance(src, dict) and src.get("type") == "github"
                and (src.get("role") or "").strip().lower() == "tracker"):
            return True, str(src.get("repo", "") or "")
    return cfg.tracker_system == "github", ""


def _empty_patch(d: Path) -> bool:
    """The close/no-fix test publish uses: patch absent or whitespace-only."""
    patch = d / "patch.diff"
    return not (patch.is_file() and patch.read_text(encoding="utf-8").strip())


def _mark_resolved(d: Path, remote: dict, today: str) -> bool:
    """Merge the #302 ``resolved`` object into notes.json (create if absent).
    False (skip) when an existing notes.json is unreadable — never clobber it."""
    notes = d / "notes.json"
    data: dict = {}
    if notes.exists():
        try:
            loaded = json.loads(notes.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
        if not isinstance(loaded, dict):
            return False
        data = loaded
    data["resolved"] = {
        "github_state": remote.get("state", ""),
        "state_reason": remote.get("stateReason", "") or "",
        "closed_at": remote.get("closedAt", "") or "",
        "note": f"tracker issue closed upstream; recorded by pdca cleanup {today}",
    }
    notes.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _discontinue(cfg: Config, d: Path, remote: dict, *, by: str, today: str) -> bool:
    reason = remote.get("stateReason", "") or "closed"
    signoff.record(d / "SUMMARY.md", action="discontinue", by=by or cfg.author or "pdca cleanup",
                   date=today, delta=f"tracker issue closed upstream ({reason}, "
                                     f"{remote.get('closedAt', '') or 'no date'}) — pdca cleanup")
    driver.run_issue(d, cfg)
    return True


def _close_issue(d: Path, number: str, repo: str, *, reason: str, fallback_body: str) -> bool:
    """Close the issue with the comment ATTACHED — one ``gh issue close --comment`` call
    (#300 review). The two-step comment-then-close left a partial state on a transient
    close failure: the comment was already posted, and a ``--apply`` retry posted it
    again — spamming the tracker and breaking the advertised idempotence. A single call
    either does everything or (on failure) has posted nothing to retry around. The
    bundle's ``tracker-comment.md`` is preferred as the body, else the fallback."""
    repo_args = ["--repo", repo] if repo else []
    comment = d / "tracker-comment.md"
    body = fallback_body
    if comment.is_file() and comment.read_text(encoding="utf-8").strip():
        body = comment.read_text(encoding="utf-8").strip()
    r = _gh(["issue", "close", number, *repo_args, "--reason", reason, "--comment", body])
    if r.returncode != 0:
        print(f"cleanup: issue_{number}: close failed: {r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _unresolve(d: Path) -> bool:
    """Drop the ``resolved`` marker from notes.json — the tracker REOPENED the issue, so
    the terminal resolution no longer holds and the bundle must return to the pending
    set (#300 review). Tolerant read; False when there is nothing safe to change."""
    notes = d / "notes.json"
    try:
        data = json.loads(notes.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if not isinstance(data, dict) or "resolved" not in data:
        return False
    del data["resolved"]
    notes.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _plan_bundle(cfg: Config, d: Path, *, issue_side: bool, repo: str,
                 by: str, today: str) -> _Row | None:
    """The reconciliation row for one bundle, or None when local and remote agree."""
    st = state.state(d)
    number = d.name.removeprefix("issue_")
    numeric = number.isdigit()
    if st == state.RESOLVED:
        # NOT unconditionally in sync (#300 review): the tracker can REOPEN an issue
        # after cleanup resolved its bundle, and RESOLVED ∈ HALTED would then suppress
        # the reopened work forever. Re-check the remote; an OPEN issue clears the
        # marker (the bundle returns to the pending set for the next Plan).
        if not issue_side or not numeric:
            return None
        remote = _issue_state(number, repo)
        if remote is None:
            return _Row(d.name, st, "unknown", "tracker state unreadable (gh failed) — no action")
        if remote.get("state") == "OPEN":
            return _Row(d.name, st, "OPEN",
                        "issue REOPENED after resolution — clear the resolved marker "
                        "so the tracker item is pending again",
                        apply=[lambda: _unresolve(d)])
        return None                                  # still closed: in sync

    # PR-side (class b): tracker-independent — reads the recorded pr_url like merged.py.
    record = publish._publish_record(d) or {}
    pr_url = str(record.get("pr_url", "") or "")
    if st != state.COMPLETE and pr_url and _pr_state(pr_url) == "MERGED":
        return _Row(d.name, st, "PR MERGED",
                    f"PR merged but bundle is {st} — reconcile by hand "
                    f"(`pdca signoff {number} --accept` after your own review); "
                    "cleanup never forges the human verdict (C6)")

    if not issue_side:
        return None
    if not numeric:
        return _Row(d.name, st, "-", "non-numeric id — no tracker issue; skipped")

    remote = _issue_state(number, repo)
    if remote is None:
        return _Row(d.name, st, "unknown", "tracker state unreadable (gh failed) — no action")

    if remote.get("state") == "CLOSED":
        if not (d / "brief.md").exists():
            if st == state.UNPLANNED:
                if (d / "notes.json").exists():
                    try:
                        json.loads((d / "notes.json").read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        return _Row(d.name, st, "CLOSED",
                                    "notes.json unreadable — NOT marking resolved "
                                    "(fix or remove it first)")
                return _Row(d.name, st, "CLOSED", "mark RESOLVED (write notes.json "
                            "resolved object, #302)",
                            apply=[lambda: _mark_resolved(d, remote, today)])
            return None
        if st == state.AWAITING_SIGNOFF:
            return _Row(d.name, st, "CLOSED",
                        "record §9 discontinue (tracker closed upstream)",
                        apply=[lambda: _discontinue(cfg, d, remote, by=by, today=today)])
        if st in _MID_FLIGHT:
            return _Row(d.name, st, "CLOSED",
                        f"issue closed upstream while {st} — finish or discontinue by "
                        f"hand (`pdca flow {number}`, then `pdca signoff {number} "
                        "--discontinue`)")
        return None                                  # COMPLETE/DISCONTINUED + closed: in sync

    if remote.get("state") == "OPEN":
        if st == state.COMPLETE:
            if _empty_patch(d):
                return _Row(d.name, st, "OPEN",
                            "close as not planned (accepted close/no-fix disposition)",
                            apply=[lambda: _close_issue(
                                d, number, repo, reason="not planned",
                                fallback_body="Closed as not planned: the review "
                                              "concluded a close/no-fix disposition "
                                              "(see the cycle records).")])
            if pr_url and _pr_state(pr_url) == "MERGED":
                return _Row(d.name, st, "OPEN",
                            "comment + close as completed (fix merged)",
                            apply=[lambda: _close_issue(
                                d, number, repo, reason="completed",
                                fallback_body=f"Fixed by {pr_url} (merged).")])
            return _Row(d.name, st, "OPEN",
                        "PR not merged (or not published) — issue stays open until merge")
        if st == state.DISCONTINUED:
            why = signoff.iteration_delta(d / "SUMMARY.md") or "discontinued at sign-off"
            return _Row(d.name, st, "OPEN", "close as not planned (discontinued locally)",
                        apply=[lambda: _close_issue(
                            d, number, repo, reason="not planned",
                            fallback_body=f"Closed as not planned: {why}")])
        return None                                  # open issue, work in flight: in sync
    return _Row(d.name, st, str(remote.get("state", "?")), "unrecognized tracker state — no action")


def run(cfg: Config, ids: list[str], *, apply: bool = False, repo: str = "",
        by: str = "", today: str = "") -> int:
    """Reconcile bundles against the tracker; report (default) or ``--apply``."""
    today = today or datetime.date.today().isoformat()
    if ids:
        # find_bundle resolves the archived completed/ path too (#171 convention).
        bundles = [cfg.find_bundle(i) for i in ids]
        missing = [d.name for d in bundles if not d.is_dir()]
        if missing:
            print(f"cleanup: no such bundle(s): {', '.join(missing)}", file=sys.stderr)
            return 2
    else:
        # The archived completed/ bundles (#171, the manual archive convention) are
        # exactly the locally-terminal cases class (c) exists to close (#300 review) —
        # sweep them too, not just the active top level.
        roots = (cfg.bundle_root, cfg.bundle_root / "completed")
        bundles = sorted(d for root in roots if root.exists()
                         for d in root.glob("issue_*") if d.is_dir())
    if not bundles:
        print("cleanup: no bundles found")
        return 0

    issue_side, default_repo = _github_tracker(cfg)
    repo = repo or default_repo
    if not issue_side:
        print(f"cleanup: tracker '{cfg.tracker_system or 'unset'}' is not GitHub — "
              "issue-state reconciliation skipped; PR-side checks still run",
              file=sys.stderr)

    # Preflight (fail-closed, before any loop): every class needs gh.
    if shutil.which("gh") is None:
        print("cleanup: `gh` not found — install the GitHub CLI first", file=sys.stderr)
        return 2
    if _gh(["auth", "status"]).returncode != 0:
        print("cleanup: `gh auth status` failed — run `gh auth login` first", file=sys.stderr)
        return 2

    rows = [r for d in bundles
            if (r := _plan_bundle(cfg, d, issue_side=issue_side, repo=repo,
                                  by=by, today=today)) is not None]
    if not rows:
        print(f"cleanup: {len(bundles)} bundle(s) checked — all in sync with the tracker")
        return 0

    failed = 0
    for r in rows:
        prefix = "" if (apply and r.apply) else ("would: " if r.apply else "note: ")
        print(f"{r.bundle} [{r.local} / tracker {r.remote}] — {prefix}{r.plan}")
        if apply and r.apply:
            ok = all(fn() for fn in r.apply)
            if not ok:
                failed += 1
                print(f"  ✗ {r.bundle}: action failed (see above)", file=sys.stderr)
    if not apply and any(r.apply for r in rows):
        print("\ncleanup: dry run — re-run with --apply to act on the 'would:' lines")
    return 1 if failed else 0
