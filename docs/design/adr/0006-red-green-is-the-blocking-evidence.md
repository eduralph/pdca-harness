---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-05
tags:
  - adr
---
# 0006. Red-before / green-after is the only evidence allowed to block a sign-off

## Context

Gates can audit many things: conformance rules, the whole test suite, interface tests,
coverage. Most of them also see code the current fix did not touch, so gating on them blocks
a correct fix for problems that already existed. A gate that blocks must answer one question
for this contribution only: did the defect exist, and is it gone?

## Decision

Only the per-fix C4 gate — the bundle's test fails without the fix and passes with it —
blocks by default. Every other tier (runtime, conformance T1/T2/T4, interface) defaults to
advisory (`gating = false`) and is promoted only once its targeted artifacts are clean
(gate-tier policy, `dce8394`, 2026-06-05). A gate that cannot produce evidence (rc 77 or a
`PDCA-UNVERIFIABLE:` line) is not a pass and not a fail: it becomes a §6 NEEDS-HUMAN item
under the C6 guard (#46, 2026-06-16).

## Consequences

A sign-off is blocked by proof about this change, not by legacy debt. Changes without an
isolable red — new code, docs-only or test-only — land as UNVERIFIABLE C4 rows the human must
judge; these are a large share of §6 today (the approach review counts 167). Instances must
write a C4 gate that can actually run the red leg; a compile error that scored PASS in one
instance was an Act finding.
