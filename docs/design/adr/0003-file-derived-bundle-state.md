---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-04
tags:
  - adr
---
# 0003. Bundle state is derived from which files exist; no database

## Context

One contribution runs for hours to days, across interactive sessions, headless model runs,
crashes, `^C` and reboots. Any state held in memory or in a separate store can disagree with
the artifacts it describes, and a run that dies between "write the artifact" and "update the
state" leaves the two out of step.

## Decision

A bundle is a directory, `results/issue_<id>/`, and its state is a pure function of which
files exist in it and what §9 of `SUMMARY.md` says (`state.state`,
`template/src/pdca_harness/state.py:339`). There is no database and no state file.

| Files present | State |
|---|---|
| no `brief.md` (or a placeholder brief) | UNPLANNED (or RESOLVED if the tracker settled it) |
| `brief.md`, no `patch.diff` / `close-disposition` | PLANNED |
| `patch.diff` or `close-disposition`, no `check-gates.json` | BUILT |
| `check-gates.json`, no `SUMMARY.md` | CHECKED |
| `SUMMARY.md` with §9 empty | AWAITING_SIGNOFF |
| `SUMMARY.md` with a §9 outcome | COMPLETE, ITERATE_DO, ITERATE_PLAN or DISCONTINUED |

The halted states (`HALTED`) and the finished states (`TERMINAL`) are explicit sets defined in
`state.py` only. Present from the first commit (`3ca179a`, 2026-06-04).

## Consequences

Any crash is resumable by running `pdca flow` again. A bundle can be inspected, archived or
committed with ordinary tools, and instances record finished bundles in git (`pdca record`).

Every reader must treat every bundle file as possibly absent or truncated, because a leaf with
`Write`/`Bash` can leave the directory in any shape. The state is also read back from
model-authored Markdown (§9 and §6), so every reader is a parser over prose; #527, #606, #634
and #642 are one recurring bug class from that. Migration step 1 (ADR-0009, #712) keeps this
decision but moves the parsing onto typed records rendered to Markdown.
