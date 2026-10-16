---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-04
tags:
  - adr
---
# 0005. The reviewer is a different vendor from the builder and never sees `build-notes.md`

## Context

A reviewer that shares the builder's model, prompt history or rationale shares its blind
spots: it tends to agree with the reasoning it was shown rather than check the result. Review
is only worth its cost if its errors are independent of the builder's.

## Decision

Decorrelate the reviewer from the builder by construction:

- **No builder rationale.** The builder writes `build-notes.md`; the driver withholds it. The
  reviewer runs in a temporary directory holding only `patch.diff`, `brief.md`,
  `check-gates.json` and the gate logs (builder/reviewer subagents, `a6cecb5`, 2026-06-04).
- **Different vendor by default.** The reviewer runs on a different model family from the
  builder (copier answer `reviewer_family`, "MUST differ from builder"). Vendor differences are
  data in `families.py` (`2ee991e`, 2026-07-02), so a codex reviewer against a claude builder
  is a configuration change.
- **Advisory leaves by complement.** Advisory reviewers are picked against the builder that
  actually ran, including an escalated one (#200, 2026-07-01). A same-vendor fallback leaves a
  §6 note instead of passing silently (#605).
- **The reviewer reproduces.** The reviewer gets a disposable git copy of base + patch so it
  can re-run the red leg itself (#419, 2026-08-06).

## Consequences

"The reviewer agrees" becomes "a different model reproduced it". Running two vendors means
two CLIs, two authentication set-ups and two sets of quirks for every instance. The
"MUST differ" rule is copier help text, not a check; #710 adds a `doctor` row for it.
