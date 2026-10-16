---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 1. Introduction and goals

> Living document. What pdca-harness is and why, as of `main` @ `d6057eb` (2026-10-15).
> Open questions are marked **[OPEN]** inline.

## 1.1 What this is

pdca-harness is a [Copier](https://copier.readthedocs.io/) template that renders a
Plan → Do → Check → Act quality cycle into a software project. Each AI-driven contribution
becomes one *bundle*: a written brief (Plan), a patch plus a test that fails before and passes
after (Do), deterministic gates, a reviewer from a different model vendor and a human sign-off
(Check), and a cross-cycle process review (Act). A deterministic Python driver, `pdca`, moves
every bundle through the cycle; models are invoked only at fixed points ("leaves") and never
decide the control flow. The walkthrough in [`docs/00-introduction.md`](../../00-introduction.md)
explains the problem in full.

## 1.2 The headline promise

**No model can accept its own work.** The accept decision is recorded by code, from a human's
decision, and is refused while any NEEDS-HUMAN item is open (ADR-0004). Everything else in the
harness exists to make that decision well-informed and cheap.

## 1.3 Quality goals

When goals conflict, the lower number wins.

| # | Goal | What it means here |
|---|------|--------------------|
| 1 | Integrity of the accept | Only a human decision accepts a bundle; evidence, not prose, blocks it (ADR-0004, ADR-0006). |
| 2 | Independent review | The reviewer's errors are independent of the builder's (ADR-0005). |
| 3 | Resumability | Any crash or interrupt is resumed by running the same command again (ADR-0003). |
| 4 | Maintainer attention | Every item put in front of the human needs a human; ritual items are a defect (risk R2). |
| 5 | Adaptability | One template serves projects with different trackers, branches, gates and toolchains (ADR-0007). |
| 6 | Cost awareness | Work is ranked by what it saves, measured in tokens and time. **[OPEN]** nothing is recorded yet (#711). |

## 1.4 Stakeholders and target users

- **Maintainers** who take AI-generated contributions into a real codebase and sign them off.
- **Instance operators** who render the template, concretize it and run `pdca flow` — today
  the wyrd, pdca-pdca (the harness building itself), autobook and nl-shell instances.
- **Harness contributors**, who work through the pdca-pdca instance.

## 1.5 Scope boundaries

In scope: the cycle, its artifacts, the driver, the leaf prompts, gates protocol, batching
(waves, lanes, integration lines), publishing draft PRs, and the Act review.

Not goals: merging PRs (always human), hosting or scheduling runs, being a model vendor's
agent framework, or replacing a project's CI.
