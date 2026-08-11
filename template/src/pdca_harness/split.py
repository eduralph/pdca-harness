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

import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import state

PROPOSAL = "split-proposal.md"

#: The lineage provenance record (issue #456): written into each child AND merged into the
#: parent by `accept`, so a split's edges survive ON DISK instead of living only in the
#: filed tracker issue's body. Without it a split child is indistinguishable from a fresh
#: oversized brief to everything that reads a bundle. See `read_lineage`.
#:
#: Deliberately NOT in `state.DOWNSTREAM_OF_BRIEF`: this is provenance about the split, not
#: an attempt's output, so an `iterate-plan` that archives a rejected attempt must leave it
#: exactly where it is.
LINEAGE = "split-lineage.json"
LINEAGE_VERSION = 1

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


def advisory(text: str, *, file=None) -> None:
    """Write one ADVISORY line, absorbing a stream failure rather than raising (issue #459).

    Every write on the acceptance path is advisory in this exact sense: what the
    convergence report below prints can only change the human's *decision*, and what
    `cli._split` prints after :func:`accept` returns cannot change anything at all — the
    tracker issues are filed and the child bundles are on disk. Neither may fail the
    command, and `pdca split 500 --accept 2>&1 | head` is enough to break the stream
    part-way through and make them try.

    Both failure shapes were real. A `BrokenPipeError` out of a report line escaped
    `preflight`, where `cli._split`'s own `except OSError` reads it as "this bundle has no
    split-proposal.md" (`cli.py:770-773`) — so a perfectly good proposal was refused with
    rc 1 and a flatly wrong reason. Out of the status line *after* `accept` returned it was
    worse: an unhandled traceback and a non-zero exit on a run whose irreversible half had
    already succeeded, which is the one thing an advisory line must never do.

    ONE definition, used by the report here, by the notices :func:`_rollback`,
    :func:`_restore_lineage` and :func:`file_children` print when something goes wrong
    mid-acceptance, and by every line `cli._split` writes — so "output can never abort an
    acceptance" is a property of the whole path rather than of the writes someone
    remembered to wrap. Guarding the report alone was not enough: the status line after
    `accept` returned still raised, and a persistently broken stream still changed the exit
    code of a run whose bundles were already on disk. In the interrupt report
    (:func:`file_children`) it also keeps a `KeyboardInterrupt` from being replaced by the
    `OSError` of failing to describe it. ``flush=True`` because a piped stdout is
    block-buffered: without it the failure lands in the interpreter's own shutdown flush,
    outside every handler, where CPython reports it and exits 120.

    The stream is resolved at CALL time (``file=None`` → ``sys.stderr``), so a caller that
    redirects `sys.stderr` — the CLI under a pipe, a test — is honoured.
    """
    try:
        print(text, file=sys.stderr if file is None else file, flush=True)
    except OSError:
        pass


def preflight(parent: Path, children: list[Child], cfg) -> None:
    """Every reason acceptance would fail that does NOT depend on the ids.

    Split out of :func:`validate` because filing happens BEFORE the ids exist, and a
    tracker issue cannot be withdrawn (#358). Without this, `pdca split <id> --accept`
    run a second time filed a whole second set of real sub-issues and only THEN
    discovered the parent was already split — and a proposal with a dependency cycle
    filed its children before `validate` refused them.

    It also emits the **convergence report** (issue #459), last, once the proposal is known
    to be acceptable. This is the only point BOTH acceptance shapes reach before anything
    irreversible happens — a filed tracker sub-issue, a materialised bundle — so it is the
    last point at which "does this split actually make the children smaller?" can still
    change the decision. Advisory in the strict sense: :func:`_emit_convergence_report`
    neither raises nor blocks, so nothing about it can change what is filed, what is
    materialised, or the exit code.
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
    _emit_convergence_report(parent, children, cfg)


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


# ---------------------------------------------------------------------------
# The convergence report (issue #459)
#
# `preflight` used to check only the reasons acceptance would FAIL. Nothing asked the one
# question the human is actually deciding: does this split make the children smaller? A
# split that leaves every child `oversized` was discovered a whole cycle later, when each
# child's own size guard fired and pointed the planner at `pdca split` again — by which
# time the sub-issues are filed, and a tracker issue cannot be withdrawn (#358). So the
# estimate runs here, at the last point where the answer can still change the decision,
# and it only ever REPORTS: the size guard it mirrors is warn-only for a calibrated reason
# (`plan_policy.size_reasons` — 62% precision at its best), and a blocking check at that
# rate is one people learn to override.
# ---------------------------------------------------------------------------


def _staged_estimates(parent: Path, children: list[Child], cfg) -> list[tuple[Child, object]]:
    """``(child, estimate)`` per child, scored exactly as the materialised bundle will be.

    Staged through :func:`materialise` — the writer acceptance itself uses — into a
    throwaway directory, with each child's LABEL standing in for the tracker id it does not
    have yet. Reusing that function is the whole point: the staged bundle differs from the
    one `--accept` will write only in the ids that do not exist yet, so the report cannot
    end up describing a bundle assembled differently from the one about to land.

    The lineage record it writes matters as much as the brief. `sizing.estimate` reads the
    sibling set from it to decide which declared conflicts are the split's own scheduling
    metadata rather than churn (#457), and at `preflight` time every ordering ref is a
    sibling LABEL by construction (:func:`_validate_ordering` has just proven it) — so
    staging with labels as ids is what makes the estimate apply the SAME rule the live
    estimator will apply the moment the children exist.

    Nothing lands in the instance: `_LABEL_RE` pins every label to ``child-\\d+`` so a path
    composed from one cannot traverse, and the `TemporaryDirectory` is outside the instance
    and removed on every exit, success or failure. That is `preflight`'s standing guarantee
    — it runs BEFORE `file_children` and must leave nothing behind.
    """
    from . import sizing   # lazy: sizing imports split, so a module-level import cycles
    labels = [c.label for c in children]
    with tempfile.TemporaryDirectory(prefix="pdca-split-convergence-") as tmp:
        staged = materialise(children, labels, cfg, Path(tmp), parent=parent)
        return [(child, sizing.estimate(d / "brief.md", cfg))
                for child, d in zip(children, staged)]


def _exposed_sibling_conflicts(est) -> int | None:
    """How many `Conflicts with` entries the estimator EXCLUDED as siblings, or ``None``.

    `SizeEstimate.sibling_conflicts` (#457) is the only honest source for this. A reader
    that judged sibling entanglement from the estimate's ``score`` or ``reasons`` instead
    would see an *excluded* 0 — the exclusion is precisely that those declarations no
    longer contribute — and report a proposal whose children all conflict pairwise as
    clean, which is the one shape that means the split separated nothing.

    ``None`` (not 0) when the estimator predates #457 and excludes nothing, so the caller
    can say so in the report rather than silently read "no sibling conflicts" off a field
    that was never written. A malformed value is treated the same way: this must not turn
    an advisory line into an exception.
    """
    value = getattr(est, "sibling_conflicts", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def convergence_report(parent: Path, children: list[Child], cfg) -> list[str]:
    """Does this split make the children smaller? One line per child, then a verdict.

    Read-only and deterministic: computed from the proposal and the parent's brief, with
    no tracker id involved and nothing written into the instance
    (:func:`_staged_estimates`). Returns the lines rather than printing them, so the
    guarded writer is the only thing that touches a stream.

    Two independent NOT-CONVERGED signals, because the band comparison alone is blind to
    the second. A `Conflicts with` edge *between* siblings is the splitter's own statement
    that those two children edit a shared resource — `leaves._split_prompt` tells it
    outright that those fields "BETWEEN children are the point" — so a proposal whose
    children conflict pairwise has separated nothing, however small each child reads. That
    signal
    survives #457's exclusion only because it is taken from the count the estimate
    EXPOSES (:func:`_exposed_sibling_conflicts`), never from the score those declarations
    were removed from.
    """
    from . import sizing   # lazy: see _staged_estimates
    parent_est = sizing.estimate(parent / "brief.md", cfg)
    labels = [c.label for c in children]
    lines = [f"split: convergence report for {parent.name} (advisory, changes nothing) — "
             f"parent bands {parent_est.band} (score {parent_est.score})"]
    lower = 0
    edges: set[frozenset[str]] = set()
    unexposed = False
    for child, est in _staged_estimates(parent, children, cfg):
        siblings = [label for label in labels if label != child.label]
        # WHICH siblings this child names; `_validate_ordering` has already refused every
        # ref that is not one. The count comes from the estimate, the identities can only
        # come from here — no id exists yet — so the estimate's count is what decides how
        # many of these edges are credited.
        declared = [ref for ref in dict.fromkeys(child.ordering("Conflicts with"))
                    if ref in siblings]
        exposed = _exposed_sibling_conflicts(est)
        if exposed is None:
            unexposed = True
        conflicts = len(declared) if exposed is None else exposed
        for ref in declared[:conflicts]:
            edges.add(frozenset((child.label, ref)))
        if est.band == parent_est.band:
            relation = "same band as the parent"
        elif sizing.higher(est.band, parent_est.band) == parent_est.band:
            relation, lower = "LOWER than the parent", lower + 1
        else:
            relation = "HIGHER than the parent"
        note = f" [{conflicts} sibling conflict(s) declared]" if conflicts else ""
        lines.append(f"split:   {child.label}: {est.band} (score {est.score}) — {relation} "
                     f"— {'; '.join(est.reasons) or 'no structural signal'}{note}")
    not_lower = len(children) - lower
    verdicts: list[str] = []
    if children and not_lower * 2 > len(children):
        verdicts.append(
            f"split: NOT CONVERGED — {not_lower} of {len(children)} child(ren) do not band "
            f"lower than {parent.name}: this split does not make the work smaller, and "
            "each child costs a full cycle. Reconsider the seams before accepting.")
    if len(children) > 1 and len(edges) == len(children) * (len(children) - 1) // 2:
        verdicts.append(
            "split: NOT CONVERGED — every pair of children declares a `Conflicts with` "
            "edge, so the split separated nothing: the splitter is saying each pair edits "
            "a shared resource, and they cannot be built independently.")
    if not verdicts:
        verdicts.append(f"split: converged — {lower} of {len(children)} child(ren) band "
                        f"lower than {parent.name}.")
    lines += verdicts
    if unexposed:
        # NAMED, never absorbed: this estimator excludes nothing, so the counts above are
        # the proposal's own — identical here by construction (`_validate_ordering` has
        # just proven every ref names a sibling), but the difference belongs on screen
        # rather than hidden behind a fallback, so a base missing #457 is visible.
        lines.append("split:   note: this estimator exposes no `sibling_conflicts` count "
                     "(#457), so the sibling edges above are read from the proposal's own "
                     "ordering fields.")
    return lines


def _emit_convergence_report(parent: Path, children: list[Child], cfg) -> None:
    """Print :func:`convergence_report`, and never let it change what `--accept` does.

    Total, in both directions. Every line goes out through :func:`advisory`, so a stream
    that fails part-way — or on every write, which is what a broken pipe actually does —
    changes neither the exit code nor the set of bundles created. And the report's own
    computation is wrapped: an advisory estimate that raised (a full disk under the
    staging directory, a future estimator that stops abstaining) would otherwise escape
    `preflight` and refuse an acceptance over a line nobody reads twice. A failure is
    NAMED on the same guarded stream rather than swallowed — the discipline
    :func:`_restore_lineage` already follows.
    """
    try:
        lines = convergence_report(parent, children, cfg)
    except Exception as exc:   # noqa: BLE001 — an advisory report may never abort an accept
        advisory(f"split: the convergence report could not be produced ({exc!r}) — "
                 "continuing, it is advisory and decides nothing")
        return
    for line in lines:
        advisory(line)


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


def _bundle_id(bundle: Path) -> str:
    """The tracker id encoded in a bundle directory's name: ``issue_<id>`` -> ``<id>``.

    Deliberately more permissive than `_parent_number` below, which is scoped to
    `gh issue create --parent` and so demands a real (all-digit) GitHub issue number. A
    lineage id is any token `validate()` accepts (``[A-Za-z0-9._-]+``) — including the
    `MANT-1` shape a non-GitHub tracker uses.
    """
    name = bundle.name
    return name[len("issue_"):] if name.startswith("issue_") else name


def read_lineage(bundle: Path) -> dict | None:
    """The parsed `split-lineage.json` in ``bundle``, or ``None`` — the one reader (#456).

    Tolerant by construction: absent, unreadable, malformed JSON, a non-object payload and
    an unrecognised ``version`` all return ``None``, and nothing raises. A provenance
    reader that can throw into a beat is worse than one that abstains — every consumer must
    behave exactly as it does today when this returns ``None``, so an operator who hand-
    edits the file into nonsense degrades the hint, never the run.

    The catch is TOTAL on purpose, not a list of the expected failure types. Enumerating
    them is precisely what failed here: bytes that are not UTF-8 raise `UnicodeDecodeError`
    out of the *read*, where only `OSError` was expected, and a pathologically nested
    payload raises `RecursionError`, which is not a `ValueError` at all — so neither
    "unreadable" nor "malformed" was caught by the handler written for it, and a corrupt
    file could still crash a consumer. A reader whose entire contract is "never raises"
    cannot be a predicate over the failure modes someone happened to think of; the file is
    a hint, and no way of failing to read a hint is worth an exception. `BaseException`
    still propagates, so Ctrl-C and `SystemExit` are untouched.

    The record carries INDEPENDENT OPTIONAL EDGES and no role discriminator:
    ``parent``/``siblings``/``depth`` iff the bundle is a split child, ``children`` iff it
    has itself been split. A bundle can legitimately be both.
    """
    try:
        data = json.loads((bundle / LINEAGE).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != LINEAGE_VERSION:
        return None
    return data


def _recorded_depth(record: dict | None) -> int:
    """A record's ``depth`` as a usable int, or ``0`` — the arithmetic half of tolerance.

    `read_lineage` abstains on a file it cannot parse; this abstains on a VALUE it cannot
    compute with. `{"depth": "one"}` and `{"depth": null}` are valid JSON, so the reader
    hands them straight back — and `depth + 1` below would then raise `TypeError` from
    inside `accept`, crashing the split on a hand-edited provenance file exactly as a
    raising reader would. Tolerating the file but not its contents would only move the
    throw one line down.

    Booleans are excluded deliberately: `True` is an `int` in Python, and a record saying
    `"depth": true` should not produce a child at depth 2.
    """
    value = (record or {}).get("depth")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _write_lineage(path: Path, record: dict) -> None:
    """One serialiser for both edges, so a child's record and a parent's cannot drift.

    Sorted keys and a trailing newline: the file lands in a bundle the cycle commits and
    diffs, so a stable byte-for-byte rendering is part of the artifact.
    """
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _merge_parent_lineage(parent: Path, children_ids: list[str]) -> dict:
    """The parent's post-accept record: whatever it already carried, PLUS ``children``.

    MERGES rather than replaces. A parent that is itself a split child keeps its own
    `parent` / `siblings` / `depth` and simply gains `children` — the mixed-role case is
    the reason the schema has independent optional edges and no `role` field: one file
    carrying one role cannot express a bundle that is both, and replacing the record
    silently drops a depth-1 bundle's sibling set.
    """
    existing = read_lineage(parent) or {}
    record: dict = {"version": LINEAGE_VERSION, "id": _bundle_id(parent)}
    for key in ("parent", "siblings", "depth"):
        if key in existing:
            record[key] = existing[key]
    record["children"] = list(children_ids)
    return record


def _restore_lineage(path: Path, prior: bytes | None) -> None:
    """Put ``path`` back exactly as this accept found it. Best-effort, LOUD, never raises.

    It runs from a rollback handler, where the exception on its way out is the one the
    operator must see and where the `CLOSE_MARKER` cleanup below it must still run — so a
    restore that failed by raising would both mask the real cause and leave "children
    rolled back" and "parent still terminal" able to coexist, the one pairing `accept`'s
    ordering exists to prevent. A restore that cannot complete is NAMED on stderr instead,
    the same discipline as `_rollback`.
    """
    try:
        if prior is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(prior)
    except OSError as exc:
        advisory(f"split: could not restore {path} while rolling back ({exc}). Check it by "
                 "hand before retrying — it may name children this run did not create.")


def materialise(children: list[Child], ids: list[str], cfg, staging: Path, *,
                parent: Path) -> list[Path]:
    """Write each child's brief AND its lineage record into ``staging``; return the dirs.

    Staged rather than written in place so a failure part-way leaves the instance
    untouched — see the module docstring. The lineage record is staged alongside the brief
    in the same directory, so it moves with it: a bundle whose brief never landed must
    never have a record either.

    ``depth`` is the parent's own recorded depth + 1, so recursion depth is written down
    at the moment it is known rather than recounted later by walking the chain.
    """
    mapping = {c.label: i for c, i in zip(children, ids)}
    parent_id = _bundle_id(parent)
    depth = _recorded_depth(read_lineage(parent)) + 1
    staged: list[Path] = []
    for child, issue_id in zip(children, ids):
        d = staging / cfg.bundle(issue_id).name
        d.mkdir(parents=True)
        (d / "brief.md").write_text(rewrite_ordering(child.body, mapping).lstrip("\n"),
                                    encoding="utf-8")
        _write_lineage(d / LINEAGE, {
            "version": LINEAGE_VERSION,
            "id": issue_id,
            "parent": parent_id,
            "siblings": [i for i in ids if i != issue_id],
            "depth": depth,
        })
        staged.append(d)
    return staged


def _rollback(created: list[Path]) -> None:
    """Undo the child bundles that landed. A part-applied accept is worse than either
    outcome: the human can neither re-run (the ids exist) nor proceed (the batch is
    incomplete), so every failure path after the first move goes through here.

    A directory that cannot be removed is NAMED. `ignore_errors=True` alone left a bundle
    on disk with its parent unmarked and said nothing, so the printed retry failed on an
    "already exists" the operator had no way to anticipate — a rollback that silently does
    not roll back is worse than one that fails loudly.
    """
    stuck: list[Path] = []
    for d in created:
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            stuck.append(d)
    if stuck:
        advisory("split: could not remove " + ", ".join(str(d) for d in stuck)
                 + " while rolling back. Delete them by hand before retrying, or the retry "
                   "will refuse them as existing bundles.")


def accept(parent: Path, ids: list[str], cfg) -> list[Path]:
    """Materialise a parent's proposal into child bundles. Returns the created dirs.

    Raises :class:`SplitError` before writing anything if the proposal, the ids, or the
    parent's existing lineage record are unusable. The parent is marked terminal only
    after every child is in place.

    Each child also gets a `LINEAGE` record naming this parent, its siblings and its
    depth, and this parent's own record gains `children` — so the split's edges are on
    disk, not only in the filed tracker issue's body.
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

    # The parent's PRIOR lineage bytes, read in the pre-write phase — with the proposal and
    # the ids, before a single directory is staged. This read is validation, not
    # bookkeeping: a record that cannot be READ cannot be RESTORED, so an accept that
    # reached it later and failed could not put the parent back the way it found it.
    # Reading it here means an unreadable record (a directory at the path, a permissions
    # error) refuses the accept while refusing is still free — nothing staged, nothing
    # moved, nothing to roll back — instead of surfacing between the two protected regions
    # below, where a raise escaped with the children already on disk and the parent left
    # open. Absent is the ordinary case and is NOT a failure: it means the rollback path
    # must remove whatever this run writes.
    lineage_path = parent / LINEAGE
    try:
        prior_lineage: bytes | None = lineage_path.read_bytes()
    except FileNotFoundError:
        prior_lineage = None
    except OSError as exc:
        raise SplitError(
            f"{parent.name}'s {LINEAGE} cannot be read ({exc}) — refusing to split. A "
            "record this run cannot read is one it cannot restore if the accept fails, so "
            "the parent could be left describing children that were rolled back. Fix or "
            "remove it, then re-run") from exc

    staging = parent / ".split-staging"
    shutil.rmtree(staging, ignore_errors=True)
    created: list[Path] = []
    try:
        staged = materialise(children, ids, cfg, staging, parent=parent)
        for src, issue_id in zip(staged, ids):
            dst = cfg.bundle(issue_id)          # the SAME path validate() checked
            if dst.exists():                    # re-checked at the moment of writing
                raise SplitError(f"{dst} appeared while accepting — refusing to overwrite")
            # Recorded BEFORE the move, not after. `shutil.move` is not atomic across
            # filesystems — it can create `dst`, copy part of the tree and then raise — so
            # appending afterwards meant a half-moved bundle was invisible to `_rollback`
            # and left behind with its parent unmarked. Every retry then refused it as an
            # existing bundle. Recording first can only ever ask the rollback to remove
            # something that does not exist, which it tolerates.
            created.append(dst)
            shutil.move(str(src), str(dst))
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
        # The lineage record next — still BEFORE the breadcrumb and the marker, for the
        # same reason those are ordered as they are: a failure here must not leave the
        # parent terminal with the wrong provenance, or with none. `_merge_parent_lineage`
        # keeps the mixed-role case whole (a parent that is itself a split child keeps its
        # own `parent`/`siblings`/`depth` and gains `children`); `_archive_iteration` above
        # cannot have touched it, because `LINEAGE` is deliberately not in
        # `DOWNSTREAM_OF_BRIEF` — it is provenance, not this attempt's output.
        _write_lineage(lineage_path, _merge_parent_lineage(parent, list(ids)))
        # The breadcrumb FIRST, the state-changing marker LAST. `CLOSE_MARKER` is what
        # makes the parent terminal, and `_rollback` only removes child directories — so
        # writing it first meant a failure on `build-notes.md` (a full disk, a path
        # collision) deleted the children while leaving the parent marked `split`, with
        # the tracker issues filed and every retry refused as "already marked". There was
        # no ordinary way back. Ordered this way the same failure leaves the parent
        # exactly as it was and the printed `--accept --ids …` retry works.
        (parent / "build-notes.md").write_text(
            "# Build notes — NO PATCH (split)\n\n"
            "This slice was decomposed rather than built. The work lives in the child "
            f"bundles: {', '.join(d.name for d in created)}.\n\n"
            "The builder and reviewer leaves were not run — there is nothing to build here. "
            "The human confirms the split at sign-off; reopening to a fix path (iterate-to-Do) "
            "archives this marker and re-enables the full Do+Check band.\n",
            encoding="utf-8")
        (parent / state.CLOSE_MARKER).write_text("split\n", encoding="utf-8")
    except Exception:
        _rollback(created)
        # The parent's record back to the bytes this accept found (or gone, if it found
        # none): a failed accept must never leave a record naming children that were just
        # rolled back, nor a half-merged one. The snapshot was taken before anything was
        # written, so this is always a restore to a state that really existed.
        _restore_lineage(lineage_path, prior_lineage)
        # Belt and braces: if the marker itself was the write that landed before the
        # failure, remove it, so "children rolled back" and "parent still terminal" can
        # never coexist.
        try:
            (parent / state.CLOSE_MARKER).unlink(missing_ok=True)
        except OSError:
            pass
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

#: `gh issue create` prints the new issue's URL, alone on its own line. The trailing path
#: segment is the number — the only part of the output this depends on.
#:
#: The line must be NOTHING BUT the URL. Anchoring to the end of the whole output failed
#: on a call that had in fact created the issue, because `gh` also emits notices and, in
#: some configurations, a "Creating issue in owner/repo" preamble. But relaxing that to
#: "the last line ending in /issues/N" is worse than the bug it fixed: a trailing notice
#: that merely MENTIONS another issue ("see https://…/issues/999") would be read as the
#: number just created, and the child bundle would be named for an unrelated issue —
#: silently, and against a real tracker item. A bare URL line cannot be confused that way.
_ISSUE_URL_RE = re.compile(r"^\s*https?://\S*/issues/(\d+)/?\s*$", re.MULTILINE)

#: The first `# Heading` of a child block, used as the tracker issue's title. The optional
#: CLOSING run of hashes is dropped: `# Extract the parser ##` is valid ATX and renders as
#: "Extract the parser" everywhere else, so carrying the hashes into a real tracker item
#: would put markup in a title a human reads.
_CHILD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")


class UncertainFiling(SplitError):
    """`gh` succeeded but its output could not be parsed, so an issue MAY exist.

    Distinct from an ordinary failure because the remedy is opposite. A call that failed
    filed nothing and should simply be retried; a call that succeeded without a readable
    URL has probably created a real issue, and telling the operator to "file the remaining
    one by hand" invites a duplicate against a tracker that cannot undo either.
    """


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
        if not m:
            continue
        # STRIP, then test. `.+?` matches a space, so `- **Slug:**` followed by nothing but
        # whitespace captured a space, `.strip()` emptied it, and the function returned ""
        # — breaking its own documented promise and handing `gh issue create` an empty
        # `--title`, which fails and aborts filing for the whole batch.
        value = m.group(1).strip()
        if value and not value.startswith("<"):
            return value
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
        # `gh` EXITED ZERO. The issue was in all likelihood created and only its number is
        # unreadable, so this is a different situation from a failed call and carries its
        # own type — see `UncertainFiling`.
        raise UncertainFiling(
            "`gh issue create` exited 0 but printed no issue URL, so the new issue's "
            f"number could not be read from: {(proc.stdout or '').strip()!r}")
    return matches[-1]


def file_children(parent: Path, children: list[Child], cfg, *,
                  prog: str = "pdca") -> list[str]:
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
        advisory(f"split: {parent.name} carries no numeric tracker id — filing the "
                 "children as standalone issues, NOT as sub-issues")
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
        except BaseException as exc:
            # `BaseException`, not `Exception`. Ctrl-C during a run that has already filed
            # issues is an ordinary operator action, and `KeyboardInterrupt` is not an
            # `Exception` — so it walked straight past this handler and the irreversible
            # numbers were lost with nothing on screen naming them, which is the single
            # failure this function exists to prevent.
            uncertain = isinstance(exc, UncertainFiling)
            report = (
                f"Filed {len(created)} of {len(children)} child issue(s) before this: "
                f"{', '.join('#' + i for i in created) or '(none)'}. These are REAL issues "
                "and cannot be rolled back — no bundle was created for any of them.\n")
            if uncertain:
                # The call SUCCEEDED; only its number is unreadable. Telling the operator
                # to file this child by hand would create a duplicate against a tracker
                # that can undo neither.
                report += (
                    "The child after those MAY ALSO HAVE BEEN FILED — `gh` exited 0 and "
                    "only its number could not be read. Check the tracker before filing "
                    "anything by hand, or you will create a duplicate.\n")
            else:
                # A non-zero exit USUALLY means nothing was created — but `gh` can also
                # time out or lose the response after GitHub has already committed the
                # issue, and this code cannot tell those apart. Saying "nothing was filed"
                # would be an overclaim that costs a duplicate on a tracker with no undo.
                report += (
                    "The child after those most likely was NOT filed — but a timeout or a "
                    "lost response can happen after GitHub has already created the issue, "
                    "so check the tracker before filing it by hand.\n")
            report += (
                "Either close them on the tracker and re-run, or file the remaining "
                f"{len(children) - len(created)} by hand and pass every id explicitly:\n"
                f"  {prog} split {_parent_number(parent) or parent.name} "
                f"--accept --ids "
                f"{','.join(created + ['<id>'] * (len(children) - len(created)))}")
            if not isinstance(exc, Exception):
                # An interrupt stays an interrupt: report, then let it propagate, so Ctrl-C
                # still aborts rather than being converted into an ordinary error.
                advisory(f"split: {exc!r}\n{report}")
                raise
            raise SplitError(f"{exc}\n{report}") from exc
    return created
