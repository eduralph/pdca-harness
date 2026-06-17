#!/usr/bin/env python3
"""lint_docs.py -- pre-publish guard for the pdca-harness docs.

One check, cheap and catching a real failure mode: no Obsidian-only link syntax.
`[[wikilinks]]` and `![[embeds]]` render in Obsidian but break on the static site
(and on GitHub). The docs use plain relative Markdown links by convention; this
guard stops the Obsidian syntax from leaking in when you edit there.

The scan ignores content inside fenced code blocks, inline code, and HTML
comments, so documenting the syntax (e.g. "write `[[Page]]` in Obsidian") or a
code sample never trips the linter.

Roots scanned: the guide (`docs/`, excluding `docs/publishing/`) and the vendored
quality-cycle model (`template/PCDA/quality-cycle/`) — i.e. everything
`render_site.py` publishes.

Usage:
    python3 docs/publishing/tools/lint_docs.py
    python3 docs/publishing/tools/lint_docs.py --warn-only   # report but exit 0
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PUBLISHING = TOOLS.parent
DOCS_ROOT = PUBLISHING.parent          # docs/
REPO_ROOT = DOCS_ROOT.parent           # repo root
SPEC_ROOT = REPO_ROOT / "template" / "PCDA" / "quality-cycle"

WIKILINK_RE = re.compile(r"(?<!!)\[\[[^\]]+\]\]")
EMBED_RE = re.compile(r"!\[\[[^\]]+\]\]")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def scannable_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) for lines outside fenced code blocks, with
    inline code spans and HTML comments blanked so patterns inside them never
    match."""
    text = HTML_COMMENT_RE.sub(" ", text)
    out: list[tuple[int, str]] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((i, INLINE_CODE_RE.sub(" ", line)))
    return out


def check_wikilinks(path: Path, rel: str) -> list[str]:
    errors = []
    for lineno, line in scannable_lines(path.read_text(encoding="utf-8")):
        if EMBED_RE.search(line):
            errors.append(f"{rel}:{lineno}: Obsidian embed ![[...]] -- "
                          f"use a Markdown image/link instead")
        elif WIKILINK_RE.search(line):
            errors.append(f"{rel}:{lineno}: Obsidian wikilink [[...]] -- "
                          f"use a relative Markdown link instead")
    return errors


def iter_docs():
    """Every published Markdown file, as (path, repo-relative posix string)."""
    for md in sorted(DOCS_ROOT.glob("*.md")):
        yield md, md.relative_to(REPO_ROOT).as_posix()
    if SPEC_ROOT.is_dir():
        for md in sorted(SPEC_ROOT.glob("*.md")):
            yield md, md.relative_to(REPO_ROOT).as_posix()


def main() -> None:
    ap = argparse.ArgumentParser(description="Lint the pdca-harness docs before publish.")
    ap.add_argument("--warn-only", action="store_true", help="Report problems but exit 0.")
    args = ap.parse_args()

    errors: list[str] = []
    for md, rel in iter_docs():
        errors += check_wikilinks(md, rel)

    if not errors:
        print("lint_docs: OK")
        return

    label = "WARNING" if args.warn_only else "ERROR"
    for e in errors:
        print(f"lint_docs: {label}: {e}", file=sys.stderr)
    print(f"\nlint_docs: {len(errors)} problem(s) found.", file=sys.stderr)
    sys.exit(0 if args.warn_only else 1)


if __name__ == "__main__":
    main()
