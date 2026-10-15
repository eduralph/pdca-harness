---
created: 15.10.2026
type: index
tags:
  - proposal
---
# Enhancement proposals

KEP/RFC-style proposals for larger changes and milestone plans. Start one by copying
[`../templates/proposal.md`](../templates/proposal.md) into `draft/`. Accepted proposals move
to `accepted/` and are never edited; a later supersession is recorded **here**, not on the
frozen file.

An Act delta that changes the architecture (not just a gate, a brief field or a prompt line)
is filed here as a proposal; its acceptance produces an ADR.

## Index

| # | Title | Status | Tracking |
|---|-------|--------|----------|
| [0001](accepted/0001-approach-review-2026-10.md) | pdca-harness — approach review (Oct 2026) | accepted 2026-10-15 | [#709](https://github.com/eduralph/pdca-harness/issues/709) |
| [0002](accepted/0002-target-architecture.md) | Target architecture and migration order | accepted 2026-10-15 | [#709](https://github.com/eduralph/pdca-harness/issues/709); steps [#711](https://github.com/eduralph/pdca-harness/issues/711), [#712](https://github.com/eduralph/pdca-harness/issues/712), [#716](https://github.com/eduralph/pdca-harness/issues/716), [#713](https://github.com/eduralph/pdca-harness/issues/713), [#714](https://github.com/eduralph/pdca-harness/issues/714), [#715](https://github.com/eduralph/pdca-harness/issues/715) |

## Decisions taken at acceptance

Recorded here because accepted proposals are not edited:

- **Cost is measured in tokens, not dollars.** Wherever 0001 and 0002 say "cost", "dollars",
  `total_cost_usd` or `max_budget_usd`, the accepted reading is token counts (input, output,
  cache read, cache write) and a `max_tokens` budget. See #711.
- **Instances are not pinned.** 0002's "instances stay pinned at v0.59 until the step-1
  renderer passes golden tests" is not adopted; instances keep following `main` (ADR-0016).
- **The §6 parser bugs** #606, #608, #634 and #644 moved to 0.62 to land with step 1; #642
  stays in 0.61.

## Candidates

Documents written elsewhere that belong here once someone moves them: wyrd-pdca's
oversized-slices report (2026-07-31), its backlog-reversal proposal (2026-09-11), and the
restructure outline in #505.
