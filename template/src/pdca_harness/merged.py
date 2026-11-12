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
:func:`merge_and_head` answers the follow-up when the dependent is cut from the run's line
for that base (#655): which commit did the merge put on the base, and which head did it
merge — so the flow can ask whether the line has either? Only a dependent built on the line
— one a seam keeps there (#727) — is asked it (#729): one on a base of its own, or on the
real base, is built from the base as fetched, so :func:`merged_into` is the whole answer.
The fold and the pre-wave carry ask it too, for a merged PR whose head is on neither the base
nor the line: a merge commit on the base then means a squash or rebase merge
(``integrate._squash_merged``, #626). So does the flow's readiness check, for a merge whose
record does not count (:func:`counts_into`): only a drop of the edge ends that hold.
:func:`has_patch` answers what comes before all of these (#839): does a bundle ship anything
to merge, and can that even be read? :func:`marked_for_this_patch` answers the one after it:
is that still the patch a PR's merge marker was written for? :func:`for_this_patch` asks the
same of every patch digest a record carries (#865), so :func:`merged_into` never counts the
merge of an older attempt as the bundle's work.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import state
from .config import Config

# The record's key beside ``merge_eligible`` (#839): :func:`patch_hash` of the patch that
# marker was written for.
MARKED_PATCH = "merge_eligible_patch"
# The PR record's key (#865): :func:`patch_hash` of the patch.diff publish pushed for that PR.
PATCH_DIGEST = "patch_digest"


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
    while ``base`` lacks the work. Here a merge counts only when the record says so
    (:func:`counts_into`). With no target to compare against (``repo`` or ``base`` empty: a
    dependent with no usable ``Repo + branch target``, which publish skips), any merge
    counts — :func:`is_merged`'s answer.

    A merge counts as the prerequisite's work only when its PR was published from the
    ``patch.diff`` the bundle holds now (#865), whether or not there is a target: the record
    survives an iterate, so after a rebuild that never reached the PR (a publish that failed
    before its push, or ``--no-publish``) the merged head is an older attempt. A record that
    carries a patch digest is counted only when :func:`for_this_patch` says each one is the
    patch's now; one for another patch, or a patch that cannot be read, is "not merged", said
    on stderr. A record with no digest at all is counted as before: one written before #865, or
    an ``Onto branch`` record, for which publish writes none (and which counts only with no
    target, above). The flow notes that this was not checked (``flow._note_no_digest``).

    Otherwise as :func:`is_merged`: a close/no-fix prereq (COMPLETE, empty or no patch) ⇒
    ``True``; not COMPLETE, or no recorded PR ⇒ ``False``. Fail-closed: a ``gh`` failure,
    ``gh`` missing included (:func:`pr_state` guards it), is "not merged", said on stderr —
    never a traceback; a ``patch.diff`` that cannot be read is no close/no-fix, so its record
    decides (:func:`has_patch`, #839). Only ``flow._runnable`` and the pre-wave merge
    (``flow._merge_prereqs``, #839) call it; :func:`is_merged` and its callers keep their
    answer."""
    d = cfg.find_bundle(dep_id)  # a merged prereq may be archived to completed/ (#171)
    if state.state(d) != state.COMPLETE:
        return False
    ships = has_patch(d)
    if ships is False:
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
    if targeted and not counts_into(rec, repo, base):
        onto = " (an `Onto branch` commit)" if rec.get("mode") == "stacked" else ""
        print(f"merged: {dep_id}'s PR ({pr_url}) merged, but into "
              f"{rec.get('repo') or '?'} @ {rec.get('base') or '?'}{onto}; "
              f"treating as not merged{into}", file=sys.stderr)
        return False
    if for_this_patch(d, rec) is False:
        why = ("its patch.diff cannot be read, so that PR is not known to hold the accepted "
               "patch" if ships is None or patch_hash(d) is None else
               "it was published for a different patch.diff than the one the bundle holds "
               "now (an older attempt)")
        print(f"merged: {dep_id}'s PR ({pr_url}) merged, but {why}; "
              f"treating as not merged{into}", file=sys.stderr)
        return False
    return True


def counts_into(rec: dict, repo: str, base: str) -> bool:
    """Whether a merge of the PR on publish record ``rec`` counts as a merge into ``repo`` @
    ``base`` (#647): the record's ``repo`` and ``base`` are those, and its mode is not
    ``"stacked"``. The one definition: :func:`merged_into` goes by it, and a squash or rebase
    merge whose record fails it needs its edge dropped, since no later merge will count
    (#626: ``integrate.DROP_PREREQ``)."""
    return (rec.get("mode") != "stacked" and rec.get("repo") == repo
            and rec.get("base") == base)


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


def merge_and_head(cfg: Config, dep_id: str) -> tuple[str, str] | None:
    """``(merge commit, head)`` of bundle ``dep_id``'s recorded PR once it has merged, or
    ``None`` (#655): the commits a line can hold that show the merged work is on it.

    Asks ``gh pr view <pr_url> --json state,headRefOid,mergeCommit`` for the PR in
    ``publish.json``, and answers only when ``state`` is ``MERGED``. The merge commit
    (``mergeCommit.oid``) is the commit the merge put on the base, for a merge, squash or
    rebase merge alike. The head (``headRefOid``, as :func:`merged_head` reads it) is on
    the base only after a merge with a merge commit, but it is on any line a fold carried
    the PR's branch onto. Either is "" when ``gh`` reports none. Fail-closed and silent, as
    :func:`pr_state` is: no recorded PR, a PR not merged, a ``gh`` failure (``gh`` missing
    included) or output it cannot parse all return ``None``; the caller names the bundle and
    the reason."""
    rec = _publish_record(cfg.find_bundle(dep_id))
    pr_url = rec.get("pr_url") if isinstance(rec, dict) else None
    info = _pr_view(str(pr_url), "state,headRefOid,mergeCommit") if pr_url else None
    if info is None or info.get("state") != "MERGED":
        return None
    mc, head = info.get("mergeCommit"), info.get("headRefOid")
    oid = mc.get("oid") if isinstance(mc, dict) else None
    return (oid if isinstance(oid, str) else "", head if isinstance(head, str) else "")


def pr_state(pr_url: str) -> tuple[str, str] | None:
    """``(state, head)`` of the PR at ``pr_url`` (``gh pr view --json state,headRefOid``):
    ``OPEN`` / ``CLOSED`` / ``MERGED`` and its head commit ("" when none is reported), or
    ``None`` when it cannot be read — a ``gh`` failure (``gh`` missing included, guarded as
    :func:`merged_head` guards it) or output it cannot parse (#646). Silent: the caller
    names the bundle and the reason."""
    info = _pr_view(pr_url, "state,headRefOid")
    if info is None or not isinstance(info.get("state"), str):
        return None
    head = info.get("headRefOid")
    return info["state"], head if isinstance(head, str) else ""


def _pr_view(pr_url: str, fields: str) -> dict | None:
    """``gh pr view <pr_url> --json <fields>`` read as a JSON object, or ``None`` when it
    cannot be: ``gh`` missing (``OSError``), ``gh`` failing, or output that is not a JSON
    object. Silent — :func:`pr_state` and :func:`merge_and_head` share it (#655; the latter
    is asked only for a dependent built on the run's line, #729, and to recognise a squash or
    rebase merge, #626)."""
    try:
        r = subprocess.run(["gh", "pr", "view", pr_url, "--json", fields],
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def has_patch(d: Path) -> bool | None:
    """Does bundle ``d`` ship a contribution? ``True``: a non-empty ``patch.diff``. ``False``:
    none, or an empty one (a close/no-fix). ``None``: one that cannot be read (not text, or a
    read that failed), which may hold anything, so it is neither: merge mode waits on it and
    never merges it for a dependent (#839). A bundle file can be in any state, so this never
    raises, and a run is not lost to one bad bundle."""
    patch = d / "patch.diff"
    try:
        return patch.is_file() and bool(patch.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):   # ValueError covers a bad decode
        return None


def patch_hash(d: Path) -> str | None:
    """The sha256 of bundle ``d``'s ``patch.diff`` as it is now, every byte counting, or
    ``None`` when it cannot be read (#839)."""
    try:
        return hashlib.sha256((d / "patch.diff").read_bytes()).hexdigest()
    except OSError:
        return None


def marked_for_this_patch(d: Path, rec: dict) -> bool:
    """Was the merge marker in ``d``'s record ``rec`` written for the ``patch.diff`` ``d`` holds
    now (#839)? The record survives an iterate, so after a rebuild that never reached the PR
    (a publish that failed before its push, or ``--no-publish``) the marker is still there and
    the PR's head is still the older attempt. A marker with no hash says nothing about that
    head, so it is not trusted either."""
    return _is_this_patch(d, rec.get(MARKED_PATCH))


def for_this_patch(d: Path, rec: dict) -> bool | None:
    """Was the PR on ``d``'s publish record ``rec`` published from the ``patch.diff`` ``d`` holds
    now (#865)? Asked of every patch digest the record carries: the one publish writes for its
    PR (:data:`PATCH_DIGEST`) and the merge marker's (:data:`MARKED_PATCH`). ``True``: each is
    the patch's :func:`patch_hash` now, as :func:`marked_for_this_patch` compares it. ``False``:
    one is not — another patch's, not a digest at all, or the patch cannot be read — so the PR
    may hold an older attempt. ``None``: the record carries no digest (one written before
    #865, or an ``Onto branch`` record), so nothing can be checked."""
    digests = [rec[k] for k in (PATCH_DIGEST, MARKED_PATCH) if k in rec]
    if not digests:
        return None
    return all(_is_this_patch(d, v) for v in digests)


def _is_this_patch(d: Path, digest: object) -> bool:
    """Is ``digest`` the :func:`patch_hash` of the ``patch.diff`` ``d`` holds now? Never when it
    cannot be read: neither side then has an answer. The one comparison both checks above use."""
    now = patch_hash(d)
    return now is not None and digest == now


def _publish_record(d: Path) -> dict | None:
    """The bundle's ``publish.json`` (the recorded PR), or ``None`` if absent/unreadable."""
    pj = d / "publish.json"
    if not pj.exists():
        return None
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
