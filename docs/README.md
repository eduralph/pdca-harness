# Worked example — driving the PDCA cycle with the harness

This folder is a **tutorial**. It walks you through using the `pdca-harness`
template end-to-end, using a real project — [**Wyrd PDCA**](https://github.com/getwyrd/wyrd-pdca),
the quality-cycle instance that contributes to [**Wyrd**](https://github.com/getwyrd/wyrd),
a distributed file storage system written in Rust — as the worked example at
every step.

The harness itself (the driver, the templates, the generic model docs) lives in
[`../template/`](../template/). The canonical *reference* for the model is the
vendored spec in
[`../template/PCDA/quality-cycle/`](../template/PCDA/quality-cycle/) (files
`00`–`10`). **This folder is the opposite of reference material**: instead of
"here is what each artifact means", it shows "here is what a maintainer actually
typed, what the harness produced, and the real decisions they made" on a live
codebase.

> Everything quoted here — briefs, gate tables, sign-off lines, the Act log — is
> copied verbatim from the wyrd-pdca instance (rendered from this template at
> `v0.59.0`). Paths look like `results/issue_779/SUMMARY.md`; those are real,
> committed files in that repo, so you can open any of them and read the whole
> artifact rather than the excerpt.

## What the harness is, in one paragraph

One contribution turns one **PDCA cycle**: **Plan** (author the spec) → **Do**
(implement) → **Check** (verify the built artifact against the spec —
correctness, conformance, *and* validation) → **Act** (improve the process so the
issues this cycle exposed don't recur). A deterministic **driver** advances a
*bundle* (a folder under `results/issue_<id>/`) through a file-derived state
machine and **stops only at the three points where a human is irreducible**:
authoring the Plan, signing off the Check, and the cross-cycle Act review.
Everything between those points is automated.

```mermaid
stateDiagram-v2
    [*] --> UNPLANNED
    UNPLANNED --> PLANNED: Plan
    PLANNED --> BUILT: Do
    BUILT --> CHECKED: Check
    CHECKED --> AWAITING_SIGNOFF: assemble — driver STOPS
    UNPLANNED --> RESOLVED: tracker settles it first

    AWAITING_SIGNOFF --> COMPLETE: accept
    AWAITING_SIGNOFF --> PLANNED: iterate-do (rebuild)
    AWAITING_SIGNOFF --> UNPLANNED: iterate-plan (re-spec)
    AWAITING_SIGNOFF --> DISCONTINUED: discontinue

    COMPLETE --> [*]
    DISCONTINUED --> [*]
    RESOLVED --> [*]
```

The five **halted** states — `UNPLANNED`, `AWAITING_SIGNOFF`, `COMPLETE`,
`DISCONTINUED`, `RESOLVED` — are where the driver hands control back. Everything
else it advances through unattended.

## The steps

Read these in order. Each maps to one beat of the cycle (or one setup step) and
shows the real wyrd-pdca artifact it produces. One contribution runs through
steps 03–05: Wyrd issue 779, which puts a `Server: wyrd/<version>` header on
every response the S3 gateway sends.

| # | Step | Beat | What you do / what the harness produces |
|---|------|------|------------------------------------------|
| [00](00-introduction.md) | Introduction | — | Why the harness exists — the problem, the benefits, the features, the vocabulary |
| [01](01-render-and-integrate.md) | Render & integrate | — | `copier copy` the template; fill `docs/INTEGRATION.md`; wire gates and leaves in `pdca.toml` (real: Wyrd delegates every gate to its own `cargo xtask`) |
| [02](02-rehearse-offline.md) | Rehearse offline | — | Drive the bundled `TOY` issue with stub leaves + stub gates — no model, no live gates, instant |
| [03](03-plan.md) | Plan | **P** | Author `brief.md` — the contribution spec (real: issue 779's brief, including the plan reviewer's findings it answered) |
| [04](04-do.md) | Do | **D** | Builder writes `patch.diff` + the test + `build-notes.md` (real: the stamp at the one point every response passes through) |
| [05](05-check.md) | Check (gates, reviewer, sign-off, publish) | **C** | Gates, reviewer and two advisory lenses run; you record §9 (`--accept` / `--iterate-do` / `--iterate-plan` / `--discontinue`; real: 779's auto-iterate round and accept, and a human reject from issue 576); accept publishes a draft PR, stacked on its prerequisite |
| [06](06-act.md) | Act | **A** | The cross-cycle review that turns recurring misses into spec/gate/rule deltas (real: the reviews of 2026-09-20 and 2026-10-14) |
| [07](07-crosscutting.md) | Cross-cutting | — | Mechanisms that span beats, not owned by one: size & `pdca split` (real: issue 736 split into 778 and 779), iteration/carry-forward + auto-iterate, parallel lanes, waves, sweep & cleanup |
| [08](08-command-reference.md) | Command reference | — | Getting set up (`copier`, `make install`, `make setup`, `pdca doctor`), then every `pdca` subcommand with its options, exit codes and environment variables |

## The whole cycle in one command

Once a repo is integrated, a maintainer rarely types the per-beat commands. The
front door is the **console script** (`pdca`, named per the project):

```bash
pdca flow 779                 # Plan → Do → Check (through sign-off and publish), for one issue
pdca flow 778 779             # several ids → batch; unbriefed are auto-planned,
                              # dependencies schedule the waves
pdca flow --from-csv issues.csv   # one Plan session briefs SEVERAL from the export, then drives all
pdca waves 778 779            # preview the wave plan without building
pdca status                   # every bundle and its state (also the bare `pdca`)
```

The per-beat detail in steps 03–06 is what `pdca flow` orchestrates for you —
worth understanding once, even though you'll mostly drive from the top. This is
what `pdca status` printed on the wyrd-pdca instance while this walkthrough was
written (trimmed) — the states a human owns are at the top, the frozen cycles
below:

```
AWAITING_SIGNOFF  issue_1007  [15 NEEDS-HUMAN]  [oversized]
AWAITING_SIGNOFF  issue_927  [18 NEEDS-HUMAN]
BUILT             issue_508  [blocked-by: 927, 658, 659]  [oversized]
PLANNED           issue_1008  [blocked-by: 1007, 927]
PLANNED           issue_1009  [blocked-by: 1008]
...
COMPLETE          issue_778  [PR https://github.com/getwyrd/wyrd/pull/885]  [oversized]
COMPLETE          issue_779  [PR https://github.com/getwyrd/wyrd/pull/886 ↑main]
COMPLETE          issue_809  [close: no PR]
DISCONTINUED      issue_636  [oversized]
RESOLVED          issue_773
```

## Prerequisites (for a live run)

The offline rehearsal in [step 02](02-rehearse-offline.md) needs only Python
3.11+. A *live* cycle additionally needs:

- **An agent CLI for the leaves.** The Plan, Do, Check-reviewer, sign-off,
  publish, and Act beats run as configured subprocesses ("leaves") — arbitrary
  commands set in `pdca.toml`, so the model is the harness's choice, not a
  requirement. The cycle model itself is **model-agnostic**; it even recommends a
  *cross-vendor* reviewer (the template defaults the builder to Claude and the
  reviewer to Codex). The worked example uses **two** CLIs: the **Claude CLI**
  (`claude`) for the planner, builder, sign-off, publisher, Act and the adversary
  lens, and the **Codex CLI** (`codex`) for the reviewer, the code-review lens and
  the plan reviewer — so the second opinion comes from a different vendor by
  configuration, not by hope.
- Whatever your **gates** need. For Wyrd that is a **Rust toolchain**: every gate
  delegates to `cargo xtask`, Wyrd's own single-sourced gate runner, through a
  short shell wrapper. Docker and the FoundationDB and TiKV client libraries are
  needed only by conformance legs that bring up a cluster, and Wyrd declares
  them as optional (`WARN`) `pdca doctor` rows rather than hard requirements. Your
  project's gates will differ — you define them in
  [step 01](01-render-and-integrate.md).
- `git` and (for publishing) the `gh` CLI.

New to the harness? Start with [step 00](00-introduction.md) for the *why*. Ready
to set it up? Go to [step 01](01-render-and-integrate.md). Just want to see the
driver move a bundle with no model or gates wired up? Jump to
[step 02](02-rehearse-offline.md).
