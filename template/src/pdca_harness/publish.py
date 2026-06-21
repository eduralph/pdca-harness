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

from . import brief, leaves, state
from .config import Config

COMMIT_MSG = "commit-msg.txt"
PR_BODY = "pr-description.md"


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
) -> int:
    """Contribute an accepted bundle's fix as a draft PR. Return a process code.

    ``skip_if_no_target`` (set by the flow): a bundle whose brief names no upstream
    ``Repo + branch target`` is a non-contributing cycle (e.g. an internal fix) —
    warn and return 0 rather than erroring, so it doesn't fail the continuous flow.

    ``pending_id`` (``--no-issue``): the first-class "no tracker id yet" path. A
    project may need to contribute before a tracker number is assigned; rather than a
    magic ``Fixes #0000`` placeholder, declare it here. The T4 contribution gate is
    then **relaxed to a flag** instead of a hard block, and the bundle is recorded
    ``id_pending`` so the human adds the real id and re-gates T4 before marking the PR
    ready. The publisher leaf omits the trailer (no invented id) in this case.
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

    # Artifacts the T4 gate needs — write them with the publisher leaf if absent.
    if not ((d / COMMIT_MSG).is_file() and (d / PR_BODY).is_file()):
        print("publish: drafting contribution artifacts "
              f"({COMMIT_MSG} / {PR_BODY})…", file=sys.stderr)
        leaves.run_publish(d, cfg)
    if not ((d / COMMIT_MSG).is_file() and (d / PR_BODY).is_file()):
        print(f"publish: {COMMIT_MSG} / {PR_BODY} still missing — aborting", file=sys.stderr)
        return 1

    # T4 contribution gate — the artifacts MUST pass before anything is pushed,
    # UNLESS pending_id (--no-issue): then a T4 failure is relaxed to a flag, since the
    # one thing legitimately missing is the not-yet-assigned tracker id. The bundle is
    # recorded id_pending so the human adds the id and re-gates T4 before ready.
    if not _t4_passes(cfg, d):
        if pending_id:
            print(f"publish: T4 contribution gate not satisfied on {COMMIT_MSG} / "
                  f"{PR_BODY} — proceeding in --no-issue (pending-id) mode; the "
                  "contribution is FLAGGED. Add the tracker id and re-run T4 before "
                  "marking the PR ready.", file=sys.stderr)
        else:
            print(f"publish: T4 contribution gate FAILED on {COMMIT_MSG} / {PR_BODY} — "
                  "fix them and retry", file=sys.stderr)
            return 1

    # Stack mode (issue #54): the brief names an existing PR's head branch — contribute a
    # commit onto it instead of a new PR. The shared spine above (guard, target, artifacts,
    # T4) already ran; the branch/steps/PR step are what differ.
    onto = brief.onto_branch(d / "brief.md")
    if onto is not None:
        return _publish_stacked(cfg, d, repo_spec, onto,
                                dry_run=dry_run, by=by, today=today, pending_id=pending_id)

    branch = _branch_name(cfg, d, slug)
    summary_line = (d / COMMIT_MSG).read_text(encoding="utf-8").splitlines()[0]
    repo = _checkout_path(cfg, repo_spec)

    git = lambda *a: ["git", "-C", str(repo), *a]
    base_remote = cfg.base_remote
    steps = [
        git("fetch", base_remote),
        git("checkout", "-B", branch, f"{base_remote}/{base}"),
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
        git("push", "-u", "origin", branch),
    ]
    # A fork-based PR's --head must be OWNER:BRANCH — `gh` resolves a bare branch name
    # against the *base* repo (where the fork branch doesn't exist) and fails with
    # "Head ref must be a branch". The branch lives on origin (the fork).
    head = f"{_fork_owner(repo) or repo_spec.split('/')[0]}:{branch}"
    pr_cmd = ["gh", "pr", "create", "--draft", "--repo", repo_spec, "--base", base,
              "--head", head, "--title", summary_line,
              "--body-file", str((d / PR_BODY).resolve())]

    if dry_run:
        print(f"publish --dry-run — {d.name} → draft PR on {repo_spec} ({branch} → {base}):")
        print(f"  # stash the target working tree (Do/Check leave it dirty), restore it after")
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

    (d / "publish.json").write_text(json.dumps({
        "mode": "new-pr",
        "branch": branch, "pr_url": pr_url, "base": base, "repo": repo_spec,
        "by": by or _signoff_by(d) or cfg.author or "unknown", "date": today,
        "id_pending": pending_id,
    }, indent=2) + "\n", encoding="utf-8")

    # A requested-but-failed PR is a partial run, not a success — the branch is
    # pushed but the cycle isn't done. Exit non-zero so `flow` doesn't read the
    # empty pr_url as "published".
    if pr_failed:
        return 1

    print(f"\nDraft PR prepared on {repo_spec} ({branch} → {base}).")
    if pr_url:
        print(f"  {pr_url}\n  watch CI:  gh pr checks {pr_url} --watch")
    if pending_id:
        print("  ⚠ id_pending: contributed without a tracker id — add the trailer "
              "(Fixes #N) and re-run T4 before marking the PR ready.")
    print("  STOP: review CI, then mark it ready / merge yourself — the human's step.")
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

    (d / "publish.json").write_text(json.dumps({
        "mode": "stacked",
        "branch": branch, "pr_url": pr_url, "base": base_ref, "repo": repo_spec,
        "by": by or _signoff_by(d) or cfg.author or "unknown", "date": today,
        "id_pending": pending_id,
    }, indent=2) + "\n", encoding="utf-8")

    print(f"\nCommit stacked onto {repo_spec} PR branch {branch} ({pr_url}).")
    print(f"  watch CI:  gh pr checks {pr_url} --watch")
    if pending_id:
        print("  ⚠ id_pending: contributed without a tracker id — add the trailer "
              "(Fixes #N) and re-run T4 before marking the PR ready.")
    print("  STOP: review CI, then mark it ready / merge yourself — the human's step.")
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
def _clean_ref(raw: str) -> str:
    """Isolate a git ref / repo spec from a brief field side, tolerating markdown
    backticks and trailing prose. A ref / ``owner/repo`` has no spaces, so prefer a
    backtick-quoted span, else the first whitespace token; strip stray backticks and
    trailing sentence punctuation."""
    raw = raw.strip()
    m = re.search(r"`([^`]+)`", raw)              # markdown code span wins
    token = m.group(1) if m else (raw.split()[0] if raw.split() else "")
    return token.strip("`").rstrip(",.;:")


def _resolve_target(d: Path) -> tuple[str, str, str]:
    """``(repo_spec, base_branch, slug)`` from the brief, e.g.
    ``("example-org/example-repo", "main", "fix-the-thing")``.

    The target field is commonly written with markdown backticks and/or trailing
    prose after the branch; ``_clean_ref`` isolates the ref on each side of ``@`` so
    that style doesn't corrupt the resolved checkout/base (see #25)."""
    bp = d / "brief.md"
    target = brief.field(bp, "repo + branch target", "repo + branch", "target")
    repo_spec, _, base = target.partition("@")
    slug = brief.field(bp, "slug") or d.name.removeprefix("issue_")
    return _clean_ref(repo_spec), _clean_ref(base), _slugify(slug)


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


def _fork_owner(repo: Path, remote: str = "origin") -> str:
    """The GitHub owner of ``remote`` (the fork the branch is pushed to), e.g.
    ``"example-user"`` from ``git@github.com:example-user/repo.git`` or the https form.
    Used to form the cross-repo PR ``--head OWNER:BRANCH`` (and the stack-mode existing-PR
    lookup). ``""`` if undetectable."""
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", remote],
                         capture_output=True, text=True).stdout.strip()
    m = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return m.group(1) if m else ""


def _t4_passes(cfg: Config, d: Path) -> bool:
    """Run every configured T4-tier gate over the bundle. No T4 gate → nothing to
    enforce (True). Keeps publish decoupled from any one project's checker."""
    t4 = [c for c in cfg.gates_checks if c.get("tier") == "T4"]
    if not t4:
        return True
    env = {**os.environ, "PDCA_BUNDLE": str(d)}
    for chk in t4:
        r = subprocess.run(chk.get("cmd", ""), shell=True, cwd=cfg.root, env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print((r.stdout or r.stderr).strip(), file=sys.stderr)
            return False
    return True


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
