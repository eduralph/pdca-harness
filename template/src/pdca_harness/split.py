"""Parse a `split-proposal.md` and materialise its children (issues #322 / #323).

The splitter leaf writes prose; this module is the deterministic half that reads it back
and turns it into runnable bundles. No model in this path.

## Why the delimiters are HTML comments

Each child body is a **full draft brief** and may contain arbitrary headings and fenced
code blocks. So the boundary marker cannot be anything that could also appear *inside* a
child — a `##` heading, a `---` rule and a bare `- **Slug:**` line are all things a child
legitimately contains. `<!-- pdca:child child-1 -->` cannot collide with brief content,
survives every Markdown renderer as invisible, and carries a version so the format can
change without silently misparsing old proposals.

## Why acceptance is transactional

`--accept` writes one bundle per child. A failure halfway through — a duplicate id, a
collision with an existing bundle, an unresolvable label — would otherwise leave some
children created and some not, with the parent already rewritten. That state is worse than
either outcome: the human cannot re-run (the ids now exist) and cannot proceed (the batch
is incomplete). So everything is validated **before anything is written**, and the writes
are staged and moved into place only once all of them succeed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import state

PROPOSAL = "split-proposal.md"
_VERSION_RE = re.compile(r"<!--\s*pdca:split-proposal\s+v(\d+)\s*-->")
_OPEN_RE = re.compile(r"^\s*<!--\s*pdca:child\s+(\S+)\s*-->\s*$")
_CLOSE_RE = re.compile(r"^\s*<!--\s*pdca:end\s+(\S+)\s*-->\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LABEL_RE = re.compile(r"^child-\d+$")
SUPPORTED_VERSION = 1

#: The ordering fields whose values are proposal-local labels and must be rewritten to real
#: tracker ids at acceptance. Getting this list wrong is the failure that makes the whole
#: feature pointless: `compute_waves` reads exactly these.
ORDERING_FIELDS = ("Depends on", "Conflicts with")


def _unfenced(text: str):
    """`(line, in_fence)` for each line, so callers can skip fenced content.

    One definition, used by BOTH the field reader and the label rewriter. They have to
    agree: a fenced `- **Depends on:** child-2` that the rewriter changes but the reader
    ignores — or vice versa — is two views of the same document, which is how a validated
    proposal materialises into something different from what was reviewed.
    """
    open_fence: tuple[str, int] | None = None
    for line in text.splitlines():
        m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if m:
            marker, rest = m.group(1), m.group(2)
            if open_fence is None:
                open_fence = (marker[0], len(marker))
                yield line, True
                continue
            if (marker[0] == open_fence[0] and len(marker) >= open_fence[1]
                    and not rest.strip()):
                open_fence = None
            yield line, True
            continue
        yield line, open_fence is not None


class SplitError(Exception):
    """A proposal that cannot be accepted as written. Always raised BEFORE any write."""


@dataclass(frozen=True)
class Child:
    label: str
    body: str

    def ordering(self, field: str) -> list[str]:
        """The labels named by one ordering field — `[]` when absent or a placeholder.

        Fenced content is skipped: a child illustrating the format in a code block would
        otherwise have its EXAMPLE validated as a real sibling reference, failing the
        cycle or unknown-label check on a proposal that is perfectly well formed.
        """
        found: list[str] | None = None
        for line, fenced in _unfenced(self.body):
            if fenced:
                continue
            m = re.match(rf"^\s*-\s*\*{{0,2}}{re.escape(field)}\*{{0,2}}:\*{{0,2}}\s*(.*)$",
                         line, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if value.startswith("<"):
                    # An unfilled placeholder is not an answer — keep looking. Returning
                    # here let a placeholder followed by a REAL value pass validation
                    # unchecked while `rewrite_ordering` still rewrote the real one, so the
                    # child shipped a dependency nobody had reviewed. `brief.parse_fields`
                    # then keeps the FIRST field, so `compute_waves` read the placeholder,
                    # saw no dependency, and scheduled both children in one wave.
                    found = found if found is not None else []
                    continue
                return [t.strip() for t in value.split(",") if t.strip()]
        return found or []


def parse(text: str) -> list[Child]:
    """The children a proposal declares, in document order.

    Order is load-bearing: `--accept` maps them to the tracker ids the human passes
    positionally, so a parser that reordered them would silently mis-assign every id.
    """
    m = _VERSION_RE.search(text)
    if not m:
        raise SplitError(
            f"{PROPOSAL} carries no `<!-- pdca:split-proposal vN -->` marker — it was not "
            "written from templates/split-proposal.md.tpl, or the marker was edited away")
    version = int(m.group(1))
    if version != SUPPORTED_VERSION:
        raise SplitError(f"{PROPOSAL} is format v{version}; this harness reads "
                         f"v{SUPPORTED_VERSION}")
    children = _scan(text)
    if not children:
        raise SplitError(f"{PROPOSAL} declares no children — expected at least one "
                         "`<!-- pdca:child child-N -->` … `<!-- pdca:end child-N -->` block")
    seen: set[str] = set()
    for child in children:
        if not _LABEL_RE.match(child.label):
            raise SplitError(f"child label {child.label!r} is not of the form `child-N`")
        if child.label in seen:
            raise SplitError(f"child label {child.label!r} is declared twice")
        seen.add(child.label)
    return children


def _scan(text: str) -> list[Child]:
    """Child blocks, ignoring delimiters inside fenced code.

    A child body is a full draft brief and may legitimately contain a fenced example of
    this very format. A regex over the whole text treats the first `<!-- pdca:end -->`
    inside such a fence as the real terminator and silently DROPS every field after it —
    the success criterion, the ordering fields — producing a materialised child that is
    quietly incomplete. So scanning is line-based and fence-aware.

    An unterminated or mismatched block is an ERROR, never a skip: `findall` would return
    the well-formed children and drop the malformed one, and acceptance would proceed
    whenever the id count happened to match the shortened list — permanently omitting a
    child that is plainly visible in the reviewed proposal.
    """
    children: list[Child] = []
    open_label: str | None = None
    buf: list[str] = []
    fenced = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(line):
            fenced = not fenced
        if not fenced:
            m = _OPEN_RE.match(line)
            if m:
                if open_label is not None:
                    raise SplitError(
                        f"line {lineno}: `{m.group(1)}` opens while `{open_label}` is "
                        "still open — child blocks cannot nest")
                open_label, buf = m.group(1), []
                continue
            m = _CLOSE_RE.match(line)
            if m:
                if open_label is None:
                    raise SplitError(f"line {lineno}: `pdca:end {m.group(1)}` closes a "
                                     "child that was never opened")
                if m.group(1) != open_label:
                    raise SplitError(
                        f"line {lineno}: `pdca:end {m.group(1)}` does not match the open "
                        f"`{open_label}` — a mistyped label would silently drop the child")
                children.append(Child(open_label, "\n".join(buf) + "\n"))
                open_label = None
                continue
        if open_label is not None:
            buf.append(line)
    if open_label is not None:
        raise SplitError(f"`{open_label}` is never closed — expected "
                         f"`<!-- pdca:end {open_label} -->`")
    return children


def _cycles(children: list[Child]) -> list[str]:
    """Child labels caught in a `Depends on` cycle — checked BEFORE anything is written.

    Two children depending on each other pass the sibling-reference test (each names a
    real sibling, neither names itself), so without this the command creates every bundle,
    marks the parent split, and the `pdca flow` it just told the human to run dies in
    `waves.check_dep_graph` — leaving a materialised batch that cannot be driven without
    hand-editing bundles.
    """
    deps = {c.label: set(c.ordering("Depends on")) for c in children}
    seen: set[str] = set()
    stack: set[str] = set()
    bad: list[str] = []

    def walk(label: str) -> bool:
        if label in stack:
            return True
        if label in seen:
            return False
        seen.add(label)
        stack.add(label)
        hit = any(walk(dep) for dep in deps.get(label, ()) if dep in deps)
        stack.discard(label)
        return hit

    for label in deps:
        seen.clear()
        stack.clear()
        if walk(label):
            bad.append(label)
    return bad


def preflight(parent: Path, children: list[Child], cfg) -> None:
    """Every reason acceptance would fail that does NOT depend on the ids.

    Split out of :func:`validate` because filing happens BEFORE the ids exist, and a
    tracker issue cannot be withdrawn (#358). Without this, `pdca split <id> --accept`
    run a second time filed a whole second set of real sub-issues and only THEN
    discovered the parent was already split — and a proposal with a dependency cycle
    filed its children before `validate` refused them.
    """
    proposal = parent / PROPOSAL
    if not proposal.exists():
        raise SplitError(f"{parent.name} has no {PROPOSAL} — run `pdca split "
                         f"{parent.name.replace('issue_', '')}` first")
    if (parent / state.CLOSE_MARKER).exists():
        raise SplitError(
            f"{parent.name} is already marked "
            f"{(parent / state.CLOSE_MARKER).read_text(encoding='utf-8').strip()!r} — a "
            "second acceptance would create a duplicate set of children and leave the "
            "first orphaned from the parent's breadcrumb. Reopen it first if that is what "
            "you want")
    _validate_ordering(children)


def _validate_ordering(children: list[Child]) -> None:
    """The proposal's internal consistency — no ids involved."""
    labels = {c.label for c in children}
    for child in children:
        for field in ORDERING_FIELDS:
            for ref in child.ordering(field):
                if ref not in labels:
                    raise SplitError(
                        f"{child.label}'s `{field}` names {ref!r}, which is not a child of "
                        "this proposal — ordering fields reference sibling labels, not "
                        "tracker ids (those are assigned by --ids)")
                if ref == child.label:
                    raise SplitError(f"{child.label}'s `{field}` names itself")
    cyclic = _cycles(children)
    if cyclic:
        raise SplitError(
            f"the proposal's `Depends on` fields form a cycle among {', '.join(cyclic)} — "
            "the children could be created but never driven (compute_waves would refuse "
            "them). Fix the ordering in the proposal first")


def validate(children: list[Child], ids: list[str], cfg) -> None:
    """Every reason acceptance would fail, checked before a single file is written."""
    if len(children) != len(ids):
        raise SplitError(
            f"the proposal declares {len(children)} child(ren) but --ids names "
            f"{len(ids)} — refusing to guess which id belongs to which child")
    if len(set(ids)) != len(ids):
        raise SplitError(f"--ids contains duplicates: {', '.join(ids)}")

    # A tracker id is a token, not a path. `cfg.bundle("x/foo")` yields
    # `results/issue_x/foo`, whose NAME is "foo" — so validation checked one path while the
    # move installed to `results/foo`, nesting into a pre-existing directory and recording
    # it as created. A later failure then rolled that directory back: `rmtree` on something
    # this command never made.
    from . import brief as _brief
    for issue_id in ids:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", issue_id) or issue_id in (".", ".."):
            raise SplitError(
                f"--ids contains {issue_id!r}, which is not a plain tracker id — ids may "
                "hold letters, digits, dot, underscore and hyphen only")
        # And it must survive the SCHEDULER's own parser. `_id_list` treats a lowercase
        # token with no digit as prose, so `--ids alpha,beta` would write
        # `Depends on: alpha`, `compute_waves` would read no dependency at all, and the
        # children would run in one wave — the ordering fields failing silently, which is
        # the one outcome this whole feature exists to prevent. Validated by round-trip
        # rather than by a second pattern, so the two cannot drift.
        if _brief._id_list(issue_id) != [issue_id]:
            raise SplitError(
                f"--ids contains {issue_id!r}, which the dependency parser does not read "
                "as an id — a `Depends on` naming it would be silently ignored and the "
                "children would run in one wave. Use a tracker id containing a digit")

    # The same checks `preflight` runs before filing. Repeated here, not merely delegated
    # from the CLI, because `accept()` is reachable directly (`--ids`, and every test) and
    # must never depend on a caller having run them.
    _validate_ordering(children)

    for issue_id in ids:
        d = cfg.bundle(issue_id)
        # `completed/` too: an archived id recreated as an active bundle would shadow the
        # COMPLETE one that another brief's `Depends on` was already satisfied by.
        archived = cfg.bundle_root / "completed" / d.name
        if archived.exists():
            raise SplitError(
                f"{d.name} already exists in {archived.parent} — reusing a completed "
                "tracker id would shadow the archived bundle for any dependent brief")
        if d.exists():
            raise SplitError(
                f"bundle {d.name} already exists — refusing to overwrite it. Pick unused "
                "tracker ids, or move the existing bundle aside first")


def rewrite_ordering(body: str, mapping: dict[str, str]) -> str:
    """Replace proposal-local labels with real tracker ids in the ordering fields ONLY.

    Scoped to those fields on purpose: a child's prose may legitimately mention `child-2`
    while explaining a seam, and a blanket substitution would corrupt it. This is the step
    that makes `compute_waves` work on the output, and the step most worth machine-checking
    — hand-editing it is exactly how children end up serialised when they could have run in
    parallel, or building blind on the same base and conflicting at fold.
    """
    out: list[str] = []
    for line, fenced in _unfenced(body):
        if fenced:
            out.append(line)   # a fenced example is content, not metadata
            continue
        for field in ORDERING_FIELDS:
            m = re.match(rf"^(\s*-\s*\*{{0,2}}{re.escape(field)}\*{{0,2}}:\*{{0,2}}\s*)(.*)$",
                         line, re.IGNORECASE)
            if m:
                value = m.group(2).strip()
                if value and not value.startswith("<"):
                    refs = [mapping.get(t.strip(), t.strip())
                            for t in value.split(",") if t.strip()]
                    line = m.group(1) + ", ".join(refs)
                break
        out.append(line)
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")


def materialise(children: list[Child], ids: list[str], cfg, staging: Path) -> list[Path]:
    """Write each child's brief into ``staging``; return the staged bundle dirs.

    Staged rather than written in place so a failure part-way leaves the instance
    untouched — see the module docstring.
    """
    mapping = {c.label: i for c, i in zip(children, ids)}
    staged: list[Path] = []
    for child, issue_id in zip(children, ids):
        d = staging / cfg.bundle(issue_id).name
        d.mkdir(parents=True)
        (d / "brief.md").write_text(rewrite_ordering(child.body, mapping).lstrip("\n"),
                                    encoding="utf-8")
        staged.append(d)
    return staged


def _rollback(created: list[Path]) -> None:
    """Undo the child bundles that landed. A part-applied accept is worse than either
    outcome: the human can neither re-run (the ids exist) nor proceed (the batch is
    incomplete), so every failure path after the first move goes through here."""
    for d in created:
        shutil.rmtree(d, ignore_errors=True)


def accept(parent: Path, ids: list[str], cfg) -> list[Path]:
    """Materialise a parent's proposal into child bundles. Returns the created dirs.

    Raises :class:`SplitError` before writing anything if the proposal or the ids are
    unusable. The parent is marked terminal only after every child is in place.
    """
    proposal = parent / PROPOSAL
    if not proposal.exists():
        raise SplitError(f"{parent.name} has no {PROPOSAL} — run `pdca split "
                         f"{parent.name.replace('issue_', '')}` first")
    if (parent / state.CLOSE_MARKER).exists():
        raise SplitError(
            f"{parent.name} is already marked "
            f"{(parent / state.CLOSE_MARKER).read_text(encoding='utf-8').strip()!r} — a "
            "second acceptance would create a duplicate set of children and leave the "
            "first orphaned from the parent's breadcrumb. Reopen it first if that is what "
            "you want")
    children = parse(proposal.read_text(encoding="utf-8"))
    validate(children, ids, cfg)

    staging = parent / ".split-staging"
    shutil.rmtree(staging, ignore_errors=True)
    created: list[Path] = []
    try:
        staged = materialise(children, ids, cfg, staging)
        for src, issue_id in zip(staged, ids):
            dst = cfg.bundle(issue_id)          # the SAME path validate() checked
            if dst.exists():                    # re-checked at the moment of writing
                raise SplitError(f"{dst} appeared while accepting — refusing to overwrite")
            shutil.move(str(src), str(dst))
            created.append(dst)
    except Exception:
        _rollback(created)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Only now is the parent marked terminal — after every child is on disk. The marker,
    # not the brief's hint, is what `driver._close_class` honours, and it has to be:
    # a parent that already iterated (the realistic case — it failed an attempt before
    # anyone concluded it was too large) is excluded from the hint path by the
    # first-attempt guard. The build-notes breadcrumb records WHY no patch exists so a
    # frozen split parent never reads as an incomplete Do.
    try:
        # ARCHIVE the abandoned attempt first. A split is decided at sign-off, so the parent
        # normally still carries the rejected attempt's patch.diff, gates, review and
        # SUMMARY.md. Leaving them live is not cosmetic: `state.state` would keep the parent at
        # AWAITING_SIGNOFF on the stale summary, and `publish.publish` does not consult the
        # close marker — so accepting that summary could publish the very implementation the
        # split exists to abandon.
        from . import driver
        if any((parent / n).exists() for n in driver.DOWNSTREAM_OF_BRIEF):
            driver._archive_iteration(parent, driver._next_iteration_no(parent),
                                      include_brief=False)
        (parent / state.CLOSE_MARKER).write_text("split\n", encoding="utf-8")
        (parent / "build-notes.md").write_text(
            "# Build notes — NO PATCH (split)\n\n"
            "This slice was decomposed rather than built. The work lives in the child "
            f"bundles: {', '.join(d.name for d in created)}.\n\n"
            "The builder and reviewer leaves were not run — there is nothing to build here. "
            "The human confirms the split at sign-off; reopening to a fix path (iterate-to-Do) "
            "archives this marker and re-enables the full Do+Check band.\n",
            encoding="utf-8")
    except Exception:
        _rollback(created)
        raise
    return created


# ---------------------------------------------------------------------------
# Filing the child issues (issue #358)
#
# #323 left this to the human, on the grounds that it "keeps the tracker the source of
# truth and avoids the driver creating issues nobody asked for". Inside an interactive
# Plan session that objection is much weaker — the human is present and approving — and
# the friction is real: leave the session, file N issues by hand, come back with the
# numbers. `--ids` remains the explicit path for a human who has already filed them, or
# for a tracker this cannot reach.
# ---------------------------------------------------------------------------

#: `gh issue create` prints the new issue's URL on stdout. The trailing path segment is
#: the number — the only part of the output this depends on. Matched per LINE and the LAST
#: match wins: `gh` also emits notices ("Warning: N uncommitted changes") and, with some
#: configurations, a "Creating issue in owner/repo" preamble, so anchoring to the end of
#: the whole output failed on a call that had in fact created the issue.
_ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$", re.MULTILINE)

#: The first `# Heading` of a child block, used as the tracker issue's title.
_CHILD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*$")


class TrackerUnavailable(SplitError):
    """The driver cannot file issues here, and the human must pass ``--ids``.

    A SUBCLASS of SplitError so every existing caller still handles it, but distinct so
    the CLI can tell "your proposal is wrong" from "I cannot reach your tracker" — those
    call for completely different actions and a single message would have to hedge.
    """


def can_file(cfg) -> tuple[bool, str]:
    """``(True, repo)`` when the driver can file child issues itself, else ``(False, why)``.

    Never a silent skip: the caller turns ``why`` into a message that names ``--ids``. A
    split that quietly filed nothing and materialised nothing would look like a no-op.
    """
    from . import sources
    is_github, repo = sources.tracker_github_repo(cfg)
    if not is_github:
        return False, (f"tracker {cfg.tracker_system or 'unset'!r} is not GitHub, so this "
                       "cannot file the child issues for you")
    if not repo:
        return False, ("the GitHub repository could not be determined from "
                       "[tracker].url, so this cannot file the child issues for you")
    if shutil.which("gh") is None:
        return False, "`gh` is not on PATH, so this cannot file the child issues for you"
    return True, repo


def _parent_number(parent: Path) -> str:
    """The tracker number of the parent bundle, or "" when its name carries none."""
    token = parent.name
    if token.startswith("issue_"):
        token = token[len("issue_"):]
    return token if token.isdigit() else ""


def child_title(child: Child, parent: Path) -> str:
    """The tracker title for one child — its own heading, or its slug, or a fallback.

    Read from the child's own text rather than generated, so the tracker shows what the
    proposal actually says. A child that names neither still gets a title: an untitled
    issue is worse than a dull one.
    """
    for line, fenced in _unfenced(child.body):
        if fenced:
            continue
        m = _CHILD_HEADING_RE.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()
    for line, fenced in _unfenced(child.body):
        if fenced:
            continue
        m = re.match(r"^\s*-\s*\*{0,2}slug\*{0,2}:\*{0,2}\s*(.+?)\s*$", line, re.IGNORECASE)
        if m and not m.group(1).startswith("<"):
            return m.group(1).strip()
    return f"{parent.name} — {child.label}"


def _create_issue(repo: str, title: str, body: str, parent_no: str, root: Path) -> str:
    """File ONE child issue; return its number. Raises SplitError naming the failure.

    ``--parent`` makes this a real tracker sub-issue rather than a convention in the body
    text, so the parent becomes an umbrella and each child gets its own PR — which is what
    the one-PR-per-issue rule requires.
    """
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    if parent_no:
        cmd += ["--parent", parent_no]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    except OSError as exc:
        raise SplitError(f"`gh issue create` could not be run ({exc})") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SplitError("`gh issue create` failed"
                         + (f": {detail[-1]}" if detail else ""))
    matches = _ISSUE_URL_RE.findall((proc.stdout or "").strip())
    if not matches:
        # The issue may well have been created — this is reported, never swallowed, so the
        # caller can tell the human that a number it cannot name may now exist.
        raise SplitError("`gh issue create` printed no issue URL, so the new issue's "
                         f"number could not be read from: {(proc.stdout or '').strip()!r}")
    return matches[-1]


def file_children(parent: Path, children: list[Child], cfg) -> list[str]:
    """File one tracker issue per child, parented to ``parent``. Returns ids in order.

    ## Why this files first and materialises second

    The DoD asks for filing to be transactional with the bundles. It cannot be, in the
    strong sense: a tracker issue cannot be rolled back. Of the two orders it allows,
    materialise-then-file is impossible here — a bundle directory is NAMED for its issue
    id (``cfg.bundle(id)``), so there is nothing to materialise until the ids exist.

    So: file, then report precisely. On a partial failure this raises with every number
    already created spelled out, and the caller prints the exact ``--ids`` command that
    resumes from there. The failure mode this forbids is the silent one — issues created
    for children whose bundles never appeared, with nothing on screen naming them.
    """
    ok, why = can_file(cfg)
    if not ok:
        raise TrackerUnavailable(why)
    parent_no = _parent_number(parent)
    if not parent_no:
        # A bundle whose directory name carries no tracker number (a hand-made bundle, a
        # non-numeric id). The children can still be filed, but NOT as sub-issues — and
        # the umbrella relationship is half the reason this exists, so say so rather than
        # quietly producing a flat set of unrelated issues.
        print(f"split: {parent.name} carries no numeric tracker id — filing the children "
              "as standalone issues, NOT as sub-issues", file=sys.stderr)
    body_head = (f"Child slice of #{parent_no}, split during Plan.\n\n"
                 if parent_no else "Child slice, split during Plan.\n\n")
    created: list[str] = []
    for child in children:
        try:
            created.append(_create_issue(
                repo=why,                       # can_file returns the repo on success
                title=child_title(child, parent),
                body=body_head + child.body.strip() + "\n",
                parent_no=parent_no,
                root=cfg.root))
        except Exception as exc:
            # `Exception`, not `SplitError`: anything escaping here — an unexpected
            # subprocess failure, a bad argument — would otherwise lose the list of issues
            # already created, which is the one thing this function must never do.
            raise SplitError(
                f"{exc}\n"
                f"Filed {len(created)} of {len(children)} child issue(s) before this: "
                f"{', '.join('#' + i for i in created) or '(none)'}. These are REAL issues "
                "and cannot be rolled back — no bundle was created for any of them. Either "
                "close them on the tracker and re-run, or file the remaining "
                f"{len(children) - len(created)} by hand and pass every id explicitly:\n"
                f"  pdca split {_parent_number(parent) or parent.name} --accept --ids "
                f"{','.join(created + ['<id>'] * (len(children) - len(created)))}"
            ) from exc
    return created
