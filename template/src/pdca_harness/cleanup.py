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

from . import brief, driver, publish, signoff, sources, state
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
    The same probe revert.py uses; fail-closed — including a successful ``gh`` (or
    shim) emitting valid NON-OBJECT JSON (``null``, ``[]``), which must read as
    unknown instead of an AttributeError aborting the whole sweep mid-plan
    (#300 review round 8; mirrors ``_issue_state``'s shape check)."""
    proc = _gh(["pr", "view", url, "--json", "state"])
    if proc.returncode != 0:
        return ""
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return ""
    return str(data.get("state", "") or "") if isinstance(data, dict) else ""


def _github_tracker(cfg: Config) -> tuple[bool, str]:
    """(issue-side reconciliation possible, default --repo).

    Delegates to :func:`sources.tracker_github_repo` (#300 review round 5) — the single
    resolution both cleanup and the flow's reopen revalidation use. Key semantics: a
    tracker-role plan.source is canonical and SUPPRESSES the legacy ``[tracker].system``
    fallback whatever its type (a gitlab tracker-role source means the tracker is
    gitlab even if ``[tracker].system`` still says github), and all comparisons use the
    normalization ``sources.seed`` applies."""
    return sources.tracker_github_repo(cfg)


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
    # VERIFY the transition (#300 review round 7): a malformed/customized SUMMARY.md
    # without the canonical §9 fields makes signoff.record substitute nothing and
    # run_issue leave the bundle AWAITING_SIGNOFF — reporting success would let
    # `cleanup --apply` exit 0 over a bundle it did not actually reconcile.
    if state.state(d) != state.DISCONTINUED:
        print(f"cleanup: {d.name}: discontinue did not take (SUMMARY.md is missing the "
              f"canonical §9 fields?) — bundle left {state.state(d)}", file=sys.stderr)
        return False
    return True


def _close_issue(d: Path, number: str, repo: str, *, reason: str, fallback_body: str) -> bool:
    """Close the issue with the comment attached, idempotently under retries.

    One ``gh issue close --comment`` call (#300 review) — but that is still TWO API
    operations under the hood, not a transaction (#300 review round 5): the comment can
    land and the close still fail transiently. So before closing, probe the issue's
    existing comments for OUR exact body; when it is already there, close WITHOUT the
    comment — a ``--apply`` retry never reposts. The bundle's ``tracker-comment.md`` is
    preferred as the body, else the fallback. A failing probe degrades to posting (one
    possible duplicate only in the double-failure case — fail toward completing the
    close, never toward losing the comment)."""
    repo_args = ["--repo", repo] if repo else []
    comment = d / "tracker-comment.md"
    body = fallback_body
    if comment.is_file() and comment.read_text(encoding="utf-8").strip():
        body = comment.read_text(encoding="utf-8").strip()
    already = False
    probe = _gh(["issue", "view", number, *repo_args, "--json", "comments"])
    if probe.returncode == 0:
        # Shape-tolerant like every other gh probe (#300 review round 9): a
        # successful gh (or shim) emitting non-object JSON — or a null entry inside
        # `comments` — must degrade to `already = False` (post the comment with the
        # close), never raise AttributeError and mark the row failed WITHOUT closing.
        try:
            decoded = json.loads(probe.stdout)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict) and isinstance(decoded.get("comments"), list):
            already = any(isinstance(c, dict) and (c.get("body") or "").strip() == body
                          for c in decoded["comments"])
    args = ["issue", "close", number, *repo_args, "--reason", reason]
    if not already:
        args += ["--comment", body]
    r = _gh(args)
    if r.returncode != 0:
        print(f"cleanup: issue_{number}: close failed: {r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _unresolve(d: Path) -> bool:
    """The tracker REOPENED the issue: retire the closure-era notes.json WHOLESALE via
    :func:`sources.clear_resolved_marker` (#300 review round 6). Deleting only the
    ``resolved`` key would leave the stale pre-reopen file in place — ``ensure_notes``
    and the tracker-role seed refuse to replace an existing notes.json, so the next
    Plan would brief from the thread that PRECEDED the reopen and miss the very
    comments that caused it. Tolerant read; False when there is nothing safe to
    change (or the set-aside rename could not be performed)."""
    notes = d / "notes.json"
    try:
        data = json.loads(notes.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if not isinstance(data, dict) or "resolved" not in data:
        return False
    sources.clear_resolved_marker(d)          # unique set-aside name, kept inspectable
    return not notes.exists()


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
    # A decodable-but-non-object publish.json (`[]`, `null`) must read as "no record",
    # not abort the whole sweep on `.get` (#300 review round 2) — one damaged bundle
    # must never block every other bundle's reconciliation.
    record = publish._publish_record(d)
    record = record if isinstance(record, dict) else {}
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
        # "Notes-only" uses the SAME placeholder semantics as state.state() (#300 review
        # round 2): an unfilled template copy is "never authored" (#113), so a closed
        # tracker bundle carrying one still takes the RESOLVED path — a bare existence
        # test left it UNPLANNED-with-no-row, unreconcilable forever.
        bp = d / "brief.md"
        if not bp.exists() or brief.is_placeholder(bp):
            # "Briefless" is not "notes-only". An iterate-to-Plan ARCHIVES brief.md, so a
            # bundle mid-cycle with a full iteration history is briefless too (#334).
            # Writing a `resolved` object there cannot transition it — `is_resolved` now
            # refuses the marker on the cycle evidence — so `cleanup --apply` would report
            # a successful mutation, change nothing, and propose the identical action on
            # every subsequent run. Report the live cycle instead of trying to resolve it.
            if st == state.UNPLANNED and state.has_cycle_evidence(d):
                return _Row(d.name, st, "CLOSED",
                            "tracker closed, but this bundle has an in-flight cycle "
                            "(iteration history / Do+Check artifacts) — NOT marking "
                            "resolved; finish or discontinue it deliberately")
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
            # A recorded MERGED PR is checked FIRST (#300 review round 14): it is
            # definitive evidence a fix shipped, while an absent/blank patch.diff may
            # merely be damage (deleted, truncated) — closing such a bundle as "not
            # planned" would record the wrong reason and a misleading no-fix comment.
            pr_state = _pr_state(pr_url) if pr_url else ""
            if pr_state == "MERGED":
                return _Row(d.name, st, "OPEN",
                            "comment + close as completed (fix merged)",
                            apply=[lambda: _close_issue(
                                d, number, repo, reason="completed",
                                fallback_body=f"Fixed by {pr_url} (merged).")])
            if pr_url and not pr_state:
                # An UNREADABLE PR state with a recorded pr_url is report-only (#300
                # review round 15): the PR may in fact be merged and the blank patch
                # mere local damage — a transient gh failure must never route this
                # bundle into the destructive not-planned close below.
                return _Row(d.name, st, "OPEN",
                            "recorded PR state unreadable (gh failed) — no action; "
                            "retry (a merged PR closes as completed, never "
                            "'not planned')")
            if _empty_patch(d):
                return _Row(d.name, st, "OPEN",
                            "close as not planned (accepted close/no-fix disposition)",
                            apply=[lambda: _close_issue(
                                d, number, repo, reason="not planned",
                                fallback_body="Closed as not planned: the review "
                                              "concluded a close/no-fix disposition "
                                              "(see the cycle records).")])
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
        # Dedupe, order-preserving (#300 review round 4): `cleanup 21 21 --apply` would
        # otherwise plan and RUN the close mutation twice — duplicating the closing
        # comment or failing after the first action already succeeded.
        ids = list(dict.fromkeys(ids))
        # find_bundle resolves the archived completed/ path too (#171 convention).
        bundles = [cfg.find_bundle(i) for i in ids]
        missing = [d.name for d in bundles if not d.is_dir()]
        if missing:
            print(f"cleanup: no such bundle(s): {', '.join(missing)}", file=sys.stderr)
            return 2
    else:
        # The archived completed/ bundles (#171, the manual archive convention) are
        # exactly the locally-terminal cases class (c) exists to close (#300 review) —
        # sweep them too, not just the active top level. Deduped by issue id with the
        # ACTIVE directory winning (#300 review round 3, Config.find_bundle semantics):
        # an issue reopened into a new active cycle must be reconciled against that
        # cycle, never against its stale archived copy — which could otherwise close
        # the reopened tracker issue while the active bundle is still in flight.
        active = {d.name: d for d in cfg.bundle_root.glob("issue_*") if d.is_dir()} \
            if cfg.bundle_root.exists() else {}
        archived_root = cfg.bundle_root / "completed"
        archived = {d.name: d for d in archived_root.glob("issue_*")
                    if d.is_dir() and d.name not in active} if archived_root.exists() else {}
        bundles = [d for _name, d in sorted({**archived, **active}.items())]
    if not bundles:
        print("cleanup: no bundles found")
        return 0

    issue_side, default_repo = _github_tracker(cfg)
    repo = repo or default_repo
    if not issue_side:
        print(f"cleanup: tracker '{cfg.tracker_system or 'unset'}' is not GitHub — "
              "issue-state reconciliation skipped; PR-side checks still run",
              file=sys.stderr)
    elif not repo:
        # FAIL CLOSED on an unknown tracker repository (#300 review round 14):
        # letting `gh issue view/close` fall back to the harness checkout's default
        # repo could inspect — and under --apply CLOSE — an unrelated same-numbered
        # issue. Issue-side reconciliation needs a derived or explicit repo.
        print("cleanup: the GitHub tracker's repository could not be derived "
              "([tracker].url unset/unparseable, no [[plan.source]] repo) — "
              "issue-side reconciliation skipped; pass --repo OWNER/REPO to enable "
              "it (gh's checkout-default repo could hold unrelated same-numbered "
              "issues)", file=sys.stderr)
        issue_side = False

    # Preflight (fail-closed, before any loop): every class needs gh.
    if shutil.which("gh") is None:
        print("cleanup: `gh` not found — install the GitHub CLI first", file=sys.stderr)
        return 2
    if _gh(["auth", "status"]).returncode != 0:
        print("cleanup: `gh auth status` failed — run `gh auth login` first", file=sys.stderr)
        return 2

    # Planning is isolated PER BUNDLE like the apply loop below (#300 review round
    # 10): one damaged bundle (a non-UTF-8 patch.diff, an unreadable artifact) must
    # become its own report-only row, never abort the sweep before the healthy
    # siblings are even planned.
    rows = []
    for d in bundles:
        try:
            r = _plan_bundle(cfg, d, issue_side=issue_side, repo=repo, by=by, today=today)
        except Exception as exc:  # noqa: BLE001 — isolate the bundle, keep planning
            rows.append(_Row(d.name, "unreadable", "unknown",
                             f"planning failed ({type(exc).__name__}: {exc}) — "
                             "no action; inspect the bundle's artifacts"))
            continue
        if r is not None:
            rows.append(r)
    if not rows:
        print(f"cleanup: {len(bundles)} bundle(s) checked — all in sync with the tracker")
        return 0

    failed = 0
    for r in rows:
        prefix = "" if (apply and r.apply) else ("would: " if r.apply else "note: ")
        print(f"{r.bundle} [{r.local} / tracker {r.remote}] — {prefix}{r.plan}")
        if apply and r.apply:
            # One damaged bundle (unwritable notes.json, an artifact read blowing up
            # mid-close) must be REPORTED as failed, never abort the sweep past the
            # remaining healthy rows (#300 review round 7) — the same isolation
            # contract the flow gives per-bundle steps.
            try:
                ok = all(fn() for fn in r.apply)
            except Exception as exc:  # noqa: BLE001 — isolate the row, keep sweeping
                ok = False
                print(f"  ✗ {r.bundle}: {type(exc).__name__}: {exc}", file=sys.stderr)
            if not ok:
                failed += 1
                print(f"  ✗ {r.bundle}: action failed (see above)", file=sys.stderr)
    if not apply and any(r.apply for r in rows):
        print("\ncleanup: dry run — re-run with --apply to act on the 'would:' lines")
    return 1 if failed else 0
