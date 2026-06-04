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
PYTHONPATH=src python -m pdca_harness.cli init-issue TOY --from-brief examples/toy/brief.md
PYTHONPATH=src python -m pdca_harness.cli run TOY        # advances to AWAITING_SIGNOFF
PYTHONPATH=src python -m pdca_harness.cli status         # the sign-off queue
```

## Maturity

This is the **vertical-slice** increment: the driver runs end-to-end with
**stubbed** Do/gates/reviewer leaves, proving the control flow (init → brief →
Do → gates → reviewer → assembled `SUMMARY.md` → human sign-off → complete).
The deterministic gates and the real model leaves are the next layers, in the
build order the model prescribes: **gates → driver → batch queue → Act tooling**.

See `template/docs/quality-cycle.md` for the model and the maturity ladder.
