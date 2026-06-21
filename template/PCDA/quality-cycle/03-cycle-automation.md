---
title: "Cycle Automation (orchestration layer)"
categories: []
managed: false
status: active
---

<!-- Vendored snapshot of the canonical PDCA spec. Obsidian [[wikilinks]] converted to relative Markdown links. The authoritative living source is the project wiki; re-vendor when it changes. -->


> One level below [02 - Cycle Artifacts](02-cycle-artifacts.md). How the PDCA cycle runs as a pipeline. Core principle: **automate where the work is mechanical (Do, plus Check's gates and reviewer), instrument where the work is human (Plan, Check's sign-off step, Act), never automate the human work away.** The pipeline runs unattended from the brief to the sign-off queue (where Check stops for the human) and resumes only when the human signs off Check; Act fires later, on a cadence, across batches of completed cycles. Living document.

> **Continuous-flow extension.** Beyond the unattended `pdca run`, the driver can run the whole cycle as one continuous, Claude-driven flow — `pdca flow <id> [--from-csv …] [--no-publish] [--no-act]` (or batch: `pdca flow --from-csv …`, one Plan session → several issues; Act runs by default after COMPLETE, `--no-act` to skip). This lifts the model from **two** leaves to **six**, *without* moving any control flow into a model: the planner (Plan — interactive, turns the human's documents into `brief.md`), the sign-off and act leaves (interactive) instrument the human steps the principle above keeps human, and the publisher opens the draft PR on accept (Check's closing step); Do (builder) and Check's reviewer stay **headless**. The state transitions, the gates, and the **C6 accept-guard remain deterministic code** — a leaf only fills an artifact. Leaves are configured in `pdca.toml` (`[leaves.*]`: `mode = stub|command`, `interactive`); set `PDCA_LEAVES_MODE=stub` to force the offline placeholders (CI / `make`).

> **Parallel-lanes extension.** Because the bundle is the unit of isolation, several cycles can run **concurrently** for throughput — see [09 - Parallel Lanes](09-parallel-lanes.md). The key discipline: mechanical isolation (a private working tree per lane) makes concurrent *execution* safe, but correctness *across* the parallel results is a separate problem — handled by **lane planning** (group same-area issues into one lane) and the **merge re-gate** (`gates.run_working_tree` over the merged tree + the draft PR), never by isolation alone. Parallelism stays in the unattended Do + Check band; the human touch points remain serial. Two realizations: N separate workspaces (zero machinery), or the **in-driver worker pool** — `[driver].lanes = N` (`PDCA_LANES` / `--lanes N`) fans the Do + Check band across N workers in one workspace, each exposing its lane slot to gates as `$PDCA_LANE`.

> **Maturity legend** — every major mechanism in this doc is tagged: **[built]** = ships in this template and runs today; **[partial]** = ships but needs per-project wiring (cells in [04 - Validation Tooling](04-validation-tooling.md) §Status today have the breakdown); **[project-provided]** = not shipped by the template — each project supplies it because it is tracker- or repo-specific. The driver (`pdca run` / `flow` / `queue` / `gates` / `act index` / `act log`, in `src/pdca_harness/`), the deterministic gate runner, the headless reviewer, the sign-off queue, and the Act-log tooling are all **[built]**. What each project still supplies: the **Plan-draft scaffolding** — a tracker scraper + handoff generator (the per-repo specification's item-9 tooling, **[project-provided]**); the **real gate check rows** (`pdca.toml`; an all-PASS stub fallback ships, **[partial]**); and the **real leaf commands** (the leaves run as offline stubs until a model is wired, **[partial]**).

## What can and cannot be automated

PDCA has four beats; automation is described per beat. Where a beat has internal components that automate at different levels, the Automation column names each.

| Beat | Owner ([01 - The Quality Cycle](01-the-quality-cycle.md)) | Automation | Maturity | Cadence |
|---|---|---|---|---|
| Plan | human | **instrumented** — a project-provided scraper / handoff generator produces brief drafts; the human authors the spec/verdict | scaffolder **[project-provided]** (tracker-specific); human step is the design | per cycle |
| Do | builder | **full** — headless agent (e.g. `claude -p`), builder subagent scope | builder subagent config **[built]** (`.claude/agents/builder.md`); leaf runs as a stub until a model command is wired | per cycle |
| Check | deterministic gates + advisory reviewer + human sign-off | mixed — **gates: full** (deterministic, unattended); **reviewer: full** (headless, advisory); **sign-off: instrumented** (result document + one-command capture, human completes Check) | gates **[built — partial]** (runner ships; fill real check rows per [04 - Validation Tooling](04-validation-tooling.md) §Status today); reviewer config **[built]**; sign-off queue **[built]** | per cycle |
| Act | human | **instrumented** — process-baseline tooling (bundle index, act-log scaffold) | tooling **[built]** (L4: `pdca act index` / `act log`); manual Act fine indefinitely | **cross-cycle**, batched |

**Human touch points vs beats.** Four rows, four beats — but the human appears at three different places, not four. The three **human touch points** in the cycle are *Plan-authoring*, *Check sign-off*, and *Act* — but these are *not* three beats. Plan and Act are two fully-human beats; Check sign-off is the human-instrumented *step inside* the Check beat (Check's gates and reviewer run unattended; sign-off is the human completion). Do is the fourth beat and has no human touch point. The 4-beat structure is preserved; the three human touches are how humans participate across the three beats that contain human work. The pipeline's job is to make each human touch **rare** (only genuine NEEDS-HUMAN items reach sign-off; Act fires once per batch, not once per cycle) and **fast** (everything pre-assembled, the common confirm-and-close is one keystroke).

The crucial cadence split: **Plan and Check (including sign-off) are per-cycle** (one of each per contribution); **Act is per-batch** (one Act review covers many cycles). The driver's state machine only carries the per-cycle work; Act runs as a separate process.

## The orchestrator is a state machine over the per-cycle bundle

> **Maturity: [built].** The driver described in this section ships in `src/pdca_harness/` and runs today (`pdca run` / `flow`). The `state()` / `advance()` loop below is implemented; each issue's bundle advances unattended to AWAITING_SIGNOFF. What a project supplies *upstream* of the driver is the Plan-draft scaffolding — a tracker scraper + handoff generator (**[project-provided]**) — plus the real gate and leaf wiring.

Do not build a monolith. Each issue's **state is its files** in `results/issue_<id>/`, and a driver advances issues idempotently:

```
(no bundle)        →  PLAN   →  brief.md present
brief.md           →  DO     →  patch.diff + test + build-notes.md present
patch.diff         →  CHECK  →  check-gates + check-review + SUMMARY.md present
SUMMARY.md         →  (AWAITING_SIGNOFF)  ← pipeline STOPS here
SUMMARY.md §9 set  →  sign-off applied:
                       accept           → cycle COMPLETE  (frozen bundle)
                       iterate-to-Do    → driver archives every Do+Check artifact into iteration-v<N>/; state ← PLANNED (re-run Do against same brief)
                       iterate-to-Plan  → driver archives the attempt (incl. brief.md) into iteration-v<N>/; state ← UNPLANNED (human authors a new brief.md, then Do re-runs)
                       discontinue      → state ← DISCONTINUED (no transition, no archive; bundle deliberately abandoned and dropped from the active set)
```

The driver stops the issue at AWAITING_SIGNOFF every time — including on iteration. After sign-off, an accepted bundle is **frozen**: it becomes input for the *next* Act review (a separate, cross-cycle pass — see below).

Properties this buys, cheaply:

- **Resumable.** A crash mid-batch resumes from file state; nothing re-runs that already produced its artifact.
- **No-clobber, with a named exception.** Idempotent `advance` never destroys an artifact it already produced or a verdict already filled. The iterate transitions are a **deliberate archive**, not an idempotency violation: they *move* everything downstream of the re-entry point into `iteration-v<N>/` so a rebuild starts from clean state while the rejected attempt is preserved, not lost. On iterate-to-Plan the `brief.md` is archived with it (state ← UNPLANNED); the rejected attempts accumulate as `iteration-v1/`, `iteration-v2/`, … — see the case study's CLAUDE_CODE_BRIEF v1/v2/v3 sequence for the precedent the skeleton matches.
- **Inspectable.** "What state is issue N in" is a directory listing, not a database.

The driver is a thin loop: for each issue, look at which files exist, run the next beat's command, write its artifact, advance. Stop the issue when it reaches AWAITING_SIGNOFF.

## Per-beat automation

### Plan — instrumented (project scaffolding + human)

Reuse an existing scrape / handoff pipeline as the Plan scaffolder (scrape → emit a `brief.md` with a TRIAGE VERDICT scaffold and auto-flags), drawn from the **current process-baseline spec template** that Act maintains. That *is* the draft `brief.md`. The human fills the spec + success criterion + resolved branch target and confirms/overrides the disposition hint. Automated: everything except the judgment. Not automated: the judgment.

### Do — full (headless builder, subagent scope)

Driver invokes the builder agent non-interactively with `brief.md` as input, builder subagent scope (read repos, write patch+test, MAY push to feature/draft branches, MAY open draft PRs, **MUST NOT mark a PR ready**). Output: `patch.diff`, the test, `build-notes.md`. Pushing and draft-PR-open are allowed because they let CI exercise the artifact during the cycle without bypassing the sign-off gate.

**The ready-mark mechanism — built, mechanically enforced.** The builder subagent (`.claude/agents/builder.md`) registers a PreToolUse hook (`.claude/hooks/builder_guard.py`) that blocks `gh pr ready`, `gh pr merge`, and `gh pr review --approve` for the builder while allowing `gh pr create --draft` and `git push`. The guard splits compound commands on shell operators and strips wrappers (`timeout`, `nice`, …) before matching, so the block can't be smuggled past. It is scoped to the builder subagent — **not** a global `settings.json` deny — so the human and the driver's accept step can still mark PRs ready (a global deny can't be overridden and would block the human's legitimate ready-mark). The constraint is therefore *enforced*, not merely asked of the builder via the brief.

### Check — mixed (gates full, reviewer full, sign-off instrumented)

Check is one beat with three components, each automating at a different level. Components run in order; the blocking logic across all three is **100% deterministic** — no LLM in the gating path.

**1. Deterministic gates → `check-gates.json` (full).** The same checks that run in CI, run locally: the correctness re-runs (repro red / verify green / regression suite) + conformance Tiers 1–4 (structural validator, semgrep, find_spec/runtime, commit-msg + branch-target + version-bump). Each emits pass/fail + rule ID + oracle + path:line. A FAIL with auto-fixable cause (lint/format/genuinely-red) is auto-fixed and re-run; a FAIL needing a decision goes to NEEDS-HUMAN.

**2. Advisory reviewer → `check-review.md` (full).** Different-vendor model (e.g. `codex`) headless, fed `{patch, test, brief, check-gates}` — **not** `build-notes.md`. Re-runs the asserted evidence (stash→red, unstash→green), re-checks cited path:lines ground, emits per-item `PASS/FAIL/NEEDS-HUMAN`. Advisory: annotates, never blocks.

**3. Human sign-off (instrumented — the second human touch point).** The driver assembles `SUMMARY.md` (the ten sections from [02 - Cycle Artifacts](02-cycle-artifacts.md)) from `brief.md` + `check-gates.json` + `check-review.md`, routes unresolved items into §6, leaves §9 (sign-off) and §10 (Act candidates) empty for the human, and marks the issue AWAITING_SIGNOFF. Then the driver presents a **sign-off queue**: an index of all AWAITING_SIGNOFF bundles, sorted so the cheap ones come first — empty §6 + disposition ∈ {already-fixed, wontfix, by-design, external} are near-instant confirms (typically the most common outcome); non-empty §6 (real adjudication) flagged and ordered last. The human opens each `SUMMARY.md`, clears §6, fills §9 via a one-command capture, completing Check:

- **accept** → driver performs the sign-off-gated transitions: marks the draft PR ready, posts the §8 tracker comment, and (where the project's per-repo spec allows it) merges. The push and draft-PR-open may already have happened during Do or Check assembly — accept only performs the steps that *required* sign-off. Cycle closes; bundle frozen.
- **iterate-to-Do** → driver archives `patch.diff`, the test, and the rest of the Do+Check downstream into `iteration-v<N>/` (preserving `brief.md`), state returns to PLANNED, driver re-invokes the builder. Same cycle.
- **iterate-to-Plan** → driver archives the whole attempt — incl. `brief.md` — into `iteration-v<N>/`, state returns to UNPLANNED; the human authors a new `brief.md`, then Do re-runs. Same cycle.
- **discontinue** → driver records §9 and performs **no** transition or archive; state becomes DISCONTINUED (terminal) and the bundle drops out of the active/pending set. For work that, on inspection, doesn't fit the cycle (e.g. handled out-of-band by hand) — a deliberate abandon, independent of §6 (no C6 accept-guard). The human records why discontinued / where the work goes instead, like the iterate rationale.

Optionally, the human jots §10 Act candidates while at the bundle — these are hints for the next Act review, not gates for this sign-off.

**Publish — contribution shape.** On accept, the closing work contributes the fix. The default shape branches off `upstream/<base>`, applies the patch, and opens a new draft PR. When the brief declares an **`Onto branch:` `<remote>/<branch>`** (issue #54), publish instead runs in **stack mode**: the fix is a commit on that existing PR's branch — Check tested against it (`$PDCA_BASE`), and publish checks out `<remote>/<branch>`, verifies the patch still applies to it (else fails loudly — the branch advanced since the fix was built), confirms an open PR has it as head (else refuses to push), then commits and pushes to that branch. No new PR is created; the existing PR's URL is recorded. The same branch is the test base, the commit base, and the push target — so a fix tested against a PR can only land on that PR.

### Act — instrumented, cross-cycle, batched

Act does not run inside the per-issue state machine. It is a **separate pass**, on a separate cadence (every N completed cycles, weekly, when a pattern surfaces). Its instrumentation:

- **Bundle index.** A read-only generator that lists frozen bundles since the last Act review, surfaces §6/§7/§9/§10 contents, and highlights recurring patterns (same NEEDS-HUMAN class across cycles, same brief-template field flagged in §10).
- **Process-baseline diff tools.** Edit-with-history for the spec template, the conformance ruleset, the gate set, the agent files (`.claude/agents/*.md` / `AGENTS.md`), with diff/preview against current.
- **Act log writer.** Appends a dated entry to `process/act-log.md` ([02 - Cycle Artifacts](02-cycle-artifacts.md) §ACT) recording: which bundles were considered, the patterns found, the concrete deltas applied, and a watch-for-recurrence note used by the next Act review.

What is *not* instrumented: the judgment of which rule to add, which template field to clarify, which skill to refine. That is Act's irreducible work.

## Independence is enforced by the orchestrator, mechanically

The decorrelation that makes the reviewer worth running is enforced by the driver, not by prompt text:

- **File withholding** — the driver constructs the reviewer's input set and omits `build-notes.md`. The builder's framing cannot anchor the reviewer because the reviewer never receives it.
- **Tool scope** — reviewer gets execute (run tests/validator, git stash/unstash) and **no write to the fix**. It cannot patch what it judges.
- **Vendor split** — reviewer is a different model family from the builder. Different family = decorrelated blind spots.

These three are orchestrator responsibilities. An LLM told "don't look at the rationale" that still has the file is not independent; the driver simply does not pass the file.

**What this enforcement does and does not buy.** The three mechanisms above decorrelate **evidence-integrity** — the reviewer can't be anchored by the builder's narrative, can't edit the artifact it judges, and brings a different model family's blind spots. They do *not* decorrelate **fix-correctness**: the reviewer's input set still includes `brief.md` and the shipped test, so a framing blind spot the brief carries reaches the reviewer too, and stash → red / unstash → green confirms the test's own narrow oracle, not that the test exercises the real defect. The reviewer attempts causal adequacy advisory (correctness step 5) and may flag NEEDS-HUMAN; the actual check on causal adequacy is the **human at sign-off** ([06 - Quality Cycle Guidelines](06-quality-cycle-guidelines.md) C6). The orchestrator's job is to make the reviewer's evidence trustworthy, not to make sign-off optional.

## Where it runs: local driver + CI re-confirmation

Two substrates, used for what each is good at:

- **Local/container driver** **[built]** runs the **contribution body** — Do + Check + assemble — co-located, full control. This is the fast inner loop: `pdca run <id>` advances one issue to AWAITING_SIGNOFF; `pdca flow <ids…>` fans it over many.
- **CI** **[built — partial]** runs the **same deterministic gates** again as the merge-gate, triggered when sign-off accepts and the draft PR is pushed. The workflow ships (`.github/workflows/check-gates.yml`, invoking `pdca gates --working-tree`); it re-runs whatever check rows are configured in `pdca.toml`, so its coverage tracks the gates a project has actually written (partial until they are). The belt-and-suspenders model is the design; the second belt is in place wherever rules exist.

The two agree because the gates are **single-sourced** — the validator(s), semgrep rules, and the runtime/suite checks are one implementation, invoked by both the local driver and CI. CI is not a second opinion; it is the same check re-run at the merge boundary so nothing merges that the body did not clear.

Act has no CI counterpart — process improvement is human work that edits the rules CI enforces, not work CI enforces back.

## Batch selection (upstream of fan-out)

Fan-out (below) starts from a chosen set of issues. Choosing that set is a deliberate Plan-front step, not "take the next N rows" — the distribution of the candidate pool decides the batch's character, so profile before selecting.

1. **Start from a real candidate pool — no fabrication.** A batch begins from an actual tracker export (a CSV, a saved query), never from invented or recalled issue IDs. Re-export when starting a batch so the pool reflects the current tracker; items get fixed and closed between batches.
2. **Profile the pool before selecting.** Tabulate the distribution — status, severity, **item age** (the key evaporation signal: old items predict already-fixed / can't-reproduce on current code; recent items predict live, reproducible defects), and component/category. The distribution, not a target count, decides what the batch can be.
3. **Choose the batch character deliberately.** Two coherent kinds, not to be blended by accident:
   - **Fix-oriented** — recent items; high yield of real patches against code that still largely exists; lower close-count.
   - **Hygiene / long-tail** — oldest items; mostly evidenced can't-reproduce / already-fixed closes. Legitimately valuable (a stale confirmed defect that no longer reproduces *should* be closed) but don't expect patches.
   A blended batch is fine *if chosen*, not as a side effect of sorting.
4. **Drop non-defects before counting.** Exclude items that aren't the reproducible-defect character a fix batch is for (source-comment typos, obsolete translation strings, dead infra links, pure doc cleanups). They may be valid tracker items; they waste triage effort in a fix batch.
5. **Pre-disposition into buckets.** Sort the candidates into starting dispositions before the driver touches them, so the batch is legible (e.g. confirmable-here / lower-confidence / platform-specific / likely-cluster). The bucket *taxonomy* is repo-specific (see the per-repo specification); the discipline of pre-sorting is not.
6. **Size is a soft target.** Pick the natural set, not a round number. Dropping valid items to hit an arbitrary count is waste; padding with weak ones to reach it is worse.

The tracker scrape + handoff generator (the per-repo specification's item-9 tooling) then turns the selected IDs into draft briefs with the triage-verdict scaffold and a batch index. Notes / comment threads are **mandatory** inputs, not the export row alone — root cause and already-fixed signals usually live in the thread, not the title.

**Selection failure modes** (cheap to prevent, expensive to discover late):

- **Wrong export** — confirm the pool actually contains the IDs you expect before building on it.
- **Stale defaults in scaffolds** — a brief generated by a session that didn't know the current branch-target rule carries a wrong default; sweep the batch index before branching.
- **Items falling through buckets** — reconcile bucket membership against the full candidate list; silently omitted items never get a verdict.
- **Count records, not lines** — multi-line descriptions inflate a naive line count; count parsed records.

## Batch fan-out

The contribution-batch case is the per-issue driver under an existing fan-out:

1. A project-provided scraper + handoff generator turns the selected IDs into N draft `brief.md` (Plan scaffolding, **[project-provided]**). **Human fills verdicts** (Plan judgment, the one human step before the body runs).
2. `pdca flow <ids>` loops every issue through Do → Check → assemble, unattended, producing N `results/issue_<id>/` bundles. Resumable via the state machine.
3. `pdca queue` emits the **sign-off queue** (burn-down index, cheap-first). Human works the queue; confirm-and-close items are one keystroke each.

So a contribution-batch is: one human pass at the **Plan** beat to author specs, an unattended run of the **Do** beat and the mechanical portion of the **Check** beat (gates + reviewer) over N issues, then one human pass to complete the **Check** beat (sign-off). Four beats, two human touches inside them.

**An Act batch is separate and slower.** Every K contribution-batches (or on a calendar), the Act tooling presents a cross-cycle bundle index and the human runs one Act review pass covering all bundles since the last review. Cadence is yours; the model only requires that Act fire *eventually* over the records, not that it fire per-cycle.

## Maturity ladder

Build the automation in order. **The three human touch points — Plan authoring, Check sign-off, and Act — stay human at every level.** (Those are three touch points across three beats: Plan and Act are fully human beats; Check is a mixed beat whose sign-off step is its human portion. Do is the fourth beat and has no human touch.)

- **L1 — scripted handoff.** **[project-provided]** A tracker scraper + handoff generator emits draft briefs and bundle directories from a candidate pool (see [Batch selection](#batch-selection-upstream-of-fan-out)). This is tracker-specific, so the template does not ship it; each project supplies it as the per-repo specification's item-9 tooling.
- **L2 — unattended per-issue body.** **[built]** `pdca run <id>` runs Do→Check→assemble→STOP for a single issue: the state machine + the two headless leaf calls + the single-sourced gate runner. (The leaves run as offline stubs until a model command is wired, and the gates use the all-PASS stub fallback until real check rows are filled — fill the gates first, per [Build order](#build-order).)
- **L3 — unattended contribution-batch + sign-off queue.** **[built]** `pdca flow <ids…>` produces N bundles; `pdca queue` is the cheap-first sign-off burn-down.
- **L4 — Act review tooling.** **[built]** `pdca act index` is the bundle index across frozen cycles; `pdca act log` scaffolds the dated entry (the deltas are left to the human). L4 is independent of L1–L3 — you can run Act manually against L3 bundles regardless.

The driver, queue, and Act tooling all ship; the gate *runner* ships too but only gates meaningfully once real check rows exist — until a project fills Tiers 1–4 into `pdca.toml`, the body runs on the all-PASS stub fallback. So the per-project build order is: **gates first**, then wire the real leaf commands, then lean on the Act tooling as manual Act becomes the bottleneck.

## What stays human (the honest boundary)

Judgment is irreducible at parts of three beats — Plan, Check, and Act. The fourth beat, **Do**, is fully delegable to the builder: it is production work without an embedded human judgment step.

- **Plan** — the entire beat is human, including any re-entry into Plan triggered by iterate-to-Plan from Check sign-off (the brief revision is Plan work). The project's Plan scaffolding drafts; the human decides what to fix and what "fixed" means.
- **Check, at the sign-off step** — the gates and reviewer run unattended (full automation); the **sign-off step** that completes Check is the human one. The human clears NEEDS-HUMAN and records the per-contribution disposition. The reviewer may attempt all of Check's judgments advisory; the human signs.
- **Act** — the entire beat is human. What rule to add, which template field to revise, whether to retire a check. The bundle index surfaces patterns; the human decides.

Automating any of these would be automating judgment, which the model says is irreducible at those three points. The pipeline does not remove the human; it removes everything around the human so the human only does the parts no oracle can.

## Implementation substrate (what is built from what)

"Maximally scripted" has a precise meaning: **minimise model-in-the-loop at run time.** Every beat that *can* be code *is* code; a model is invoked only at the **leaves** — the two *headless* ones where the sole available oracle is a model (Do builder, Check reviewer), plus the *interactive* ones that instrument the human touch points (Plan, Check sign-off, Check publish, Act). A leaf's instruction file does not reduce the number of model decisions — it makes each one repeatable. So the leaf configs are rows in the table below, not the backbone.

| Beat(s) served | Concern | Vehicle | Rationale |
|---|---|---|---|
| all four (cross-beat) | Orchestration, state machine, `SUMMARY.md` assembly, sign-off queue | **deterministic driver** (e.g. Python; extends an existing batch runner) | control flow must be code, not a model — a model in the control path can skip a gate or reorder steps |
| Check (gates component) | Check gates (correctness re-runs + conformance T1–4) | **plain code, single-sourced** (validator, semgrep, suite, commit-hooks) | this is the *scripting* of Check; the same impl CI invokes |
| Do | Do — build consistency | **Claude Code subagent** (`.claude/agents/builder.md`) | pins how a brief becomes patch+test+build-notes; committed, versioned |
| Check (reviewer component) | Reviewer — review consistency | **Codex `AGENTS.md`** | Codex's instruction vehicle; different vendor by design |
| Do + Check (reviewer) | Independence / tool scope | **Claude Code subagents** (`.claude/agents/`) | builder=write, reviewer=execute-no-write, enforced mechanically |
| Plan (cycle entry) | Human single-issue trigger | **slash command** | thin invocation only |
| Check (gates re-run on the actual PR) | Merge re-gate | **CI** (e.g. GitHub Actions) | re-runs the same single-sourced gates on the real PR |
| Plan + Check (read), Act (maintains) | Persistent rules | **CLAUDE.md / AGENTS.md** | already in place |
| Act | Act tooling (L4) | **separate scripts + the act-log** | runs against frozen bundles; never touches in-flight ones |

**The anti-pattern to avoid:** a single agentic skill/loop that "runs the PDCA cycle." It feels like more automation and is less — it re-inserts a model into the control path the design works to keep deterministic (no LLM in the gating path). The cycle is *run by* a script; the model is *invoked by* the script at its leaves. Hold that inversion and the rest follows.

The model leaves are configured as `.claude/agents/*.md` subagents (planner, builder, sign-off, publisher, act); each leaf's instruction file pins its own consistency, so don't share one across leaves. The **reviewer** is the one deliberately decorrelated leaf — a *different vendor* from the builder (Codex via `AGENTS.md`), since independence is a Check property.

## Driver skeleton

> **Maturity: [built].** This is the reference shape of the L2 driver;
> the shipped implementation in `src/pdca_harness/` (`state.py`,
> `driver.py`, `gates.py`, `leaves.py`, `assemble.py`) follows it. The
> sketch below is kept as the readable model — read the modules for the
> exact code. Each function corresponds to a piece in the
> [Build order](#build-order) below.

The driver is a thin loop: read each issue's file-state, run the next
beat's command, write its artifact, advance, STOP at AWAITING_SIGNOFF.
Gates and agents are called through narrow interfaces so they are
swappable and individually testable.

```python
# states are derived from files present in results/issue_<id>/ — no DB
def state(d):
    if not (d/"brief.md").exists():        return "UNPLANNED"
    if not (d/"patch.diff").exists():      return "PLANNED"        # ready for Do
    if not (d/"check-gates.json").exists():return "BUILT"          # ready for Check
    if not (d/"SUMMARY.md").exists():      return "CHECKED"        # ready to assemble
    if not signoff_set(d/"SUMMARY.md"):    return "AWAITING_SIGNOFF" # STOP — human
    return iterate_or_complete(d/"SUMMARY.md")  # ITERATE_DO | ITERATE_PLAN | COMPLETE

def advance(d):
    s = state(d)
    if s == "PLANNED":
        do_build(d)                 # Claude Code headless — leaf 1
    elif s == "BUILT":
        run_gates(d)                # deterministic; writes check-gates.json
        run_review(d)               # Codex headless — leaf 2 (advisory)
    elif s == "CHECKED":
        assemble_summary(d)         # pure code: brief+gates+review -> SUMMARY.md §1-8
    elif s == "ITERATE_DO":
        archive_iteration(d, include_brief=False)  # MOVE every Do+Check
                                       # artifact into iteration-v<N>/ —
                                       # see implementation below
        # state now == "PLANNED" on next call; do_build re-runs
    elif s == "ITERATE_PLAN":
        archive_iteration(d, include_brief=True)    # archive the attempt
                                       # incl. brief.md into iteration-v<N>/
        # state now == "UNPLANNED"; human authors a new brief.md, then
        # do_build re-runs
    # AWAITING_SIGNOFF and COMPLETE: driver does nothing (human work or done)

def run_issue(d):
    while state(d) not in ("UNPLANNED", "AWAITING_SIGNOFF", "COMPLETE"):
        advance(d)                  # idempotent; resumable; no-clobber
                                    # (iterate transitions archive deliberately —
                                    # see archive_iteration below)

# ---- iterate transitions: ARCHIVE every downstream artifact (move, don't delete) ----
# Files downstream of brief.md (everything Do and Check write):
DOWNSTREAM_OF_BRIEF = [
    "patch.diff",
    "build-notes.md",
    "check-gates.json",
    "check-review.md",
    "SUMMARY.md",
]

def archive_iteration(d, include_brief):
    """Iterate: MOVE the previous attempt into d/iteration-v<N>/ rather than
    deleting it — so a rejected attempt is preserved, not lost — and state()
    returns to the re-entry point.

    iterate-to-Do (include_brief=False) archives the Do+Check downstream + the
    bundle-local test, leaving brief.md → state PLANNED. iterate-to-Plan
    (include_brief=True) archives brief.md too → state UNPLANNED, the human
    re-authors. Preserving history matches the case study's CLAUDE_CODE_BRIEF
    v1/v2/v3 sequence ([07 - Case Study - CI Hardening](07-case-study-ci-hardening.md))."""
    n = next_iteration_no(d)         # 1 for first iterate; counts existing iteration-v*/
    arch = d/f"iteration-v{n}"
    names = list(DOWNSTREAM_OF_BRIEF) + (["brief.md"] if include_brief else [])
    # the shipped test file named in brief.md, if it lives inside the bundle
    names += [str(tf) for tf in test_files_from_brief(d/"brief.md") if within(tf, d)]
    for name in names:
        if (d/name).is_file():
            arch.mkdir(exist_ok=True)
            (d/name).rename(arch/Path(name).name)

# ---- leaf 1: Do (builder) — full write + push + draft-PR (no ready-mark) ----
def do_build(d):
    run(["claude","-p","--agent","builder","--input", d/"brief.md",
         "--cwd", target_repo(d)])  # writes patch.diff, the test, build-notes.md

# ---- leaf 2: Check-review (reviewer) — INDEPENDENCE ENFORCED HERE ----
def run_review(d):
    inputs = [d/"patch.diff", d/"test", d/"brief.md", d/"check-gates.json"]
    # build-notes.md is DELIBERATELY ABSENT from inputs — the driver withholds it
    run(["codex","exec","--config","review.AGENTS.md",
         "--read-only-fix", *inputs])   # execute (stash/run), no write to the fix
                                        # writes check-review.md

# ---- the gate runner: one interface, every check single-sourced ----
def run_gates(d):
    results = []
    results += correctness_reruns(d)      # repro red / verify green / regression
    results += conformance_tier(d, 1)     # structural validator (rule IDs)
    results += conformance_tier(d, 2)     # semgrep             (rule IDs)
    results += conformance_tier(d, 3)     # find_spec / runtime suite
    results += conformance_tier(d, 4)     # commit-msg / branch-target / version-bump
    write_json(d/"check-gates.json", results)   # each row: {check, result, oracle, rule_id, path_line}
    # NOTE: blocking logic reads ONLY this file — the reviewer never gates

# ---- Act review (L4) — SEPARATE process, NOT in run_issue ----
def act_review(since: date):
    bundles = list_frozen_bundles_since(since)            # COMPLETE state only
    patterns = scan_for_recurring_signals(bundles)        # §6, §7, §10 across bundles
    # human reviews patterns, decides concrete deltas, applies them via the
    # process-baseline tools; act_log.append() records the decision and the
    # watch-for-recurrence note used by the next act_review.
```

Four invariants the skeleton encodes, each a design rule from
[01 - The Quality Cycle](01-the-quality-cycle.md) / [02 - Cycle Artifacts](02-cycle-artifacts.md):

1. **No model in control flow.** `run_issue`/`advance`/`state` are pure
   code; `claude` and `codex` are called only inside the two leaf
   functions.
2. **Independence is a missing list element.** `run_review`'s `inputs`
   omit `build-notes.md`. Independence is enforced by what the driver
   *does not pass*, not by instruction.
3. **Gating is deterministic.** Blocking reads `check-gates.json` only;
   `check-review.md` is consumed by `assemble_summary` into §5/§6 as
   advisory annotation, never as a gate.
4. **Act is out-of-band.** `act_review` is not called from `run_issue`.
   Per-contribution control flow finishes at AWAITING_SIGNOFF /
   COMPLETE; Act runs separately, across frozen bundles, on a cadence
   the human picks.

The gate runner is the long pole: `conformance_tier(d, n)` must resolve
to single-sourced implementations (the structural validator, the
semgrep rules, the runtime/suite checks, the Tier-4 hooks) that **CI
calls too**. The runner ships with an all-PASS stub fallback, so the
body runs unattended from day one — but it gates nothing real until a
project fills those rows in, which is why **gates come first** in the
per-project build order. See
[04 - Validation Tooling](04-validation-tooling.md) for the tier × home decomposition that
drives where each gate is built.

## Build order (restated as concrete deliverables)

Status against each step is shown in brackets. The driver, queue, and
Act tooling all **ship**; what each project supplies is the L1 scraper,
the real gate rows, and the real leaf commands.

1. **Gates, single-sourced** **[built — partial]** — the deterministic
   gate runner ships (`pdca gates`, driven by `pdca.toml`
   `[[gates.checks]]`) with an all-PASS stub fallback; each row is
   callable identically by the driver and by CI. Per-project: fill the
   real Tier 1–4 rows (structural validator, semgrep, runtime/suite
   checks, commit-msg + branch-target + version-bump hooks). See
   [04 - Validation Tooling](04-validation-tooling.md) for the tier × home decomposition.
2. **The driver** **[built]** — the shipped state machine in
   `src/pdca_harness/` (state derivation, the iteration paths, the two
   leaf invocations with file-withholding, the gate runner,
   `assemble_summary`); run via `pdca run`.
3. **The contribution-batch queue** **[built]** — `pdca flow <ids…>` fans the
   driver over N issues; `pdca queue` emits the cheap-first sign-off
   burn-down index.
4. **The two leaf instruction files** **[built]** — the builder subagent
   (`.claude/agents/builder.md`) and the reviewer (`AGENTS.md` +
   `.claude/agents/reviewer.md`), with the ready-mark enforced by
   `.claude/hooks/builder_guard.py` (see [Do — full](#do--full)). The
   configs ship; the leaves run as offline stubs until
   `leaves_mode = "command"` in `pdca.toml` wires a real model.
5. **Act tooling (L4)** **[built]** — `pdca act index` (bundle index
   across frozen cycles) and `pdca act log` (dated entry scaffold; the
   deltas are the human's). Usable anytime; manual Act is also fine
   indefinitely.
