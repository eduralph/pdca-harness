---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-10-15
tags:
  - adr
---
# 0001. Record architecture decisions

## Context

pdca-harness has made decisions since its first commit (2026-06-04) that everything else
depends on: file-derived state, a human-only accept, a decorrelated reviewer. None of them is
written down outside commit messages and docstrings. The result is visible in the tracker:
#505 (restructure outline) and #607 (state read back from Markdown) both re-derive decisions
the code already embodies, and docstrings carry issue history at more than one line of prose
per line of code. The approach review of 2026-10-15 named this as a gap.

## Decision

We will record significant decisions as Architecture Decision Records in
`docs/design/adr/`, using the Nygard-style template in `docs/design/templates/adr.md`.

- Files are numbered `NNNN-short-title.md` and indexed in `adr/README.md`.
- An ADR starts as Proposed. Once Accepted it MUST NOT be edited; a later ADR supersedes it,
  and the index records the supersession.
- A decision is "significant" when reversing it would change what the harness guarantees
  (the accept path, decorrelation, the evidence rule), the shape of the bundle files, or the
  contract with rendered instances.
- Decisions made before this ADR are backfilled (0002–0008), each dated to the commit or
  issue that made it.

## Consequences

The reasons for the driver's shape survive refactors, and a migration step can cite the
decision it changes. Reopening a settled question needs a new ADR, which costs more than an
issue comment; that is intended. Docstrings can drop their issue history over time and point
at an ADR instead.
