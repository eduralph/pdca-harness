#!/usr/bin/env python3
"""Stop hook for the interactive PDCA leaves — the checked exit contract (issue #331).

The interactive leaves (Plan, sign-off, publish, Act) each have a checkable exit
contract, and until now nothing checked it at the boundary: the driver discards the
session's exit code, so "the human pressed Ctrl-D" and "the leaf discharged its
contract" are the same event. This hook makes the ``/handoff`` check non-optional —
a slash command cannot terminate its own session or be relied on to be typed — by
blocking a session that tries to end with a missing or malformed contract artifact,
with feedback naming exactly what is missing, and a deliberate-abandon escape hatch.

Protocols (mirroring ``builder_guard.py``, the mechanical-discipline peer):

* **Stop hook** (Claude Code): the event arrives as JSON on stdin; exit 0 allows the
  stop, exit 2 blocks it (stderr is fed back to the model). Inert — exit 0 — outside a
  driver-spawned leaf session (no ``PDCA_HANDOFF_ROLE`` in the environment), so an
  ad-hoc human session in the instance is never blocked.
* **``--check <id>``** (vendor-neutral CLI, used by the rendered ``/handoff`` command):
  verify the current leaf's contract for ONE required id; PASS ⇒ 0, FAIL ⇒ 1. There is
  no scan mode. The verdict is exit status + report — nothing is written to the bundle.
* **``--abandon "<why>"``**: the escape hatch — record a TYPED reason in the driver's
  session channel; the next stop is allowed and the driver reports the reason.

All contract logic lives in ``pdca_harness.handoff`` (plain Python, unit-tested
offline); this file only bootstraps the import and speaks the hook protocol.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _bootstrap():
    """Import the harness from the instance this hook is rendered into."""
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or
                Path(__file__).resolve().parents[2])
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from pdca_harness import handoff  # noqa: PLC0415 — deliberate late import
    from pdca_harness.config import Config
    return handoff, Config.load(root)


def _stop_verdict() -> int:
    role = (os.environ.get("PDCA_HANDOFF_ROLE") or "").strip()
    if not role:
        return 0  # not a driver-spawned leaf session — nothing to enforce
    try:
        json.load(sys.stdin)  # the Stop event envelope; presence is all we need
    except Exception:  # noqa: BLE001 — an unparseable event must not block a human
        pass
    try:
        handoff, cfg = _bootstrap()
        problems = handoff.stop_problems(cfg, role, handoff.load_state())
    except Exception as exc:  # noqa: BLE001 — a broken check must not trap the session
        print(f"handoff_guard: contract check unavailable ({exc}) — allowing the stop",
              file=sys.stderr)
        return 0
    if not problems:
        return 0
    print(
        f"This {role} leaf session may not end yet — its exit contract is not "
        "discharged:\n" + "\n".join(f"  - {p}" for p in problems) + "\n"
        "Write the missing/malformed artifact(s), verify with `/handoff <id>` "
        "(the required id: the bundle's issue id, or the act-log entry date), then end "
        "the session again. To deliberately abandon instead, run:\n"
        '  python3 .claude/hooks/handoff_guard.py --abandon "<why>"',
        file=sys.stderr)
    return 2


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--check":
        if len(argv) < 2 or not argv[1].strip():
            print("handoff_guard: --check requires an id (issue id or act-log entry "
                  "date) — there is no scan mode", file=sys.stderr)
            return 2
        handoff, cfg = _bootstrap()
        return handoff.run_check(cfg, argv[1])
    if argv and argv[0] == "--abandon":
        handoff, _cfg = _bootstrap()
        raw = os.environ.get(handoff.ENV_STATE, "")
        if not raw:
            print("handoff_guard: no leaf session is registered "
                  f"({handoff.ENV_STATE} unset) — nothing to abandon", file=sys.stderr)
            return 2
        reason = argv[1] if len(argv) > 1 else ""
        if not reason.strip():
            print("handoff_guard: --abandon requires a typed reason — the driver "
                  "reports it when the session ends", file=sys.stderr)
            return 2
        handoff.record_abandon(Path(raw), reason)
        print("handoff_guard: abandonment recorded — the session may now end; the "
              "driver will report the reason")
        return 0
    return _stop_verdict()


if __name__ == "__main__":
    raise SystemExit(main())
