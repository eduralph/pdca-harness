---
created: 15.10.2026
type: index
tags:
  - spec
---
# Specifications

Normative, versioned contracts (see [`../README.md`](../README.md)). Write one from
[`../templates/spec.md`](../templates/spec.md), at `<domain>/v1.md`, before a second consumer
depends on a format.

## Index

| Spec | Where | Status |
|------|-------|--------|
| The PDCA quality-cycle model (beats, artifacts, automation, gating) | [`template/PCDA/quality-cycle/`](../../../template/PCDA/quality-cycle.md), by reference | Stable; vendored into every rendered project |
| Bundle document format v1 (`brief.md`, `SUMMARY.md`, the JSON records, the gate marker protocol, the decision tokens) | `bundle-format/v1.md` | Planned — migration step 1, [#712](https://github.com/eduralph/pdca-harness/issues/712) |
| Role output schemas | `roles/` | Planned — migration step 3, [#716](https://github.com/eduralph/pdca-harness/issues/716) |

The quality-cycle model stays in `template/PCDA/` so that every rendered project carries the
same text; this index points at it instead of holding a second copy.

Until the bundle-format spec exists, the shape of the bundle files is defined in three places
that can disagree: spec doc 02 (cycle artifacts), the `templates/*.tpl` files, and the parsers
in `assemble.py`, `signoff.py`, `state.py` and `autoiterate.py`. That gap is risk R5 in
[architecture §11](../architecture/10-quality-risks-glossary.md#11-risks-and-technical-debt).
