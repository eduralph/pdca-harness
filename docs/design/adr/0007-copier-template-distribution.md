---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-04
tags:
  - adr
---
# 0007. Distribution is a Copier template; instances concretize and update

## Context

The harness must run inside other repositories, each with its own tracker, branches, gates and
toolchain. A library dependency cannot ship agent prompts, hooks, CI files and docs, and a
one-off copy cannot receive fixes.

## Decision

pdca-harness is a [Copier](https://copier.readthedocs.io/) template (`copier.yml`, first
commit `3ca179a`, 2026-06-04). A project renders it once; repo-specific facts go into
`docs/INTEGRATION.md` and `pdca.toml`; later releases arrive with `copier update`. The driver
source is rendered into each instance under `src/`.

## Consequences

An instance can concretize everything the template parameterizes and still receive upstream
fixes. Anything an instance changes inside rendered files — especially `src/` — is overwritten
or conflicts on the next `copier update`; the wyrd instance carries about 2,300 lines of such
local patches. Migration step 5 (ADR-0013, #714) adds a `pdca_ext/` boundary that
`copier update` never crosses, so instance behaviour has a supported home.
