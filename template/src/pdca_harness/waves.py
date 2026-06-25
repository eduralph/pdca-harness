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


def check_dep_graph(cfg: Config, bundles: list[Path]) -> None:
    """Validate the declared dependency DAG before any build (issue #36).

    A dependency that is neither in this batch nor an already-COMPLETE bundle on disk
    is a misconfigured brief; a cycle is unschedulable. Both raise ``ValueError`` so the
    run aborts before touching any bundle. No deps declared ⇒ no-op.
    """
    names = {b.name for b in bundles}
    graph: dict[str, list[str]] = {}
    for b in bundles:
        bp = b / "brief.md"
        edges: list[str] = []
        # All three ordering fields are topological prerequisites for the DAG (existence
        # + cycle); in the wave model they fold into one dependency edge (#107/#123).
        for dep in (declared_deps(bp) if bp.exists() else []):
            dn = cfg.bundle(dep).name
            if dn in names:
                edges.append(dn)
            elif state.state(cfg.bundle(dep)) != state.COMPLETE:
                raise ValueError(
                    f"{b.name}: declared dependency '{dep}' is neither in this batch "
                    f"nor an existing COMPLETE bundle")
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
                raise ValueError("dependency cycle: " + " → ".join(cyc))
            if color[m] == WHITE:
                visit(m)
        path.pop()
        color[n] = BLACK

    for n in graph:
        if color[n] == WHITE:
            visit(n)


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


def compute_waves(cfg: Config, bundles: list[Path]) -> list[list[Path]]:
    """Partition ``bundles`` into ordered waves.

    ``wave[k]`` holds the bundles whose every prerequisite is in an earlier wave; within
    a wave there is no dependency *and* no conflict edge, so it builds in parallel and
    folds onto the integration branch in one step. Raises ``ValueError`` (via
    :func:`check_dep_graph`) on an unschedulable graph — a cycle, or a dependency neither
    in this batch nor already COMPLETE. No fields declared ⇒ one wave, sort-by-name.
    """
    check_dep_graph(cfg, bundles)  # cycle / unresolved dep → ValueError, before any work
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


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


def diff_files(patch_path: Path) -> set[str]:
    """The repo-relative paths a unified diff (``patch.diff``) touches.

    Parses the ``diff --git a/<old> b/<new>`` headers (covering modify / add / delete /
    rename); ``/dev/null`` is ignored. A best-effort heuristic for the overlap audit —
    a path containing a space is ambiguous in the git header and may be imperfect — so
    the audit it feeds is advisory (it flags a likely-undeclared conflict for a human),
    never a hard gate.
    """
    if not patch_path.is_file():
        return set()
    files: set[str] = set()
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _DIFF_GIT_RE.match(line)
        if m:
            for p in (q.strip() for q in m.groups()):
                if p and p != "/dev/null":
                    files.add(p)
    return files
