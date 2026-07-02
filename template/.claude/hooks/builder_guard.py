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

Second protocol (vendor-neutral, used by pdca_harness.guard's `gh` PATH shim so
non-claude builders get the SAME single-sourced rules):
``builder_guard.py --command "<command line>"`` — exit 0 to allow, 2 to block.
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
# A `gh pr` invocation carrying command substitution can't be statically verified —
# the substituted text could expand to `ready`/`merge` (e.g. `gh pr $(echo ready)`).
# Deny it outright rather than trying to evaluate shell.
_GH_PR = re.compile(r"\bgh\s+pr\b")
_SUBSTITUTION = re.compile(r"\$\(|`")

_BLOCK_MSG = (
    "Blocked by the builder STOP discipline: the Do beat must not "
    "mark a PR ready or merge it. Push and open a DRAFT PR instead; "
    "the ready-mark happens at human Check sign-off (docs 03 §Do)."
)
_SUBST_MSG = (
    "Blocked by the builder STOP discipline: `gh pr` with command substitution "
    "cannot be statically verified — write the gh arguments literally."
)


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


def block_reason(command: str) -> str:
    """The STOP-discipline violation in ``command``, or "" if it passes."""
    for seg in _segments(command):
        normalized = _strip_wrappers(seg)
        for pat in BLOCKED:
            if pat.search(normalized):
                return _BLOCK_MSG
        if _GH_PR.search(normalized) and _SUBSTITUTION.search(normalized):
            return _SUBST_MSG
    return ""


def _verdict(command: str) -> int:
    reason = block_reason(command)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--command":
        # Vendor-neutral CLI mode (the guard.py PATH shim): the command line is
        # an argument, no JSON envelope.
        return _verdict(argv[1] if len(argv) > 1 else "")
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # not a parseable tool call — let other layers decide
    command = (data.get("tool_input") or {}).get("command", "")
    if not command:
        return 0
    return _verdict(command)


if __name__ == "__main__":
    raise SystemExit(main())
