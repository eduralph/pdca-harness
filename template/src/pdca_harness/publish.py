"""Publish — the **closing work of Check**: contribute an accepted fix as a draft PR.

Publishing is NOT a new PDCA beat. Check already owns the gates (including T4
contribution conformance) and the human sign-off; turning the accepted fix into a
draft pull request whose upstream CI the human weighs is the *contribution arm of
the same beat*. Once a bundle is accepted at sign-off (``state.COMPLETE``), this:

    contribution leaf → commit-msg.txt + pr-description.md     (the T4 gate's inputs)
    → T4 gate (must pass)
    → branch from upstream/<base> → git apply → commit → push → ``gh pr create --draft``
    → STOP.

STOP discipline: it never marks a PR ready or merges — that stays the human's
sign-off disposition. The mechanics are deterministic ``git``/``gh`` subprocesses
(no model decides control flow); the prose is written by the *publisher* leaf.

Project-specifics are config-driven (``pdca.toml``): the branch pattern
(``[publisher].fix_branch_pattern`` / ``feature_branch_pattern``) and the
repo→checkout map (``[publisher.checkouts]``, with the sibling convention as the
fallback). The issue trailer the T4 gate enforces is ``[tracker].issue_trailer``.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import brief, gates, leaves, progress, state, worktree
# Aliased: `publish()` builds a local `record = {...}` (its publish.json payload,
# below) that would shadow the module name at the #317 call-in.
from . import record as record_mod
from .config import Config

COMMIT_MSG = "commit-msg.txt"
PR_BODY = "pr-description.md"
# The wave driver records the run's integration branch here for a wave>0 bundle, so its
# Do worktree and stacked PR base off the prior waves' folded work (#wave-model); absent ⇒
# build / open the PR off the target base.
STACK_BASE_FILE = "stack-base"
# The pre-push host-CI record (issue #311): the [gates] host_ci rows as run against the
# pinned base + patch.diff tree, written when the gate REFUSES (so the refusal survives
# for the human, naming the command and the base) and removed again once they pass.
HOST_CI_JSON = "host-ci.json"


def _ensure_texts(cfg: Config, d: Path) -> bool:
    """Draft the two contribution artifacts with the publisher leaf if absent (only-if-
    missing, so re-runs never clobber an edited text); False when still missing after."""
    if not ((d / COMMIT_MSG).is_file() and (d / PR_BODY).is_file()):
        print("publish: drafting contribution artifacts "
              f"({COMMIT_MSG} / {PR_BODY})…", file=sys.stderr)
        leaves.run_publish(d, cfg)
    if not ((d / COMMIT_MSG).is_file() and (d / PR_BODY).is_file()):
        print(f"publish: {COMMIT_MSG} / {PR_BODY} still missing — aborting", file=sys.stderr)
        return False
    return True


def draft_texts(cfg: Config, d: Path, *, run_t4: bool = True, draft: bool = True) -> bool:
    """Pre-pass (issue #295): make bundle ``d`` text-ready for publishing — NO git/gh.

    Drafts the two contribution artifacts (``commit-msg.txt`` / ``pr-description.md``)
    if absent and runs the T4 contribution gate over them, so the flow can generate and
    validate EVERY accepted bundle's publishing texts before any mechanical publishing
    starts — a mid-wave drafting failure then blocks only its bundle, never leaves a
    wave half-pushed.

    ``run_t4=False`` is the flow's DRAFT-ONLY phase (#295 review round 2): publisher
    leaves run from the shared project root, so a later bundle's leaf can touch an
    earlier bundle's artifacts — validation is therefore a separate pass the flow runs
    only after EVERY leaf has finished, so T4 always judges the final contents.

    ``draft=False`` is the flow's VALIDATION-ONLY phase (#295 review round 4): a
    missing artifact there means a later leaf DELETED it, and re-drafting would invoke
    the publisher leaf again mid-validation — reopening the exact shared-root mutation
    window the phase split closes (in a ≥3-bundle wave, the re-draft can mutate a
    bundle T4 already passed). Validation fails on missing files instead.

    Returns True when the phase succeeded — including the cases where there is
    legitimately nothing to draft (not COMPLETE, a close/no-fix empty patch, no usable
    target): :func:`publish`'s own guards re-decide and report those with their richer
    messages. False = drafting or T4 failed: do not enter the mechanics loop. (The
    ``--no-issue``/pending-id narrow T4 mode stays exclusive to :func:`publish` — the
    flow never publishes pending-id, so this pre-pass always gates in the default
    id-known mode.)
    """
    if state.state(d) != state.COMPLETE:
        return True
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return True                                  # close/no-fix: nothing to contribute
    repo_spec, base, _slug = _resolve_target(d)
    if not repo_spec or not base:
        return True                                  # non-contributing cycle
    if draft:
        if not _ensure_texts(cfg, d):
            return False
    elif not ((d / COMMIT_MSG).is_file() and (d / PR_BODY).is_file()):
        print(f"publish: {COMMIT_MSG} / {PR_BODY} missing at validation for {d.name} — "
              "a later draft removed them? NOT re-drafting mid-validation; the bundle "
              "is not ready.", file=sys.stderr)
        return False
    if run_t4 and not _t4_passes(cfg, d):
        print(f"publish: T4 contribution gate FAILED on {COMMIT_MSG} / {PR_BODY} for "
              f"{d.name} — fix them and retry", file=sys.stderr)
        return False
    return True


def publish(
    cfg: Config,
    issue_id: str,
    *,
    dry_run: bool = False,
    open_pr: bool = True,
    by: str = "",
    today: str | None = None,
    skip_if_no_target: bool = False,
    pending_id: bool = False,
    texts_prevalidated: bool = False,
) -> int:
    """Contribute an accepted bundle's fix as a draft PR. Return a process code.

    ``skip_if_no_target`` (set by the flow): a bundle whose brief names no upstream
    ``Repo + branch target`` is a non-contributing cycle (e.g. an internal fix) —
    warn and return 0 rather than erroring, so it doesn't fail the continuous flow.

    ``pending_id`` (``--no-issue``): the first-class "no tracker id yet" path. A
    project may need to contribute before a tracker number is assigned; rather than a
    magic ``Fixes #0000`` placeholder, declare it here. The T4 contribution gate then
    runs in the **narrow pending-id mode** (#384): ``$PDCA_PENDING_ID`` is exported so
    the registered checker drops exactly the tracker-id requirement (``contribcheck
    --no-issue``) — every other contribution rule still hard-blocks the push. The
    bundle is recorded ``id_pending`` so the human adds the real id and re-gates T4
    before marking the PR ready. The publisher leaf omits the trailer (no invented
    id) in this case.

    ``texts_prevalidated`` (set by the flow, #295 review): the caller already ran
    :func:`draft_texts` — drafting AND the T4 gate — over this bundle, so this call is
    mechanics-only. Skipping the second T4 run matters beyond cost: a transient or
    stateful T4 command that passed the pre-pass but failed here would recreate the
    half-published wave the pre-pass exists to prevent. Direct ``pdca publish`` never
    sets it, keeping the lazy draft+gate path self-contained.
    """
    d = cfg.bundle(issue_id)
    today = today or datetime.date.today().isoformat()

    # Guard — publish is Check's CLOSING act: only on an accepted bundle.
    s = state.state(d)
    if s != state.COMPLETE:
        print(f"publish: {d.name} is {s}, not COMPLETE — accept it at sign-off first",
              file=sys.stderr)
        return 1

    # Close-disposition bundle (issue #60): an accepted close / no-fix outcome has no
    # patch.diff, so there is nothing to `git apply` / open a PR for. This is not a
    # failure — close the tracker item by hand. Return 0 so the continuous flow's
    # publish-on-accept doesn't error (mirrors skip_if_no_target).
    #
    # A 0-byte / whitespace-only patch.diff counts as "no patch" too (issue #95): a
    # verify-first close can leave an empty patch.diff behind, and `is_file()` alone
    # would let it past this guard — after which `git apply` is a no-op and the commit
    # fails with "nothing to commit". Treat empty content the same as a missing file.
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        print(f"publish: {d.name} has no (non-empty) patch.diff (close / no-fix "
              "disposition) — nothing to contribute; close the tracker item by hand.",
              file=sys.stderr)
        return 0

    # Resolve the target from the brief (the contribution's where).
    repo_spec, base, slug = _resolve_target(d)
    if not repo_spec or not base:
        msg = (f"publish: brief has no usable 'Repo + branch target' "
               f"(got repo={repo_spec!r} base={base!r})")
        if skip_if_no_target:
            print(msg + " — skipping publish (no upstream contribution).", file=sys.stderr)
            return 0
        print(msg, file=sys.stderr)
        return 1

    # Artifacts the T4 gate needs — write them with the publisher leaf if absent, then
    # gate them. Skipped wholesale when the flow's pre-pass already drafted AND gated
    # (texts_prevalidated, #295 review) — re-running a transient/stateful T4 here could
    # fail mid-wave AFTER siblings pushed, recreating the half-published state the
    # pre-pass exists to prevent. The lazy path keeps direct `pdca publish`
    # self-contained, and is idempotent — only-if-missing.
    if not texts_prevalidated:
        if not _ensure_texts(cfg, d):
            return 1

        # T4 contribution gate — the artifacts MUST pass before anything is pushed, in
        # BOTH modes. Under pending_id (--no-issue) the gate itself runs in the narrow
        # pending-id mode (#384): the one thing legitimately missing is the
        # not-yet-assigned tracker id, so exactly that requirement is dropped — by the
        # checker (`contribcheck --no-issue` via $PDCA_PENDING_ID), not by waving the
        # whole failed gate through as a printed flag, which also amnestied a broken PR
        # body / commit message. Whatever still fails here is a real defect that blocks
        # the push. The bundle is still recorded id_pending so the human adds the id
        # and re-gates T4 before ready.
        if not _t4_passes(cfg, d, pending_id=pending_id):
            print(f"publish: T4 contribution gate FAILED on {COMMIT_MSG} / {PR_BODY} — "
                  "fix them and retry", file=sys.stderr)
            return 1
    elif not ((d / COMMIT_MSG).is_file() and (d / PR_BODY).is_file()):
        # Defensive: prevalidated promises the texts exist; a vanished artifact means
        # the promise no longer holds — refuse rather than push without a commit message.
        print(f"publish: {COMMIT_MSG} / {PR_BODY} missing despite prevalidation — "
              "aborting", file=sys.stderr)
        return 1

    # Stack mode (issue #54): the brief names an existing PR's head branch — contribute a
    # commit onto it instead of a new PR. The shared spine above (guard, target, artifacts,
    # T4) already ran; the branch/steps/PR step are what differ.
    onto = brief.onto_branch(d / "brief.md")
    if onto is not None:
        return _publish_stacked(cfg, d, repo_spec, onto,
                                dry_run=dry_run, by=by, today=today, pending_id=pending_id)

    # Auto-stacked chain (issue #123): the brief `Stacks on:` a prereq whose branch was
    # produced earlier in THIS run — build + publish a NEW draft PR based on that branch (a
    # separate stacked PR), not on the target base. Distinct from `Onto branch` (#54), which
    # appends a commit to one existing PR. The base is derived from the prereq's publish.json
    # (never hand-written — it doesn't exist at Plan time).
    stack_branch = _stack_base_branch(cfg, d)
    if brief.stacks_on(d / "brief.md") and not stack_branch:
        print(f"publish: {d.name} `Stacks on` a prereq with no published branch yet — the "
              "prereq must publish first (the flow schedules this).", file=sys.stderr)
        return 1

    branch = _branch_name(cfg, d, slug)
    summary_line = (d / COMMIT_MSG).read_text(encoding="utf-8").splitlines()[0]
    repo = _checkout_path(cfg, repo_spec)

    git = lambda *a: ["git", "-C", str(repo), *a]
    base_remote = cfg.base_remote
    # Stacked: cut the dependent's branch off the PARENT / integration branch (on origin) so it
    # carries the predecessors' diffs; otherwise off the target base (#123). pr_base is what
    # `gh --base` gets — and it MUST be a branch in the upstream (`--repo`) repo.
    checkout_base = f"origin/{stack_branch}" if stack_branch else f"{base_remote}/{base}"
    # Own-repo (base on origin): the integration/parent branch IS an upstream branch, so
    # `--base` it for a clean, increment-only stacked PR. Fork (base on a separate upstream a
    # fork contributor can't push to): that branch lives on origin (the fork) and can't be a
    # `--base`, so the PR opens against the upstream base and carries the CUMULATIVE stacked
    # diff (predecessors + this fix). It still merges cleanly bottom-up — once a prerequisite
    # is merged, its identical content re-merges as a no-op — but the displayed diff does NOT
    # auto-reduce on merge: the fold's `pdca-integrate:*` commits aren't ancestors of the
    # prereq's PR-merge, so the PR merge-base doesn't advance; the diff clears only when the
    # dependent is rebuilt off the merged base (a later `pdca flow` run). That visible overlap
    # is the cost of fork wave-stacking (#185). The dependent's branch is cut off the parent
    # branch either way.
    own_repo = base_remote == "origin"
    pr_base = stack_branch if (stack_branch and own_repo) else base
    # Merge-mode base guard (#411) — fail-closed, BEFORE any branch/push/PR work. Under
    # `[driver].wave_mode = "merge"` the driver merges each accepted bundle's PR "into its
    # base" (merge.py:32-33), unattended, mid-flow — whatever base that PR happens to carry.
    # So a PR opened against a branch THIS run produced silently lands the fix in another
    # bundle's branch instead of the shared target, and the wave still reports success.
    # Refuse here rather than at merge time: the publisher is an interactive step with a
    # human present, and a PR never opened against a run-produced branch leaves the merge
    # nothing wrong to merge. Same shape as the `Stacks on` refusal above (stderr, return 1,
    # nothing pushed). Stack mode — the default — is untouched: chaining onto a predecessor
    # is correct there, because nothing is ever merged for you.
    if cfg.wave_mode == "merge":
        refusal = _merge_base_refusal(cfg, d, repo_spec, pr_base, base)
        if refusal:
            print(refusal, file=sys.stderr)
            return 1
    steps = [
        git("fetch", "origin" if stack_branch else base_remote),
        git("checkout", "-B", branch, checkout_base),
        git("apply", str((d / "patch.diff").resolve())),
        # `commit -a` stages only modified-tracked files and would silently drop the
        # patch's NEW files (the regression test — the most important file in a fix
        # PR). Stage everything the patch did, then commit — the checkout is clean
        # (checkout -B off upstream + the _check_repo guard), so `add --all` picks up
        # exactly the patch's files (modified and added), nothing stray.
        git("add", "--all"),
        # `-s` adds the Signed-off-by trailer (DCO) from the committer identity in the
        # target checkout, so a DCO-gated host accepts the PR by construction; harmless
        # on non-DCO hosts (issue #81).
        git("commit", "-s", "-F", str((d / COMMIT_MSG).resolve())),
        # `--force-with-lease`, not a plain push: re-publishing a rebuilt bundle (after
        # `signoff --iterate-do`) commits a FRESH `checkout -B branch origin/<base>` off
        # the current base, which is not a fast-forward of the previous attempt already on
        # the PR branch — a plain push is rejected and the re-Done bundle never publishes
        # (#108). The lease is safe: `fetch` above refreshed `origin/<branch>`, so the
        # force refuses if the remote moved unexpectedly, and it still creates the branch
        # on a first publish.
        git("push", "--force-with-lease", "-u", "origin", branch),
    ]
    # A fork-based PR's --head must be OWNER:BRANCH — `gh` resolves a bare branch name
    # against the *base* repo (where the fork branch doesn't exist) and fails with
    # "Head ref must be a branch". The branch lives on origin (the fork).
    head = f"{_fork_owner(repo) or repo_spec.split('/')[0]}:{branch}"
    # Normalize the PR body's tracker refs (#233 + #238 review): bare the closing-keyword
    # trailer so GitHub auto-closes on merge, and deterministically ensure a clickable
    # `[#id](url)` reference off that trailer when a URL pattern is configured. Done AFTER
    # the T4 gate (which sees the model's raw output) and just before `--body-file` reads it.
    _normalize_tracker_refs(cfg, d, issue_id)
    pr_cmd = ["gh", "pr", "create", "--draft", "--repo", repo_spec, "--base", pr_base,
              "--head", head, "--title", summary_line,
              "--body-file", str((d / PR_BODY).resolve())]

    if dry_run:
        kind = "draft PR"
        if stack_branch:
            kind = "stacked draft PR" if own_repo else "stacked draft PR (fork: cumulative diff vs base)"
        print(f"publish --dry-run — {d.name} → {kind} on {repo_spec} ({branch} → {pr_base}):")
        print(f"  # stash the target working tree (Do/Check leave it dirty), restore it after")
        if cfg.host_ci_checks:
            print(f"  # host CI gate (#311): fetch, pin the exact {checkout_base} commit the "
                  f"push will build on, and run {len(cfg.host_ci_checks)} declared command(s) "
                  "against base + patch.diff in an ephemeral worktree — ANY non-zero exit "
                  "blocks the push")
        for c in steps + ([pr_cmd] if open_pr else []):
            print("  " + " ".join(shlex.quote(x) for x in c))
        return 0

    # Real run: the checkout must exist with the base + push remotes. Do/Check edit the
    # target in place, so the tree is normally dirty — stash it (publish re-applies the
    # fix from patch.diff onto a fresh branch, it doesn't use the working tree) and
    # restore it afterward, so edit-in-place and a clean publish checkout coexist (#83).
    rc = _check_repo(repo, repo_spec, required_remotes={base_remote, "origin"})
    if rc != 0:
        return rc
    if stack_branch:
        _warn_if_squash_only(repo_spec)  # a stacked PR must merge-commit, not squash (#123)

    # Host-only CI parity gate ([gates] host_ci, issue #311): the declared commands must
    # pass against the tree the push would publish, before anything is pushed. The T4
    # gate above runs with cwd=cfg.root against the tree BEFORE patch.diff is applied,
    # so it structurally cannot see content that arrives in the patch (the wyrd `typos`
    # class: Check green, PR opens red on a required status). The gate fetches and PINS
    # the exact base commit; the plan's `checkout -B` is then rebased onto that same
    # commit so the certified tree IS the pushed tree even when the base advanced since
    # Check (iteration-1 C5). Undeclared ⇒ ci_base stays "" and the steps run unchanged.
    ci_ok, ci_base = _host_ci_passes(cfg, d, repo,
                                     "origin" if stack_branch else base_remote,
                                     checkout_base)
    if not ci_ok:
        return 1
    if ci_base:
        steps = _pin_checkout(steps, git, branch, ci_base)

    orig_ref = _current_ref(repo)
    stashed = _stash_worktree(repo)
    try:
        for c in steps:
            print("→ " + " ".join(c[3:]))  # drop the `git -C <repo>` prefix in the echo
            if subprocess.run(c).returncode != 0:
                hint = " (patch may not apply against %s/%s — rebase the fix)" % (base_remote, base) \
                    if c[3] == "apply" else ""
                print(f"publish: step failed: {' '.join(c)}{hint}", file=sys.stderr)
                return 1
    finally:
        _restore_worktree(repo, orig_ref, stashed)

    pr_url = ""
    pr_failed = False
    if open_pr:
        print("→ gh pr create --draft …")
        r = subprocess.run(pr_cmd, capture_output=True, text=True)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            pr_failed = True
            print(r.stderr, file=sys.stderr)
            print("\n!!! publish: branch pushed, but `gh pr create` FAILED — "
                  "no draft PR was opened.\n"
                  "    Open it by hand, then re-run if needed. This is NOT done.\n",
                  file=sys.stderr)
        else:
            print(out)
            pr_url = out.splitlines()[-1] if out else ""

    record = {
        "mode": "stacked-pr" if stack_branch else "new-pr",
        "branch": branch, "pr_url": pr_url, "base": pr_base, "repo": repo_spec,
        "by": by or _signoff_by(d) or cfg.author or "unknown", "date": today,
        "id_pending": pending_id,
    }
    if ci_base:
        # The base commit the host-CI gate certified — and, via _pin_checkout, the exact
        # parent the pushed branch was built on (#311): auditable certified == pushed.
        record["host_ci_base"] = ci_base
    if stack_branch:
        record["stacks_on"] = brief.stacks_on(d / "brief.md")
    (d / "publish.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    # A requested-but-failed PR is a partial run, not a success — the branch is
    # pushed but the cycle isn't done. Exit non-zero so `flow` doesn't read the
    # empty pr_url as "published".
    if pr_failed:
        return 1

    print(f"\nDraft PR prepared on {repo_spec} ({branch} → {pr_base}).")
    if pr_url:
        print(f"  {pr_url}\n  watch CI:  gh pr checks {pr_url} --watch")
    if pending_id:
        print("  ⚠ id_pending: contributed without a tracker id — add the trailer "
              "(Fixes #N) and re-run T4 before marking the PR ready.")
    print("  STOP: review CI, then mark it ready / merge yourself — the human's step.")
    # Recording call-in (#317): strictly AFTER the publish.json write above — never
    # mid-publish — and best-effort ([records] mode "off", the default, is a no-op).
    record_mod.after_publish(cfg)
    return 0


def _publish_stacked(
    cfg: Config, d: Path, repo_spec: str, onto: tuple[str, str], *,
    dry_run: bool, by: str, today: str, pending_id: bool,
) -> int:
    """Stack mode (issue #54): contribute the fix as a commit on an existing PR's branch.

    The work branch IS the PR branch (``<remote>/<branch>`` from the brief's ``Onto
    branch``). No ``gh pr create`` — the PR already exists; it is resolved and recorded.
    Two guards make "tested-against == committed-onto == pushed-to" true before any push:
    ``git apply --check`` against the freshly-fetched branch (fails loudly if it advanced
    since the fix was built and tested), and an existing-open-PR lookup (refuse to push a
    commit to a branch with no PR).
    """
    remote, branch = onto
    base_ref = f"{remote}/{branch}"
    repo = _checkout_path(cfg, repo_spec)
    patch = str((d / "patch.diff").resolve())
    git = lambda *a: ["git", "-C", str(repo), *a]
    owner = _fork_owner(repo, remote) or repo_spec.split("/")[0]
    # `gh pr list --head` filters on the bare headRefName only — the `owner:branch` form
    # (correct for `gh pr create --head`, #23b) is "not supported" here and never matches
    # (#58). Filter by bare branch; the fork owner is re-checked in code (_existing_pr).
    pr_list = ["gh", "pr", "list", "--repo", repo_spec, "--head", branch,
               "--state", "open", "--json", "url,number,headRefName,headRepositoryOwner"]
    steps = [
        git("fetch", remote),
        git("checkout", "-B", branch, base_ref),
        git("apply", "--check", patch),  # the fix must still fit the branch it was tested on
        git("apply", patch),
        git("add", "--all"),
        # `-s` adds the Signed-off-by trailer (DCO) — same as the new-PR path (issue #81).
        git("commit", "-s", "-F", str((d / COMMIT_MSG).resolve())),
        git("push", remote, f"HEAD:{branch}"),
    ]

    if dry_run:
        print(f"publish --dry-run — {d.name} → commit stacked onto {repo_spec} "
              f"PR branch {branch} (base {base_ref}):")
        print(f"  # stash the target working tree (Do/Check leave it dirty), restore it after")
        if cfg.host_ci_checks:
            print(f"  # host CI gate (#311): fetch, pin the exact {base_ref} commit the "
                  f"push will build on, and run {len(cfg.host_ci_checks)} declared command(s) "
                  "against base + patch.diff in an ephemeral worktree — ANY non-zero exit "
                  "blocks the push")
        for c in steps:
            print("  " + " ".join(shlex.quote(x) for x in c))
        print("  " + " ".join(shlex.quote(x) for x in pr_list)
              + "   # resolve the existing open PR (no new PR is created)")
        return 0

    rc = _check_repo(repo, repo_spec, required_remotes={remote})
    if rc != 0:
        return rc

    # Resolve the existing PR BEFORE pushing — never push a commit to a branch with no PR.
    pr_url = _existing_pr(pr_list, branch, owner)
    if not pr_url:
        print(f"publish: no open PR with head {owner}:{branch} on {repo_spec} — refusing "
              "to push a commit to a branch with no PR. Open the PR first, or drop the "
              "'Onto branch' brief field to use the default new-PR flow.", file=sys.stderr)
        return 1

    # Host-only CI parity gate (#311) — this path pushes too, so it is gated the same
    # way as the new-PR path: fetch, pin the exact PR-branch commit the push builds on,
    # run the declared commands against pinned base + patch.diff, block on ANY non-zero.
    ci_ok, ci_base = _host_ci_passes(cfg, d, repo, remote, base_ref)
    if not ci_ok:
        return 1
    if ci_base:
        steps = _pin_checkout(steps, git, branch, ci_base)

    # Stash the (Do/Check-dirtied) tree so checkout -B + apply run clean; restore after (#83).
    orig_ref = _current_ref(repo)
    stashed = _stash_worktree(repo)
    try:
        for c in steps:
            print("→ " + " ".join(c[3:]))  # drop the `git -C <repo>` prefix in the echo
            if subprocess.run(c).returncode != 0:
                hint = ""
                if c[3:5] == ["apply", "--check"]:
                    hint = (f" — the patch no longer applies to {base_ref} (it advanced since "
                            "the fix was built and tested; rebuild/re-Check against the PR branch)")
                print(f"publish: step failed: {' '.join(c)}{hint}", file=sys.stderr)
                return 1
    finally:
        _restore_worktree(repo, orig_ref, stashed)

    rec = {
        "mode": "stacked",
        "branch": branch, "pr_url": pr_url, "base": base_ref, "repo": repo_spec,
        "by": by or _signoff_by(d) or cfg.author or "unknown", "date": today,
        "id_pending": pending_id,
    }
    if ci_base:
        rec["host_ci_base"] = ci_base  # certified == pushed, auditable (#311)
    (d / "publish.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")

    print(f"\nCommit stacked onto {repo_spec} PR branch {branch} ({pr_url}).")
    print(f"  watch CI:  gh pr checks {pr_url} --watch")
    if pending_id:
        print("  ⚠ id_pending: contributed without a tracker id — add the trailer "
              "(Fixes #N) and re-run T4 before marking the PR ready.")
    print("  STOP: review CI, then mark it ready / merge yourself — the human's step.")
    # Recording call-in (#317): same contract as the new-PR path — strictly after
    # this path's publish.json write, best-effort, no-op under mode "off".
    record_mod.after_publish(cfg)
    return 0


def _existing_pr(pr_list_cmd: list[str], branch: str, owner: str) -> str:
    """The URL of the open PR whose head is ``owner:branch`` (via ``gh pr list``), or ``""``.

    The command filters by the bare ``--head <branch> --state open`` (gh does not support
    the ``owner:branch`` form there, #58), so the fork owner is disambiguated HERE: match
    both ``headRefName == branch`` and ``headRepositoryOwner.login == owner`` so a
    same-named branch on a different fork can't loose-match. ``""`` on no PR / gh error /
    unparseable output — the caller fails loudly rather than pushing."""
    r = subprocess.run(pr_list_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return ""
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    for pr in prs:
        if (pr.get("headRefName") == branch
                and (pr.get("headRepositoryOwner") or {}).get("login") == owner):
            return pr.get("url", "")
    return ""


# ----------------------------------------------------------------------------
def _resolve_target(d: Path) -> tuple[str, str, str]:
    """``(repo_spec, base_branch, slug)`` from the brief, e.g.
    ``("example-org/example-repo", "main", "fix-the-thing")``.

    The target field is commonly written with markdown backticks and/or trailing prose
    after the branch; ``brief.repo_target`` isolates the ref on each side of ``@`` so that
    style doesn't corrupt the resolved checkout/base (see #25). That parse lives in
    ``brief`` — with the other per-field accessors — rather than here (issue #387): the
    same value has to reach a bundle-scoped gate command as ``$PDCA_BRIEF_BASE``, and a
    second implementation of it is exactly what #235 and #262 were."""
    bp = d / "brief.md"
    repo_spec, base = brief.repo_target(bp)
    slug = brief.field(bp, "slug") or d.name.removeprefix("issue_")
    return repo_spec, base, _slugify(slug)


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")


def _branch_name(cfg: Config, d: Path, slug: str) -> str:
    """The PR branch, from ``cfg.fix_branch_pattern`` / ``feature_branch_pattern``.
    The feature pattern is used when the brief's Kind / disposition marks a feature;
    both are ``.format(id=, slug=)`` strings from ``pdca.toml`` ``[publisher]``."""
    issue_id = d.name.removeprefix("issue_")
    kind = brief.field(d / "brief.md", "kind", "disposition hint").lower()
    is_feature = any(k in kind for k in ("enhanc", "feature", "new-feature", "proposal"))
    pattern = cfg.feature_branch_pattern if is_feature else cfg.fix_branch_pattern
    return pattern.format(id=issue_id, slug=slug)


def _checkout_path(cfg: Config, repo_spec: str) -> Path:
    """Local checkout for an upstream ``repo_spec``. A configured ``[publisher.checkouts]``
    entry wins (relative paths resolve against the project root); otherwise the sibling
    convention ``<root>/../<last-segment>`` (e.g. 'org/foo' → ../foo)."""
    mapped = cfg.repo_checkouts.get(repo_spec)
    if mapped:
        p = Path(mapped)
        return (p if p.is_absolute() else cfg.root / p).resolve()
    return (cfg.root.parent / repo_spec.split("/")[-1]).resolve()


def _publish_record(d: Path) -> dict | None:
    """The bundle's ``publish.json`` (the recorded PR/branch), or None if absent/unreadable."""
    pj = d / "publish.json"
    if not pj.exists():
        return None
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def write_stack_base(d: Path, branch: str) -> None:
    """Record the integration branch a wave>0 bundle stacks on, so its Do worktree and
    stacked PR base off the prior waves' folded work (read by :func:`_stack_base_branch`)."""
    (d / STACK_BASE_FILE).write_text(branch + "\n", encoding="utf-8")


def clear_stack_base(d: Path) -> None:
    """Remove any recorded integration stack base so the bundle builds off its own target
    base. Clears a **stale** marker a prior/resumed run wrote for a target that this run does
    not integrate — otherwise the bundle would build + open its PR against an old integration
    branch (read via :func:`_stack_base_branch`) instead of its own base (#187)."""
    (d / STACK_BASE_FILE).unlink(missing_ok=True)


def _read_stack_base(d: Path) -> str:
    """The recorded integration branch for a wave>0 bundle, or "" (absent ⇒ build off base)."""
    p = d / STACK_BASE_FILE
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def read_stack_base(d: Path) -> str:
    """Public accessor for the run-scoped integration branch a wave>0 bundle stacks on, or
    "" when the bundle builds off its own target base (wave 0, or a non-wave run).

    The wave driver stamps this per bundle (:func:`write_stack_base`) before Check, so a
    gate that must verify a dependent against the folded base — not the brief's origin base —
    can read it (issue #273). Lazily imported by ``gates`` to avoid the
    ``gates → publish → leaves → gates`` import cycle."""
    return _read_stack_base(d)


def _stack_base_branch(cfg: Config, d: Path) -> str | None:
    """The branch a stacked bundle bases off (worktree + ``gh --base``), or None.

    The wave driver's run-scoped integration branch — recorded in the bundle's
    ``stack-base`` file (#wave-model) — wins: a wave>0 bundle builds + opens its PR on the
    prior waves' folded work. Else the legacy single-chain ``Stacks on:`` (#123) parent
    branch from its publish.json, for a brief that still hand-declares a stack. None when
    neither applies (or the legacy prereq hasn't published yet)."""
    wave_base = _read_stack_base(d)
    if wave_base:
        return wave_base
    parents = brief.stacks_on(d / "brief.md")
    if not parents:
        return None
    rec = _publish_record(cfg.bundle(parents[0]))
    return rec.get("branch") if rec else None


def _merge_base_refusal(cfg: Config, d: Path, repo_spec: str, pr_base: str, base: str) -> str:
    """Why ``pr_base`` is not a base that exists independently of this run — or "" if it is.

    Only consulted under ``[driver].wave_mode = "merge"`` (#411), where the driver merges
    the PR into whatever base it carries. Two routes put another bundle's branch there,
    and the message names BOTH branches in either:

    1. ``pr_base`` differs from the bundle's own resolved target ``base`` — it came from
       the auto-stacked chain (:func:`_stack_base_branch`: a recorded integration branch,
       or the legacy ``Stacks on:`` parent's fix branch). Merge mode records no integration
       branch at all (``flow`` only fills it on the stack path), so in merge mode that
       wiring has no business choosing a base: wave order carries the dependency.
    2. ``pr_base`` IS the bundle's resolved target base, but that base is a branch another
       bundle in this batch produced — a brief whose ``Repo + branch target`` names a
       predecessor's fix branch (the documented stack-mode practice). The two strings match,
       so route 1's comparison sees nothing; the batch's ``publish.json`` records
       (:func:`_publish_record`) are what expose it, offline.

    The bundle's own base is never re-parsed here — it is passed in from
    :func:`_resolve_target`, the one parse (#235/#262/#387).
    """
    fix = ('`[driver].wave_mode = "merge"` merges this PR into whatever it targets, '
           "unattended — so it may only target a base that exists independently of this "
           "run. Point the brief's `Repo + branch target` at the shared base, or use the "
           'default wave_mode = "stack", where chaining onto a predecessor is correct.')
    if pr_base != base:
        return (f"publish: {d.name} would open its PR against `{pr_base}`, not its target "
                f"base `{base}` — that branch is one this run produced (a `Stacks on:` "
                f"prereq's fix branch / a recorded integration branch). {fix}")
    producer = _batch_branch_producer(cfg, d, repo_spec, pr_base)
    if producer:
        return (f"publish: {d.name} would open its PR against `{pr_base}` — its own target "
                f"base `{base}`, but that base is the branch {producer} produced in this "
                f"batch, not a shared base. {fix}")
    return ""


def _batch_branch_producer(cfg: Config, d: Path, repo_spec: str, branch: str) -> str:
    """The name of another bundle in this batch whose published branch is ``branch``
    (in the same repo), or "". Reads the siblings' ``publish.json`` records — no network."""
    for sub in sorted(cfg.bundle_root.glob("issue_*")):
        if sub.resolve() == d.resolve():
            continue
        rec = _publish_record(sub)
        # A record written before the `repo` field existed (or by a stub) is assumed to be
        # this repo's — fail-closed: the refusal is recoverable, a silent wrong merge is not.
        if rec and rec.get("branch") == branch and rec.get("repo", repo_spec) == repo_spec:
            return sub.name
    return ""


def _warn_if_squash_only(repo_spec: str) -> None:
    """Warn if the target repo can't merge a stacked PR with a merge commit (issue #123).

    Stacked PRs must be merged bottom-up with merge-commit / rebase-merge: a SQUASH drops
    the parent's commits from the base, so a child retargeted to the base re-shows the
    parent's diff until rebased. Best-effort via ``gh repo view``; any failure is silent."""
    try:
        r = subprocess.run(
            ["gh", "repo", "view", repo_spec, "--json", "mergeCommitAllowed,squashMergeAllowed"],
            capture_output=True, text=True)
    except OSError:  # gh not installed — best-effort, never fatal
        return
    if r.returncode != 0:
        return
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        return
    if info.get("mergeCommitAllowed") is False:
        print(f"publish: ⚠ {repo_spec} does not allow merge commits — a stacked PR must be "
              "merged bottom-up with a MERGE COMMIT (not squash), or each child re-shows its "
              "parent's diff until rebased (#123).", file=sys.stderr)


def _fork_owner(repo: Path, remote: str = "origin") -> str:
    """The GitHub owner of ``remote`` (the fork the branch is pushed to), e.g.
    ``"example-user"`` from ``git@github.com:example-user/repo.git`` or the https form.
    Used to form the cross-repo PR ``--head OWNER:BRANCH`` (and the stack-mode existing-PR
    lookup). ``""`` if undetectable."""
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", remote],
                         capture_output=True, text=True).stdout.strip()
    m = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return m.group(1) if m else ""


def publish_gates(cfg: Config) -> list[dict]:
    """The T4 rows publish is responsible for running (issue #339).

    The tier alone used to select them, so registering ANY T4-tier check for Check
    silently made publish re-run it before every push. In one instance that check was a
    batched 3x model review of the whole ``patch.diff``: ~6 minutes, re-paid on every
    publish attempt and every retry — and the push and ``gh pr create`` sit downstream of
    it, so retries happen.

    Duplicated cost is the smaller half. **Publish re-samples a nondeterministic reviewer
    after the human has signed off**: a bundle green at Check can be refused at publish
    over a finding that did not exist when §9 was recorded. Observed in both directions on
    one bundle — two findings each seen by only 1 of 3 passes, and a re-run of the
    identical command minutes later reporting none. That is not re-checking a decision
    against a fixed oracle; it is drawing a fresh sample from a distribution, after the
    decision, with the branch push gated on the result.

    What the slot is actually for (this module's own docstring): checks whose subject is
    the contribution artifacts publish just drafted — ``commit-msg.txt`` /
    ``pr-description.md`` — which do not exist at Check time, so Check cannot have
    validated them.

    **The default is keyed on `scope`, not a flat True.** A bundle-scoped T4 row is about
    the bundle's own artifacts, so it defaults to running here — which keeps the shipped
    ``T4-contribution`` row gating publish, unchanged, including for an instance taking a
    ``copier update``. A repo-scoped row cannot be about artifacts publish just drafted, so
    it defaults off; a flat ``True`` would preserve the original defect for exactly those
    rows, and worse, publish's environment carries only ``$PDCA_BUNDLE`` (no
    ``$PDCA_WORKTREE``, which the Check runner exports), so a repo-scoped row depending on
    it passes Check and then falsely blocks the push.

    An explicit ``at_publish`` always wins, in both directions.

    Not modelled here, and worth knowing: a check cannot yet be publish-ONLY. ``_applies``
    knows only ``scope``/``target``, so a row whose subject is ``pr-description.md`` still
    runs at Check — where ``pdca contribcheck`` is deliberately default-open (no
    ``pr-description.md`` yet => pass), which is what lets one registration serve both
    phases. A real ``phase`` property would express it directly; that is the larger change
    #339 records for later.
    """
    return [c for c in cfg.gates_checks
            if c.get("tier") == "T4"
            and c.get("at_publish", c.get("scope", "repo") == "bundle")]


def _t4_passes(cfg: Config, d: Path, *, pending_id: bool = False) -> bool:
    """Run every configured T4-tier gate over the bundle. No T4 gate → nothing to
    enforce (True). Keeps publish decoupled from any one project's checker.

    ``pending_id`` (``--no-issue``, #384): the gate must be told which mode it runs
    in, or the amnesty for the not-yet-assigned tracker id covers the WHOLE checker.
    The mode is exported as ``$PDCA_PENDING_ID``, derived from THIS run's flag —
    mirroring how the Check runner derives each gate's env from the driver's own
    state (``gates._run_one``: ``PDCA_BRIEF_BASE`` / ``PDCA_LANE``), never inherited:
    an ambient value is scrubbed, so a stray export from an earlier ``--no-issue``
    run cannot relax the tracker-id rule for a ticketed publish. The shipped checker
    honours it as ``contribcheck --no-issue`` (via the env, not an edit to the
    registered row line — that breaks ``copier update`` for instances that appended
    a row beside it)."""
    t4 = publish_gates(cfg)
    if not t4:
        return True
    env = {**os.environ, "PDCA_BUNDLE": str(d)}
    env.pop("PDCA_PENDING_ID", None)          # derived per run — never inherited (#384)
    if pending_id:
        env["PDCA_PENDING_ID"] = "1"
    for chk in t4:
        # Resolve `subcmd` through the SAME helper Check uses (#338). Reading the raw
        # `cmd` ran the empty string for a delegated row — and `subprocess.run("")` exits
        # 0, so a gate an instance believed it had registered passed vacuously at publish
        # while working correctly at Check.
        cmd, cmd_error = gates._delegated_cmd(chk, cfg.gates_runner)
        label = f"{chk.get('id', '')}: {chk.get('label', '')}".strip(": ")
        if cmd_error:
            print(f"publish: T4 gate '{label}' is misconfigured — {cmd_error}",
                  file=sys.stderr)
            return False
        # Heartbeat, not a bare captured run (#338): a T4 gate can be minutes of complete
        # silence — 6m25s measured for three parallel model review passes over a 300 KB
        # patch.diff — and on a bundle whose contribution texts already exist this is the
        # FIRST thing publish does, so the terminal goes quiet immediately and an operator
        # reasonably kills a working run.
        #
        # Deliberately NO `status=progress.bundle_activity`: that probe reports the newest
        # write in the bundle, which suits a Do leaf or an artifact-producing Check gate. A
        # T4 gate reads patch.diff and writes its report once, at the end, so the newest
        # write is whatever Check left hours earlier and every tick would render
        # "no writes 180m" — a stall warning on the very run proving it is not stalled.
        #
        # Announce BEFORE the heartbeat (#384, regression from the #338 rework; the
        # #181 "reads as a hang" finding): the first tick is a full interval away, and
        # on a bundle whose texts already exist this is publish's first action after
        # its guards. Same shape as the peer gate runner (gates._run_one). The
        # heartbeat label stays unprefixed — the announce already says "T4 gate".
        print(f"  · {f'T4 gate {label}'.rstrip()} (this can take minutes)…",
              file=sys.stderr, flush=True)
        try:
            rc, output, _ = progress.run_with_heartbeat(
                cmd, cwd=cfg.root, shell=True, env=env, capture=True,
                label=label or "T4 gate",
            )
        except Exception as exc:  # command not found, etc. — a failing gate, surfaced
            print(f"publish: T4 gate '{label}' could not run — {exc}", file=sys.stderr)
            return False
        if rc != 0:
            print(output.strip(), file=sys.stderr)
            return False
    return True


def _pinned_base(repo: Path, fetch_remote: str, base_ref: str) -> tuple[str, str]:
    """``(sha, error)`` — fetch and resolve the exact base commit the push will build on.

    Runs the SAME fetch the push plan's first step runs, then resolves ``base_ref`` to a
    commit SHA. The SHA — not the moving ref — is what the host-CI gate certifies and
    what :func:`_pin_checkout` rebases the push onto, so "certified tree == pushed tree"
    holds by construction (iteration-1 C5: a warm, no-fetch reconstruction certified a
    stale base while the push fetched afterward). A non-empty ``error`` means the base
    cannot be pinned; the caller fails closed."""
    r = subprocess.run(["git", "-C", str(repo), "fetch", fetch_remote],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        return "", f"`git fetch {fetch_remote}` failed ({tail[-1] if tail else 'no output'})"
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                        base_ref + "^{commit}"], capture_output=True, text=True)
    sha = (r.stdout or "").strip()
    if r.returncode != 0 or not sha:
        return "", f"base ref '{base_ref}' does not resolve after the fetch"
    return sha, ""


def _pin_checkout(steps: list[list[str]], git, branch: str, base_commit: str) -> list[list[str]]:
    """Rebase the push plan's ``checkout -B`` step onto the exact commit host CI certified.

    The plan was built against a moving ref (``<remote>/<base>``), which its own
    ``fetch`` step would re-resolve at push time — re-opening the certified-vs-pushed
    gap on a base that advances mid-publish (#311, iteration-1 C5). Matched on the
    step's verbs, not a list index, so a plan reordering can't silently pin the wrong
    step."""
    return [git("checkout", "-B", branch, base_commit) if c[3:5] == ["checkout", "-B"]
            else c for c in steps]


def _host_ci_passes(cfg: Config, d: Path, repo: Path, fetch_remote: str,
                    base_ref: str) -> tuple[bool, str]:
    """Run the declared host-only CI commands (``[gates] host_ci``, issue #311) against
    the exact tree the push would publish; ``(False, "")`` blocks the publish before
    anything is pushed.

    The pre-push counterpart of the Check-time host-CI rows (``gates._run_checks``) —
    but NOT the lane reconstruction those trust: the lane is deliberately warm and
    never fetches (``worktree.rebuild_for_gate`` — Check attests the base Do built
    against), while the push builds on the freshly fetched base, so a lane-based run
    can certify a stale base and still let publish push a tree the declared CI never
    saw (iteration-1 C5). So this fetches (the same fetch the push plan runs), PINS
    the resolved base commit, materializes an ephemeral ``base + patch.diff`` tree at
    that commit (``worktree.for_publish``), runs the commands there, and returns the
    pinned SHA so the push's ``checkout -B`` builds on it — certified tree == pushed
    tree, by construction.

    Nothing declared ⇒ ``(True, "")`` with no work at all — an instance that opts out
    is byte-identical to today (criterion c). EVERY command that does not pass blocks
    the push — including exit 77 (a Check gate's "cannot decide" channel) and any row
    hand-marked non-gating: the #311 criterion is literal ("a command that exits
    non-zero … blocks publish — no branch is pushed, no PR is opened"), the host's CI
    will fail the PR on these commands regardless of a carve-out, and sign-off did not
    bless one (iteration-1 C3). A refusal names the command on stderr and records
    every row plus the pinned base in the bundle's ``host-ci.json`` (removed again
    once they pass). Fail CLOSED when the base can't be pinned or no tree can be
    materialized — pushing content the declared CI never saw is the exact blind spot
    this closes."""
    if not cfg.host_ci_checks:
        return True, ""
    record = d / HOST_CI_JSON

    def _refuse(reason: str, rows: list[dict], base: str = "") -> tuple[bool, str]:
        record.write_text(json.dumps({"overall": "fail", "reason": reason,
                                      "base": base, "rows": rows},
                                     indent=2) + "\n", encoding="utf-8")
        print(f"publish: host CI gate — {reason} — nothing was pushed, no PR was "
              f"opened; see {d.name}/{HOST_CI_JSON}.", file=sys.stderr)
        return False, ""

    sha, err = _pinned_base(repo, fetch_remote, base_ref)
    if not sha:
        return _refuse(f"cannot pin the base commit the push would build on — {err}", [])
    try:
        wt = worktree.for_publish(d, repo, sha)
    except worktree.WorktreeError as exc:
        return _refuse(f"no pushed-tree to run against ({exc})", [], base=sha)
    rows: list[dict] = []
    try:
        for chk in cfg.host_ci_checks:
            rows.append(gates._run_one(chk, cfg=cfg, cwd=wt, bundle=d,
                                       runner=cfg.gates_runner, worktree_path=wt))
    finally:
        worktree.overflow_remove(repo, wt)
    failed = [r for r in rows if r["result"] != "pass"]
    if failed:
        for r in failed:
            print(f"publish: host CI '{r['rule_id']}' did NOT pass against the pinned "
                  f"base + patch.diff ({r['result']}) — command: {r['oracle']}\n"
                  f"    {r['path_line']}", file=sys.stderr)
        return _refuse(f"{len(failed)} host CI command(s) did not pass against the "
                       f"tree the push would publish (base {sha[:12]} + patch.diff) — "
                       "fix the tree (or the command) and retry", rows, base=sha)
    record.unlink(missing_ok=True)
    return True, sha


def _check_repo(repo: Path, repo_spec: str, required_remotes=("upstream", "origin")) -> int:
    """The local checkout must exist and have the remotes this publish path needs.

    A dirty tree is NOT a failure (issue #83): Do/Check edit the target in place, so the
    tree is normally dirty at publish time — :func:`_stash_worktree` cleans it for the
    checkout and :func:`_restore_worktree` puts it back. ``required_remotes`` is the set
    this path actually uses (base + push), so own-repo (no ``upstream``) is accepted.
    """
    hint = (f"create/clone the checkout for '{repo_spec}' at {repo} "
            "(or set [publisher.checkouts] in pdca.toml if it lives elsewhere)")
    if not (repo / ".git").exists():
        print(f"publish: checkout not found: {repo} — {hint}", file=sys.stderr)
        return 1
    remotes = subprocess.run(["git", "-C", str(repo), "remote"],
                             capture_output=True, text=True).stdout.split()
    for r in required_remotes:
        if r not in remotes:
            print(f"publish: {repo} has no '{r}' remote — {hint}", file=sys.stderr)
            return 1
    return 0


def _current_ref(repo: Path) -> str:
    """The checkout's current branch (or commit SHA if detached) — to return to after publish."""
    r = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
                       capture_output=True, text=True)
    ref = r.stdout.strip()
    if ref:
        return ref
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _stash_worktree(repo: Path) -> bool:
    """Stash the target's dirty tree (incl. untracked) so ``checkout -B`` + ``apply`` run on
    a clean base; return True iff something was stashed (the caller restores it). Publish
    re-applies the fix from ``patch.diff``, so the working-tree edits are not needed here."""
    dirty = bool(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip())
    if dirty:
        subprocess.run(["git", "-C", str(repo), "stash", "push", "--include-untracked",
                        "-m", "pdca-publish"], capture_output=True, text=True)
    return dirty


def _restore_worktree(repo: Path, orig_ref: str, stashed: bool) -> None:
    """Return the checkout to where publish found it: back on ``orig_ref`` with the stashed
    edits popped — so Do/Check's edit-in-place survives a publish. Best-effort."""
    subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", orig_ref],
                   capture_output=True, text=True)
    if stashed:
        subprocess.run(["git", "-C", str(repo), "stash", "pop"], capture_output=True, text=True)


_BY_RE = re.compile(r"^- By / date:\s*(.+?)\s*/", re.MULTILINE)


def _signoff_by(d: Path) -> str:
    """The name from §9 'By / date', for the publish record."""
    summary = d / "SUMMARY.md"
    if not summary.exists():
        return ""
    m = _BY_RE.search(summary.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else ""


# GitHub's auto-close parser fires only on a BARE `#<id>` right after a closing keyword
# (`Fixes #123`); a Markdown link (`Fixes [#123](url)`) is NOT recognised, so the issue
# silently stays open on merge (#233). So the closing trailer must stay bare — and the
# clickable tracker reference lives on a SEPARATE (non-closing) line.
_CLOSING_VERB_RE = re.compile(r"(?i)^\s*(fix(e[sd])?|close[sd]?|resolve[sd]?)\b")
_SUMMARY_RE = re.compile(r"(?i)^\s*#+\s*summary\b")


def _normalize_tracker_refs(cfg: Config, d: Path, issue_id: str) -> None:
    """Make ``pr-description.md`` auto-close-safe AND keep a deterministic clickable link.

    Two guarantees, neither relying on the model complying with the prompt:

    * **Auto-close (#233):** the closing-keyword trailer stays a BARE ``#<id>`` — strip any
      ``[#<id>](url)`` wrapper off a ``Fixes/Closes/Resolves`` line back to ``#<id>``, since
      GitHub auto-closes only on the bare form.
    * **Click-through (#238 review):** when ``issue_url_pattern`` is configured for a real
      (numeric) ticket, a clickable ``[#<id>](url)`` reference must exist somewhere OTHER
      than that closing trailer — so a weak/omitting model can't drop it. An existing such
      link is kept; else a bare non-closing ``#<id>`` (e.g. a Summary ``Reported in #<id>``)
      is linked; else a ``Reported in [#<id>](url).`` line is inserted at the end of the
      Summary section (or the top of the body if there is no Summary heading).

    Idempotent; a no-op for a non-numeric (slug / ``--no-issue``) id or a missing body.
    """
    if not issue_id.isdigit():
        return
    body_path = d / PR_BODY
    if not body_path.is_file():
        return
    linked = re.compile(r"\[#" + re.escape(issue_id) + r"\]\([^)]*\)")   # `[#123](…)`
    bare = re.compile(r"(?<!\[)#" + re.escape(issue_id) + r"\b")         # bare `#123`
    lines = body_path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False

    # (1) Auto-close: bare the closing-keyword trailer (strip any link wrapper).
    for i, line in enumerate(lines):
        if _CLOSING_VERB_RE.match(line) and linked.search(line):
            new = linked.sub(f"#{issue_id}", line)
            if new != line:
                lines[i], changed = new, True

    # (2) Click-through: guarantee a clickable reference off the closing trailer.
    url = cfg.issue_url_pattern.format(id=issue_id) if cfg.issue_url_pattern else ""
    if url:
        def non_closing(i: int) -> bool:
            return not _CLOSING_VERB_RE.match(lines[i])
        if not any(non_closing(i) and linked.search(lines[i]) for i in range(len(lines))):
            # Prefer linking a bare `#id` already in prose (e.g. Summary "Reported in #123").
            for i in range(len(lines)):
                if non_closing(i) and bare.search(lines[i]):
                    lines[i] = bare.sub(f"[#{issue_id}]({url})", lines[i], count=1)
                    changed = True
                    break
            else:
                # No reference outside the trailer → insert one at the end of the Summary.
                ref = f"Reported in [#{issue_id}]({url}).\n"
                s = next((i for i, ln in enumerate(lines) if _SUMMARY_RE.match(ln)), None)
                if s is not None:
                    end = next((k for k in range(s + 1, len(lines))
                                if lines[k].lstrip().startswith("#")), len(lines))
                    last = max((k for k in range(s + 1, end) if lines[k].strip()), default=s)
                    lines.insert(last + 1, ref)
                else:
                    lines[0:0] = [ref, "\n"]
                changed = True

    if changed:
        body_path.write_text("".join(lines), encoding="utf-8")
