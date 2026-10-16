---
created: 15.10.2026
type: adr
status: Accepted
decided: 2026-06-04
tags:
  - adr
---
# 0008. The driver is pure stdlib, Python ≥ 3.11

## Context

The driver is rendered into every instance and runs on maintainers' machines, in CI and in
sandboxes next to whatever toolchain the target project uses. Third-party dependencies would
have to be installed and kept compatible in every one of those environments.

## Decision

The driver uses the Python standard library only: `dependencies = []` and
`requires-python = ">=3.11"` in `template/pyproject.toml.jinja`, from the first commit
(`3ca179a`, 2026-06-04). External tools (`git`, `gh`, the model CLIs, Copier) are invoked as
subprocesses and checked by `pdca doctor`.

## Consequences

An instance needs only Python and the CLIs it already uses. Parsing TOML uses `tomllib`
(hence 3.11). Anything a library would give — schema validation, structured config, typed
records — is written by hand; migration steps 1 and 3 keep to this when they add records and
output schemas. The docs site renderer under `docs/publishing/` is outside the driver and may
use its own pinned packages.
