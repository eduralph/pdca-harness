#!/usr/bin/env python3
"""PreToolUse guard for the builder subagent — enforces the STOP discipline.

The Do beat MAY push to a feature/draft branch and open a draft PR (useful for
CI), but MUST NOT mark a PR ready or merge it — that is the human's Check
sign-off step (docs 01/03 §STOP discipline). This is enforced mechanically here
rather than asked of the model: scoped to the builder subagent via a PreToolUse
hook, so the human and the driver's accept step can still mark PRs ready.

Protocol (Claude Code hooks): read the tool call as JSON on stdin; exit 0 to
allow, exit 2 to block (stderr is shown to the model). Compound commands are
split on shell operators and every segment must pass — matching how Claude Code
itself evaluates Bash permission rules.
"""

from __future__ import annotations

import json
import re
import sys

# Segments matching these (after stripping leading wrappers) are blocked.
BLOCKED = [
    re.compile(r"^gh\s+pr\s+ready\b"),
    re.compile(r"^gh\s+pr\s+merge\b"),
    re.compile(r"^gh\s+pr\s+review\b.*--approve"),
]
_SEPARATORS = re.compile(r"&&|\|\||;|\|&|\||&|\n")
_WRAPPERS = ("timeout", "time", "nice", "nohup", "stdbuf", "env")


def _segments(command: str) -> list[str]:
    return [s.strip() for s in _SEPARATORS.split(command) if s.strip()]


def _strip_wrappers(seg: str) -> str:
    parts = seg.split()
    while parts and parts[0] in _WRAPPERS:
        parts = parts[1:]
        # skip a trailing numeric arg to `timeout`/`nice` etc. if present
        while parts and parts[0].lstrip("-").replace(".", "").isdigit():
            parts = parts[1:]
    return " ".join(parts)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # not a parseable tool call — let other layers decide
    command = (data.get("tool_input") or {}).get("command", "")
    if not command:
        return 0
    for seg in _segments(command):
        normalized = _strip_wrappers(seg)
        for pat in BLOCKED:
            if pat.search(normalized):
                print(
                    "Blocked by the builder STOP discipline: the Do beat must not "
                    "mark a PR ready or merge it. Push and open a DRAFT PR instead; "
                    "the ready-mark happens at human Check sign-off (docs 03 §Do).",
                    file=sys.stderr,
                )
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
