"""Auto-merge mode for wave sequencing (#wave-model, opt-in) — merge each wave's PRs so
the next wave builds on the genuinely-merged base.

The **default** sequencing folds accepted work onto an integration branch without merging
(fork-safe, STOP discipline intact — see :mod:`integrate`). For an own-repo /
continuous-delivery target where "landed in the base" is the deliverable *and* the operator
has merge rights on ``base_remote``, ``[driver].wave_mode = "merge"`` instead merges each
non-final wave's PRs (``gh pr merge``) and fetches the base, so the next wave's Do worktree
(which resets to ``<base_remote>/<base>``) builds on the merged result.

Fail-closed: a PR that does not merge — a conflict, a failing required check, no merge
rights — returns non-zero so the caller STOPs; the next wave must never build on an
unmerged base. Idempotent (a resumed run skips an already-merged PR). Merging is
deterministic ``git``/``gh`` (no model); dry-run (stubbed publisher) prints the plan and
merges nothing. The harness's own ``gh pr merge`` runs in the orchestrator, outside the
``builder_guard`` hook that blocks the model leaves from merging — exactly as publish's
``gh pr create`` does.

"Never on an unmerged base" is only half the rule: the next wave must never build on a
base whose verification was not GREEN either, and ``gh pr merge``'s own refusal cannot
carry that (issue #413). It fails closed only on the checks the HOST repo marks *required*
in branch protection, so on a thinly-protected host a red non-required job — or a run
still in flight — merges anyway. Correctness here must not hinge on per-instance host
config, so ``_merge_one`` reads the PR's FULL check rollup itself (``gh pr checks``) and
refuses on any failing, pending or missing check. The read happens AFTER ``gh pr ready``
and immediately before ``gh pr merge``: marking a draft ready can itself trigger
``ready_for_review`` CI, so a rollup observed only pre-ready cannot promise green at merge
time. Refusing after the ready-mark is safe — a re-run resumes idempotently. An EMPTY
rollup refuses too (absence of evidence is not green); skipped/neutral checks are
completed non-failures and do not block. ``[driver].merge_requires = "required"``
(default ``"all"``) opts back into host-config-only semantics, skipping the gate.

A wave boundary fires SECONDS after the PR was opened (issue #462: getwyrd/wyrd#703, six
seconds between create and ready_for_review), so the FIRST rollup read above is routinely
``pending`` or ``empty`` — not a verdict, just evidence that has not arrived yet.
``_wait_for_green`` re-reads the rollup until it resolves or ``[driver].merge_wait_secs``
(default 300; ``0`` disables the wait — the original immediate-refusal behaviour) of
wall-clock time elapses, through the patchable ``_sleep`` below so a test costs no real
time. A ``green`` read is confirmed once before it is believed (issue #582): the rollup
lists only the checks registered so far, so a fast check that passed can read green while
a slow job has not reported yet. The wait re-reads one full poll interval later and merges
only if that read is green too; a green seen with less than a poll interval of budget left
to confirm it refuses as pending.
Whichever way ``_merge_one`` declines to merge a PR it already readied — the rollup
never resolving green, a failing ``gh pr merge`` — ``_undo_ready`` marks it back to draft
(``gh pr ready --undo``) before returning, so a stopped wave never leaves a PR advertising
a readiness no human granted (``docs/INTEGRATION.md`` §10).

A green rollup also says nothing about the BASE it was earned against (issue #531). The
wave's PRs merge one after another, so once an earlier member lands, a later member's head
— and its rollup — still describe the old base: merging it lands a combination nothing
verified (on a host whose branch protection does not require up-to-date branches,
``strict`` off), or the host refuses it and the wave stops after its first merge (``strict``
on). Whether that happens must not hinge on ``strict`` any more than on required checks, so
under the default ``merge_requires = "all"`` ``_merge_one`` decides, after the ready-mark,
whether the PR is behind its base, in plain git as ``publish._line_tip_refusal`` does
(#593): read the PR's head from the host, fetch, record the base branch's tip, and ask
``git merge-base --is-ancestor <tip> <head>`` (``_base_read``). A PR that is behind is
brought up to date with a merge-commit update of its own branch (``gh pr update-branch``,
never ``--rebase`` — a rebase rewrites the commits sign-off reviewed — whatever
``merge_method`` is). GitHub may finish that update after the command returns, so
``_wait_for_update`` re-reads the head until it contains the recorded tip, charged to the
same ``merge_wait_secs`` budget the rollup wait then gets for the NEW head. After the green
the head and base are read again: the head must be the one read before the wait (so the
green was that head's) and must not be behind, and ``gh pr merge`` is pinned to it
(``--match-head-commit``), so a head that changes after the green read is refused by the
host instead of merged. Every refusal on that path undoes the ready-mark. The base moving
between the last read and the host executing the merge is a gap no client can close: a
``strict`` host refuses that merge, a non-strict one would merge it.
``merge_requires = "required"`` (trust the host's protection) is unchanged: no base read,
no update, no pin.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from . import merged, publish, state
from .config import Config

# Patchable indirection so a test can drive the wait loop below with no real wall-clock
# cost (issue #462) — tests replace this, never `time.sleep` itself.
_sleep = time.sleep

# `gh pr checks --json name,bucket` classifies every check into one of five buckets:
# pass | fail | pending | skipping | cancel (`gh pr checks --help`). "pass" and "skipping"
# (skipped/neutral) are completed non-failures and do not block; "pending" (running or
# queued) always blocks; everything else — "fail", "cancel", or a bucket a later gh grows
# that this harness has never heard of — counts as failing, because the fail-safe direction
# is to refuse, never to guess green on a bucket we cannot interpret.
_ROLLUP_OK = frozenset({"pass", "skipping"})
_ROLLUP_PENDING = frozenset({"pending"})


def merge_wave(cfg: Config, bundles: list[Path], *, dry_run: bool = False,
               method: str = "merge") -> int:
    """Merge each accepted bundle's PR into its base, then fetch the base. Return 0 iff
    every bundle merged (or had nothing to merge); non-zero (STOP) on the first failure."""
    fetched: set[str] = set()
    for d in bundles:
        rc = _merge_one(cfg, d, dry_run=dry_run, method=method, fetched=fetched)
        if rc:
            return rc
    return 0


def _check_rollup(pr_url: str) -> tuple[str, str]:
    """Classify PR ``pr_url``'s FULL check rollup (issue #413). Returns
    ``(verdict, detail)``; only ``"green"`` may merge.

    * ``"green"``      — every reported check completed without failing (pass, or
      skipped/neutral); ``detail`` counts what was verified, for the run log.
    * ``"pending"``    — at least one check is still running or queued.
    * ``"failing"``    — at least one check failed, was cancelled, or reports a bucket
      this harness does not recognise.
    * ``"empty"``      — no checks were reported at all; absence of evidence is not green.
    * ``"unreadable"`` — ``gh`` could not enumerate the checks (auth, network, a ``gh``
      too old for ``--json``). Fail-closed, same as a failing check.

    ``gh pr checks`` prints the JSON *and then* sets an exit code summarising the rollup
    — 0 all passed, 1 something failed, 8 something is pending (``gh help exit-codes``) —
    so the exit code is not evidence of an error and the buckets, not the code, are what
    is classified. This needs a ``gh`` whose ``pr checks`` supports ``--json`` with the
    documented ``bucket`` field; one too old for it exits non-zero printing no JSON, which
    lands in ``unreadable`` and refuses — no version floor to enforce, because the
    degradation is already fail-closed.
    """
    r = subprocess.run(["gh", "pr", "checks", str(pr_url), "--json", "name,bucket"],
                       capture_output=True, text=True)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if not out:
        # No JSON at all. gh reports a rollup with nothing in it as an error ("no checks
        # reported on the '<branch>' branch") rather than an empty list, so recognise that
        # one shape as EMPTY for a truthful message; anything else is unreadable. Both
        # refuse under the default, so a gh that reworded the message costs a message, not
        # a wrong merge.
        if r.returncode == 0 or "no checks reported" in err.lower():
            return "empty", err or "no checks reported"
        return "unreadable", err or f"`gh pr checks` exited {r.returncode}"
    try:
        checks = json.loads(out)
    except ValueError:
        return "unreadable", f"unparsable `gh pr checks` output: {out[:200]}"
    if not isinstance(checks, list):
        return "unreadable", f"unexpected `gh pr checks` payload: {out[:200]}"
    if not checks:
        return "empty", "no checks reported"
    failing = [c for c in checks if _bucket(c) not in _ROLLUP_OK | _ROLLUP_PENDING]
    if failing:
        return "failing", _names(failing)
    waiting = [c for c in checks if _bucket(c) in _ROLLUP_PENDING]
    if waiting:
        return "pending", _names(waiting)
    return "green", f"{len(checks)} check{'' if len(checks) == 1 else 's'}"


def _bucket(check: object) -> str:
    return str(check.get("bucket") or "") if isinstance(check, dict) else ""


def _names(checks: list) -> str:
    return ", ".join(
        f"{(c.get('name') if isinstance(c, dict) else None) or '?'} ({_bucket(c) or '?'})"
        for c in checks)


def _wait_for_green(pr_url: str, wait_secs: int, *, poll_interval: int = 15,
                    spent: int = 0) -> tuple[str, str]:
    """Re-read ``pr_url``'s check rollup (``_check_rollup``) until it clears ``pending``/
    ``empty`` or ``wait_secs`` of (patchable) wall-clock time is exhausted (issue #462).
    Returns the final ``(verdict, detail)`` — this never itself decides to merge.

    ``spent`` (issue #531, ``0 <= spent <= wait_secs``) is the part of ``wait_secs`` the
    caller already used waiting for ``gh pr update-branch`` to land (``_wait_for_update``).
    The loop starts with that much already waited, so the two waits share one bound: time
    the update took is time this wait no longer has, and a green it cannot confirm in what
    is left refuses as pending, like any other.

    A ``green`` read is not believed on its own (issue #582): seconds after a PR opens, the
    rollup lists only the checks registered SO FAR, so one fast check that already passed
    reads as ``green`` while a slow job has not created its check run yet. A green is
    therefore re-read once more, one full poll interval later (charged to ``wait_secs``),
    and returned only if that read is ``green`` too. A confirm that reads ``pending``/
    ``empty`` goes back into the wait; ``failing``/``unreadable`` is returned at once. The
    confirm is never shortened to fit the budget: a green first seen with less than one
    poll interval of budget left is returned as ``pending`` (fail-closed), with a detail
    that says so — so a ``wait_secs`` below ``poll_interval`` never returns ``green``. This
    compares verdicts only, not check names: a slow job that has not registered within one
    poll interval still gets through.

    ``wait_secs <= 0`` performs exactly one read and returns its verdict as-is: the
    original behaviour, for a host whose checks are known to already be in by the time the
    wave boundary fires. Sleeps go through the module-level ``_sleep`` so a test can make
    the whole loop cost no real time; their sum never exceeds ``wait_secs``.
    """
    verdict, detail = _check_rollup(pr_url)
    if wait_secs <= 0:
        return verdict, detail
    waited = spent
    while True:
        while verdict in ("pending", "empty") and waited < wait_secs:
            step = min(poll_interval, wait_secs - waited)
            _sleep(step)
            waited += step
            verdict, detail = _check_rollup(pr_url)
        if verdict != "green":
            return verdict, detail
        # Confirm a full poll interval later or not at all: a re-read squeezed into what is
        # left of the budget is too soon to show a slow job registering, and overrunning
        # the budget would break its bound — so refuse the green as unconfirmed instead.
        left = wait_secs - waited
        if left < poll_interval:
            return "pending", (f"green first seen with {left}s of wait budget left, too "
                               f"little to confirm it {poll_interval}s later ({detail})")
        _sleep(poll_interval)
        waited += poll_interval
        verdict, detail = _check_rollup(pr_url)
        if verdict == "green":
            return verdict, detail


def _pr_head(pr_url: str) -> tuple[str, str, str]:
    """PR ``pr_url``'s head commit and base branch as the host reports them (issue #531),
    from one ``gh pr view``: ``(head_sha, base_branch, "")``, or ``("", "", why)`` when the
    answer cannot be interpreted. The head is the host's, not a local ref: it is the SHA
    ``gh pr merge --match-head-commit`` is checked against."""
    r = subprocess.run(["gh", "pr", "view", str(pr_url), "--json", "headRefOid,baseRefName"],
                       capture_output=True, text=True)
    try:
        pr = json.loads(r.stdout or "") if r.returncode == 0 else None
    except ValueError:
        pr = None
    head = pr.get("headRefOid") if isinstance(pr, dict) else None
    base = pr.get("baseRefName") if isinstance(pr, dict) else None
    if isinstance(head, str) and head and isinstance(base, str) and base:
        return head, base, ""
    err = (r.stderr or r.stdout or "").strip()[:200]
    return "", "", f"its head and base could not be read (`gh pr view`: {err or 'no output'})"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _git_failed(repo: Path, what: str, r: subprocess.CompletedProcess) -> str:
    tail = (r.stderr or "").strip().splitlines()
    return (f"git failed: `git {what}` in {repo} exited {r.returncode} "
            f"({tail[-1] if tail else 'no output'})")


def _fetch(cfg: Config, repo: Path) -> str:
    """Fetch the PR's base (``base_remote``) and its branch (``origin``, where publish
    pushed it — the same remote on an own-repo checkout) into ``repo``. "" or why not."""
    for remote in dict.fromkeys((cfg.base_remote, "origin")):
        r = _git(repo, "fetch", remote)
        if r.returncode != 0:
            return _git_failed(repo, f"fetch {remote}", r)
    return ""


def _contains(repo: Path, tip: str, head: str) -> tuple[bool | None, str]:
    """Whether commit ``head`` contains commit ``tip``: ``git merge-base --is-ancestor <tip>
    <head>`` in ``repo`` — exit 0 yes, exit 1 no. Any other exit is git failing (a commit
    this checkout does not have lands here too) and returns ``(None, why)``."""
    r = _git(repo, "merge-base", "--is-ancestor", tip, head)
    if r.returncode in (0, 1):
        return r.returncode == 0, ""
    return None, _git_failed(repo, f"merge-base --is-ancestor {tip[:12]} {head[:12]}", r)


def _base_read(cfg: Config, repo: Path, pr_url: str) -> tuple[str, str, bool | None, str]:
    """Whether PR ``pr_url``'s head lacks its base branch's current tip (issue #531), in
    plain git as ``publish._line_tip_refusal`` decides its question (#593), not by a
    host-side comparison: the head and base branch from the host (``_pr_head``), a fetch
    into the checkout ``repo`` (``_fetch``), the base tip recorded from
    ``<base_remote>/<base>``, then ``_contains``. Returns ``(head, tip, behind, "")``. On any
    failure — ``gh`` unreadable, a failed fetch, a base that does not resolve, ``git
    merge-base`` exiting other than 0/1 — ``behind`` is None and the last item says why:
    the caller refuses, fail-closed, and never guesses "up to date"."""
    head, base, why = _pr_head(pr_url)
    if not head:
        return "", "", None, why
    why = _fetch(cfg, repo)
    if why:
        return head, "", None, why
    ref = f"{cfg.base_remote}/{base}"
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    tip = (r.stdout or "").strip()
    if r.returncode != 0 or not tip:
        return head, "", None, (f"its base {ref} does not resolve in {repo} after the "
                                f"fetch (`git rev-parse` exited {r.returncode})")
    contains, why = _contains(repo, tip, head)
    if contains is None:
        return head, tip, None, why
    return head, tip, not contains, ""


def _wait_for_update(cfg: Config, repo: Path, pr_url: str, stale: str, tip: str,
                     wait_secs: int, *, poll_interval: int = 5) -> tuple[str, int, str]:
    """Wait for ``gh pr update-branch`` to land on PR ``pr_url`` (issue #531). GitHub may
    apply the update after the command returns, so a read straight after it can still see
    the old head ``stale``: re-read the head (``_pr_head``) until it contains the recorded
    base tip ``tip`` (``_fetch`` + ``_contains``) or ``wait_secs`` of (patchable)
    wall-clock time is spent. Returns ``(head, waited, "")`` for the first head that
    contains ``tip`` — ``waited`` is then charged to the rollup wait
    (``_wait_for_green(spent=waited)``) — or ``("", waited, why)`` to refuse: the bound ran
    out, or a read failed (``gh`` unreadable, a failed fetch, ``git merge-base`` exiting
    other than 0/1). Only a head not seen before is fetched and checked. ``wait_secs <=
    0`` reads once; the sleeps go through ``_sleep`` and never sum past ``wait_secs``."""
    waited = 0
    seen = stale
    while True:
        head, _, why = _pr_head(pr_url)
        if not head:
            return "", waited, why
        if head != seen:
            why = _fetch(cfg, repo)
            if why:
                return "", waited, why
            contains, why = _contains(repo, tip, head)
            if contains is None:
                return "", waited, why
            if contains:
                return head, waited, ""
            seen = head
        if waited >= wait_secs:
            return "", waited, (f"its update had not landed after {wait_secs}s — its head "
                                f"{head[:12]} still lacks base commit {tip[:12]}; raise "
                                "[driver] merge_wait_secs if the host is slow to apply it")
        step = min(poll_interval, wait_secs - waited)
        _sleep(step)
        waited += step


def _undo_ready(pr_url: str) -> None:
    """Return ``pr_url`` to draft (issue #462): the documented inverse of the ``gh pr
    ready`` call in ``_merge_one``, run on every path where that function declines to merge
    a PR it already readied, so a stopped wave never leaves a PR advertising a readiness no
    human granted. Its own failure is reported — never masking the real reason the wave
    stopped — but does not change the caller's already-decided non-zero return."""
    print(f"→ gh pr ready {pr_url} --undo")
    undo = subprocess.run(["gh", "pr", "ready", str(pr_url), "--undo"],
                          capture_output=True, text=True)
    if undo.returncode != 0:
        print((undo.stderr or undo.stdout).strip(), file=sys.stderr)
        print(f"!!! merge: could not return {pr_url} to draft after declining to merge it "
              "— it is left marked ready; a human must re-draft it.", file=sys.stderr)


def _refuse(d: Path, pr_url: str, why: str) -> int:
    """Decline to merge ``d``'s already-readied PR (issue #531): say why, STOP, and undo
    the ready-mark (``_undo_ready``), as every other refusal in ``_merge_one`` does."""
    print(f"\n!!! merge: {d.name} ({pr_url}) was NOT merged — {why}. STOP: later waves are "
          "NOT run; resolve at the PR, then re-run (the run resumes idempotently).\n",
          file=sys.stderr)
    _undo_ready(pr_url)
    return 1


def _merge_one(cfg: Config, d: Path, *, dry_run: bool, method: str,
               fetched: set[str]) -> int:
    """Merge one bundle's recorded PR (idempotent, fail-closed). ``fetched`` dedupes the
    post-merge base fetch across bundles that share a checkout."""
    if state.state(d) != state.COMPLETE:
        return 0  # not accepted — nothing of this bundle's to merge
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return 0  # close / no-fix disposition — no contribution to merge
    rec = publish._publish_record(d)
    pr_url = rec.get("pr_url") if rec else None
    repo_spec = rec.get("repo") if rec else None
    if not pr_url:
        print(f"merge: {d.name} is COMPLETE but has no recorded PR — cannot merge a wave "
              "whose member wasn't published. STOP.", file=sys.stderr)
        return 1

    cmd = ["gh", "pr", "merge", str(pr_url), f"--{method}"]
    if dry_run:
        print(f"merge --dry-run — {d.name}: {' '.join(cmd)}")
        return 0
    iid = d.name.removeprefix("issue_")
    if merged.is_merged(cfg, iid):
        return 0  # already merged (a resumed run) — idempotent

    # The publisher opens every PR as a draft (STOP discipline), but `gh pr merge` refuses a
    # draft — so in merge mode a non-final wave's PRs must be readied before they can advance
    # the base (issue #279). `merge_wave` is only called for non-final waves, so this readies
    # exactly the PRs about to be merged; the final wave never reaches here and keeps its
    # draft for the human's ready-mark. Idempotent: `gh pr ready` on an already-ready PR is a
    # no-op. Fail-closed like the merge itself — if it can't be readied, it can't be merged.
    print(f"→ gh pr ready {pr_url}")
    ready = subprocess.run(["gh", "pr", "ready", str(pr_url)], capture_output=True, text=True)
    if ready.returncode != 0:
        print((ready.stderr or ready.stdout).strip(), file=sys.stderr)
        print(f"\n!!! merge: {d.name} ({pr_url}) could not be marked ready to merge. "
              "STOP: later waves are NOT run; resolve at the PR, then re-run.\n",
              file=sys.stderr)
        return 1

    # Full check-rollup gate (issue #413), read AFTER the ready-mark and immediately before
    # the merge: `gh pr ready` can itself trigger `ready_for_review` CI, so only a rollup
    # read here says anything about green AT MERGE TIME. `gh pr merge` below fails closed
    # only on the checks the host repo marks required in branch protection; this refuses on
    # ANY failing, pending or missing check, whatever that host's protection happens to be.
    # `!= "required"` rather than `== "all"` so an unexpected value gates rather than
    # merging (Config.load already coerces one, but this module is the one that must not
    # merge past a red rollup).
    if cfg.merge_requires != "required":
        # Issue #531: the rollup below describes the PR's head as it stands — tested on the
        # base that head was built on, not the base it merges into once an earlier member of
        # this wave (or anything else) moved it. So read whether the head is behind its base
        # and, if it is, bring it up to date first (a merge-commit update of its own branch,
        # never a rebase of the reviewed commits): the rollup waited on below is then the
        # rollup of the combination that merges.
        if not repo_spec:
            return _refuse(d, pr_url, "its publish record names no repo, so there is no "
                                      "checkout to read its base in")
        repo = publish._checkout_path(cfg, repo_spec)
        head, tip, behind, why = _base_read(cfg, repo, str(pr_url))
        if behind is None:
            return _refuse(d, pr_url, why)
        waited = 0
        if behind:
            print(f"   head {head[:12]} lacks base commit {tip[:12]} — bringing it up to date")
            print(f"→ gh pr update-branch {pr_url}")
            up = subprocess.run(["gh", "pr", "update-branch", str(pr_url)],
                                capture_output=True, text=True)
            if up.returncode != 0:
                print((up.stderr or up.stdout).strip(), file=sys.stderr)
                return _refuse(d, pr_url, "it is behind its base and could not be brought up "
                               "to date (`gh pr update-branch` failed: a conflict with the "
                               "base, the update refused, or a `gh` too old for it)")
            # GitHub may apply the update after the command returns: poll for it, charged
            # to the same merge_wait_secs budget the rollup wait below then gets.
            head, waited, why = _wait_for_update(cfg, repo, str(pr_url), head, tip,
                                                 cfg.merge_wait_secs)
            if not head:
                return _refuse(d, pr_url, why)
            print(f"   updated: head {head[:12]} contains base commit {tip[:12]}")
        else:
            print(f"   head {head[:12]} contains base commit {tip[:12]} — up to date")
        print(f"→ gh pr checks {pr_url}")
        # A wave boundary fires seconds after the PR opened (issue #462), so the first read
        # is routinely pending/empty — not a verdict yet. Wait for it to resolve, bounded by
        # [driver].merge_wait_secs, before treating an unresolved rollup as a refusal.
        verdict, detail = _wait_for_green(str(pr_url), cfg.merge_wait_secs, spent=waited)
        if verdict != "green":
            why = {
                "failing": f"a check is FAILING — {detail}",
                "pending": f"a check has not finished within {cfg.merge_wait_secs}s — "
                           f"{detail}",
                "empty": f"the check rollup was still EMPTY after {cfg.merge_wait_secs}s "
                         f"— {detail}; absence of evidence is not green",
                "unreadable": f"the check rollup could not be read — {detail}",
            }[verdict]
            print(f"\n!!! merge: {d.name} ({pr_url}) was NOT merged — {why}. The host's "
                  "required-checks config is not enough: this wave's base must be green "
                  "before the next wave builds on it. STOP: later waves are NOT run; "
                  "re-run once the checks are green (the run resumes idempotently), or set "
                  "[driver] merge_requires = \"required\" to merge on the host's required "
                  "checks alone, or raise [driver] merge_wait_secs if the checks just take "
                  "longer than that to report.\n", file=sys.stderr)
            _undo_ready(pr_url)
            return 1
        # Positive evidence in the run log that this merge was gated, not merged blind.
        print(f"   check rollup green ({detail})")
        # Issue #531: `gh pr checks` does not say which head it read, so read the head and
        # base again: the head must be the one read before the wait (the green rollup is
        # then that head's) and must still contain its base's tip. The merge is pinned to
        # it, so a head that changes after this read is refused by the host, not merged.
        after, _, behind, why = _base_read(cfg, repo, str(pr_url))
        if behind is None:
            return _refuse(d, pr_url, why)
        if after != head:
            return _refuse(d, pr_url, f"its head changed from {head[:12]} to {after[:12]} "
                           "while its checks were being read, so the green rollup may not "
                           "be that head's")
        if behind:
            return _refuse(d, pr_url, "its base moved while its checks were being read, so "
                           "the green rollup is not of the combination it would merge; "
                           "re-run to bring it up to date and verify it again")
        cmd += ["--match-head-commit", head]

    print(f"→ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip(), file=sys.stderr)
        print(f"\n!!! merge: {d.name} ({pr_url}) did not merge — a conflict, no merge "
              "rights on the base, a host-required check that failed or started after the "
              "rollup gate above, a head that changed after that gate read it green (the "
              "merge is pinned to it), or a base that moved after the last read on a host "
              "that requires up-to-date branches. STOP: later waves are NOT run; resolve at "
              "the PR, then re-run.\n", file=sys.stderr)
        _undo_ready(pr_url)
        return 1
    # Refresh the base so the NEXT wave's worktree resets to the merged result.
    if repo_spec and repo_spec not in fetched:
        repo = publish._checkout_path(cfg, repo_spec)
        subprocess.run(["git", "-C", str(repo), "fetch", cfg.base_remote],
                       capture_output=True, text=True)
        fetched.add(repo_spec)
    return 0
