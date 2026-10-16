---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-04
tags:
  - adr
---
# 0004. No model writes §9; `accept` is refused while a NEEDS-HUMAN item is open (C6)

## Context

A model grades its own work leniently and will mark work done without end-to-end evidence.
If the accept decision can be written by a model, every other check in the cycle is advisory.
The maintainer's sign-off is the one step the harness exists to protect.

## Decision

- **One writer.** `signoff.record` (`template/src/pdca_harness/signoff.py:255`) is the only
  code that writes §9, and the driver's decision step is its only caller. Leaves propose a
  decision token; the human decides; the driver records it.
- **C6 accept-guard.** `accept` is refused while any `- [ ]` row is open under the **last**
  `## 6. NEEDS-HUMAN` heading of `SUMMARY.md` (`signoff.open_needs_human`). A SUMMARY with no
  §6 section cannot be evaluated and is refused (`signoff.unrecordable`).
- **Auto-iterate is narrow.** The automatic loop may only ever write `iterate-do`, only while an
  implementation-level finding exists, and never past `max_auto_iters`; it defers findings
  that need a person instead of ticking them.
- **Merging is human.** No leaf may mark a PR ready, merge it or approve it; a `gh` shim and a
  PreToolUse hook (`builder_guard`) enforce this.

Present from the first commit (`3ca179a`, 2026-06-04); the guard was extended by the `park`
disposition (#42) and the `unverifiable` class (#46) in June 2026.

## Consequences

Acceptance is code, not a prose verdict, and no model output can release a publish on its
own. The human carries the sign-off load, so the quality of §6 decides whether the guard is
read or rubber-stamped (risk R2 in architecture §11).

**Known gap.** The interactive sign-off session runs with `Edit`/`Write`, and nothing yet stops
it editing §9 directly; `state.state` would read that text as decided. The guard there is the
prompt and a watching human. #710 closes this with a hook.
