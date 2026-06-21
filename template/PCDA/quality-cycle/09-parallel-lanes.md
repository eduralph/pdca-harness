---
title: "Parallel Lanes (running cycles concurrently)"
categories: []
managed: false
status: active
---

<!-- Vendored snapshot of the canonical PDCA spec. Obsidian [[wikilinks]] converted to relative Markdown links. The authoritative living source is the project wiki; re-vendor when it changes. -->


> One level beside [03 - Cycle Automation](03-cycle-automation.md). How to run **several PDCA cycles at once** for throughput without the concurrent runs corrupting one another. Core principle: **the bundle is already the unit of isolation, so concurrent execution is safe once every *shared mutable resource* a cycle touches outside its bundle is made private to a lane — and correctness *across* the parallel results is a separate problem, solved by planning and the merge re-gate, never by isolation alone.** Living document.

> **Maturity legend** (as in [03 - Cycle Automation](03-cycle-automation.md)): **[built]** ships in this template; **[project-provided]** is supplied per project because it is repo- or runner-specific. The integration primitives this doc relies on — per-issue bundles, the single-sourced gates (`pdca gates` over a bundle *and* over the working tree), and the publisher's draft PR — are **[built]**. The **in-driver worker pool** (`[driver].lanes`, running the unattended Do+Check band concurrently and exposing each worker's lane slot to gates as `$PDCA_LANE`) is now **[built]** too. What remains **[project-provided]** is the lane *isolation itself* — deriving each lane's working tree / checkout, container names, ports, and scratch dirs *from* `$PDCA_LANE` inside the project's gate commands — because *what* must be isolated depends on what the project's gates and builder touch.

## A lane

A **lane** is an isolated execution context that runs cycles independently of the other lanes — in practice an independent copy (or `git worktree`) of the workspace, with its own bundle root (`results/`) and its own copy of every checkout the gates and builder mutate. A lane drives bundles exactly as a single-machine run does; running N lanes is running N drivers over **disjoint** sets of issues.

The bundle (`results/issue_<id>/`, [02 - Cycle Artifacts](02-cycle-artifacts.md)) is already self-contained, and its state is *derived from the files present*, so two different bundles never share artifact state. What is **not** automatically private is everything a cycle reaches *outside* its bundle — and that is where tangling lives.

## Two tanglings, two mechanisms

Parallel cycles can tangle in two unrelated ways. Conflating them is the common mistake: isolation fixes the first and does nothing for the second.

**1. Runtime tangling** — concurrent cycles corrupting *shared mutable state*: the target checkout while a gate applies and reverts a patch in place, a runner's container / artifact names, a scratch directory. Two cycles against one checkout stomp each other's apply/revert; two runners sharing a container name collide. → solved by **mechanical isolation**: give each lane its own working tree (copy or worktree) and uniquely-named runner artifacts. This is what makes concurrent *execution* safe. It is operational and **[project-provided]** — the harness does not own the project's checkout or runner.

**2. Integration tangling** — two patches that are each *green in isolation* but conflict or invalidate each other when **combined**. Two lanes that both edit the same function produce patches that merge with a conflict; or one lane's fix changes behavior the other lane's test asserts. Isolation does not touch this — it is a property of the *results*, not the *runs*. → solved by **lane planning** (up front) and **integration validation** (at the end), below.

The reason isolation cannot help with the second: Check's per-fix gate (the red→green verify, [04 - Validation Tooling](04-validation-tooling.md)) runs each patch against a **clean base**, by design — that is what makes "this fix, alone, is correct" a meaningful verdict. It is therefore *blind to the other lanes*. So:

> **A bundle accepted in a lane is "correct on its own", not "mergeable with the others."** Lane sign-off establishes per-fix correctness; it says nothing about the combination.

## Lane mechanics — cheap, safe isolation

The mechanical isolation that fixes runtime tangling is **[project-provided]** (the harness doesn't own the project's checkout or runner), but the *techniques* for making it cheap and safe are generic — every project building lanes hits the same handful of git/runner traps:

- **Reference-clone the target checkouts so a lane costs a working tree, not a full clone.** `git clone --reference <primary>/<repo> <source> <lane>/<repo>` borrows the primary's object store via `alternates`; the lane materializes only a working tree. Keep the primary around and don't aggressively `gc` / delete it — the alternates point into it.
- **A reference-clone inherits objects but *not* remote config.** Re-add the `upstream` / contribution remote in each lane clone, or an upstream-anchored, fetch-based setup (per-version checkouts based on `upstream/<base>`) silently fails.
- **Never `cp` a `git worktree` between lanes.** A worktree's `.git` is a *file* holding an **absolute `gitdir:` pointer** back at the repo that created it — copying it cross-links the new lane to the source (the one trap that re-tangles silently). Create each lane's worktrees *in that lane*, from its own clone. (Same reason a worktree bind-mounted into a container needs its primary gitdir mounted at its own absolute path, or in-container git breaks.)
- **Share read-only / immutable resources across lanes; isolate only *mutable* state.** A built runner image, a vendored ruleset, fixtures — read-only: build/fetch once and share. Only what a cycle *writes* — the target checkout it patches, runner containers / artifacts, scratch dirs — must be lane-private.
- **Name runner artifacts uniquely per run** (a PID/uuid in the container name, ports, scratch paths) so two lanes' runners can't collide, and make the runner **refuse to operate on a dirty checkout** — a loud-failure backstop if isolation is ever breached.
- **Disjoint issue ids per lane.** Beyond preventing run-tangling, it is the single rule that stops two lanes producing duplicate contribution branches / PRs on the shared fork remote.

These are the concrete content of "give each lane its own working tree and uniquely-named runner artifacts" from the runtime-tangling row above; *what exactly* must be isolated still depends on what the project's gates and builder touch.

## Lane planning — partition by what changes, not by id

The first defense against integration tangling is to not create it. Assign work to lanes by **code locality**:

- Issues whose fixes touch the **same area** go to the **same lane** — they run *serially* within it, so a later fix sees the earlier one already on its base. No conflict can arise between them.
- Lanes run in parallel only across **disjoint** areas of the codebase.

Partitioning by issue id alone is not enough — it isolates the *runs* but not the *changes*. The information needed is already produced at Plan: root-cause analysis names the files / area a fix will touch. Lane assignment is therefore a **Plan-beat judgment** — the same place the human decides scope and which issues to brief ([03 - Cycle Automation](03-cycle-automation.md)) — not a mechanical sharding step. When the touched areas genuinely cannot be predicted, prefer fewer, broader lanes and lean on the integration check below.

### Declared ordering — `Depends on:` / `Conflicts with:` [built]

Manual wave-splitting (run a prerequisite batch to COMPLETE, *then* the next) enforces ordering by hand; it does not scale to a batch with a real dependency graph, which is exactly when the lane pool is most useful. A brief may instead **declare** its ordering constraints and let the scheduler enforce them:

- **`- **Depends on:** <id>[, <id>…]`** — a topological gate. The in-driver pool dispatches a bundle only once every declared prerequisite is **COMPLETE** (signed off, not merely built). Because a prereq reaches COMPLETE only after its sign-off in an earlier pass, a dependent waits across passes — exactly the manual wave plan, now machine-enforced.
- **`- **Conflicts with:** <id>[, <id>…]`** — a same-wave exclusion. Two bundles that touch a shared resource (e.g. both edit one `ci.yml`) are **never in flight in the same concurrent wave**; the pool serializes them across lanes while still parallelizing everything else.

The fields are **additive and backwards-compatible**: with none declared, every bundle is always eligible and dispatch is byte-for-byte the prior **sort-by-name pool**. An unschedulable graph — a cycle, or a dependency that is neither in the batch nor an already-COMPLETE bundle — is a **hard error rejected before any build** (`pdca flow` aborts up front). `pdca status` shows a `[blocked-by: <ids>]` flag so the queue reads as a DAG, not a flat list. Declared ordering complements lane planning: planning *avoids* integration tangling by code locality; `depends_on` / `conflicts_with` *enforce* the residual ordering that locality cannot express.

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

## Two realizations — separate workspaces vs an in-driver pool

The fan-out can be realized two ways, and they trade off cleanly:

- **N separate workspaces** (the [project-provided] model above) — each lane is an independent, *serial* driver run in its own `$WORKSPACE`. This needs **no harness change**: the driver is serial and keeps no state outside its workspace, so N concurrent runs can't tangle at the harness level — all isolation is the filesystem boundary. The cost is full copies (disk) and a per-lane sign-off queue (the human attends each in turn).
- **An in-driver worker pool** (one workspace, the driver running bundles concurrently) — lighter on disk (only N lane-scoped checkouts, reused across all bundles) and a single batched sign-off. This is now **[built]**: set `[driver].lanes = N` in `pdca.toml` (or `PDCA_LANES=N` / `--lanes N` for one run). The driver runs the unattended **Do + Check** band across a pool of N workers; **Plan, sign-off, publish, and Act stay serial** (the human band — and an `iterate-plan` re-open is re-planned in a serial pre-pass, never in the pool). Each worker is pinned to a fixed lane slot `0..N-1` for its lifetime and exposes it to every gate command as **`$PDCA_LANE`**.

  The harness owns the *concurrency and the lane id*; it does **not** own the project's checkout or runner, so the actual isolation stays **[project-provided]**: a gate that applies/reverts a target checkout, or starts a container / binds a port / writes a scratch dir, must name that resource by `$PDCA_LANE` (e.g. a `repo-lane$PDCA_LANE` checkout, `--name app-l$PDCA_LANE`, `port = 8000 + $PDCA_LANE`) — exactly the "name runner artifacts uniquely per lane" rule above, now keyed off a harness-supplied slot. Because a worker reuses its slot across the bundles it pulls, only N copies are ever needed, not one per bundle. (Publish is unaffected — it runs in the serial join, so its checkout is never contended and needs no lane scoping.)

  **The lane is the worker's, not the bundle's.** A worker reads its *own* slot at the moment it runs a gate and passes it down as `$PDCA_LANE`; the slot is held in worker-local state and is **never written into the bundle** (`results/issue_<id>/`). So a bundle is not bound to a lane — re-run it and it may land on a different slot — which is sound precisely because a lane scopes only *disposable* shared resources (the checkout it patches then reverts, a container, a scratch dir), never anything the bundle persists. The serial driver (`lanes = 1`) pins no slot, so gates receive no `$PDCA_LANE` and run exactly as a single run does. A gate therefore needs no notion of "which lane am I" beyond reading `$PDCA_LANE` (absent ⇒ serial ⇒ the shared resource).

  **Pool size — how many lanes.** `lanes` is floored at 1 (the serial driver) and has **no hard ceiling**; the driver runs `min(lanes, bundles-in-flight)` workers, so configuring more lanes than there are bundles simply leaves the extra idle. The real bound is the **host**: each live lane is a full concurrent Check gate-suite (its own runner container) plus the lane's builder/reviewer model calls, so the ceiling is set by CPU/RAM for concurrent gates and by the shared **model-API rate limit** — tune per host, not a fixed number. One precondition the project owns: the N lane-private resources for slots `0..N-1` must **exist before the run** (the per-lane checkouts/worktrees/ports created up front to match the chosen N); a worker pinned to a slot whose resource is missing fails loudly — which the refuse-on-dirty / missing-resource guards are there to surface.

  **Combine + re-gate** stays external and composes from existing [built] pieces: drive each lane's bundles into a per-lane bundle root if you want them separated (`PDCA_BUNDLE_ROOT`), then an external script merges the accepted lane branches and runs the repo-scoped merge re-gate (`pdca gates --working-tree`) over the combined tree (§Integration validation). The harness ships no lane-merge orchestrator — that's a project script over these primitives.

Start with separate workspaces (it works today with the generic mechanics above); reach for the in-driver pool only when per-lane disk or sign-off ergonomics actually bite.

## The one rule

> A bundle is touched by exactly one lane; every mutable thing a lane reaches outside its bundle is private to that lane; and the *combination* of accepted lanes is validated at the merge boundary — never assumed from per-lane green.
