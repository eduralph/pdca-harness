# PCDA — the quality-cycle model

This is the vendored, **project-agnostic** model the harness automates: Plan · Do ·
Check · Act over every contribution, with the 5/5/1 inside Check. It is **plain
Markdown** with relative links — read it in any Markdown viewer, on GitHub, or
(optionally) as an Obsidian vault. No Obsidian dependency: the content uses no
wikilinks or vault-only syntax (a CI lint guard enforces that), and the `.obsidian/`
vault config — if you create one — is local-only and never committed or rendered.

Your repo's concretizations live next door in [docs/INTEGRATION.md](../docs/INTEGRATION.md).

## Start here

- **[quality-cycle.md](quality-cycle.md)** — the model in one read (the digest).

## Full spec (`quality-cycle/`)

| # | Doc | What it covers |
|---|-----|----------------|
| 00 | [overview](quality-cycle/00-overview.md) | Purpose and the three-layer learning path |
| 01 | [the quality cycle](quality-cycle/01-the-quality-cycle.md) | The PDCA model, four beats, the 5/5/1, role partition |
| 02 | [cycle artifacts](quality-cycle/02-cycle-artifacts.md) | The artifact flow per beat; the per-bundle structure |
| 03 | [cycle automation](quality-cycle/03-cycle-automation.md) | The driver as a state machine; the L1–L4 maturity ladder |
| 04 | [validation tooling](quality-cycle/04-validation-tooling.md) | Check implementation: gate tiers, reviewer decorrelation |
| 05 | [repository integration](quality-cycle/05-repository-integration.md) | What each repo provides: tracker, branches, fixtures, ruleset |
| 06 | [quality-cycle guidelines](quality-cycle/06-quality-cycle-guidelines.md) | Per-beat MUST/SHOULD rules (P-/D-/C-/A-numbered) |
| 07 | [case study: CI hardening](quality-cycle/07-case-study-ci-hardening.md) | A worked example (Plan v1→v2→v3) |
| 08 | [glossary](quality-cycle/08-glossary.md) | Terms index with doc ownership |
| 09 | [parallel lanes](quality-cycle/09-parallel-lanes.md) | Running cycles concurrently; lane isolation |
| 10 | [adapting](quality-cycle/10-adapting.md) | The render-to-running playbook |
