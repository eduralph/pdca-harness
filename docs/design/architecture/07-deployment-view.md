---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 7. Deployment view

> Living document. Where the harness runs. Open questions are marked **[OPEN]** inline.

## 7.1 What the user gets

`copier copy gh:eduralph/pdca-harness <dir>` renders an instance: the driver under `src/`,
`pdca.toml`, `docs/INTEGRATION.md`, agent prompts and hooks under `.claude/` and `agents/`,
the vendored model spec under `PCDA/`, gate script scaffolds under `engine/`, and the offline
tests. `make install` installs the `<instance>` CLI; `copier update` brings later releases.

Known instances: getwyrd/wyrd-pdca, eduralph/pdca-pdca (develops the harness itself),
Ralphovi/autobook-pdca and eduralph/nl-shell-pdca. Instances are not pinned to a template
version (ADR-0016).

**[OPEN]** Instance behaviour that the template does not parameterize has no supported home;
wyrd carries local patches to `src/` that `copier update` overwrites. Step 5 adds `pdca_ext/`
(#714).

## 7.2 Test matrix

| Suite | Command | Where it runs |
|---|---|---|
| Offline driver suite | `cd template && PYTHONPATH=src python3 -m unittest discover -s tests` | Locally and in every instance's CI |
| Root template suite (render + `copier update`) | `python3 -m tests.run_root_suite` | `.github/workflows/render-check.yml` |
| Docs lint and site link check | `lint_docs.py`, `render_site.py --check` | `.github/workflows/docs-check.yml` |
| PR has a linked issue | — | `.github/workflows/require-linked-issue.yml` |

## 7.3 Development environment

Harness changes are made through the pdca-pdca instance: `pdca-pdca flow <ids>` plans, builds,
checks and publishes draft PRs against eduralph/pdca-harness. Python 3.11+, `git`, `gh`, and
the model CLIs configured in its `pdca.toml`.
