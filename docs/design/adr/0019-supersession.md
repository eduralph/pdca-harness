---
created: 29.10.2026 22:21
type: adr
status: Proposed
tags:
  - adr
  - process
  - documentation
---
# 0019. How a frozen document is superseded

## Context

ADR-0001 and ADR-0018 freeze ratified documents, and both name *superseded* as a later
state. A reader who opens an old document needs to see that it has been replaced, and the
class index needs to say so too. Editing the frozen file in any other way must stay
impossible.

## Decision

Supersession is recorded in three places, in the same pull request:

1. The **new** document carries `supersedes: <old number or path>` in its frontmatter. It is
   a new file, which the immutability check always allows.
2. The **old** document receives a *supersession stamp*: its `status:` changes to
   `Superseded` (ADRs) or `superseded` (proposals, specs), optionally with a
   `superseded-by:` frontmatter field. Nothing else in the file may change. The
   `docs-immutability` check allows exactly this edit and nothing more: the body and every
   other frontmatter line must stay byte-for-byte identical, and a superseded document is
   frozen again.
3. The **class index** (`adr/README.md`, `proposals/README.md`, `specs/README.md`) marks the
   old entry *superseded by NNNN*. Each class MUST keep such an index; it is the place to
   look up a document's current status.

## Consequences

- An old document shows on its own page that it was replaced, and the index agrees.
- The record itself never changes, so references to it stay trustworthy.
- Every ratification or supersession includes an index edit, which is always allowed.
