---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-20
tags:
  - adr
---
# 0002. Apache-2.0 license and DCO sign-off

## Context

The harness started under MIT (commit `3ca179a`, 2026-06-04) and was relicensed to GPLv3 the
same day (`c81cece`). It is a Copier template: its files are rendered into other people's
repositories, which may be proprietary. A copyleft license on files that end up inside a
user's project is a barrier to using the template at all. Contributions also needed a clear
statement of origin without a contributor license agreement.

## Decision

We license pdca-harness under Apache-2.0 and gate contributions on the Developer Certificate
of Origin 1.1 (`git commit -s`), with a `NOTICE` file (commit `d6f4786`, #67, 2026-06-20).
Rendered projects receive the same `LICENSE`, `NOTICE` and `DCO`.

## Consequences

The template can be rendered into any project. Every commit MUST carry a `Signed-off-by`
trailer. Apache-2.0 includes a patent
grant, which MIT did not. Relicensing again would need every contributor's agreement.
