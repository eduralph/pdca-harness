"""Merge-mode remark rounds, stage one (issue #921, the first child of #840).

In merge mode a non-final wave's PR can be green and still be refused because review
threads are open on it; issue #838 names them in ``merge-remarks.json`` and the run stops.
With ``[driver].remark_rounds`` above 0 (default 0, off) the flow instead **reopens** the
accepted bundle as a *remark round*: it archives the accepted attempt, records the round in
``remark-round.json``, appends the collaborator threads to ``brief.md`` and drives the
bundle again, built on the PR's own head. This stage stops at the round's sign-off: it
pushes, replies to, resolves and merges nothing. A later stage (#922) pushes the round's
commit; until then :func:`line` is the one sentence every refusal of a round bundle gives,
and nothing here ends a live round: the human deletes its record, and only once the PR has
merged (:data:`ENDS`, which an accepted round's line carries).

What opens a round (:func:`open_round`), checked in this order, each "no" leaving the run
exactly as #838 leaves it:

* the knob is above 0, and the bundle ``merge_wave`` stopped on has a ``merge-remarks.json``
  for the PR its ``publish.json`` records, with a non-empty ``threads`` list;
* at least one thread is a collaborator's: the first comment's ``authorAssociation`` is
  exactly OWNER, MEMBER or COLLABORATOR. Any other value, or none, is recorded as
  ``ignored: non-collaborator``; its body never reaches ``brief.md``;
* fewer rounds are recorded than the knob allows ("remark rounds exhausted" otherwise), and
  at least one collaborator thread is new to the record;
* this run pushed the bundle's branch, so its PR holds the patch accepted here (an earlier
  attempt's PR head would leave that patch out of the round);
* ``[driver].worktree`` is on (the round must build in a worktree on the PR head), and a
  pass of the run's pool is left to drive the round in;
* ``origin/<branch>`` can be fetched: its commit is ``pinned_head``. It must hold only work
  this run pushed — that commit, with at most its base merged in by clean merges: a commit
  someone else pushed on top (a reviewer's committed suggestion), or a merge carrying an
  edit or a hand-resolved conflict, would be on the PR and in no archive the round records,
  so ``drift`` and the round's build would disagree.

Then the reopen: the pre-archive steps iterate-do runs (``driver._retire_cleared_deferrals``,
``driver._carry_plan_advisory_ticks``), ``driver._archive_iteration`` into the next
``iteration-v<n>/``, and only then the record and the carry-forward; every file any of these
moved or wrote goes back if one fails part-way or is interrupted. A live round (the record
exists) moves the bundle's build base to ``pinned_head`` (``worktree._target``), tells the
builder which threads to answer, from the record (:func:`builder_notice`), gives the
per-fix verifier ``PDCA_BASE = origin/<branch>`` after checking the PR head has not moved
(:func:`head_check`), makes ``drift`` apply the archives already on the PR before
``patch.diff`` (:func:`contribution_patches`), and adds the reply plan to SUMMARY.md
(:func:`record_reply_plan`) whose texts T4 checks (:func:`reply_problems`). The driver never
merges its PR, and no dependent is built while the round is live, publishing or not
(``flow._runnable``; ``merged.merged_into`` reads it as not merged whatever its PR reads),
since no PR holds the round's work yet. Nothing else may take ``patch.diff``, the round's
delta, for the contribution either: with ``[driver].worktree`` off a live round is neither
built nor gated (``worktree.ensure`` / ``for_gate`` fail closed with :data:`NEEDS_WORKTREE`),
nor is it with no worktree to be had for it (``ensure`` / ``for_gate`` /
``rebuild_for_gate`` fail closed rather than run in place), ``pdca cleanup`` only reports
it, and ``pdca revert`` refuses it. A failed :func:`head_check` is the human's: the gate
marks it ``stops_rebuilds``, so it is a HUMAN §6 row and auto-iterate stops on it. So is a
gate read of a live round that failed closed (``gates._run_checks``).

Imports only ``state`` and ``config`` at load: ``worktree``, ``gates``, ``merge`` and
``merged`` read it, so everything heavier is imported where it is used.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import state
from .config import Config

RECORD = state.REMARK_ROUND
RESPONSES = state.REMARK_RESPONSES
# Where a merge-mode PR's branch lives: publish pushes a `new-pr` / `stacked-pr` record's
# branch to origin (`integrate._published_ref`), and merge mode refuses every other record
# (`merge._merge_one`, #658) before it can be blocked by remarks.
REMOTE = "origin"
# The `authorAssociation` values whose threads open a round (Plan decision 1, #840).
ELIGIBLE = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
OPEN = "open"
IGNORED = "ignored: non-collaborator"
# A reply is posted as one comment line on the thread: one line, and short.
MAX_REPLY = 500
# Why a round needs worktrees: said when one cannot open (:func:`open_round`) and when a
# live one is asked to build or gate with them off (`worktree.ensure` / `for_gate`).
NEEDS_WORKTREE = ("a remark round needs [driver].worktree = true — it builds on the PR's own "
                  "head in a worktree, and with worktrees off Do would build on the target "
                  "base")
# How a live round ends in this stage, which has nothing that ends one (#922, #923): by hand,
# and only once its PR has merged. Deleted before that, the bundle reads as an ordinary
# accepted one, whose publish cuts a fresh branch off the base and force-pushes it over the
# PR (`publish.publish`): patch.diff, the round's delta, without the accepted work.
ENDS = (f"The round ends only when {RECORD} is deleted, and only after the PR has merged: "
        "deleted before that, the next publish of this bundle would force-push patch.diff, "
        "this delta alone, over the PR")

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ARCHIVE = re.compile(r"iteration-v\d+")
_LINE_BREAK = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85  ]")

# A git patch's extended header lines, as `git apply` reads them (apply.c's header table):
# the ones naming a rename's source, and all of them (a line that is none ends the header).
_RENAME_FROM = re.compile(rb"rename (?:from|old) (.*)")
_GIT_HEADER = re.compile(rb"(?:old mode|new mode|deleted file mode|new file mode|copy from|"
                         rb"copy to|rename old|rename new|rename from|rename to|"
                         rb"similarity index|dissimilarity index|index) ")
# git's C-style quoting of a path (quote.c): the one-letter escapes, and three octal digits.
_C_ESCAPES = {ord(k): ord(v) for k, v in zip('abfnrtv"\\', '\a\b\f\n\r\t\v"\\')}
_OCTAL = re.compile(rb"[0-3][0-7]{2}")


# -- the record --------------------------------------------------------------------------

def live(d: Path) -> bool:
    """True while bundle ``d`` has a remark round: its ``remark-round.json`` exists."""
    p = d / RECORD
    return p.exists() or p.is_symlink()


def read(d: Path) -> dict | None:
    """``d``'s round record; ``None`` when it has none, ``{}`` when it exists but cannot be
    read as a JSON object (still live: every reader fails closed on it)."""
    if not live(d):
        return None
    try:
        rec = json.loads((d / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _rounds(rec: dict) -> int:
    n = rec.get("rounds")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0


def _pinned(rec: dict) -> str:
    head = rec.get("pinned_head")
    return head if isinstance(head, str) and _SHA.fullmatch(head) else ""


def pinned_head(d: Path) -> str | None:
    """The commit a live round builds on; ``None`` with no round, ``""`` when the record
    names no usable one (the callers then fail closed rather than build elsewhere)."""
    rec = read(d)
    return None if rec is None else _pinned(rec)


def _branch(rec: dict) -> str:
    b = rec.get("branch")
    return b if isinstance(b, str) and b else ""


def line(d: Path) -> str:
    """The one sentence that says what a round bundle is waiting for. No pushing, replying
    or merging of a round exists yet (#922, #923), so it never advises ``pdca publish``.
    An accepted round's also says how the round ends (:data:`ENDS`)."""
    rec = read(d) or {}
    n = rec.get("rounds", "?")
    url = rec.get("pr_url") or "?"
    st = state.state(d)
    if st == state.COMPLETE:
        return (f"remark round {n} for {url} accepted; round publish not available yet — "
                f"apply patch.diff to {_branch(rec) or '?'} by hand. {ENDS}")
    if st == state.DISCONTINUED:
        return (f"remark round {n} for {url} discontinued; the PR is still open and is the "
                "human's to close")
    return f"remark round {n} for {url} is {st}; round publish not available yet"


def _write(path: Path, data: dict) -> None:
    """``data`` as JSON at ``path`` in one step (a sibling temp file, then ``os.replace``).
    Whatever stops it, an interrupt included, leaves no temp file behind."""
    tmp = path.with_name(f".{path.name}.partial")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


# -- threads -----------------------------------------------------------------------------

def _eligible(t: dict) -> bool:
    a = t.get("association")
    return isinstance(a, str) and a in ELIGIBLE


def _where(t: dict) -> str:
    """``path:line``, or ``path`` alone for a thread on a whole file."""
    path = t.get("path") or "?"
    ln = t.get("line")
    return str(path) if ln is None else f"{path}:{ln}"


def _listing(threads: list[dict]) -> str:
    return ", ".join(f"{t['id']} {_where(t)}{' (outdated)' if t.get('outdated') else ''} "
                     f"by {t.get('author') or '?'}" for t in threads)


def _ignored_text(threads: list[dict]) -> str:
    return (f"; ignored (non-collaborator): {_listing(threads)}" if threads else "")


def _remarks(d: Path, pr_url: str) -> list[dict]:
    """The threads of ``d``'s ``merge-remarks.json``, when it describes ``pr_url``: a file
    for another PR, an unreadable one, or ``threads`` that is not a non-empty list gives
    none. A thread with no id cannot be recorded, so it is left out."""
    try:
        data = json.loads((d / state.MERGE_REMARKS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("pr_url") != pr_url:
        return []
    threads = data.get("threads")
    if not isinstance(threads, list):
        return []
    return [t for t in threads if isinstance(t, dict) and isinstance(t.get("id"), str)
            and t["id"]]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _fetch_head(primary: Path, branch: str) -> tuple[str, str]:
    """Fetch ``origin/<branch>`` into the checkout ``primary`` and read its commit:
    ``(sha, "")``, or ``("", why)`` when it cannot be read (offline, the branch deleted
    because the PR was merged or closed, not a git checkout)."""
    if not (primary / ".git").exists():
        return "", f"{primary} is not a git checkout"
    ref = f"refs/remotes/{REMOTE}/{branch}"
    r = _git(primary, "fetch", "--quiet", REMOTE, f"+refs/heads/{branch}:{ref}")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        return "", (f"`git fetch {REMOTE} {branch}` failed in {primary} "
                    f"({tail[-1] if tail else f'exit {r.returncode}'})")
    r = _git(primary, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    head = (r.stdout or "").strip()
    if r.returncode != 0 or not _SHA.fullmatch(head):
        return "", f"{REMOTE}/{branch} does not resolve in {primary} after the fetch"
    return head, ""


def _not_only_pushed_here(cfg: Config, primary: Path, branch: str, head: str,
                          base: object) -> str:
    """"" when PR head ``head`` holds only work this run pushed, else the reason it does not,
    naming the commit this run pushed and the head found.

    The commit this run pushed is the local ``<branch>`` its publish cut, committed and
    pushed in the checkout ``primary`` (the round opens only for a bundle pushed this run).
    The head may be that commit, or that commit with its base merged in (the merge step's
    own update of a head behind its base, #531): every commit on it that is neither this
    run's nor on ``<base_remote>/<base>`` is a merge, and each of those merges is the clean
    merge of its two parents (:func:`_not_a_clean_merge`). Anything else — a commit pushed
    on top by someone else (a reviewer's committed suggestion), a merge that carries an edit
    of its own or a conflict resolved by hand, or a head that no longer contains the pushed
    commit — is work the round's record would not list, so ``drift`` and the round's build
    would disagree about what the PR holds. Read only from local refs the run has just
    fetched; any git failure is a refusal."""
    r = _git(primary, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    mine = (r.stdout or "").strip()
    if r.returncode != 0 or not _SHA.fullmatch(mine):
        return (f"the commit this run pushed to {branch} cannot be read in {primary}, so the "
                f"PR head {head} cannot be shown to hold only this run's work")
    if mine == head:
        return ""
    found = f"this run pushed {mine} to {branch}, and the PR head is {head}"
    unshown = "so it cannot be shown to hold only this run's work"
    r = _git(primary, "merge-base", "--is-ancestor", mine, head)
    if r.returncode == 1:
        return (f"the PR head holds commits this run did not push — {found}, which does not "
                "contain it (the branch was rewritten)")
    if r.returncode != 0:
        return f"{found}; `git merge-base` failed in {primary}, {unshown}"
    if not (isinstance(base, str) and base):
        return f"{found}; its publish record names no base to tell the base's commits by, {unshown}"
    base_ref = f"{cfg.base_remote}/{base}"
    # Each commit on the head that is neither this run's nor the base's, with its parents.
    r = _git(primary, "rev-list", "--parents", head, f"^{mine}", f"^refs/remotes/{base_ref}",
             "--")
    if r.returncode != 0:
        return f"{found}; `git rev-list` against {base_ref} failed in {primary}, {unshown}"
    listed = [ln.split() for ln in r.stdout.splitlines() if ln.strip()]
    extra = [c for c in listed if len(c) < 3]          # one parent, or none: not a merge
    if extra:
        return (f"the PR head holds commits this run did not push — {found}, with "
                f"{len(extra)} commit(s) on it that are neither this run's nor {base_ref}'s (a "
                "reviewer's committed suggestion, say); a round records only the work this "
                "run pushed")
    for merge, *parents in listed:
        why = _not_a_clean_merge(primary, merge, parents)
        if why:
            return (f"the PR head holds work this run did not push — {found}; {why}; a round "
                    "records only the work this run pushed")
    return ""


def _not_a_clean_merge(primary: Path, merge: str, parents: list[str]) -> str:
    """"" when merge commit ``merge`` is the clean merge of its two ``parents``: its tree is
    the one ``git merge-tree --write-tree`` makes of them (git 2.38 or later), the only merge
    the merge step's own update of a branch makes (``gh pr update-branch`` fails on a
    conflict). Else what it holds beyond that, which no archive the round records lists: an
    edit made in the merge (GitHub's conflict editor, or ``git merge`` and then an edit), a
    conflict resolved by hand (which may drop part of the accepted work), or a third parent.
    A git failure is a refusal too."""
    if len(parents) != 2:
        return (f"merge commit {merge} on it has {len(parents)} parents; an update of the "
                "branch from its base has two")
    r = _git(primary, "merge-tree", "--write-tree", "--no-messages", *parents)
    if r.returncode == 1:
        return (f"merge commit {merge} on it resolves a conflict between its parents by hand, "
                "so its tree is no clean merge of them")
    clean = (r.stdout or "").split("\n", 1)[0].strip()
    if r.returncode != 0 or not _SHA.fullmatch(clean):
        tail = (r.stderr or "").strip().splitlines()
        return (f"`git merge-tree --write-tree` (git 2.38 or later) could not merge the parents "
                f"of merge commit {merge} in {primary} "
                f"({tail[-1] if tail else f'exit {r.returncode}'}), so it cannot be shown to be "
                "a clean merge")
    r = _git(primary, "rev-parse", "--verify", "--quiet", f"{merge}^{{tree}}")
    tree = (r.stdout or "").strip()
    if r.returncode != 0 or not _SHA.fullmatch(tree):
        return f"the tree of merge commit {merge} cannot be read in {primary}"
    if tree != clean:
        return (f"merge commit {merge} on it is not the clean merge of its parents: it carries "
                "an edit of its own (GitHub's conflict editor, or `git merge` and then an "
                "edit)")
    return ""


def _checkout(cfg: Config, d: Path) -> Path | None:
    """The checkout ``d``'s worktree lives beside, resolved as ``worktree._target`` does."""
    from . import publish
    try:
        repo_spec = publish._resolve_target(d)[0]
    except (OSError, ValueError):
        return None
    return publish._checkout_path(cfg, repo_spec) if repo_spec else None


# -- opening a round ---------------------------------------------------------------------

def open_round(cfg: Config, d: Path, *, pushed: bool,
               passes_left: int) -> tuple[bool, str]:
    """Reopen accepted bundle ``d`` as a remark round, if its stop calls for one.

    ``d`` is the bundle ``merge.merge_wave`` stopped on in this run (its caller passes no
    other), and ``pushed`` says whether this run pushed its branch. An iterate keeps an
    earlier attempt's ``publish.json``, so a bundle accepted again but not pushed (its
    publish texts failed, say) still names a PR, whose head is that earlier attempt: a
    round on it would record the patch accepted here as on the PR when it is not, and build
    the round without it. The same holds for a PR head with work this run did not push (a
    reviewer's committed suggestion, or a merge carrying an edit of its own): the round
    would build on work its record does not list, so it is not opened either.
    ``passes_left`` is what is left of the run's pass pool: a round opened with none left
    would be archived and never driven. Returns
    ``(True, what opened)`` with ``d`` back at PLANNED, or ``(False, why not)``: ``why`` is
    "" where the run stops exactly as #838 leaves it with nothing to add (rounds off, no
    remarks file for this PR, no threads in it), else the one line to add to that stop.
    Nothing is fetched before every check up to the fetch has passed, and nothing is moved
    or written before the PR head's check after it. A reopen that fails part-way puts back
    every file it moved or wrote (the brief and the record included) and returns why; one
    that is interrupted (Ctrl-C) puts them back the same way, says so on stderr, and lets
    the interrupt go on.
    """
    # lazy: each of these imports this module, directly or through another
    from . import assemble, autoiterate, driver, integrate, publish

    cap = getattr(cfg, "remark_rounds", 0)
    if not isinstance(cap, int) or cap <= 0:
        return False, ""
    pub = publish._publish_record(d)
    pr_url = pub.get("pr_url") if isinstance(pub, dict) else None
    if not isinstance(pr_url, str) or not pr_url:
        return False, ""
    threads = _remarks(d, pr_url)
    if not threads:
        return False, ""
    old = read(d)
    if old == {}:
        return False, (f"remark round not opened for {pr_url}: its {RECORD} exists but "
                       "cannot be read")
    eligible = [t for t in threads if _eligible(t)]
    ignored = [t for t in threads if not _eligible(t)]
    if not eligible:
        return False, (f"remark round not opened for {pr_url}: no open thread is a "
                       f"collaborator's{_ignored_text(ignored)}")
    used = _rounds(old) if old else 0
    if used >= cap:
        return False, (f"remark rounds exhausted for {pr_url}: {used} of [driver]."
                       f"remark_rounds = {cap} used; open collaborator threads: "
                       f"{_listing(eligible)}{_ignored_text(ignored)}")
    known = (old or {}).get("threads")
    known = known if isinstance(known, dict) else {}
    new = [t for t in eligible if t["id"] not in known]
    if not new:
        return False, (f"remark round not opened for {pr_url}: every open collaborator "
                       f"thread ({', '.join(t['id'] for t in eligible)}) was answered in "
                       "an earlier round")
    if not pushed:
        return False, (f"remark round not opened for {pr_url}: this run pushed no branch for "
                       f"{d.name}, so its PR does not hold the patch accepted here — a round "
                       "built on that PR's head would leave the accepted work out")
    if not cfg.worktree:
        return False, f"remark round not opened for {pr_url}: {NEEDS_WORKTREE}"
    if passes_left <= 0:
        return False, (f"remark round not opened for {pr_url}: pass budget spent — no pass of "
                       "this run's pool is left to drive the round in")
    # Where the PR's branch is, read as the fold and the merge read it (#650): only a branch
    # publish pushed to origin, never an `Onto branch` commit on someone else's PR.
    ref = integrate._published_ref(d)
    branch = (ref[1].removeprefix(f"{REMOTE}/") if ref is not None and not ref[2]
              and ref[0] == REMOTE else "")
    primary = _checkout(cfg, d)
    if not branch or primary is None:
        return False, (f"remark round not opened for {pr_url}: its publish record names no "
                       "PR branch on origin to build on")
    pinned, why = _fetch_head(primary, branch)
    if why:
        return False, (f"remark round not opened for {pr_url}: the PR head could not be "
                       f"read — {REMOTE}/{branch}: {why}")
    why = _not_only_pushed_here(cfg, primary, branch, pinned, pub.get("base"))
    if why:
        return False, f"remark round not opened for {pr_url}: {why}"

    # The reopen. Every file it moves or writes is put back if any step fails part-way, an
    # interrupt (Ctrl-C) included, so a failed reopen leaves P as #838 leaves it: COMPLETE,
    # its accepted patch.diff at the top level, no new round record and the brief as it was.
    # First the steps iterate-do runs before it archives (#335, #603), so §6 rows the human
    # cleared at the accept stay cleared in the round's SUMMARY (they write the deferred
    # ledger and the plan-advisory record); then the archive; only then the record and the
    # carry-forward.
    try:
        kept = {name: _snapshot(d / name)
                for name in (autoiterate.DEFERRED_FILE, assemble.PLAN_ADVISORY_CLEARED,
                             RECORD, "brief.md")}
    except OSError as exc:
        return False, (f"remark round not opened for {pr_url}: a file it would change cannot "
                       f"be read, so it could not be put back ({exc}); nothing was moved")
    moved: list[tuple[Path, Path]] = []
    arch, step = "", "reading the §6 rows cleared at the accept"
    try:
        driver._retire_cleared_deferrals(d, cfg)
        driver._carry_plan_advisory_ticks(d)
        n = driver._next_iteration_no(d)
        arch = f"iteration-v{n}"
        step = f"archiving the accepted attempt to {arch}/"
        driver._archive_iteration(d, n, include_brief=False, moved=moved)
        step = f"writing {RECORD}"
        rounds = used + 1
        record = {
            "rounds": rounds,
            "pr_url": pr_url,
            "branch": branch,
            "pinned_head": pinned,
            "contribution": [*_names(old), arch],
            "threads": dict(known),
        }
        for t in eligible + ignored:
            if t["id"] not in known:
                record["threads"][t["id"]] = {
                    "path": t.get("path"), "line": t.get("line"),
                    "author": t.get("author") or "", "outdated": bool(t.get("outdated")),
                    "url": t.get("url") or "", "status": OPEN if _eligible(t) else IGNORED,
                    "round": rounds}
                if _eligible(t):
                    # Kept for the builder (:func:`builder_notice`): an iterate-to-Plan
                    # archives the brief and the block below with it. Never a
                    # non-collaborator's text, which no builder is shown.
                    record["threads"][t["id"]]["body"] = str(t.get("body") or "")
        _write(d / RECORD, record)
        step = "appending the round's carry-forward to brief.md"
        with (d / "brief.md").open("a", encoding="utf-8") as fh:
            fh.write(_carry_forward(rounds, pr_url, branch, pinned, arch, new))
    except BaseException as exc:
        back = _undo(d, moved, arch, kept)
        failed = isinstance(exc, Exception)
        what = (f"failed ({type(exc).__name__}: {exc})" if failed
                else f"was interrupted ({type(exc).__name__})")
        why = f"remark round not opened for {pr_url}: {step} {what}{back}"
        if not failed:
            # Ctrl-C or an exit: the bundle is put back first, then it stops the run.
            print(f"flow: {d.name} — {why}.", file=sys.stderr)
            raise
        return False, why
    fresh_ignored = [t for t in ignored if t["id"] not in known]
    return True, (f"remark round {rounds} opened for {pr_url} on {pinned} "
                  f"({REMOTE}/{branch}); the accepted attempt is in {arch}/; collaborator "
                  f"threads: {_listing(new)}{_ignored_text(fresh_ignored)}")


def _names(rec: dict | None) -> list[str]:
    got = (rec or {}).get("contribution")
    return [x for x in got if isinstance(x, str)] if isinstance(got, list) else []


def _snapshot(p: Path) -> bytes | None:
    """``p``'s bytes, or ``None`` when it does not exist. A file that exists and cannot be
    read raises ``OSError``: the reopen could not put it back, so it changes nothing."""
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None


def _restore(p: Path, data: bytes | None) -> str:
    """Put file ``p`` back to ``data`` (``None``: it did not exist); "" or what failed.

    A file that was only appended to (the brief) is cut back to its old length, which needs
    no free space — the append may have failed for want of it. Any other change is replaced
    whole through a temporary file, so a failed restore never leaves it half-written."""
    try:
        if data is None:
            p.unlink(missing_ok=True)
            return ""
        now = p.read_bytes() if p.is_file() else None
        if now == data:
            return ""
        if now is not None and now.startswith(data):
            os.truncate(p, len(data))
            return ""
        tmp = p.with_name(f".{p.name}.restore")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, p)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        return ""
    except OSError as exc:
        return f"{p.name} ({exc})"


def _move_back(moved: list[tuple[Path, Path]], arch: Path | None) -> list[str]:
    """Move each archived file back where it came from, last first; the ones that would not
    move. An entry whose destination does not exist never moved (the move that failed, or
    one an interrupt stopped first), so it is left where it is."""
    stuck = []
    for src, dst in reversed(moved):
        if not (dst.exists() or dst.is_symlink()):
            continue
        try:
            dst.rename(src)
        except OSError as exc:
            stuck.append(f"{dst} ({exc})")
    if arch is not None:
        with contextlib.suppress(OSError):
            arch.rmdir()        # only if the failure left it empty
    return stuck


def _undo(d: Path, moved: list[tuple[Path, Path]], arch: str,
          kept: dict[str, bytes | None]) -> str:
    """Put back everything a failed reopen of ``d`` changed: the archived files, then each
    file in ``kept`` (name → the bytes it had, ``None``: none). Returns the end of the stop
    line: what is back, or what is not."""
    stuck = _move_back(moved, d / arch if arch else None)
    stuck += [why for name, data in kept.items() if (why := _restore(d / name, data))]
    if stuck:
        return (f"; could NOT put back {'; '.join(stuck)} — move or restore it by hand "
                "before the next run")
    return "; every file it had moved or written is back as it was"


def _carry_forward(n: int, pr_url: str, branch: str, pinned: str, arch: str,
                   threads: list[dict]) -> str:
    """The round's block for ``brief.md``: the threads to answer and how. Only collaborator
    threads new to this round appear in it."""
    out = [f"\n## Remark round {n} — carry-forward (review threads on {pr_url})\n",
           "- This bundle was accepted and published, and merging its PR was blocked by the "
           "review threads below. This round rebuilds it on the PR's own head, "
           f"`{pinned}` ({REMOTE}/{branch}): the Do worktree is checked out there.\n",
           "- `patch.diff` is the DELTA against that head — only this round's change. The "
           f"accepted attempt is archived in `{arch}/` and is already on the PR.\n",
           "- Threads to answer:\n", *_thread_lines(threads)]
    out.append(f"- Write `{RESPONSES}` in the bundle with one entry per thread above: "
               '`{"<thread id>": {"addressed": true or false, "reply": "<one line>"}}`. '
               "`addressed: true` counts only if `patch.diff` changes that thread's file. "
               f"The reply is the exact text a later step posts on the thread: one line, at "
               f"most {MAX_REPLY} characters.\n")
    out.append("- Nothing is pushed, replied to, resolved or merged in this round: the human "
               "signs off the reply plan in SUMMARY.md.\n")
    return "".join(out)


def _thread_lines(threads: list[dict]) -> list[str]:
    """Each thread as a builder reads it: id, ``path:line``, author and url, then its text
    quoted line by line. Shared by the brief's block and :func:`builder_notice`."""
    out = []
    for t in threads:
        out.append(f"  - `{t['id']}` — {_where(t)}{' (outdated)' if t.get('outdated') else ''}"
                   f" by {t.get('author') or '?'} — {t.get('url') or '(no url)'}\n")
        body = str(t.get("body") or "").replace("\r\n", "\n").replace("\r", "\n")
        out += [f"    > {ln}\n" for ln in (body.splitlines() or ["(no text)"])]
    return out


def builder_notice(d: Path) -> str:
    """What the builder of bundle ``d`` is told beyond the standard prompt, which asks for a
    diff against the target branch: in a round the worktree is the PR's head, ``patch.diff``
    is the diff against that head, and each thread of the round gets an entry in
    ``remark-responses.json``. "" outside a round, so the prompt is byte-identical there.

    The threads come from the record, not from the brief: a round signed iterate-to-Plan
    archives the brief, and the round's carry-forward block with it, while the round goes
    on (same ``rounds``, same ``pinned_head``), so the re-planned brief may not list them."""
    rec = read(d)
    if rec is None:
        return ""
    threads = [{**e, "id": tid} for tid, e in _this_round(rec)]
    pinned, branch = _pinned(rec), _branch(rec)
    head = f"`{pinned}` ({REMOTE}/{branch})" if pinned and branch else "its PR's head"
    out = [f"\n\nThis bundle is in REMARK ROUND {_rounds(rec)} for {rec.get('pr_url') or '?'}: "
           f"$PDCA_WORKTREE is checked out at {head}, not at the target branch, and "
           "patch.diff must be the diff against THAT head — this round's change only, not the "
           f"whole contribution. Also write {RESPONSES} in the bundle with one entry per "
           'review thread below: {"<thread id>": {"addressed": true or false, "reply": '
           '"<one line>"}}. `addressed: true` counts only if patch.diff changes that '
           "thread's file. The reply is the exact text a later step posts on the thread: one "
           f"line, at most {MAX_REPLY} characters. The threads, from {RECORD}:\n"]
    out += _thread_lines(threads) or [f"  (none: {RECORD} lists no thread for this round)\n"]
    return "".join(out).rstrip("\n")


# -- building and gating on the PR head --------------------------------------------------

def head_check(cfg: Config, d: Path) -> tuple[str, str]:
    """The base the per-fix verifier of round bundle ``d`` resets to: ``(origin/<branch>,
    "")`` once a fresh fetch shows that branch still at ``pinned_head``, so the gate tests
    on the commit Do built on. Else ``("", why)``, and the gate fails closed: "PR head
    moved" naming both commits, or "PR head unreadable" (the branch deleted, the PR merged
    or closed by hand, offline). It never falls back to the target base.

    Nothing in this stage moves a round to a new head, so a moved head leaves the round out
    of date for good: its line says so and names the way out — discontinue the round and
    re-run — right after the two commits, ahead of anything a gate row may cut off. No
    rebuild changes either failure, so the gate row that carries it is marked
    ``stops_rebuilds`` (``gates._run_one``): §6 lists it as the human's, and auto-iterate
    stops on it instead of rebuilding on the same ``pinned_head``."""
    rec = read(d) or {}
    pinned, branch = _pinned(rec), _branch(rec)
    if not pinned or not branch:
        return "", (f"PR head unreadable: {RECORD} names no usable pinned_head and branch; "
                    "the round's gate does not fall back to the target base")
    primary = _checkout(cfg, d)
    if primary is None:
        return "", ("PR head unreadable: the brief names no checkout to fetch "
                    f"{REMOTE}/{branch} in; the round's gate does not fall back to the "
                    "target base")
    head, why = _fetch_head(primary, branch)
    if why:
        return "", (f"PR head unreadable: {why}; the round's gate does not fall back to "
                    "the target base")
    if head != pinned:
        return "", (f"PR head moved: {pinned} -> {head}; this round is out of date: "
                    f"discontinue it (`pdca signoff {d.name.removeprefix('issue_')} "
                    f"--discontinue`) and re-run. {REMOTE}/{branch} no longer points at the "
                    "commit the round was built on, so a gate on it would not test what Do "
                    "built")
    return f"{REMOTE}/{branch}", ""


def contribution_patches(d: Path) -> tuple[list[Path] | None, str]:
    """For ``drift`` (Plan decision 3): the ``patch.diff`` of each archive already on round
    bundle ``d``'s PR, in order, to apply before its own ``patch.diff`` (the delta). ``(None,
    "")`` with no round; ``(None, why)`` when the record cannot say."""
    rec = read(d)
    if rec is None:
        return None, ""
    names = rec.get("contribution")
    if not isinstance(names, list) or not names:
        return None, f"{RECORD} lists no contribution archive"
    out = []
    for name in names:
        if not isinstance(name, str) or not _ARCHIVE.fullmatch(name):
            return None, f"{RECORD} lists {name!r}, which is not an iteration archive"
        p = d / name / "patch.diff"
        if not p.is_file():
            return None, f"{name}/patch.diff is missing"
        out.append(p)
    return out, ""


# -- the reply plan ----------------------------------------------------------------------

def responses(d: Path) -> tuple[dict | None, str]:
    """The builder's ``remark-responses.json`` as a mapping, or ``(None, why)``."""
    p = d / RESPONSES
    if not p.exists():
        return None, f"{RESPONSES} is missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{RESPONSES} cannot be read ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, f"{RESPONSES} is not a JSON object"
    return data, ""


def _this_round(rec: dict) -> list[tuple[str, dict]]:
    """The collaborator threads the current round opened, in record order."""
    threads = rec.get("threads")
    n = _rounds(rec)
    return [(tid, e) for tid, e in (threads.items() if isinstance(threads, dict) else ())
            if isinstance(e, dict) and e.get("status") == OPEN and e.get("round") == n]


def _reply(resp: dict | None, tid: str) -> str:
    entry = resp.get(tid) if resp else None
    reply = entry.get("reply") if isinstance(entry, dict) else None
    return reply if isinstance(reply, str) else ""


def _unquote_c(s: bytes) -> bytes | None:
    """``s``, which opens with ``"``, unquoted as git's ``unquote_c_style`` does: the bytes
    up to the closing quote with each ``\\`` escape undone (``\\303`` is one byte); None
    where git's would fail (an unknown escape, no closing quote)."""
    out = bytearray()
    i = 1
    while i < len(s):
        c = s[i]
        if c == 0x22:                                   # the closing quote
            return bytes(out)
        if c != 0x5C:
            out.append(c)
            i += 1
        elif s[i + 1:i + 2] and s[i + 1] in _C_ESCAPES:
            out.append(_C_ESCAPES[s[i + 1]])
            i += 2
        elif _OCTAL.fullmatch(s[i + 1:i + 4]):
            out.append(int(s[i + 1:i + 4], 8))
            i += 4
        else:
            return None
    return None


def _rename_sources(raw: bytes) -> set[str]:
    """The paths git patch ``raw`` renames a file FROM, read from its extended headers as
    ``git apply`` reads them: a ``rename from`` (or older ``rename old``) line in the
    header that follows a ``diff --git`` line, the name C-unquoted when git quoted it, else
    up to the line's end, with runs of ``/`` squashed. A copy's source (``copy from``) is
    never one: the copy leaves it as it was."""
    out: set[str] = set()
    in_header = False
    for ln in raw.split(b"\n"):
        if ln.startswith(b"diff --git "):
            in_header = True
        elif in_header and (m := _RENAME_FROM.match(ln)):
            rest = m.group(1)
            name = _unquote_c(rest) if rest.startswith(b'"') else None
            if name is None:                            # as git: read it as it stands
                name = re.match(rb"[^\r]*", rest).group(0)
            if name:
                out.add(os.fsdecode(re.sub(rb"/{2,}", b"/", name)))
        elif in_header and not _GIT_HEADER.match(ln):
            in_header = False                           # `---`, `@@`, or no header at all
    return out


def _changed_files(d: Path) -> tuple[set[str], str]:
    """``(files, "")``: the files round bundle ``d``'s ``patch.diff`` changes, read by
    ``git apply --numstat -z`` as git itself applies the patch — a name with a space, one
    git quoted (``"a/caf\\303\\251.py"`` is ``café.py``), and a hunk line that only looks
    like a header all come out right. The reversed patch names each file's old name too; of
    those only a rename's source counts (:func:`_rename_sources`), never a copy's, whose
    source the patch does not change. Empty for a round that changed nothing. ``(set(),
    why)`` when git cannot read the patch: no thread is addressed on a patch nobody can read.

    Run in the bundle directory with git's repository search stopped there
    (``GIT_CEILING_DIRECTORIES``): run below the top of a repository, ``git apply`` drops,
    without a word, every path outside that directory, and a bundle directory usually sits
    inside the instance's repository."""
    patch = d / "patch.diff"
    try:
        raw = patch.read_bytes()
    except FileNotFoundError:
        return set(), ""
    except OSError as exc:
        return set(), f"patch.diff cannot be read ({exc})"
    if not raw.strip():
        return set(), ""
    renamed = _rename_sources(raw)
    where = d.resolve()
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    env["GIT_CEILING_DIRECTORIES"] = str(where.parent)
    files: set[str] = set()
    for reverse in ([], ["-R"]):
        try:
            r = subprocess.run(["git", "apply", "--numstat", "-z", *reverse,
                                str(where / patch.name)], cwd=where, env=env,
                               capture_output=True)
        except OSError as exc:
            return set(), f"git could not be run to read patch.diff ({exc})"
        if r.returncode != 0:
            tail = r.stderr.decode("utf-8", "replace").strip().splitlines()
            return set(), (f"git apply cannot read patch.diff "
                           f"({tail[-1] if tail else f'exit {r.returncode}'})")
        for entry in r.stdout.split(b"\0"):
            parts = entry.split(b"\t", 2)      # added, deleted, then the name as it is
            if len(parts) == 3 and parts[2]:
                name = os.fsdecode(parts[2])
                if not reverse or name in renamed:
                    files.add(name)
    return files, ""


def _held(tid: str, entry: dict, rows: list[str]) -> bool:
    """A §6 row of this round's Check names the thread: its id, or its exact
    ``path:line`` (no other line, no other path ending the same way). A whole-file thread
    is held only by its id."""
    by_id = re.compile(r"(?<![\w-])" + re.escape(tid) + r"(?![\w-])")
    if any(by_id.search(r) for r in rows):
        return True
    path, ln = entry.get("path"), entry.get("line")
    if not (isinstance(path, str) and path and isinstance(ln, int)
            and not isinstance(ln, bool)):
        return False
    at = re.compile(r"(?<![\w./-])" + re.escape(f"{path}:{ln}") + r"(?!\d)")
    return any(at.search(r) for r in rows)


def record_reply_plan(d: Path, rows: list[str]) -> list[str]:
    """Judge each thread of round bundle ``d``'s current round, write the verdict and the
    reply into its ``remark-round.json`` entry, and return the SUMMARY.md section showing
    them; ``[]`` with no round. ``rows`` are the §6 rows raised by THIS round's Check — not
    the standing Validation row, not rows carried from an earlier attempt.

    * **held** — a row in ``rows`` names the thread (:func:`_held`);
    * **addressed** — ``remark-responses.json`` says ``"addressed": true`` (a JSON bool) and
      this round's ``patch.diff`` changes the thread's file (:func:`_changed_files`);
    * **declined** — everything else; every thread when the responses file is missing or
      cannot be read.

    Nothing is posted: this only records what a later stage (#923) will post."""
    rec = read(d)
    if rec is None:
        return []
    if not rec:
        return ["## Remark round — reply plan", "",
                f"- {RECORD} cannot be read, so there is no reply plan to sign off.", ""]
    resp, missing = responses(d)
    touched, unread = _changed_files(d)
    n = _rounds(rec)
    plan = []
    for tid, e in _this_round(rec):
        said = resp.get(tid) if resp else None
        if missing:
            verdict = "declined"
        elif _held(tid, e, rows):
            verdict = "held"
        elif (isinstance(said, dict) and said.get("addressed") is True
              and e.get("path") in touched):
            verdict = "addressed"
        else:
            verdict = "declined"
        e["verdict"], e["reply"] = verdict, _reply(resp, tid)
        plan.append((tid, e))
    _write(d / RECORD, rec)
    out = [f"## Remark round {n} — reply plan", "",
           "Each collaborator thread this round answers, the verdict the driver gives it and "
           "the exact reply a later step posts (nothing is posted, resolved or merged by this "
           "round). T4 checks every reply.", ""]
    if missing:
        out.append(f"- {_shown(missing)}, so every thread is declined.")
    elif unread:
        out.append(f"- {_shown(unread)}, so no thread counts as addressed.")
    for tid, e in plan:
        reply = _shown(json.dumps(e["reply"], ensure_ascii=False)) if e["reply"] else "(none)"
        out.append(f"- `{_shown(tid)}` {_shown(_where(e))}"
                   f"{' (outdated)' if e.get('outdated') else ''} by "
                   f"{_shown(e.get('author') or '?')} — **{e['verdict']}** — reply: {reply}")
    ignored = [tid for tid, e in (rec.get("threads") or {}).items()
               if isinstance(e, dict) and e.get("status") == IGNORED and e.get("round") == n]
    if ignored:
        out.append("- Not answered (non-collaborator): "
                   f"{', '.join(f'`{_shown(t)}`' for t in ignored)}")
    out.append("")
    return out


def _shown(text: object) -> str:
    """``text`` (as ``str``) for one line of SUMMARY.md, with each character ``str.splitlines``
    ends a line at (:data:`_LINE_BREAK`) written as its ``\\u`` escape.

    Every SUMMARY.md reader splits it that way (``signoff._section``), and the reply plan sits
    between §6 and §9. Unescaped, a reply (or a path, an author, a thread id) holding U+2028,
    U+2029 or ``\\x85`` would start a line of its own there: a ``## 9.`` heading, the first
    one, which every §9 reader takes for the real §9, so the human's sign-off could not be
    recorded or read; or a ``## 6.`` heading, the last one, which the §6 readers take, so C6
    would count that line's ticks instead of the real §6's open rows. ``json.dumps`` escapes
    the other line breaks (``\\n``, ``\\x1c``…) but not these three; escaped here, a quoted
    reply is still that exact reply as a JSON string, for the human to sign off."""
    return _LINE_BREAK.sub(lambda m: f"\\u{ord(m.group()):04x}", str(text))


def reply_problems(d: Path) -> list[str]:
    """The T4 rules for a round's replies, checked as the commit message and PR body are
    (``cli.contribution_problems``): every collaborator thread of the current round has a
    reply that is not empty, is one line, and is at most :data:`MAX_REPLY` characters.
    ``[]`` with no round."""
    rec = read(d)
    if rec is None:
        return []
    if not rec:
        return [f"{RECORD} cannot be read, so the remark round's replies cannot be checked"]
    resp, _missing = responses(d)
    out = []
    for tid, e in _this_round(rec):
        reply = _reply(resp, tid)
        about = f"the reply to review thread {tid} ({_where(e)}) in {RESPONSES}"
        if not reply.strip():
            out.append(f"{about} is empty — each collaborator thread gets a one-line reply")
        elif _LINE_BREAK.search(reply):
            out.append(f"{about} is not one line")
        elif len(reply) > MAX_REPLY:
            out.append(f"{about} is {len(reply)} characters, over {MAX_REPLY}")
    return out


def ignored_threads(d: Path) -> str:
    """The non-collaborator threads ``d``'s record ignored, for a stop line, or "none"."""
    threads = (read(d) or {}).get("threads")
    names = [f"{tid} {_where(e)}" for tid, e in (threads.items()
             if isinstance(threads, dict) else ()) if isinstance(e, dict)
             and e.get("status") == IGNORED]
    return ", ".join(names) or "none"
