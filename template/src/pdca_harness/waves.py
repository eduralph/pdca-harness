"""Wave partitioning for batch flow — order a set of bundles into dependency waves.

A batch handed to ``flow`` runs as an **ordered sequence of waves**: each wave's
bundles build in parallel (no dependency *and* no conflict between them), a Check
barrier and per-wave sign-off close the wave, the wave's accepted work is folded onto
a run-scoped integration branch, and the *next* wave builds on top of it. So a bundle
that ``Depends on`` another lands in a *later* wave and builds on the prerequisite's
already-accepted result — the one mechanism that subsumes ``Depends on (merged)``
(#107) and ``Stacks on`` (#123): both are just "this builds after that", an ordinary
dependency edge, with the integration branch (not a human merge) carrying the
predecessor's diff forward.

Two declared fields shape the partition (docs 09):

* ``Depends on:`` — a directed prerequisite edge (with the back-compat
  ``Depends on (merged)`` / ``Stacks on``, all folded by :func:`declared_deps`).
* ``Conflicts with:`` — an *undirected* "these two edit a shared resource" relation.
  Because the harness now folds each wave onto the base before the next builds, two
  conflicting bundles must not share a wave (built blind on one base, the second's
  patch conflicts at fold). So each conflict pair is **oriented** into a dependency
  edge — by a deterministic id order — unless a dependency path already separates them.

A third, ``Merges with:`` (#412), names an atomic merge group: bundles that must not land
on the base separately. It adds no edge. Under ``wave_mode = "merge"`` the driver merges
each PR on its own, so a group this run can see (:func:`merge_groups`) is refused by the
strict check and held by the tolerant one; any other mode ignores it.

The result is a list of waves; within each there is provably no dependency and no
conflict edge, so the wave is safe to build in parallel and fold in one step. With no
fields declared the whole batch is a single wave, sorted by name — byte-for-byte the
prior sort-by-name dispatch. Control flow stays deterministic code: the planner only
*declares* the fields; this module *computes* the order.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import brief, state
from .config import Config


def declared_deps(bp: Path) -> list[str]:
    """Every declared prerequisite id of a brief — the union of ``Depends on``,
    ``Depends on (merged)`` (#107) and ``Stacks on`` (#123).

    In the wave model all three mean the same thing for ordering — "this bundle builds
    after that one" — because the integration branch carries the prerequisite's accepted
    diff into the dependent's base regardless of which field named it. The merged/stacks
    variants are kept for back-compat parsing and fold in here.
    """
    return brief.depends_on(bp) + brief.depends_on_merged(bp) + brief.stacks_on(bp)


class DependencyGraphError(ValueError):
    """The declared dependency graph is unschedulable — operator input, not a crash (#589).

    Still a ``ValueError``, so every caller that already expects one keeps working; the
    distinct type lets ``pdca flow`` turn exactly THIS refusal into a short message and
    rc 2 without swallowing any other ``ValueError``. ``remedy`` is the one-line "ways out"
    the CLI prints under the message.
    """

    def __init__(self, message: str, remedy: str) -> None:
        super().__init__(message)
        self.remedy = remedy


def check_dep_graph(cfg: Config, bundles: list[Path], *,
                    offered: frozenset[str] = frozenset()) -> None:
    """Validate the declared dependency DAG before any build (issue #36).

    A dependency that is neither in this batch nor an already-COMPLETE bundle on disk
    is a misconfigured brief; a cycle is unschedulable. Both raise
    :class:`DependencyGraphError` (a ``ValueError``) so the run aborts before touching any
    bundle. No deps declared ⇒ no-op.

    ``offered`` (bundle names, #590) are ids the request will schedule although they are
    not in ``bundles`` yet: the in-flight children ``pdca flow`` adopts from a recovery
    seed it was named (``flow._offered_by_seeds``). A ``Depends on`` / ``Depends on
    (merged)`` edge to one is resolvable and adds NO edge here — the child joins the
    schedule only at the seed splice, which re-levels with it in place, so that is where
    the edge orders anything. A ``Stacks on`` edge to one stays refused (a stack parent must
    be an active COMPLETE bundle). Empty ⇒ exactly the check as it always was.

    Under ``wave_mode = "merge"`` a run with any ``Merges with`` group member
    (:func:`merge_groups`) is refused the same way (#412): the driver merges each PR on its
    own, so a member could land on the base without the rest. Any other mode: no check.
    """
    names = {b.name for b in bundles}
    graph: dict[str, list[str]] = {}
    for b in bundles:
        bp = b / "brief.md"
        edges: list[str] = []
        # All three ordering fields are topological prerequisites for the DAG (existence
        # + cycle); in the wave model they fold into one dependency edge (#107/#123).
        stacks = set(brief.stacks_on(bp)) if bp.exists() else set()
        for dep in (declared_deps(bp) if bp.exists() else []):
            dn = cfg.bundle(dep).name
            if dn in names:
                edges.append(dn)
                continue
            if dn in offered and dep not in stacks:
                continue  # scheduled by the seed splice, which orders it (#590)
            # Out-of-batch: an archived (completed/) prereq satisfies Depends on / Depends
            # on (merged) (#171). But a `Stacks on` parent must be an ACTIVE bundle — the
            # dependent bases its worktree + stacked PR on the parent's *live* published
            # branch, read from the active bundle by publish._stack_base_branch — so resolve
            # Stacks on against bundle() (an archived-only stack parent stays rejected).
            resolved = cfg.bundle(dep) if dep in stacks else cfg.find_bundle(dep)
            if state.state(resolved) != state.COMPLETE:
                raise DependencyGraphError(
                    f"{b.name}: declared dependency '{dep}' is neither in this batch "
                    f"nor an existing COMPLETE bundle",
                    remedy=f"add {dep} to this run's ids; or, if it landed outside the "
                           f"cycle, drop the edge from {b.name}'s brief; or finish {dep} "
                           f"first")
        graph[b.name] = edges

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    path: list[str] = []

    def visit(n: str) -> None:
        color[n] = GRAY
        path.append(n)
        for m in graph[n]:
            if color[m] == GRAY:
                cyc = path[path.index(m):] + [m]
                raise DependencyGraphError(
                    "dependency cycle: " + " → ".join(cyc),
                    remedy="drop one edge of the cycle from its brief (`Depends on` / "
                           "`Depends on (merged)` / `Stacks on`)")
            if color[m] == WHITE:
                visit(m)
        path.pop()
        color[n] = BLACK

    for n in graph:
        if color[n] == WHITE:
            visit(n)

    # Merge mode `gh pr merge`s each non-final wave's PRs one at a time, so a group member
    # could land on the base alone (#412). Every group this run can see is refused, a
    # single-wave one too: a later merge-mode run (a resume, or a batch where another bundle
    # depends on one member) can pick its open PRs up and merge them one by one. Holding a
    # member and merging the group together is #778.
    if cfg.wave_mode == "merge":
        groups = merge_groups(cfg, bundles)
        if groups:
            raise DependencyGraphError(
                "; ".join(f"`Merges with` group {_group_text(g, names)}" for g in groups)
                + ': wave_mode = "merge" merges each PR on its own, so a member could land '
                  "on the base without the rest",
                remedy=_GROUP_WAYS_OUT)


#: The two ways out of a ``Merges with`` group under ``wave_mode = "merge"`` (#412), shared
#: by the strict refusal and the tolerant hold so they say the same thing.
_GROUP_WAYS_OUT = ('set [driver] wave_mode = "stack" and merge the group\'s PRs together '
                   "yourself; or fold the group into one bundle")


def _group_text(group: list[str], names: set[str]) -> str:
    """``issue_A + issue_B``: a group's members, each one outside the run marked so."""
    return " + ".join(n if n in names else f"{n} (not in this run)" for n in group)


def merge_groups(cfg: Config, bundles: list[Path]) -> list[list[str]]:
    """The atomic merge groups this run can see (#412), each a sorted list of bundle names.

    Read from the ``Merges with`` fields of the briefs IN the run, symmetrically (the way
    :func:`conflict_map` reads ``Conflicts with``): a bundle is a group member when its own
    brief declares a non-empty ``Merges with``, or when another in-run brief names it there.
    Unlike a conflict, a partner outside the run is kept, not dropped: a member merged by
    this run would still land on the base without it. Only in-run briefs are read, so a
    group declared only by a bundle outside the run is not seen. None declared ⇒ ``[]``.
    """
    parent: dict[str, str] = {}

    def root(n: str) -> str:
        while parent.setdefault(n, n) != n:
            n = parent[n]
        return n

    for b in bundles:
        bp = b / "brief.md"
        if not bp.exists():
            continue
        for mid in brief.merges_with(bp):
            parent[root(cfg.bundle(mid).name)] = root(b.name)
    groups: dict[str, list[str]] = {}
    for n in sorted(parent):
        groups.setdefault(root(n), []).append(n)
    return sorted(groups.values())


def conflict_map(cfg: Config, bundles: list[Path]) -> dict[str, set[str]]:
    """Symmetric bundle-name → conflicting-bundle-names map, restricted to this batch.

    A declared conflict naming a bundle outside the batch is moot (it cannot be
    co-scheduled with something that is not running) and is dropped.
    """
    names = {b.name for b in bundles}
    conflicts: dict[str, set[str]] = {b.name: set() for b in bundles}
    for b in bundles:
        bp = b / "brief.md"
        if not bp.exists():
            continue
        for cid in brief.conflicts_with(bp):
            other = cfg.bundle(cid).name
            if other in names and other != b.name:
                conflicts[b.name].add(other)
                conflicts[other].add(b.name)
    return conflicts


def _reaches(deps: dict[str, set[str]], src: str, dst: str) -> bool:
    """True iff ``dst`` is a transitive prerequisite of ``src`` (``dst`` builds before
    ``src``) in the current edge set — used to test whether a conflict pair is already
    ordered by a dependency path before orienting it."""
    seen = {src}
    stack = [src]
    while stack:
        for p in deps[stack.pop()]:
            if p == dst:
                return True
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return False


def compute_waves(cfg: Config, bundles: list[Path], *,
                  offered: frozenset[str] = frozenset()) -> list[list[Path]]:
    """Partition ``bundles`` into ordered waves.

    ``wave[k]`` holds the bundles whose every prerequisite is in an earlier wave; within
    a wave there is no dependency *and* no conflict edge, so it builds in parallel and
    folds onto the integration branch in one step. Raises :class:`DependencyGraphError`
    (a ``ValueError``, via :func:`check_dep_graph`) on an unschedulable graph — a cycle,
    or a dependency neither in this batch nor already COMPLETE (nor ``offered``, passed
    straight to :func:`check_dep_graph`) — and, in merge mode, on a ``Merges with`` group
    (#412). No fields declared ⇒ one wave, sort-by-name.
    """
    # cycle / unresolved dep → ValueError, before any work
    check_dep_graph(cfg, bundles, offered=offered)
    by_name = {b.name: b for b in bundles}

    # Directed prerequisite edges, restricted to the batch (out-of-batch prereqs are
    # already COMPLETE — validated above — so they impose no ordering here). deps[n] is
    # the set of in-batch bundles n must build after.
    deps: dict[str, set[str]] = {n: set() for n in by_name}
    for b in bundles:
        bp = b / "brief.md"
        if not bp.exists():
            continue
        for dep in declared_deps(bp):
            dn = cfg.bundle(dep).name
            if dn in by_name and dn != b.name:
                deps[b.name].add(dn)

    # Orient each conflict pair into a dependency edge so the two never share a wave. All
    # added edges point from the name-lower to the name-higher id (a strict total order),
    # so conflict edges alone are acyclic; we skip a pair a dependency path already orders
    # (either direction), which also prevents contradicting a dep edge — so the union
    # stays a DAG, which the leveling below relies on.
    cmap = conflict_map(cfg, bundles)
    for lo in sorted(cmap):
        for hi in sorted(cmap[lo]):
            if hi <= lo:
                continue  # visit each unordered pair once, with lo < hi by name
            if _reaches(deps, hi, lo) or _reaches(deps, lo, hi):
                continue  # already separated by a dependency path
            deps[hi].add(lo)  # the name-lower bundle builds first

    # Longest-path level per bundle: a source is wave 0; a dependent is one past its
    # latest prerequisite. The edge set is a DAG, so the memoised recursion terminates.
    level: dict[str, int] = {}

    def lvl(n: str) -> int:
        if n not in level:
            level[n] = 1 + max((lvl(p) for p in deps[n]), default=-1)
        return level[n]

    for n in by_name:
        lvl(n)

    waves: list[list[Path]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
    for n, k in level.items():
        waves[k].append(by_name[n])
    for w in waves:
        w.sort(key=lambda p: p.name)
    return waves


def _cycle_members(graph: dict[str, list[str]]) -> set[str]:
    """Every node that participates in a dependency cycle in ``graph`` (an adjacency map of
    in-batch prerequisite edges). Unlike :func:`check_dep_graph`, which raises on the first
    cycle, this collects *all* cycle members so the tolerant resume sweep can drop them."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    in_cycle: set[str] = set()
    stack: list[str] = []

    def visit(n: str) -> None:
        color[n] = GRAY
        stack.append(n)
        for m in graph.get(n, ()):
            if color.get(m) == GRAY:                 # back edge → stack[idx(m):] is a cycle
                in_cycle.update(stack[stack.index(m):])
            elif color.get(m) == WHITE:
                visit(m)
        stack.pop()
        color[n] = BLACK

    for n in graph:
        if color[n] == WHITE:
            visit(n)
    return in_cycle


def _propagate_holds(edges: dict[str, list[str]], held: dict[str, str]) -> None:
    """Fixpoint: mark any not-yet-held bundle whose in-batch prerequisite is held — it can't
    build on a base missing that prerequisite's change either. Mutates ``held`` in place."""
    changed = True
    while changed:
        changed = False
        for name, prereqs in edges.items():
            if name in held:
                continue
            blocked = [p for p in prereqs if p in held]
            if blocked:
                held[name] = f"prerequisite held ({', '.join(sorted(blocked))})"
                changed = True


def partition_schedulable(cfg: Config, bundles: list[Path]
                          ) -> tuple[list[Path], dict[str, str]]:
    """Split ``bundles`` into ``(schedulable, held)`` so one bad bundle can't abort the whole
    run (#191). Where :func:`check_dep_graph` *raises* on any unschedulable bundle — right for
    an explicit ``flow <ids>`` / ``pdca waves`` request — the resume sweep (:func:`flow_batch`)
    pulls in **every** in-flight bundle, so a single stale ``Depends on`` in an unrelated
    leftover would take the whole batch down with it.

    A bundle is **held** (skipped this run, left in-flight for a later one) when a declared
    dependency is *unresolvable* — neither in the surviving batch nor an existing COMPLETE
    bundle on disk (archived ``completed/`` too, #171; ``Stacks on`` against the active bundle)
    — or when it sits in a dependency cycle, or, under ``wave_mode = "merge"``, when it is a
    ``Merges with`` group member (:func:`merge_groups`, #412). Held-ness propagates to
    in-batch dependents (a fixpoint: a bundle whose prerequisite is held can't build either).
    The schedulable remainder is a valid DAG that :func:`compute_waves` levels without
    raising. ``held`` maps each held bundle name to a one-line reason for the caller to
    report."""
    names = {b.name for b in bundles}
    by_name = {b.name: b for b in bundles}
    edges: dict[str, list[str]] = {}
    held: dict[str, str] = {}
    for b in bundles:
        bp = b / "brief.md"
        in_batch: list[str] = []
        unresolved: list[str] = []
        stacks = set(brief.stacks_on(bp)) if bp.exists() else set()
        for dep in (declared_deps(bp) if bp.exists() else []):
            dn = cfg.bundle(dep).name
            if dn in names:
                in_batch.append(dn)
                continue
            resolved = cfg.bundle(dep) if dep in stacks else cfg.find_bundle(dep)
            if state.state(resolved) != state.COMPLETE:
                unresolved.append(dep)
        edges[b.name] = in_batch
        if unresolved:
            held[b.name] = f"unresolved dependency ({', '.join(unresolved)})"

    # What check_dep_graph refuses in merge mode, held here instead, with the same ways out
    # (#412). Before the propagation below, so a member's dependents are held as well.
    if cfg.wave_mode == "merge":
        for group in merge_groups(cfg, bundles):
            why = (f"`Merges with` group {_group_text(group, names)}, which wave_mode = "
                   f"\"merge\" would merge one PR at a time — {_GROUP_WAYS_OUT}")
            for name in group:
                if name in names:
                    held[name] = f"{held[name]}; {why}" if name in held else why

    # A bundle whose in-batch prerequisite is held can't build on a base missing that
    # prerequisite's change either — propagate to a fixpoint.
    _propagate_holds(edges, held)

    # Cycles among the survivors are unschedulable too (leveling can't terminate) — hold their
    # members, then RE-propagate so a downstream dependent of a cycle member is held as well.
    # (Without the second pass, a bundle depending on a cycle member survives into a reduced
    # batch that compute_waves then rejects — defeating the tolerance this adds, #197.)
    survivor_edges = {n: [p for p in e if p not in held] for n, e in edges.items()
                      if n not in held}
    for name in _cycle_members(survivor_edges):
        held.setdefault(name, "dependency cycle")
    _propagate_holds(edges, held)

    schedulable = [by_name[n] for n in by_name if n not in held]
    return schedulable, held


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_GIT = "diff --git "
# Git's C-quoting (quote.c) writes these one-letter escapes, and a three-digit octal escape
# for any other byte it quotes (first digit 0-3, so the value fits in one byte).
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
_C_OCTAL = re.compile(r"[0-3][0-7][0-7]")


def _unquote_c(s: str) -> tuple[str, str] | None:
    """Decode the Git C-quoted string that ``s`` starts with (``s[0]`` is ``"``).

    Returns the decoded text and what follows its closing quote, or ``None`` when the
    quoting is malformed: an unknown escape (``\\q``), an octal escape that is short or
    more than a byte (``\\3``, ``\\477``), or no closing quote. Octal escapes are bytes;
    the bytes are decoded as UTF-8 with U+FFFD for an invalid sequence — the same
    ``errors="replace"`` the patch text itself is read with.
    """
    out = bytearray()
    i = 1
    while i < len(s):
        c = s[i]
        if c == '"':
            return out.decode("utf-8", errors="replace"), s[i + 1:]
        if c != "\\":
            out += c.encode("utf-8", errors="replace")
            i += 1
        elif s[i + 1:i + 2] in _C_ESCAPES:
            out.append(_C_ESCAPES[s[i + 1]])
            i += 2
        elif _C_OCTAL.match(s, i + 1):
            out.append(int(s[i + 1:i + 4], 8))
            i += 4
        else:
            return None
    return None


def _header_paths(line: str) -> list[str]:
    """The old and new path in one ``diff --git`` header line, or ``[]`` when it cannot be
    read.

    Each side is ``a/<path>`` / ``b/<path>``, plain or C-quoted (``"a/caf\\303\\251.txt"``),
    and a rename can mix the two (``a/plain.txt "b/caf\\303\\251.txt"``). A quoted side
    starts the header or follows the space between the sides. A header with no quoted
    side is read by ``_DIFF_GIT_RE`` exactly as before. With one, both sides must parse —
    valid quoting, the ``a/`` / ``b/`` prefix, a non-empty path, nothing after the second
    side — or the header yields ``[]``. A plain side next to a quoted one is taken as
    written.
    """
    rest = line[len(_DIFF_GIT):]
    if not rest.startswith('"') and ' "' not in rest:
        m = _DIFF_GIT_RE.match(line)
        return [q.strip() for q in m.groups()] if m else []
    if rest.startswith('"'):
        first = _unquote_c(rest)
        if first is None or not first[1].startswith(" "):
            return []
        old, new = first[0], first[1][1:]
    else:
        old, _, new = rest.partition(' "')
        new = '"' + new
    if new.startswith('"'):
        second = _unquote_c(new)
        if second is None or second[1].strip():
            return []
        new = second[0]
    if old[:2] != "a/" or new[:2] != "b/" or not old[2:] or not new[2:]:
        return []
    return [old[2:], new[2:]]


def diff_files(patch_path: Path) -> set[str]:
    """The repo-relative paths a unified diff (``patch.diff``) touches.

    Parses the ``diff --git a/<old> b/<new>`` headers (covering modify / add / delete /
    rename); ``/dev/null`` is ignored. A side Git C-quoted — a name with a non-ASCII byte
    (``core.quotePath``), a ``"``, a ``\\`` or a control character, e.g.
    ``"a/caf\\303\\251.txt"`` — is decoded to the real path. Its octal escapes are bytes,
    decoded as UTF-8 with U+FFFD in place of bytes that are not valid UTF-8, so such a name
    is still counted. A header whose quoting is malformed (a bad escape, no closing quote)
    is skipped; patch content never makes this raise. A best-effort heuristic for the
    overlap audit — an unquoted path containing a space is ambiguous in the git header and
    may be imperfect — so the audit it feeds is advisory (it flags a likely-undeclared
    conflict for a human), never a hard gate.
    """
    if not patch_path.is_file():
        return set()
    files: set[str] = set()
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(_DIFF_GIT):
            for p in _header_paths(line):
                if p and p != "/dev/null":
                    files.add(p)
    return files
