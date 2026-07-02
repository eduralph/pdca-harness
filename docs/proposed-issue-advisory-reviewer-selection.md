# Driver suggests a different-vendor advisory reviewer for the build model that ran

## Context

Check's independence rests on **cross-vendor decorrelation** (INTEGRATION §4): the
reviewer should be a *different* family from the builder that produced the patch. Today
that pairing is fixed at config-render time — `[leaves.reviewer].family` and each
`[[leaves.advisory]].family` are static strings — while the builder family is *not* fixed
per bundle. `select_builder()` already resolves the Do backend three ways (explicit
`- **Do model:** <name>` pin #167, difficulty `when` routing #134, and the escalation
ladder #135), so the same run can build one bundle with Codex and the next with Claude.
When that happens, a statically-configured reviewer can land **same-vendor as the
builder**, silently weakening the independence Check relies on.

## What I want

The decorrelation should be the **driver's** job, not the human's. Once the build model is
resolved for a bundle, the driver looks at which vendor built it and **suggests an advisory
reviewer from a different vendor** — automatically, per bundle. Codex built it → suggest a
Claude reviewer; Claude built it → suggest a Codex reviewer. No per-brief edits, no
hand-mirrored config for each direction.

## Current behaviour

- The reviewer/advisory family is chosen at config-render time and never reacts to the
  builder that actually ran.
- `[[leaves.advisory]].when = {field, substring}` conditions an advisory on **brief fields
  only**, so it can't react to the *resolved* builder vendor (which may come from routing
  or escalation, not from any brief field).
- Approximating this today means hand-maintaining two mirrored advisory entries keyed on a
  `Do model` string — brittle, and it breaks the moment #134/#135 picks the backend instead
  of the brief.

## Proposed behaviour

Give the driver a **vendor-complement selection** step for the advisory reviewer:

1. Configure a small **pool** of advisory reviewer backends, one per vendor/family
   (e.g. one Claude entry, one Codex entry) sharing the same review `role`.
2. After `select_builder()` resolves the build model, the driver reads its `family`
   (the resolved fact, recorded on the bundle — see below) and **selects/suggests** a
   pool entry whose `family` differs from the builder's.
3. If more than one different-vendor entry qualifies, pick deterministically (config
   order); if none is configured, fall back rather than skip — a same-vendor review still
   beats no review — and note the fallback in SUMMARY §6.

Sketch (illustrative — exact schema TBD):

```toml
# A vendor-keyed advisory pool the driver draws a *complement* from.
[[leaves.advisory]]
id = "review-claude"
family = "claude"
role = "correctness bugs + reuse/simplification/efficiency cleanups"
argv = ["claude", "-p", "--agent", "code-review", "--permission-mode", "acceptEdits", "--allowedTools", "Read,Bash,Grep,Glob"]

[[leaves.advisory]]
id = "review-codex"
family = "codex"
role = "correctness bugs + reuse/simplification/efficiency cleanups"
argv = ["codex", "exec", "--config", "AGENTS.md"]

# Opt into driver-driven vendor complement (vs. today's static / when-gated behaviour):
[leaves.advisory_selection]
mode = "vendor-complement"   # driver picks a pool entry whose family != resolved builder
```

The key move is that **the driver derives the choice from the resolved builder vendor**,
so the human declares vendors *once* and both directions ("codex→claude" and
"claude→codex") fall out automatically — including when the builder was chosen by
difficulty routing or escalation, not by `Do model`.

## Why "suggests"

This should default to a suggestion the driver applies, but stay observable/overridable —
surface the chosen pairing (and any same-vendor fallback) in SUMMARY §6 so a human can see
whether decorrelation actually held for a given bundle. Decorrelation is a soft preference,
not a hard invariant to enforce by skipping review.

## Design notes / open questions

- **Source of the resolved builder vendor:** `select_builder()` resolves it during Do; the
  advisory runs during Check. `loop-telemetry.json` already records which backend ran each
  pass — reuse that as the source of truth so Check reads a recorded fact, not a
  re-derivation.
- **Selection policy when the pool has >1 complement:** config order? explicit priority?
  round-robin for extra decorrelation across bundles?
- **Interaction with existing `when`:** vendor-complement selection and brief-field `when`
  gating should compose (a pool entry can still be `when`-gated off for a bundle).
- **Relationship to the primary `[leaves.reviewer]`:** does the same complement logic apply
  to the primary reviewer, or only to the advisory pool? (Recommend advisory first; the
  primary reviewer's family is a bigger blast radius.)

## Acceptance criteria

- With a two-vendor advisory pool and `mode = "vendor-complement"`, a Codex-built bundle
  runs the Claude advisory and a Claude-built bundle runs the Codex advisory — no per-brief
  edits — including when the builder came from #134 routing or #135 escalation.
- The resolved builder family is recorded on the bundle and is the source the driver reads.
- When no different-vendor entry is configured, the driver falls back (does not skip) and
  records a same-vendor note in SUMMARY §6.
- Advisory stays strictly advisory — never gates; `- NEEDS-HUMAN —` findings still route to
  §6.
- Docs updated: `template/pdca.toml.jinja` advisory block + docs/05-check.md, stating the
  decorrelation intent (INTEGRATION §4) where the config lives.

## Test plan

- Unit: given a recorded builder family of `codex`, the driver selects the `claude` pool
  entry; given `claude`, it selects `codex`; given a single-vendor pool, it falls back and
  emits the §6 note.
- Unit: builder resolved via `Do model`, via difficulty routing, and via escalation each
  yield the correct complementary advisory.
- Offline suite stays green:
  `cd template && PYTHONPATH=src python3 -m unittest discover -s tests`.

## Related

- #64 (optional advisory reviewers — the open list this extends)
- #167 (`Do model` explicit builder pin), #134 (difficulty-routed variants),
  #135 (escalation ladder) — the reasons the builder vendor isn't knowable from the brief
- INTEGRATION §4 (cross-vendor decorrelation is the default and the ideal)
