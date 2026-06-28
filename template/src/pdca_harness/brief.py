"""Parsing the Plan artifact, ``brief.md`` (docs 02 §PLAN).

The brief is human-authored Markdown following ``templates/brief.md.tpl``. The
driver and the leaves need a few fields out of it (the test file path so iterate
can clear it; the spec fields so SUMMARY can be assembled). Parsing is
deliberately lenient: a field is read from a ``- **Label:** value`` or
``- Label: value`` bullet, case-insensitive on the label.
"""

from __future__ import annotations

import re
from pathlib import Path

# The colon may sit INSIDE the bold (`**Label:**`, as `brief.md.tpl` and every real
# brief write it) or outside (`**Label**:`), or there may be no bold (`Label:`). The
# trailing `\*{0,2}` after the colon absorbs the closing markers in the first shape
# so they never leak into the value; the label group excludes `*`/`:` so no marker
# leaks into the key either.
_FIELD_RE = re.compile(r"^\s*-\s*\*{0,2}([^:*]+?)\*{0,2}:\*{0,2}\s*(.*?)\s*$")


def parse_fields(brief_path: Path) -> dict[str, str]:
    """Return ``{lowercased label: value}`` for every bullet field in the brief."""
    fields: dict[str, str] = {}
    for line in brief_path.read_text(encoding="utf-8").splitlines():
        m = _FIELD_RE.match(line)
        if m:
            key = m.group(1).strip().lower()
            fields.setdefault(key, m.group(2).strip())
    return fields


def _is_placeholder(value: str) -> bool:
    """True if a value is still the template's unfilled ``<…>`` placeholder, so a
    consumer treats it as absent. Without this, a substring gate matches the placeholder
    text itself — e.g. an untouched ``Difficulty: <low | medium | high>`` would fire a
    ``substring="high"`` advisory/variant, defeating the absent-is-safe default (#133).

    A field value is parsed line-by-line, so a *multi-line* placeholder yields only its
    first line — which opens with ``<`` but never closes. So a value counts as a
    placeholder when it opens with ``<`` and either closes with ``>`` (a single-line
    placeholder) or has no ``>`` at all (the unterminated first line of a multi-line one).
    A partly-filled value (no leading ``<``, or a closed ``<x>`` mid-text) is kept."""
    v = value.strip()
    return v.startswith("<") and (v.endswith(">") or ">" not in v)


def field(brief_path: Path, *labels: str, default: str = "") -> str:
    """First matching field value among ``labels`` (lowercased), else ``default``. A field
    left as its ``<…>`` template placeholder reads as absent (falls through to ``default``)."""
    fields = parse_fields(brief_path)
    for label in labels:
        val = fields.get(label.lower())
        if val and not _is_placeholder(val):
            return val
    return default


def disposition_hint(brief_path: Path) -> str:
    """The brief's ``- **Disposition hint:** value`` field, or "" if absent.

    The one place the disposition label is spelled, so the driver's close-fast-path
    classifier (issue #60) and any other reader share it.
    """
    return field(brief_path, "disposition hint", "disposition")


def do_model(brief_path: Path) -> str:
    """The Do backend the brief pins explicitly via ``- **Do model:** <name>`` (issue #167).

    The name is matched against a ``[[leaves.builder_variant]]`` ``model`` key to select the
    Do builder directly, bypassing the ``when`` routing. "" ⇒ unset ⇒ the ``when`` routing /
    default builder (the common case)."""
    return field(brief_path, "do model", "do_model", "builder model")


def planning_artifact(brief_path: Path) -> str:
    """The host planning artifact this brief points at, or "" if it's a self-contained brief.

    The optional ``- **Planning artifact:** <path|url>`` field (issue #67, ``plan-pointer``
    template): a reference to the host's OWN plan (an ADR / proposal / spec) that Do treats
    as authoritative. Absent ⇒ an ordinary brief that carries its own spec.
    """
    return field(brief_path, "planning artifact", "plan artifact", "plan source")


def is_placeholder(brief_path: Path) -> bool:
    """True if the brief is still an unfilled template — Slug missing or a ``<…>`` token.

    A ``brief.md`` copied from ``brief.md.tpl`` but never authored *looks* PLANNED (the
    file exists) yet carries no ticket content; ``state`` treats it as UNPLANNED so the
    Plan beat re-plans it instead of the planner being silently skipped (issue #113). The
    Slug — the first, always-filled field of any real brief — is the cheap, reliable
    sentinel: an authored slug is kebab-case, never an angle-bracket placeholder.
    """
    slug = field(brief_path, "slug").strip()
    return not slug or slug.startswith("<")


def test_files(brief_path: Path) -> list[Path]:
    """Paths named by the brief's test-requirement field, relative to the bundle.

    Used by the iterate transitions to unlink the shipped test (docs 03
    §clear_downstream_of_brief). Returns bundle-relative paths; the driver
    resolves them against the bundle dir.
    """
    raw = field(brief_path, "test file", "test path", "test requirement")
    if not raw:
        return []
    # Pull anything that looks like a path token out of the field value.
    tokens = re.findall(r"[\w./-]+\.\w+", raw)
    return [Path(t) for t in tokens]


def depends_on(brief_path: Path) -> list[str]:
    """Issue ids this bundle must wait for — each must be COMPLETE before it runs.

    The optional ``- **Depends on:** <id>[, <id>…]`` field (docs 09). Absent ⇒
    ``[]`` ⇒ today's sort-by-name scheduling, unaffected.
    """
    return _id_list(field(brief_path, "depends on", "depends_on"))


def depends_on_merged(brief_path: Path) -> list[str]:
    """Issue ids whose PR must be **merged** before this bundle runs (issue #107).

    The optional ``- **Depends on (merged):** <id>[, <id>…]`` field (docs 09): a stricter
    ``Depends on`` for a dependent that edits files a prerequisite also edits. Plain
    ``Depends on`` only waits for the prereq to reach COMPLETE — a draft PR, **not
    merged** — so a dependent built off the target base misses the prereq's diff and
    conflicts at merge. This gate holds the dependent until the prereq is merged into the
    base, so Do genuinely builds on the predecessor. Absent ⇒ ``[]``.
    """
    return _id_list(field(brief_path, "depends on (merged)", "depends_on_merged"))


def conflicts_with(brief_path: Path) -> list[str]:
    """Issue ids that must never run in the same concurrent wave as this bundle.

    The optional ``- **Conflicts with:** <id>[, <id>…]`` field (docs 09): a pair
    that edits a shared resource and so cannot be co-scheduled across lanes.
    """
    return _id_list(field(brief_path, "conflicts with", "conflicts_with"))


def stacks_on(brief_path: Path) -> list[str]:
    """Issue ids whose just-produced branch this bundle stacks on (issue #123).

    The optional ``- **Stacks on:** <id>[, <id>…]`` field: build this bundle on top of a
    prerequisite's *produced patch branch* within the SAME ``flow`` run — not waiting for
    a merge (unlike ``Depends on (merged)``) — and publish it as a separate stacked PR
    (``gh pr create --base <prereq-branch>``). Use for a planned, file-overlapping refactor
    sequence so the whole chain completes in one run. Names the immediate parent(s); the
    worktree + PR base derive from the parent's ``publish.json`` (never hand-written — the
    branch doesn't exist at Plan time). Absent ⇒ ``[]``.
    """
    return _id_list(field(brief_path, "stacks on", "stacks_on"))


def onto_branch(brief_path: Path) -> tuple[str, str] | None:
    """``(remote, branch)`` of an existing PR's head to stack a commit onto, or ``None``.

    The optional ``- **Onto branch:** <remote>/<branch>`` field (issue #54). Present ⇒
    publish contributes the fix as a commit on that branch instead of a new PR, and the
    same branch is the test base (Check's ``PDCA_BASE``), the commit base, and the push
    target. Absent ⇒ ``None`` ⇒ today's new-branch → new-PR flow. The documented shape is
    ``<remote>/<branch>``; a value with no ``/`` is treated as a branch on ``origin``.
    """
    raw = field(brief_path, "onto branch", "onto_branch").strip().strip("`").strip()
    if not raw:
        return None
    if "/" not in raw:
        return ("origin", raw)
    remote, _, branch = raw.partition("/")
    return (remote or "origin", branch)


def _id_list(raw: str) -> list[str]:
    """Issue ids out of the **leading id-list** of a field value, normalised to bare ids.

    Tolerates a leading ``#`` and the ``issue_`` bundle prefix so a brief may write
    ``#36`` / ``36`` / ``issue_36`` interchangeably; matches how ``cfg.bundle(id)``
    keys bundles.

    Parses only the leading run of id tokens and **stops at the first non-id token**, so
    a trailing rationale is ignored (issue #103). ``Depends on:`` / ``Conflicts with:``
    are the only list-parsed brief fields, yet authors and the headless planner routinely
    append a note — a parenthetical, or an em-dash meaning "none" — mirroring the
    template's own ``value (explanation)`` hint; left whole, that prose parsed into bogus
    ids and crashed the whole batch in ``_check_dep_graph``. An id is a bare reference
    (an issue number ``139``, or a tracker key ``PROJ-12`` / ``AA``); a natural-language
    rationale word — lowercase letters and no digit (``no``, ``kept``, ``PR-order``) —
    ends the run, so a value of pure prose or a bare ``—`` for "none" yields ``[]``.
    """
    ids: list[str] = []
    for tok in re.findall(r"#?[\w./-]+", raw or ""):
        bare = tok.lstrip("#").removeprefix("issue_")
        is_id = any(ch.isdigit() for ch in bare) or not any(ch.islower() for ch in bare)
        if not is_id:
            break  # a rationale word — the id-list has ended
        ids.append(bare)
    return ids
