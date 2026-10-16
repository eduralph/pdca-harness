---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 4. Solution strategy

> Living document. The shape of the solution and why. Open questions are marked **[OPEN]** inline.

## 4.1 The shape in one paragraph

A deterministic driver advances file-based bundles through four beats. Models fill artifacts
at six leaves inside those beats — planner, builder, reviewer, sign-off assistant, publisher,
Act — plus optional advisory, sizer and splitter leaves. The driver owns every transition,
every gate and the accept guard; leaves only produce files. Batches of bundles run in waves
(ordered by declared `Depends on` / `Conflicts with` edges) across parallel lanes, each in its
own git worktree, and accepted bundles are published as draft PRs, optionally stacked on a
shared integration line.

## 4.2 Key strategies

| Strategy | How | Decision |
|---|---|---|
| State is files | No database; state computed from which artifacts exist | ADR-0003 |
| Accept is code | One writer of §9; C6 refuses accept with open §6 rows | ADR-0004 |
| Decorrelated review | Different vendor; no builder notes; reviewer re-runs red→green | ADR-0005 |
| Evidence, not opinion | Only the per-fix red→green gate blocks by default | ADR-0006 |
| Template, not library | Copier render + `copier update` | ADR-0007 |
| Vendor differences are data | `families.py` profiles; one place builds the argv (`leaves._invoke`) | ADR-0005 |
| Fail closed | A reconstructed worktree before every gate read; a self-reported external dependency counts only if a `doctor` probe confirms it | — |

## 4.3 Build strategy

The target is the six-layer architecture of
[proposal 0002](../proposals/accepted/0002-target-architecture.md): records, a pure machine,
effects, roles, policies and orchestration, with typed outcomes. It is reached by a strangler
migration (ADR-0016); [§9](09-build-order-and-roadmap.md) holds the order.
