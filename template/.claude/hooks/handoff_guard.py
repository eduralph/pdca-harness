#!/usr/bin/env python3
"""The session side of the interactive PDCA leaves' exit contract (issues #331, #534).

The interactive leaves (Plan, sign-off, publish, Act) each have a checkable exit
contract. The driver judges it when it reaps the leaf's process and reports what it
finds to the human (``pdca_harness.handoff.session``). This script is what the session
itself runs (vendor-neutral CLI):

* **``--check <id>``** (used by the rendered ``/handoff`` command): verify the current
  leaf's contract for ONE required id; PASS ⇒ 0, FAIL ⇒ 1. There is no scan mode. The
  verdict is exit status + report — nothing is written to the bundle.
* **``--abandon "<why>"``**: record a TYPED reason in the driver's session channel; when
  the session ends the driver prints it, followed by everything its exit-contract check
  finds (an abandon explains the gap and hides none of it).
* **No arguments** is how the retired #331 **Stop** hook registration ran this script,
  and it is inert: exit 0, no output, stdin unread. Stop fires every time the main agent
  finishes a turn, not when the session ends, and a Stop hook's exit 2 sends its stderr
  back to the model instead of handing the turn to the human. So a leaf that asked the
  human a question never reached them: the model was told to write the missing artifact
  (for sign-off, the human's own decision) and was blocked again on every turn (#534).
  The template no longer registers the hook. An instance whose ``settings.json`` still
  does (a ``copier update`` merge that kept it) must not deadlock, so this path does
  nothing.

All contract logic lives in ``pdca_harness.handoff`` (plain Python, unit-tested
offline); this file only bootstraps the import and speaks the CLI.
"""

from __future__ import annotations

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
        print("handoff_guard: abandonment recorded — when the session ends, the driver "
              "shows the human this reason and whatever its exit-contract check finds")
        return 0
    if argv:
        print(f"handoff_guard: unknown mode {argv[0]!r} — use --check <id> or "
              '--abandon "<why>"', file=sys.stderr)
        return 2
    # No arguments: how the retired Stop registration ran this script (#534). Never
    # block a turn end and never write text the model would read: exit 0, silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
