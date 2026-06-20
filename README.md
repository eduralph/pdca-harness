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

## Learn by example

New here? **[`docs/`](docs/README.md)** is a step-by-step walkthrough that drives
one real contribution through every beat of the cycle, using a live project
([Gramps Testbed v2](docs/README.md)) as the worked example. Start with the
**[introduction](docs/00-introduction.md)** for the why-and-what, then follow
steps 01–07 from rendering the template to publishing a fix and the cross-cycle
Act review.

## What you get when you render it

```
<your project>/
  pdca.toml                 # driver config: bundle paths, the two leaf commands
  PCDA/
    quality-cycle.md        # the generic model (Plan/Do/Check/Act, 5/5/1) — reference
    quality-cycle/          # the full vendored spec (00–07), an Obsidian vault
  docs/
    INTEGRATION.md          # YOUR repo's concretizations — fill the TODOs
  src/pdca_harness/         # the deterministic driver (state machine over bundles)
  templates/                # brief / SUMMARY / tracker-comment / pr-description tpls
  examples/toy/             # a worked brief the driver can advance offline
  results/                  # per-cycle bundles land here
  process/act-log.md        # cross-cycle process deltas (Act)
```

## Use

### 1. Render the harness into a new project

```bash
copier copy gh:<you>/pdca-harness ../my-new-project   # answer the prompts
cd ../my-new-project
```

Re-apply template updates later, from inside the rendered project:

```bash
copier update
```

### 2. Drive a contribution cycle

`pdca` is the entry point once the package is installed; otherwise prefix any
command with `PYTHONPATH=src python -m pdca_harness.cli …`.

```bash
pdca init-issue TOY --from-brief examples/toy/brief.md   # seed a bundle from a brief
pdca run TOY              # Do → gates → reviewer → assembled SUMMARY → AWAITING_SIGNOFF
pdca status              # all bundle states (cheap-first)
pdca batch A B C         # fan the driver over many issues
pdca queue               # the cheap-first sign-off burn-down
pdca gates TOY           # the deterministic gates (CI runs `pdca gates --working-tree`)
pdca signoff TOY --accept --by you   # refused while §6 NEEDS-HUMAN is open (C6)
```

The vertical slice runs **offline** out of the box (stub Do/gates/reviewer
leaves), so `init-issue` → `run` → `signoff` works before you wire anything real.

### 3. Make it yours

1. Read `PCDA/quality-cycle.md` (the model) and fill `docs/INTEGRATION.md` —
   tracker, branch targets, fixtures, and the conformance ruleset.
2. Fill real gate tiers in `pdca.toml` `[[gates.checks]]` (the long pole).
3. Wire the real model leaves: set `leaves_mode = "command"` in `pdca.toml`.

## What's built

- **Driver** — the deterministic state machine over result bundles (doc 03),
  end-to-end with **stubbed** Do/gates/reviewer leaves so the loop runs offline.
- **Single-sourced gates** — defined once in `pdca.toml` `[[gates.checks]]`, run by
  both the driver and CI via one `pdca gates` command (stub fallback until filled).
- **Batch fan-out + sign-off queue** — `pdca batch` over N issues, `pdca queue`
  cheap-first burn-down.
- **In-driver lane concurrency** — `[driver].lanes = N` (`PDCA_LANES` /
  `pdca flow|batch --lanes N`) fans the unattended Do + Check band across N workers in
  one workspace; each gate sees its worker slot as `$PDCA_LANE` to keep checkouts /
  runners lane-private (doc 09).
- **Mechanical STOP discipline** — `.claude/agents/builder.md` + a PreToolUse hook
  block the builder from marking a PR ready/merging; `reviewer.md` has execute-only
  scope; the decorrelated reviewer path is cross-vendor Codex via `AGENTS.md`.
- **Act tooling (L4)** — `pdca act-index` is a read-only index of frozen cycles
  that surfaces §6/§7/§10 and recurring signals; `pdca act-log` scaffolds a dated
  act-log entry with the considered bundles + patterns pre-filled (the deltas stay
  the human's). All four L-rungs of the maturity ladder are now present.
- **Full spec** vendored under `template/PCDA/quality-cycle/` (reference docs,
  kept separate from a rendered project's own `docs/`).

## Still ahead

Real gate-tier implementations for your project (the long pole) and wiring the
real model leaves (swap `leaves_mode = "command"` in `pdca.toml`). The
scaffolding for every rung exists; what remains is project-specific gate code and
the model-command wiring.

See `template/PCDA/quality-cycle.md` for the model and the maturity ladder.

## License

Copyright © 2026 Eduard Ralph.

pdca-harness is licensed under the **Apache License, Version 2.0**
([LICENSE](LICENSE), [NOTICE](NOTICE)) — a permissive license. The driver code and
templates this project copies into a rendered repo are Apache-2.0, so the rendered
output carries no copyleft and is compatible with a permissive license policy (e.g. a
`cargo-deny`-style allowlist); a host repo keeps its own license. A rendered project
declares its own license via the `project_license` copier question (default
`Apache-2.0`).

Contributions are accepted under Apache-2.0 and gated on the
[Developer Certificate of Origin](DCO) — sign off commits with `git commit -s`. See
[CONTRIBUTING.md](CONTRIBUTING.md).
