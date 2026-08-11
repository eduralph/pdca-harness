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
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import merged, publish, state
from .config import Config

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
        print(f"→ gh pr checks {pr_url}")
        verdict, detail = _check_rollup(str(pr_url))
        if verdict != "green":
            why = {
                "failing": f"a check is FAILING — {detail}",
                "pending": f"a check has not finished — {detail}",
                "empty": f"the check rollup is EMPTY — {detail}; absence of evidence is "
                         "not green",
                "unreadable": f"the check rollup could not be read — {detail}",
            }[verdict]
            print(f"\n!!! merge: {d.name} ({pr_url}) was NOT merged — {why}. The host's "
                  "required-checks config is not enough: this wave's base must be green "
                  "before the next wave builds on it. STOP: later waves are NOT run; "
                  "re-run once the checks are green (the run resumes idempotently — the "
                  "PR stays ready), or set [driver] merge_requires = \"required\" to "
                  "merge on the host's required checks alone.\n", file=sys.stderr)
            return 1
        # Positive evidence in the run log that this merge was gated, not merged blind.
        print(f"   check rollup green ({detail})")

    print(f"→ gh pr merge {pr_url} --{method}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip(), file=sys.stderr)
        print(f"\n!!! merge: {d.name} ({pr_url}) did not merge — a conflict, no merge "
              "rights on the base, or a host-required check that failed or started after "
              "the rollup gate above. STOP: later waves are NOT run; resolve at the PR, "
              "then re-run.\n", file=sys.stderr)
        return 1
    # Refresh the base so the NEXT wave's worktree resets to the merged result.
    if repo_spec and repo_spec not in fetched:
        repo = publish._checkout_path(cfg, repo_spec)
        subprocess.run(["git", "-C", str(repo), "fetch", cfg.base_remote],
                       capture_output=True, text=True)
        fetched.add(repo_spec)
    return 0
