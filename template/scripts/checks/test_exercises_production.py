#!/usr/bin/env python3
"""Reference ADVISORY gate (#154): does the bundle's shipped test exercise the PRODUCTION
path, or a hand-ported copy?

A real Act finding caught a cycle whose test was green against a *parallel re-implementation*
of production — proving nothing. This is a deterministic, bundle-scoped heuristic for that
miss: every *added* test file in ``$PDCA_BUNDLE/patch.diff`` must import the project's
production package (``$PDCA_PROD_PACKAGE``). A test that imports nothing from production is
likely exercising a copy.

ADVISORY by construction: it always exits 0 and, when it cannot confirm, prints the
``PDCA-UNVERIFIABLE:`` marker so the harness routes it into SUMMARY §6 for the human to
adjudicate at sign-off — it never blocks. It is a *reference* (Python, import-based): wire
it as a ``[[gates.checks]]`` with ``scope = "bundle"``, ``gating = false`` and tune the
test-file pattern / package to your project.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

UNVERIFIABLE = "PDCA-UNVERIFIABLE:"
# A Python test module by the usual convention; tune for your project's layout.
_TEST_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")


def added_test_blocks(diff_text: str) -> dict[str, list[str]]:
    """``{test_path: [added lines]}`` for each **newly-added** test ``.py`` file in the diff.

    Only NEW test files (``new file mode`` / ``--- /dev/null``) are returned: an *edit* to
    an existing test may already import production as unchanged context, so requiring the
    import among the added lines would false-positive on routine test edits. A new test
    file, by contrast, has all of its content in the added lines — the import must be there.
    """
    files: dict[str, dict] = {}
    cur, keep = None, False
    for line in diff_text.splitlines():
        m = _DIFF_GIT.match(line)
        if m:
            cur = m.group(2).strip()
            keep = bool(_TEST_RE.search(cur))
            if keep:
                files.setdefault(cur, {"new": False, "added": []})
            continue
        if not keep or not cur:
            continue
        if line.startswith("new file mode") or line.startswith("--- /dev/null"):
            files[cur]["new"] = True
        elif line.startswith("+") and not line.startswith("+++"):
            files[cur]["added"].append(line[1:])
    return {p: f["added"] for p, f in files.items() if f["new"]}


def unverifiable_reason(diff_text: str, pkg: str) -> str | None:
    """The reason this bundle can't be confirmed to exercise production, or ``None`` (OK).

    A test file that adds no ``import``/``from`` of ``pkg`` may be exercising a copy."""
    blocks = added_test_blocks(diff_text)
    if not blocks:
        return None  # no test file added in this patch — nothing to assert
    imp = re.compile(rf"^\s*(from\s+{re.escape(pkg)}[.\s]|import\s+{re.escape(pkg)}([.\s,]|$))")
    missing = [p for p, lines in blocks.items() if not any(imp.search(line) for line in lines)]
    if missing:
        return (f"test file(s) add no import of the production package '{pkg}' — may "
                f"exercise a copy, not production: {', '.join(missing)}")
    return None


def main() -> int:
    pkg = os.environ.get("PDCA_PROD_PACKAGE", "").strip()
    bundle = os.environ.get("PDCA_BUNDLE", "")
    if not pkg:
        print(f"{UNVERIFIABLE} PDCA_PROD_PACKAGE is unset — set it to the production "
              "import root (the package a test must exercise)")
        return 0
    patch = Path(bundle, "patch.diff") if bundle else None
    if not patch or not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        print("no patch to check (close / no-fix disposition)")  # nothing to assert — pass
        return 0
    reason = unverifiable_reason(patch.read_text(encoding="utf-8", errors="replace"), pkg)
    if reason:
        print(f"{UNVERIFIABLE} {reason}")
        return 0
    print(f"added test(s) import the production package '{pkg}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
