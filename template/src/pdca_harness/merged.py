"""Is a prerequisite's contribution merged into its base? (issue #107)

``Depends on`` gates a dependent until its prerequisite reaches **COMPLETE** — but
COMPLETE means "a draft PR was opened", not merged. A dependent's Do runs in a worktree
off the target base (``origin/<base>``), which does **not** contain a prereq whose PR is
still open, so file-overlapping work is built without the predecessor's diff and its PR
conflicts at merge. The stricter ``Depends on (merged):`` field gates the dependent until
the prereq is genuinely merged; this module answers "is it merged yet?".

Merge state is read from the prerequisite bundle's recorded PR (``publish.json``) via
``gh pr view --json state``. It is **best-effort and fail-closed**: anything we cannot
confirm as merged (no PR yet, or a ``gh`` failure) returns ``False`` so the dependent
stays safely blocked rather than building off an unmerged base — the dependent is then
picked up by a later ``pdca flow`` run, after the prereq's PR is merged.

:func:`merged_head` answers the integration fold's narrower question (#593): which commit
did a merged PR's branch end at? The fold compares that SHA with its line when the branch
itself is gone (deleted on merge), so the answer is a commit, never a commit message.
:func:`pr_state` answers the flow's pre-wave carry (#646): is a finished prerequisite's PR
open, merged or closed, and at which head commit?
:func:`merged_into` answers the flow's readiness question for a prerequisite no line of the
run carries (#647): is it merged into the DEPENDENT's target base — that repo and branch,
not just some branch?
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import state
from .config import Config


def is_merged(cfg: Config, dep_id: str) -> bool:
    """True iff prerequisite ``dep_id``'s contribution is merged into its base.

    A close/no-fix prereq (COMPLETE with no patch) ships nothing to merge ⇒ ``True``. A
    prereq not yet COMPLETE, or accepted-but-unpublished, ⇒ ``False`` (wait). Otherwise
    the recorded PR's ``state`` is queried; ``MERGED`` ⇒ ``True``, and any ``gh`` failure
    is treated as not-merged.
    """
    d = cfg.find_bundle(dep_id)  # a merged prereq may be archived to completed/ (#171)
    if state.state(d) != state.COMPLETE:
        return False  # prereq hasn't even finished its own cycle
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return True  # close/no-fix disposition — no contribution to wait on
    rec = _publish_record(d)
    pr_url = rec.get("pr_url") if rec else None
    if not pr_url:
        return False  # accepted but no PR published yet
    r = subprocess.run(["gh", "pr", "view", str(pr_url), "--json", "state"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"merged: could not read PR state for {dep_id} ({pr_url}); "
              "treating as not merged", file=sys.stderr)
        return False
    try:
        return json.loads(r.stdout or "{}").get("state") == "MERGED"
    except ValueError:
        return False


def merged_into(cfg: Config, dep_id: str, repo: str, base: str) -> bool:
    """True iff prerequisite ``dep_id``'s contribution is merged into ``repo`` @ ``base`` —
    the DEPENDENT's target, as ``publish._resolve_target`` resolves it (#647).

    :func:`is_merged` answers "did its PR merge?", into whatever branch the PR targeted. A
    ``"stacked-pr"`` record's PR may target an integration line, and an ``Onto branch``
    record (``"stacked"``) is a commit on another PR's branch, so either can read MERGED
    while ``base`` lacks the work. Here a merge counts only when the record's ``repo`` and
    ``base`` are the dependent's and its mode is not ``"stacked"`` — the rule the fold
    applies to a gone branch (``integrate._gone_branch``). With no target to compare
    against (``repo`` or ``base`` empty: a dependent with no usable ``Repo + branch
    target``, which publish skips), any merge counts — :func:`is_merged`'s answer.

    Otherwise as :func:`is_merged`: a close/no-fix prereq (COMPLETE, empty or no patch) ⇒
    ``True``; not COMPLETE, or no recorded PR ⇒ ``False``. Fail-closed: a ``gh`` failure,
    ``gh`` missing included (:func:`pr_state` guards it), is "not merged", said on stderr —
    never a traceback. Only ``flow._runnable`` calls it; :func:`is_merged` and its callers
    keep their answer."""
    d = cfg.find_bundle(dep_id)  # a merged prereq may be archived to completed/ (#171)
    if state.state(d) != state.COMPLETE:
        return False
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return True  # close/no-fix disposition — no contribution to wait on
    rec = _publish_record(d)
    pr_url = rec.get("pr_url") if isinstance(rec, dict) else None
    if not pr_url:
        return False  # accepted but no PR published yet
    targeted = bool(repo and base)
    into = f" into {repo} @ {base}" if targeted else ""
    st = pr_state(str(pr_url))
    if st is None:
        print(f"merged: could not read PR state for {dep_id} ({pr_url}); "
              f"treating as not merged{into}", file=sys.stderr)
        return False
    if st[0] != "MERGED":
        return False
    if targeted and (rec.get("mode") == "stacked" or rec.get("repo") != repo
                     or rec.get("base") != base):
        onto = " (an `Onto branch` commit)" if rec.get("mode") == "stacked" else ""
        print(f"merged: {dep_id}'s PR ({pr_url}) merged, but into "
              f"{rec.get('repo') or '?'} @ {rec.get('base') or '?'}{onto}; "
              f"treating as not merged{into}", file=sys.stderr)
        return False
    return True


def merged_head(cfg: Config, dep_id: str) -> str | None:
    """The head commit bundle ``dep_id``'s recorded PR merged with, or ``None`` (#593).

    Asks ``gh pr view <pr_url> --json state,headRefOid`` for the PR in ``publish.json``
    and returns ``headRefOid`` only when ``state`` is ``MERGED``. Fail-closed, as
    :func:`is_merged` is: no recorded PR, a PR not merged, a ``gh`` failure (``gh``
    missing included) or output it cannot parse all return ``None``, which the caller
    treats as "not known to be merged".
    """
    rec = _publish_record(cfg.find_bundle(dep_id))
    pr_url = rec.get("pr_url") if isinstance(rec, dict) else None
    if not pr_url:
        return None
    try:
        r = subprocess.run(["gh", "pr", "view", str(pr_url), "--json", "state,headRefOid"],
                           capture_output=True, text=True)
    except OSError:
        r = None
    if r is None or r.returncode != 0:
        print(f"merged: could not read PR state for {dep_id} ({pr_url}); "
              "treating as not merged", file=sys.stderr)
        return None
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(info, dict) or info.get("state") != "MERGED":
        return None
    head = info.get("headRefOid")
    return head if isinstance(head, str) and head else None


def pr_state(pr_url: str) -> tuple[str, str] | None:
    """``(state, head)`` of the PR at ``pr_url`` (``gh pr view --json state,headRefOid``):
    ``OPEN`` / ``CLOSED`` / ``MERGED`` and its head commit ("" when none is reported), or
    ``None`` when it cannot be read — a ``gh`` failure (``gh`` missing included, guarded as
    :func:`merged_head` guards it) or output it cannot parse (#646). Silent: the caller
    names the bundle and the reason."""
    try:
        r = subprocess.run(["gh", "pr", "view", pr_url, "--json", "state,headRefOid"],
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(info, dict) or not isinstance(info.get("state"), str):
        return None
    head = info.get("headRefOid")
    return info["state"], head if isinstance(head, str) else ""


def _publish_record(d: Path) -> dict | None:
    """The bundle's ``publish.json`` (the recorded PR), or ``None`` if absent/unreadable."""
    pj = d / "publish.json"
    if not pj.exists():
        return None
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
