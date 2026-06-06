---
title: "Parallel Lanes (running cycles concurrently)"
categories: []
managed: false
status: active
---

<!-- Vendored snapshot of the canonical PDCA spec. Obsidian [[wikilinks]] converted to relative Markdown links. The authoritative living source is the project wiki; re-vendor when it changes. -->


> One level beside [03 - Cycle Automation](03-cycle-automation.md). How to run **several PDCA cycles at once** for throughput without the concurrent runs corrupting one another. Core principle: **the bundle is already the unit of isolation, so concurrent execution is safe once every *shared mutable resource* a cycle touches outside its bundle is made private to a lane — and correctness *across* the parallel results is a separate problem, solved by planning and the merge re-gate, never by isolation alone.** Living document.

> **Maturity legend** (as in [03 - Cycle Automation](03-cycle-automation.md)): **[built]** ships in this template; **[project-provided]** is supplied per project because it is repo- or runner-specific. The integration primitives this doc relies on — per-issue bundles, the single-sourced gates (`pdca gates` over a bundle *and* over the working tree), and the publisher's draft PR — are **[built]**. The lane *mechanism* (how a project gives each lane its own working tree / checkout, and uniquely-named runner artifacts) is **[project-provided]**, because *what* must be isolated depends on what the project's gates and builder touch.

## A lane

A **lane** is an isolated execution context that runs cycles independently of the other lanes — in practice an independent copy (or `git worktree`) of the workspace, with its own bundle root (`results/`) and its own copy of every checkout the gates and builder mutate. A lane drives bundles exactly as a single-machine run does; running N lanes is running N drivers over **disjoint** sets of issues.

The bundle (`results/issue_<id>/`, [02 - Cycle Artifacts](02-cycle-artifacts.md)) is already self-contained, and its state is *derived from the files present*, so two different bundles never share artifact state. What is **not** automatically private is everything a cycle reaches *outside* its bundle — and that is where tangling lives.

## Two tanglings, two mechanisms

Parallel cycles can tangle in two unrelated ways. Conflating them is the common mistake: isolation fixes the first and does nothing for the second.

**1. Runtime tangling** — concurrent cycles corrupting *shared mutable state*: the target checkout while a gate applies and reverts a patch in place, a runner's container / artifact names, a scratch directory. Two cycles against one checkout stomp each other's apply/revert; two runners sharing a container name collide. → solved by **mechanical isolation**: give each lane its own working tree (copy or worktree) and uniquely-named runner artifacts. This is what makes concurrent *execution* safe. It is operational and **[project-provided]** — the harness does not own the project's checkout or runner.

**2. Integration tangling** — two patches that are each *green in isolation* but conflict or invalidate each other when **combined**. Two lanes that both edit the same function produce patches that merge with a conflict; or one lane's fix changes behavior the other lane's test asserts. Isolation does not touch this — it is a property of the *results*, not the *runs*. → solved by **lane planning** (up front) and **integration validation** (at the end), below.

The reason isolation cannot help with the second: Check's per-fix gate (the red→green verify, [04 - Validation Tooling](04-validation-tooling.md)) runs each patch against a **clean base**, by design — that is what makes "this fix, alone, is correct" a meaningful verdict. It is therefore *blind to the other lanes*. So:

> **A bundle accepted in a lane is "correct on its own", not "mergeable with the others."** Lane sign-off establishes per-fix correctness; it says nothing about the combination.

## Lane planning — partition by what changes, not by id

The first defense against integration tangling is to not create it. Assign work to lanes by **code locality**:

- Issues whose fixes touch the **same area** go to the **same lane** — they run *serially* within it, so a later fix sees the earlier one already on its base. No conflict can arise between them.
- Lanes run in parallel only across **disjoint** areas of the codebase.

Partitioning by issue id alone is not enough — it isolates the *runs* but not the *changes*. The information needed is already produced at Plan: root-cause analysis names the files / area a fix will touch. Lane assignment is therefore a **Plan-beat judgment** — the same place the human decides scope and which issues to brief ([03 - Cycle Automation](03-cycle-automation.md)) — not a mechanical sharding step. When the touched areas genuinely cannot be predicted, prefer fewer, broader lanes and lean on the integration check below.

## Integration validation — at the merge boundary

Whatever planning misses, correctness-*under-combination* is established where the patches actually meet: the **merge boundary**, not the lane. The harness already has the primitive — the gates are **single-sourced** ([04 - Validation Tooling](04-validation-tooling.md) §Single-sourcing): the same `pdca gates` runs over a bundle (per-fix, in a lane) **and** over the working tree (repo-scoped — `gates.run_working_tree`, "the CI merge re-gate"). Run the repo-scoped re-gate over the **merged** tree and it sees the combination the per-lane gates could not.

The contribution path already routes through that point: the publisher (Check's publish step, [03 - Cycle Automation](03-cycle-automation.md)) opens a **draft PR** per accepted bundle. The draft PR *is* where the combination is checked — the host surfaces merge **conflicts**, and CI runs the repo-scoped re-gate on the **merge result**. No new gate is needed; parallel lanes simply make the existing merge-time re-gate load-bearing rather than incidental. A conflict or a re-gate failure at the boundary is resolved exactly as any multi-contributor project resolves it — at the PR, before merge — not by re-opening a lane.

## What stays serial: the human

The lanes parallelize the **unattended** band only. The three human touch points ([03 - Cycle Automation](03-cycle-automation.md): Plan-authoring, Check sign-off, Act) are one-human / one-terminal and do not parallelize:

- **Plan** is where lane assignment happens — one session, serial.
- **Do + Check (gates + reviewer)** are headless — this is the band that fans out across lanes.
- **Check sign-off** is interactive — converge here. (A *single-workspace* run can batch sign-off across the fanned-out bundles into one cheap-first session; independent lane *copies* each carry their own sign-off queue, so the human attends them in turn — an ergonomic cost of full copies versus an in-driver fan-out.)
- **Act** runs once, across the completed cycles of all lanes — serial by nature.

So the shape is: **Plan (serial) → Do + Check fan out across lanes → sign-off (serial join) → publish → integration re-gate at the merge boundary → Act once.** Parallelism lives entirely in the unattended middle; planning and the merge re-gate carry correctness across the results.

## The one rule

> A bundle is touched by exactly one lane; every mutable thing a lane reaches outside its bundle is private to that lane; and the *combination* of accepted lanes is validated at the merge boundary — never assumed from per-lane green.
