---
description: Verify the current PDCA leaf's exit contract for ONE named id (PASS/FAIL)
argument-hint: <issue_id>
allowed-tools: Bash(python3:*)
---

Verify the current interactive leaf's exit contract for the REQUIRED id `$1` — the
bundle's issue id (`issue_<id>` or the bare id), or, in an Act session, the date of the
act-log entry this session wrote. There is no scan mode: every invocation names exactly
one id. The check writes nothing into the bundle — its verdict is the exit status plus
the report below.

Using your Bash tool, run this now from the project root — no `!` pre-execution block:
the permission checker cannot match one that carries a shell expansion, so this call is
yours to make directly, the same as the `--abandon` call below:
`python3 .claude/hooks/handoff_guard.py --check $1`

Relay the PASS/FAIL verdict above to the human verbatim. On FAIL, fix the listed items
(write or repair the named contract artifact — never a stand-in), then run `/handoff $1`
again before ending the session. This command is the session's self-check; nothing
checks the contract when a turn ends. When the session ends, the driver re-checks the
bundles it registered for this session and tells the human what is still unmet — in a
session that picks its own issues, or an Act session, it checks that a `/handoff`
passed instead of re-reading what the session wrote. A deliberate abandonment is
recorded with
`python3 .claude/hooks/handoff_guard.py --abandon "<why>"`.
