---
created: <DD.MM.YYYY HH:MM>
type: proposal
# status: draft | accepted | implemented | withdrawn.
# Keep comments off the status line: the immutability check reads the whole value.
status: draft
author:
tracking-issue:
tags:
  - proposal
---
# Proposal: <title>

> Enhancement proposals (KEP/RFC-style) are how larger changes and milestone plans are
> proposed. Move this file from `draft/` to `accepted/` when accepted; an accepted proposal
> is never edited — a later proposal supersedes it, recorded in `../proposals/README.md`.
> If a decision is foundational rather than a change, it may instead warrant an ADR.

## Motivation

What problem does this solve? Why now? What does the user see?

## Design

The proposed change. Be concrete about the affected components and about any effect on the
properties the threat model says must hold — those carry the highest bar.

## Alternatives considered

What else was weighed, and why this over those?

## Graduation criteria

How do we know it is done and correct? Which tests, which quality scenarios (architecture
§10), and what definition of done?

## Security impact

Which untrusted inputs from `.oss-scanner/threat_model.md` does this touch? Does it add a
new one, or change what is trusted? Update the threat model in the same change if so.

## Backward compatibility

Effect on existing data, configs, deployments and the public interface.

## Open questions

Mark unresolved points explicitly.
