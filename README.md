# pdca-harness

A [Copier](https://copier.readthedocs.io/) template for spinning up an
**AI-driven PDCA quality-cycle harness** in a new project.

One contribution turns one PDCA cycle: **Plan** (author the spec) → **Do**
(implement) → **Check** (verify the built artifact against the spec —
correctness, conformance, *and* validation) → **Act** (improve the process so
the issues this cycle exposed don't recur) → back to Plan with a better
baseline. The cycle keeps a human at three irreducible touch points
(Plan-authoring, Check sign-off, Act) and automates everything around them.

This repo packages the project-agnostic parts of that cycle — the bundle
state machine (the driver), the artifact templates, the generic model docs,
and the 11-item integration scaffold each repo fills in — so a new project
starts from a prepared harness instead of tribal knowledge.

## What you get when you render it

```
<your project>/
  pdca.toml                 # driver config: bundle paths, the two leaf commands
  docs/
    quality-cycle.md        # the generic model (Plan/Do/Check/Act, 5/5/1)
    INTEGRATION.md          # YOUR repo's concretizations — fill the TODOs
  src/pdca_harness/         # the deterministic driver (state machine over bundles)
  templates/                # brief / SUMMARY / tracker-comment / pr-description tpls
  examples/toy/             # a worked brief the driver can advance offline
  results/                  # per-cycle bundles land here
  process/act-log.md        # cross-cycle process deltas (Act)
```

## Use

```bash
copier copy gh:<you>/pdca-harness ../my-new-project
cd ../my-new-project
pdca init-issue TOY --from-brief examples/toy/brief.md   # or: PYTHONPATH=src python -m pdca_harness.cli ...
pdca run TOY              # advances Do → gates → reviewer → assembled SUMMARY → AWAITING_SIGNOFF
pdca status              # all bundle states (cheap-first)
pdca batch A B C         # fan the driver over many issues
pdca queue               # the cheap-first sign-off burn-down
pdca gates TOY           # the deterministic gates (CI runs `pdca gates --working-tree`)
pdca signoff TOY --accept --by you   # refused while §6 NEEDS-HUMAN is open (C6)
```

## What's built

- **Driver** — the deterministic state machine over result bundles (doc 03),
  end-to-end with **stubbed** Do/gates/reviewer leaves so the loop runs offline.
- **Single-sourced gates** — defined once in `pdca.toml` `[[gates.checks]]`, run by
  both the driver and CI via one `pdca gates` command (stub fallback until filled).
- **Batch fan-out + sign-off queue** — `pdca batch` over N issues, `pdca queue`
  cheap-first burn-down.
- **Mechanical STOP discipline** — `.claude/agents/builder.md` + a PreToolUse hook
  block the builder from marking a PR ready/merging; `reviewer.md` has execute-only
  scope; the decorrelated reviewer path is cross-vendor Codex via `AGENTS.md`.
- **Full spec** vendored under `template/docs/quality-cycle/`.

## Still ahead

Real gate-tier implementations for your project (the long pole), the real model
leaves (swap `leaves_mode = "command"` in `pdca.toml`), and **Act tooling** (L4:
a bundle index across frozen cycles + an act-log writer). Build order the model
prescribes: **gates → driver → batch queue → Act tooling**.

See `template/docs/quality-cycle.md` for the model and the maturity ladder.
