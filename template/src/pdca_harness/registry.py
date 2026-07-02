"""Reverse registry-consistency check (issue #205) — a pure, deterministic diff analysis.

The FORWARD direction (a source file a patch *adds* must be registered in a manifest, e.g.
gramps' ``T2-potfiles``) is an instance gate. This is the REVERSE, which nothing caught
before: every line a patch *adds* to a declared registry/manifest file must reference a path
the **same patch** touches. A registry line pointing at a file the patch never touches is
cross-bundle / build-tree contamination (a line that belongs to another bundle, leaked in
through a shared checkout) — it makes the stored patch fail to apply to its clean publish
base, yet passed Check and sat COMPLETE. This flags it statically over ``patch.diff`` at
Check, no build and no checkout.

Generic in the harness: an instance names its registry files (``[gates.registry_consistency]
files``) and, for manifests whose lines aren't bare paths, an extraction ``pattern`` (a
regex whose first group is the referenced path; default ⇒ the whole line, for POTFILES-style
bare-path manifests). :func:`find_violations` is the pure predicate; the ``registry-check``
CLI subcommand wires it into a bundle-scoped ``[[gates.checks]]`` entry.
"""

from __future__ import annotations

import re

_DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.MULTILINE)


def touched_paths(diff_text: str) -> set[str]:
    """Every repo path the diff touches (added / modified / deleted), read from its
    ``diff --git a/<p> b/<p>`` headers — the allowed set a registry line may reference. Both
    sides are included so a rename's old and new path both count as touched."""
    paths: set[str] = set()
    for m in _DIFF_GIT.finditer(diff_text):
        paths.add(m.group("a"))
        paths.add(m.group("b"))
    return paths


def added_lines(diff_text: str) -> dict[str, list[str]]:
    """Map each file the diff writes to → the CONTENT of the ``+`` lines it adds there (the
    leading ``+`` stripped; the ``+++`` header itself excluded). The current file is tracked
    from each ``+++ b/<path>`` header, so a ``+`` line is attributed to the hunk it lands in;
    a ``+++ /dev/null`` (a deletion) attributes nothing."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            # Drop a trailing tab-timestamp some diffs carry ("b/path\t2026-...").
            target = target.split("\t", 1)[0]
            current = None if target == "/dev/null" else re.sub(r"^b/", "", target)
        elif line.startswith("--- "):
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            if current is not None:
                out.setdefault(current, []).append(line[1:])
    return out


def _referenced_path(added_line: str, pattern: re.Pattern[str] | None) -> str | None:
    """The path a registry line references, or ``None`` for a line that references none
    (blank or a ``#`` comment). With no ``pattern`` the whole stripped line is the path (a
    bare-path manifest like POTFILES); with one, its first group is the path."""
    s = added_line.strip()
    if not s or s.startswith("#"):
        return None
    if pattern is None:
        return s
    m = pattern.search(s)
    return m.group(1).strip() if m and m.group(1) else None


def find_violations(diff_text: str, registry_files: list[str], pattern: str = "") -> list[str]:
    """Every reverse-consistency violation in ``diff_text``: an added line, in one of the
    named ``registry_files``, that references a path the same diff does not touch. Returns a
    list of human-readable messages (empty ⇒ consistent). ``pattern`` (optional) extracts the
    path from a non-bare-path manifest line (group 1). Pure — no I/O."""
    if not registry_files:
        return []
    touched = touched_paths(diff_text)
    added = added_lines(diff_text)
    rx = re.compile(pattern) if pattern else None
    violations: list[str] = []
    for reg in registry_files:
        for line in added.get(reg, []):
            ref = _referenced_path(line, rx)
            if ref is not None and ref not in touched:
                violations.append(
                    f"{reg}: adds a registry line for '{ref}', a path this patch does not "
                    "touch (cross-bundle / build-tree contamination)")
    return violations
