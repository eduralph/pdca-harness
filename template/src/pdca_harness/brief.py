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


_HEADING_RE = re.compile(r"^\s*#{1,6}\s")


def whole_field(brief_path: Path, *labels: str, default: str = "") -> str:
    """The COMPLETE value of the first field matching ``labels`` — its inline remainder
    plus every continuation line — or ``default`` when the field is absent (issue #336).

    :func:`field` is line-based, so a value wrapped onto following lines is cut at its
    first line. `brief.md.tpl` itself writes the Success criterion placeholder across two
    lines, planners copy that shape, and the renderer then dropped all but the first —
    which is why 93% of criteria reached sign-off truncated.

    **Where the block ends**, in priority order, because the order is the whole difficulty:

    1. A line indented MORE than the field's own bullet is a continuation — *even when it
       looks like a field*. A nested sub-bullet list is ordinary Markdown, and
       ``  - **API:** …`` under a ``- **Scope:**`` matches the field pattern exactly.
       Testing the pattern first (as the calibration script's ``field_block`` does) ends
       the block at the first nested bullet and renders the field EMPTY — worse than the
       truncation this function exists to remove.
    2. Otherwise a field-shaped line at the same or lower indent starts the next field.
    3. Otherwise a heading, or unindented prose, ends the value — continuation membership
       is indentation, so a field near the end of a brief cannot swallow what follows it.

    Returns the value **raw**: no :func:`_is_placeholder` filtering, so an unfilled field
    still renders exactly as it does today rather than silently vanishing. Callers that
    need the absent-is-safe semantics keep using :func:`field`.
    """
    text = brief_path.read_text(encoding="utf-8")
    for label in labels:  # label PRIORITY, as field() resolves it — not file order
        found = _block_for(text, label.lower())
        if found is not None:
            return found
    return default


def _block_for(text: str, label: str) -> str | None:
    """The block belonging to ``label``, or None when the brief has no such field.

    Continuation lines keep their indentation RELATIVE to each other. Stripping each line
    independently would flatten a nested value — `Scope → API → GET` becomes three
    siblings — and `_item` then indents them all equally, so SUMMARY §1 states a different
    specification from the one the brief authored, in the artifact the human and the C6
    guard read. The block is dedented by the common leading whitespace of its continuation
    lines, which puts the shallowest at column 0 and preserves every level below it.
    """
    head: str | None = None
    cont: list[str] = []
    base_indent: int | None = None
    for line in text.splitlines():
        m = _FIELD_RE.match(line)
        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            if m and m.group(1).strip().lower() == label:
                base_indent = indent
                head = m.group(2)  # the value only, never the label
            continue
        if line.strip() and indent > base_indent:
            cont.append(line)      # nested bullet or wrapped prose — still this field
            continue
        if m:
            break                  # a sibling field at the same or lower indent
        if _HEADING_RE.match(line) or (line.strip() and indent <= base_indent):
            break
        cont.append(line)          # a blank line inside the block
    if base_indent is None:
        return None
    widths = [len(ln) - len(ln.lstrip()) for ln in cont if ln.strip()]
    shift = min(widths) if widths else 0
    body = [ln[shift:] if ln.strip() else "" for ln in cont]
    return "\n".join([head or ""] + body).strip("\n").rstrip()


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

    Read through :func:`whole_field`, not :func:`field` (#336/#334): a brief that writes
    its Slug on the line BENEATH the label — a shape `brief.md.tpl` itself teaches and four
    briefs in one measured corpus use — reads as *placeholder* under the line-based
    accessor. That misreading is load-bearing beyond re-planning: `state()` consults the
    tracker's terminal `resolved` marker for a placeholder brief, so a live, authored,
    reopened bundle carrying a stale marker would be classified RESOLVED and abandoned.
    """
    slug = whole_field(brief_path, "slug").strip()
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


# A backticked dependency token, plus any immediately-following parenthetical annotation:
#   `protoc` (build)                 → checkable, id must be "protoc"
#   `partition-cluster` (no-check: …) → exempt, nothing can detect it
_DEP_TOKEN_RE = re.compile(r"`([^`]+)`\s*(\([^)]*\))?")

# Annotations that mark a declared dependency as having no possible detect command.
_NO_CHECK_MARKERS = ("no-check", "topology")


def external_dependency_tokens(brief_path: Path) -> list[str]:
    """Backticked tokens in ``External dependencies`` that MUST each name a registered
    ``[[doctor.checks]]`` row ``id`` (issue #263).

    Registration is a forcing function, not best-effort: a dependency a human installs or
    provides is written as a **backticked token equal to that row's id** (`` `protoc` `` ↔
    ``id = "protoc"``), and the driver reconciles the two at Check. A dependency with no
    possible detect command — a topology / environment shape (a ≥3-replica cluster, a
    partition-capable stack) — is written in plain prose, or annotated ``(no-check: <why>)``
    / ``(topology …)``; either is exempt and yields no token. ``none``, and an unfilled
    ``<…>`` placeholder (``field`` reads it as absent), yield ``[]``.

    Deliberately conservative: only an explicitly-backticked token is checkable, so free
    prose can never manufacture a false "unregistered dependency". Like every brief field
    this reads the label's own line, so a token on a wrapped continuation line is missed —
    a false NEGATIVE, never a false positive.
    """
    raw = field(brief_path, "external dependencies", "external deps")
    if not raw or raw.strip().lower().rstrip(".") == "none":
        return []
    tokens: list[str] = []
    for token, annotation in _DEP_TOKEN_RE.findall(raw):
        note = (annotation or "").lower()
        if any(marker in note for marker in _NO_CHECK_MARKERS):
            continue
        token = token.strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


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


def _clean_ref(raw: str) -> str:
    """Isolate a git ref / repo spec from a brief field side, tolerating markdown
    backticks and trailing prose. A ref / ``owner/repo`` has no spaces, so a
    fully-backtick-quoted ref (``\\`main\\``` / ``\\`owner/repo\\```) wins, else the first
    whitespace token; strip stray backticks and trailing sentence punctuation.

    The backtick span is honored only when it is the START of the field (``re.match``),
    NOT anywhere in it (#235): a base written ``main (feature branch \\`feat/x\\`)`` names
    the base ``main`` — the backticked span is a parenthetical aside about a *different*
    branch, and taking it silently resolves the wrong base (whose ref doesn't exist →
    worktree isolation was falling back to mutating the operator's checkout in place).

    **THE one parse of the target field** (issue #387). It lived in ``publish`` while the
    base-resolution ladder the harness publishes to gate scripts
    (``engine/scripts/run-verify.sh``) ends in "the brief's ``Repo + branch target``" — a
    rung reachable only from Python, so every instance re-derived this rule in bash and
    inherited the pre-#235 unanchored version. It sits here, with the other per-field
    accessors, so ``publish`` and ``gates`` (which exports the resolved value as
    ``$PDCA_BRIEF_BASE``) share one implementation and no consumer re-derives it."""
    raw = raw.strip()
    m = re.match(r"`([^`]+)`", raw)               # a fully-backtick-quoted ref at the start wins
    token = m.group(1) if m else (raw.split()[0] if raw.split() else "")
    return token.strip("`").rstrip(",.;:")


def repo_target(brief_path: Path) -> tuple[str, str]:
    """``(repo_spec, base_branch)`` of the brief's ``- **Repo + branch target:**`` field,
    e.g. ``("example-org/example-repo", "main")``; ``("", "")`` when the field is absent.

    The field is commonly written with markdown backticks and/or trailing prose after the
    branch (``owner/repo @ main (feature branch \\`feat/x\\`)``); the two sides are split on
    ``@`` and each isolated by :func:`_clean_ref`, so that style cannot corrupt the resolved
    checkout/base (see #25, #235, #262)."""
    target = field(brief_path, "repo + branch target", "repo + branch", "target")
    repo_spec, _, base = target.partition("@")
    return _clean_ref(repo_spec), _clean_ref(base)


def base_branch(brief_path: Path, default: str = "") -> str:
    """The brief's OWN base branch — the ``@`` side of ``Repo + branch target`` — or
    ``default`` (the project's default branch) when the brief names none (issue #387).

    The value publish will commit against, so a bundle-scoped verify gate that must
    establish red→green on the deploy base can be *told* it (``$PDCA_BRIEF_BASE``,
    ``gates._run_one``) instead of parsing ``brief.md`` itself."""
    return repo_target(brief_path)[1] or default


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
