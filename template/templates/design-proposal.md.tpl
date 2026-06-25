# Design proposal — issue <id> / <slug>

> The Plan artifact for the **exception**: a change significant enough to warrant a
> GEPS-style design proposal (major architecture / public API / data model / UX, or
> anything needing design buy-in before implementation). **Most work — bug fixes and
> ordinary new functionality — uses `brief.md.tpl` instead; not every feature is a
> design proposal.** Authored interactively at Plan (the planner leaf) with the human.
> Do reads ONLY this file and implements it; Check runs the regular gated check on the code.
>
> Keep the `- **Label:** value` lines — they are parsed by the driver (Do reads the
> spec from them; the driver/SUMMARY read slug/criterion/branch). The prose `##`
> sections are the design rationale: the reviewer and the human read them at
> sign-off, and they are what you carry upstream into any design-proposal process.

- **Slug:** <short-kebab-slug>
- **Kind:** enhancement (design proposal)
- **Goal:** <the capability this adds — the observable new behaviour>
- **Success criterion:** <the observable condition that means it works — what the shipped test asserts>
- **Repo + branch target:** <repo @ branch — resolved here per INTEGRATION §2, not left to Do>
- **Scope:** <the one feature, in one logical change> / out of scope: <what is explicitly excluded>
- **Difficulty:** <`low` | `medium` | `high` — the change's **blast-radius / cross-file
  reach** (files/call-sites touched and how far effects propagate, what a diff-reviewer
  must hold in view), NOT edge-case density. Routes the Do backend and review depth
  (issues #133/#134). Optional; absent/unknown is the safe default — nothing is skipped.>
- **Test file:** <path where the feature's test ships — red before, green after>
- **Citations expected:** Do must cite path:line on the target branch for every change.
- **Disposition hint:** new-feature

## Motivation
<the user need / problem this solves; why it's worth doing now>

## Design
<the approach: data model, UI, public APIs touched, key decisions. Enough that Do
can implement without re-deciding the design.>

## Alternatives considered
<other approaches weighed, and why this one wins>

## Impact & compatibility
<what existing behaviour changes; migration, settings, i18n, deprecations, risk>

## Open questions
<anything to settle with the maintainer before or during implementation>

## STOP discipline

Draft only until Check sign-off. Pushing to a feature/draft branch and opening a
draft PR MAY happen during the cycle (useful for CI feedback). The PR MUST NOT be
marked ready before sign-off accepts.
