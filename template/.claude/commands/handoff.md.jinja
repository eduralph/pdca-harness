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

!`python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/handoff_guard.py" --check "$1"`

Relay the PASS/FAIL verdict above to the human verbatim. On FAIL, fix the listed items
(write or repair the named contract artifact — never a stand-in), then run `/handoff $1`
again before ending the session. The Stop hook enforces this same contract when the
session ends; a deliberate abandonment is recorded with
`python3 .claude/hooks/handoff_guard.py --abandon "<why>"`.
