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

_FIELD_RE = re.compile(r"^\s*-\s*\*{0,2}([^:*]+?)\*{0,2}:\s*(.*?)\s*$")


def parse_fields(brief_path: Path) -> dict[str, str]:
    """Return ``{lowercased label: value}`` for every bullet field in the brief."""
    fields: dict[str, str] = {}
    for line in brief_path.read_text(encoding="utf-8").splitlines():
        m = _FIELD_RE.match(line)
        if m:
            key = m.group(1).strip().lower()
            fields.setdefault(key, m.group(2).strip())
    return fields


def field(brief_path: Path, *labels: str, default: str = "") -> str:
    """First matching field value among ``labels`` (lowercased), else ``default``."""
    fields = parse_fields(brief_path)
    for label in labels:
        if label.lower() in fields:
            return fields[label.lower()]
    return default


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
