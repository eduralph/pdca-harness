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
from dataclasses import dataclass
from pathlib import Path

from . import state

PROPOSAL = "split-proposal.md"
_VERSION_RE = re.compile(r"<!--\s*pdca:split-proposal\s+v(\d+)\s*-->")
_CHILD_RE = re.compile(r"<!--\s*pdca:child\s+(\S+)\s*-->\n(.*?)<!--\s*pdca:end\s+\1\s*-->",
                       re.DOTALL)
_LABEL_RE = re.compile(r"^child-\d+$")
SUPPORTED_VERSION = 1

#: The ordering fields whose values are proposal-local labels and must be rewritten to real
#: tracker ids at acceptance. Getting this list wrong is the failure that makes the whole
#: feature pointless: `compute_waves` reads exactly these.
ORDERING_FIELDS = ("Depends on", "Conflicts with")


class SplitError(Exception):
    """A proposal that cannot be accepted as written. Always raised BEFORE any write."""


@dataclass(frozen=True)
class Child:
    label: str
    body: str

    def ordering(self, field: str) -> list[str]:
        """The labels named by one ordering field — `[]` when absent or a placeholder."""
        for line in self.body.splitlines():
            m = re.match(rf"^\s*-\s*\*{{0,2}}{re.escape(field)}\*{{0,2}}:\*{{0,2}}\s*(.*)$",
                         line, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if value.startswith("<"):
                    return []          # unfilled template placeholder
                return [t.strip() for t in value.split(",") if t.strip()]
        return []


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
    children = [Child(label, body) for label, body in _CHILD_RE.findall(text)]
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


def validate(children: list[Child], ids: list[str], cfg) -> None:
    """Every reason acceptance would fail, checked before a single file is written."""
    if len(children) != len(ids):
        raise SplitError(
            f"the proposal declares {len(children)} child(ren) but --ids names "
            f"{len(ids)} — refusing to guess which id belongs to which child")
    if len(set(ids)) != len(ids):
        raise SplitError(f"--ids contains duplicates: {', '.join(ids)}")

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

    for issue_id in ids:
        d = cfg.bundle(issue_id)
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
    for line in body.splitlines():
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


def accept(parent: Path, ids: list[str], cfg) -> list[Path]:
    """Materialise a parent's proposal into child bundles. Returns the created dirs.

    Raises :class:`SplitError` before writing anything if the proposal or the ids are
    unusable. The parent is marked terminal only after every child is in place.
    """
    proposal = parent / PROPOSAL
    if not proposal.exists():
        raise SplitError(f"{parent.name} has no {PROPOSAL} — run `pdca split "
                         f"{parent.name.replace('issue_', '')}` first")
    children = parse(proposal.read_text(encoding="utf-8"))
    validate(children, ids, cfg)

    staging = parent / ".split-staging"
    shutil.rmtree(staging, ignore_errors=True)
    created: list[Path] = []
    try:
        staged = materialise(children, ids, cfg, staging)
        for src in staged:
            dst = cfg.bundle_root / src.name
            shutil.move(str(src), str(dst))
            created.append(dst)
    except Exception:
        for d in created:            # roll back the moves that did land
            shutil.rmtree(d, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Only now is the parent marked terminal — after every child is on disk. The marker,
    # not the brief's hint, is what `driver._close_class` honours, and it has to be:
    # a parent that already iterated (the realistic case — it failed an attempt before
    # anyone concluded it was too large) is excluded from the hint path by the
    # first-attempt guard. The build-notes breadcrumb records WHY no patch exists so a
    # frozen split parent never reads as an incomplete Do.
    (parent / state.CLOSE_MARKER).write_text("split\n", encoding="utf-8")
    (parent / "build-notes.md").write_text(
        "# Build notes — NO PATCH (split)\n\n"
        "This slice was decomposed rather than built. The work lives in the child "
        f"bundles: {', '.join(d.name for d in created)}.\n\n"
        "The builder and reviewer leaves were not run — there is nothing to build here. "
        "The human confirms the split at sign-off; reopening to a fix path (iterate-to-Do) "
        "archives this marker and re-enables the full Do+Check band.\n",
        encoding="utf-8")
    return created
