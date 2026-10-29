---
created: 15.10.2026
type: index
# Supersession is recorded in this index only; the old ADR stays as it is (ADR-0001).
supersession: index-only
tags:
  - adr
---
# Architecture Decision Records

Numbered, immutable records of significant decisions and *why* they were made. An ADR is
never edited after acceptance; a later ADR supersedes it, and this index records the
supersession. Format follows the lightweight Nygard template (see ADR-0001).

ADRs 0001–0008 were backfilled on 2026-10-15: they record decisions the harness had already
made, each dated to the commit or issue that made it. ADRs 0009–0015 are written as the first
PR of each migration step (see [architecture §9](../architecture/09-build-order-and-roadmap.md)).

## Index

| # | Title | Decided | Status |
|---|-------|---------|--------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | 2026-10-15 | Accepted |
| [0002](0002-apache-2-and-dco.md) | Apache-2.0 license and DCO sign-off | 2026-06-20 | Accepted |
| [0003](0003-file-derived-bundle-state.md) | Bundle state is derived from which files exist; no database | 2026-06-04 | Accepted |
| [0004](0004-no-model-writes-the-decision.md) | No model writes §9; `accept` is refused while a NEEDS-HUMAN item is open (C6) | 2026-06-04 | Accepted |
| [0005](0005-decorrelated-reviewer.md) | The reviewer is a different vendor from the builder and never sees `build-notes.md` | 2026-06-04 | Accepted |
| [0006](0006-red-green-is-the-blocking-evidence.md) | Red-before / green-after is the only evidence allowed to block a sign-off | 2026-06-05 | Accepted |
| [0007](0007-copier-template-distribution.md) | Distribution is a Copier template; instances concretize and update | 2026-06-04 | Accepted |
| [0008](0008-stdlib-only-python.md) | The driver is pure stdlib, Python ≥ 3.11 | 2026-06-04 | Accepted |
| 0009 | The bundle document format is a versioned spec; Markdown is rendered from typed records | step 1 | Planned ([#712](https://github.com/eduralph/pdca-harness/issues/712)) |
| 0010 | Leaves run behind a `LeafRunner` protocol; budgets and token telemetry live in the runner | step 2 | Planned ([#711](https://github.com/eduralph/pdca-harness/issues/711)) |
| 0011 | A role is data: prompt, input manifest, output schema, stub | step 3 | Planned ([#716](https://github.com/eduralph/pdca-harness/issues/716)) |
| 0012 | One driver; transitions are pure functions over records | step 4 | Planned ([#713](https://github.com/eduralph/pdca-harness/issues/713)) |
| 0013 | Policies implement a `Policy` protocol discovered from `pdca.toml` or an instance's `pdca_ext/` | step 5 | Planned ([#714](https://github.com/eduralph/pdca-harness/issues/714)) |
| 0014 | Every phase returns `Ok / Held / FailedInfra`, recorded in `run.json` and the exit code | step 5 | Planned ([#714](https://github.com/eduralph/pdca-harness/issues/714)) |
| 0015 | Integration modes are strategy objects with stated invariants and property tests | step 6 | Planned ([#715](https://github.com/eduralph/pdca-harness/issues/715)) |
| [0016](0016-strangler-migration.md) | The migration is a strangler, never a rewrite | 2026-10-15 | Accepted |
| [0017](0017-issue-priority-and-automated-triage.md) | Issue priority and automated triage: severity × reach set P0–P3 with security and decision-integrity overrides; a GitHub Action applies it to this repository's issues with one Claude API call per issue | — | Proposed |
