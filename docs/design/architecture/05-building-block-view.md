---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 5. Building block view

> Living document. The modules of `template/src/pdca_harness/` (28,046 lines at `d6057eb`) and
> what each is responsible for. Open questions are marked **[OPEN]** inline.

## 5.1 Components

Grouped by the layer of the [target architecture](../proposals/accepted/0002-target-architecture.md)
each will move to. Today the three concerns — what the harness *knows*, *decides* and *does* —
are interleaved, mainly in `flow.py`, `leaves.py` and `assemble.py`.

| Target layer | Modules today (lines) | Responsibility |
|---|---|---|
| Records | `state.py` (377), `brief.py` (371), `signoff.py` (387), `assemble.py` (1,081), `rubric.py` (282), `record.py` (225) | Read state from files; parse briefs and §6/§9; assemble `SUMMARY.md`; commit finished bundles. |
| Machine | `driver.py` (494), `flow.py` (2,834) | Advance a bundle one beat (`driver`); run whole batches (`flow`). Two drivers exist for one state machine (`flow.flow` and `flow_ids`). |
| Effects | `leaves.py` (4,706), `gates.py` (1,029), `progress.py` (1,103), `worktree.py` (640), `publish.py` (1,193), `sources.py` (324), `families.py` (172), `drive_claim.py` (297), `sweep.py` (307) | Invoke model CLIs with heartbeat and memory caps; run gates; manage worktrees; open draft PRs; fetch tracker sources; per-bundle claims. |
| Roles | prompts and stubs inside `leaves.py`; `agents/*.md` in the template | What each leaf is told and what it may see. |
| Policies | `sizing.py` (565), `size_signal.py` (522), `autoiterate.py` (472), `plan_policy.py` (297), `dependency_halt.py` (270), `split.py` (1,340) | Size estimate and signal, auto-iterate eligibility, Plan-exit checks, external-dependency adjudication, splitting. |
| Orchestration | `waves.py` (350), `integrate.py` (990), `merge.py` (638), `merged.py` (211), `revert.py` (176), `lane.py`, `queue.py`, `preflight.py` | Wave levelling, the stack-mode integration line and carry, merge mode, revert detection, lanes. |
| Act and housekeeping | `act.py` (873), `triage.py` (613), `cleanup.py` (542), `handoff.py` (695), `revalidate.py`, `drift.py` | Act index and log; PR-review triage into the ledger; tracker cleanup; interactive-session hand-off and exit contracts. |
| Entry and config | `cli.py` (1,508), `config.py` (956), `doctor.py` (563), `guard.py`, `registry.py` | The `pdca` command; `pdca.toml`; environment checks; the STOP-discipline guard. |

**[OPEN]** The module graph is cyclic (about twenty `from . import …  # avoid an import cycle`
sites). Steps 2–4 of the migration remove the cycles by giving each layer one direction of
call.

## 5.2 Build-time components

| Component | Responsibility |
|---|---|
| `copier.yml`, `template/**/*.jinja` | The template and its questions. |
| `template/PCDA/quality-cycle/` | The model specification, vendored into every instance. |
| `template/tests/` (about 2,460 tests) | Offline driver suite; runs every leaf as a stub. |
| `tests/` (root) | Renders the template with Copier and runs `copier update` from the previous release. |
| `docs/publishing/` | Builds the pdca.dev site from `docs/*.md` and the spec. |
