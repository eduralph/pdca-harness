---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-10-15
tags:
  - adr
---
# 0016. The migration to the target architecture is a strangler, never a rewrite

## Context

The approach review of 2026-10-15 (proposal 0001) found the model right and the
implementation grown into a platform: 28,046 lines of driver, `leaves.py` at 4,706 lines,
`flow.py` at 2,834, a cyclic module graph and two drivers for one state machine. Proposal
0002 sets a target of six layers. Instances run the harness every day, and pdca-pdca builds
the harness with itself, so the driver cannot stop working while it is restructured.

## Decision

We migrate in six steps (records, runner seam, roles, pure machine, policies and outcomes,
orchestration strategies), each delivered as sliced briefs under the size budget through the
normal cycle. Every step keeps the offline suite green, opens with its ADR, and is done only
when its exit gate is met, whatever its PR count. Milestones keep shipping; the steps open
0.61, 0.62, 0.64, 0.67 and 0.70 (architecture §9).

Instances are not pinned to an older template version during the migration.

## Consequences

The harness stays usable throughout, and every step is reviewed the way any other
contribution is. Old and new structures coexist for a while, which costs some duplication.
Test migration is the main cost — most tests are coupled to module shapes, not behaviour — and
is budgeted in each step's brief. Because instances follow `main`, every step must keep the
rendered output and the bundle files compatible; step 1 enforces this with golden-file tests
against the frozen bundles.
