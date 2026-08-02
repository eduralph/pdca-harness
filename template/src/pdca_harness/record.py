"""Record — commit terminal-finished result bundles to the instance repo (issue #317).

The driver's state IS the files in the bundle directory (``state.py``): an
uncommitted terminal bundle is provenance that exists on one machine only, and the
forgetting is silent — observed as four bundles uncommitted for five days, one of
them DISCONTINUED with its §9 the sole provenance for an open upstream PR.

``pdca record [<ids>…]`` selects the bundles whose cycle is OVER — ``state.state``
in ``state.TERMINAL`` — and commits the batch as ONE commit with the configured
conventional subject; ``[records] mode = "pr"`` additionally branches, pushes and
opens one draft PR for the whole batch. The classification is CONSUMED from
``state``, never re-enumerated here: a bundle in motion, or halted for a human, is
excluded by construction — which is the argument for the engine owning this rather
than an instance script re-implementing the state machine and drifting.

Deterministic ``git``/``gh`` subprocesses in the ``publish.py`` shape — no model in
the loop. ``[records] mode = "off"`` (the default) disables everything: no new
behaviour anywhere, including for instances that do not version ``results/``.
"""

from __future__ import annotations

import datetime
import shlex
import subprocess
import sys
from pathlib import Path

from . import state
from .config import Config


def select(cfg: Config, ids: list[str] | None = None) -> list[Path]:
    """The bundles a record run operates on: terminal-finished only.

    With ``ids``, each named bundle is checked and a missing or non-terminal one is
    reported and EXCLUDED — never committed: the selection predicate is the safety
    property (a bundle in motion must not be frozen into the repo mid-cycle), so an
    explicit id does not override it. With no ids, every ``issue_*`` bundle is
    scanned. Classification is ``state.state`` against ``state.TERMINAL`` — the
    driver's own primitive, not a local re-enumeration (#317).
    """
    if ids:
        dirs = [cfg.bundle(i) for i in ids]
    else:
        dirs = sorted(cfg.bundle_root.glob("issue_*")) if cfg.bundle_root.exists() else []
    picked: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            print(f"record: no such bundle: {d} — excluded", file=sys.stderr)
            continue
        s = state.state(d)
        if s in state.TERMINAL:
            picked.append(d)
        elif ids:
            print(f"record: {d.name} is {s}, not terminal-finished — excluded",
                  file=sys.stderr)
    return picked


def record(cfg: Config, ids: list[str] | None = None, *, dry_run: bool = False,
           today: str | None = None, interactive: bool | None = None) -> int:
    """Commit the terminal-finished bundles as one batch commit; optionally one PR.

    Batch-by-default: one invocation → one commit (and, in pr mode, one PR) for
    everything selected. Returns a process code: 0 = recorded (or legitimately
    nothing to record), 1 = a git/gh step failed, 2 = disabled ([records] mode
    = "off", the default).
    """
    mode = cfg.records_mode
    if mode == "off":
        print('record: [records] mode = "off" (the default) — set mode = "commit" or '
              '"pr" in pdca.toml to enable recording', file=sys.stderr)
        return 2

    bundles = select(cfg, ids)
    if not bundles:
        print("record: no terminal-finished bundles to record.")
        return 0

    today = today or datetime.date.today().isoformat()
    root = cfg.root
    names = [d.name.removeprefix("issue_") for d in bundles]
    subject = _format(cfg.records_subject, n=len(bundles), ids=", ".join(names),
                      date=today)
    paths = [_repo_path(d, root) for d in bundles]
    git = lambda *a: ["git", "-C", str(root), *a]

    # `issue = "ask"` (pr mode): a records PR references its tracker issue (the
    # one-issue-per-PR instance rule). Interactive → ask once; headless (a flow's
    # publish call-in, a piped run) or --dry-run → fall back to commit-only and SAY
    # so — never hang a headless run on input() (the brief's open question, resolved
    # toward "skip PR mode and report").
    issue = str(cfg.records_issue).strip().lstrip("#")
    if mode == "pr" and issue.lower() == "ask":
        if interactive is None:
            interactive = sys.stdin.isatty()
        if dry_run or not interactive:
            print('record: [records] issue = "ask" but this run cannot ask '
                  "(headless / dry-run) — recording the commit only; open the PR "
                  "yourself or set issue = <N> in pdca.toml", file=sys.stderr)
            mode, issue = "commit", ""
        else:
            issue = input("record: tracker issue # for the records PR "
                          "(empty = commit only): ").strip().lstrip("#")
            if not issue:
                mode = "commit"

    branch = _format(cfg.records_branch, date=today)
    steps = [
        git("add", "-A", "--", *paths),
        # ONE commit for the whole batch. The pathspec scopes the commit to the
        # recorded bundles even when the operator has unrelated changes staged —
        # publish stages-then-commits the same way (publish.py:264-268), but there
        # the checkout is dedicated; here it is the instance's own working repo, so
        # the commit must not sweep a human's half-staged work in.
        git("commit", "-m", subject, "--", *paths),
    ]
    pr_cmd: list[str] | None = None
    if mode == "pr":
        # Branch + push the CURRENT head — deliberately no `checkout -B` (the
        # publish shape at publish.py:257 switches branches in a dedicated target
        # checkout; flipping the instance repo's own branch would strip the just-
        # committed bundle files from the working tree until the PR merges).
        # `--force-with-lease` for the same re-run reason as publish.py:276.
        steps += [
            git("branch", "-f", branch, "HEAD"),
            git("push", "--force-with-lease", "-u", "origin", branch),
        ]
        body_lines = [f"Record {len(bundles)} terminal-finished result bundle(s):", ""]
        body_lines += [f"- {d.name}: {state.state(d)}" for d in bundles]
        if issue:
            trailer = (_format(cfg.issue_trailer, id=issue) if cfg.issue_trailer
                       else f"#{issue}")
            body_lines += ["", trailer]
        pr_cmd = ["gh", "pr", "create", "--draft", "--base", cfg.default_branch,
                  "--head", branch, "--title", subject,
                  "--body", "\n".join(body_lines) + "\n"]

    if dry_run:
        print(f"record --dry-run — {len(bundles)} bundle(s) "
              f"({', '.join(d.name for d in bundles)}) → one commit"
              + (f" + one PR ({branch} → {cfg.default_branch})" if pr_cmd else "")
              + ":")
        for c in steps + ([pr_cmd] if pr_cmd else []):
            print("  " + " ".join(shlex.quote(x) for x in c))
        return 0

    # Stage first, then probe: a batch whose every selected bundle is already
    # committed is a quiet success, not a failing `git commit` ("nothing to commit").
    add = steps[0]
    if subprocess.run(add).returncode != 0:
        print(f"record: step failed: {' '.join(add)}", file=sys.stderr)
        return 1
    probe = subprocess.run(git("diff", "--cached", "--quiet", "--", *paths))
    if probe.returncode == 0:
        print(f"record: {len(bundles)} terminal-finished bundle(s) already "
              "recorded — nothing new to commit.")
        return 0
    if probe.returncode != 1:  # 1 = differences; anything else is a git error
        print("record: step failed: "
              + " ".join(git("diff", "--cached", "--quiet", "--", *paths)),
              file=sys.stderr)
        return 1
    for c in steps[1:]:
        print("→ " + " ".join(c[3:]))  # drop the `git -C <repo>` prefix in the echo
        if subprocess.run(c).returncode != 0:
            print(f"record: step failed: {' '.join(c)}", file=sys.stderr)
            return 1
    if pr_cmd:
        print("→ gh pr create --draft …")
        r = subprocess.run(pr_cmd, cwd=str(root), capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            print("record: commit made and branch pushed, but `gh pr create` "
                  "FAILED — open the PR by hand.", file=sys.stderr)
            return 1
        out = (r.stdout or "").strip()
        if out:
            print(out)
    print(f"record: committed {len(bundles)} bundle(s) — {subject}")
    return 0


def after_publish(cfg: Config) -> None:
    """Publish's recording call-in (#317) — called STRICTLY after :func:`publish.publish`
    wrote ``publish.json`` (its closing write, publish.py:374 / :487), never mid-publish.

    Best-effort by contract: changing what publish itself does is out of scope, so a
    recording problem is reported with the manual fallback and NEVER fails the publish
    that triggered it. ``mode = "off"`` (the default) returns immediately — the publish
    path stays byte-identical to today. Batch-by-default: the call records every
    terminal-finished bundle, not only the one just published.
    """
    if cfg.records_mode == "off":
        return
    try:
        rc = record(cfg)
        if rc != 0:
            print(f"record: post-publish recording did not complete (rc {rc}) — run "
                  "`pdca record` by hand; the publish itself succeeded.",
                  file=sys.stderr)
    except Exception as exc:  # never let bookkeeping break a completed publish
        print(f"record: post-publish recording failed — {exc}; run `pdca record` "
              "by hand; the publish itself succeeded.", file=sys.stderr)


def _repo_path(d: Path, root: Path) -> str:
    """``d`` as a git pathspec for the instance repo at ``root`` — relative when
    possible (the normal ``results/`` layout), absolute otherwise (a redirected
    ``PDCA_BUNDLE_ROOT``; git resolves absolute pathspecs inside the repo itself)."""
    try:
        return str(d.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(d)


def _format(pattern: str, **kw) -> str:
    """``.format`` tolerant of an instance pattern with unknown / no placeholders —
    the config keys are instance data, and a KeyError out of a bookkeeping verb helps
    no one; the raw pattern is the honest fallback."""
    try:
        return pattern.format(**kw)
    except (KeyError, IndexError, ValueError):
        return pattern
