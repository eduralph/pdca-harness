---
created: 15.10.2026
type: index
tags:
  - index
---
# pdca-harness — design documentation

The design documentation is organized into four classes, each with its own purpose and
change process. The split is the one pdca-harness gives every rendered project through
[pdca-project-template](https://github.com/eduralph/pdca-project-template): some documents
describe the system and change with it, some record decisions and never change, and some are
contracts that other code must honour.

The walkthrough in [`docs/`](../README.md) stays the applied guide — how to render, integrate
and run the harness. This folder records *why* the driver is shaped the way it is, so the same
debates are not re-derived in issues.

## The four document classes

| Class | Location | Nature | Change process |
|-------|----------|--------|----------------|
| 1. Specifications | [`specs/`](specs/README.md) | Normative, versioned | Strict; a stable version is never edited, only replaced by a new version |
| 2. Architecture overview | [`architecture/`](architecture/01-introduction-and-goals.md) | Descriptive, living | Edited continuously; always describes the current system |
| 3. Decision records (ADRs) | [`adr/`](adr/README.md) | Immutable history | Append-only; superseded, never edited |
| 4. Enhancement proposals | [`proposals/`](proposals/README.md) | The change process | Draft → accepted → implemented; accepted records are immutable |

### 1. Specifications (`specs/`)

Contracts that more than one piece of code depends on, written in RFC 2119 language
(MUST / SHOULD / MAY). Write one before a second consumer depends on the format. The
quality-cycle model in [`template/PCDA/quality-cycle/`](../../template/PCDA/quality-cycle.md)
is the harness's first specification; it is referenced from `specs/`, not copied, so it keeps a
single source.

### 2. Architecture overview (`architecture/`)

A trimmed [arc42](https://arc42.org/) structure (a standard set of chapters for describing a
software architecture). It describes the system as it currently is and must not lag the code.
Where the [target architecture](proposals/accepted/0002-target-architecture.md) differs from
today, a chapter says so and names the migration step that closes the gap.

### 3. Architecture Decision Records (`adr/`)

Short, numbered records of *why* a decision was made. An ADR is never edited after
acceptance; a later ADR supersedes it. Format: the lightweight
[Nygard-style](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
template. See [ADR-0001](adr/0001-record-architecture-decisions.md).

### 4. Enhancement proposals (`proposals/`)

How larger changes and milestone plans are proposed (modeled on Kubernetes KEPs and Rust
RFCs). A proposal moves from `draft/` to `accepted/` and carries motivation, design,
alternatives and graduation criteria.

The fill-in templates for all four classes are in [`templates/`](templates/README.md).

## Process rules

These rules come from the [approach review](proposals/accepted/0001-approach-review-2026-10.md)
and keep the structure current:

- **A migration step opens with its ADR.** Each step of the
  [migration order](architecture/09-build-order-and-roadmap.md) carries its ADR in its first
  PR; the step is not open until that ADR is Accepted.
- **Structural PRs update the architecture.** A PR that changes a layer boundary, a record
  format or a role contract updates `architecture/` in the same PR.
- **Act feeds the decision record.** An Act delta that changes architecture (not just a gate,
  a brief field or a prompt line) is filed as a proposal; its acceptance produces an ADR.
- **Instances reference, never copy.** An instance keeps its own `docs/design/` for its own
  decisions and refers to harness ADRs by number.

## Conventions

- **Diagrams as code.** Diagrams are Mermaid source in the Markdown, never binary exports.
- **Docs live with code.** Documentation is in the same repository as the code it describes
  and is reviewed in the same pull requests.
- **ADR-first habit.** When a design question is settled, the output is an ADR, not an issue
  comment.
- **Cite the code.** A claim about the driver names `path:line` on `main` at a stated commit,
  the same rule briefs and reviews follow.

## Reading order for newcomers

1. [`architecture/01-introduction-and-goals.md`](architecture/01-introduction-and-goals.md) — what the harness is and why.
2. [`architecture/08-crosscutting-concepts.md`](architecture/08-crosscutting-concepts.md) — the core concepts.
3. [`adr/`](adr/README.md) — the founding decisions.
4. [`architecture/09-build-order-and-roadmap.md`](architecture/09-build-order-and-roadmap.md) — the migration order and where to start.
