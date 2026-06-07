# The quality cycle (PDCA)

The model this harness automates. Project-agnostic — it ships verbatim. Your
repo's concretizations live next door in [INTEGRATION.md](INTEGRATION.md).

## The cycle in one paragraph

One contribution turns one PDCA cycle: **Plan** (author the spec) → **Do**
(implement) → **Check** (verify the built artifact against the spec —
correctness, conformance, *and* validation) → **Act** (process improvement:
adjust the spec template, ruleset, gates, or agent skills so the issues this
cycle exposed do not recur) → back to **Plan** with a better baseline.

Plan + Do + Check operate on the **contribution** (this bug, this fix); Act
operates on the **process** (the ruleset, the template, the workflow). The next
Do should not recreate the issues the previous Act tried to eliminate — if it
does, the Act was not effective.

## The four beats are the four roles

| Beat | Activity | Owner |
|---|---|---|
| **Plan** | author the spec; triage (repro-or-close, scope, success criterion) | **you** (human) |
| **Do** | implement the fix — production only | **builder** (model leaf) |
| **Check** | run correctness + conformance + validation against the artifact | **gates + reviewer + human sign-off** |
| **Act** | adjust the process baseline so the next cycle starts better | **you** (human) |

Three human touch points across those beats: **Plan-authoring**, **Check
sign-off**, **Act**. Do has none. The harness makes each human touch *rare* and
*fast*; it removes the work *around* the human, never the judgment itself.

## Inside Check — the 5 / 5 / 1

- **Correctness — 5-step chain** (ordered): spec → reproduction → change →
  verification → causal adequacy. Steps 1 (spec) and 3 (change) are *inputs*
  (from Plan, from Do), not work Check performs.
- **Conformance — 5-tier stack** (independent layers): structure → shape →
  runtime → contribution → judgment.
- **Validation — 1 indivisible act**: is this the right thing to do at all?

The **gates path** (correctness 2 & 4, conformance T1–4) is fully deterministic
and is the *only* thing that blocks accept — no model in the gating loop. The
**judgment path** (correctness 5, conformance T5, validation) is implemented by
a cross-vendor reviewer model and finalised by the human at sign-off. The
reviewer never gates; the human never edits the fix at Check time.

## The independence contract

The reviewer is a *different model family* from the builder, and is fed
`{patch.diff, brief.md, check-gates.json}` — **never** `build-notes.md`. The
builder's framing must not anchor the reviewer; this is enforced by what the
driver *does not pass*, not by prompt wording. `build-notes.md` joins the
human's audit packet at sign-off, beside the independent verdict.

## The driver — a state machine over files

An issue's state *is* the files in its bundle (`results/issue_<id>/`):

```
(no brief.md)           UNPLANNED          → human authors brief (Plan)
brief.md                PLANNED            → Do: patch.diff + test + build-notes
+ patch.diff            BUILT              → Check: gates + reviewer
+ check-gates.json      CHECKED            → assemble SUMMARY.md
+ SUMMARY.md (§9 empty) AWAITING_SIGNOFF   → STOP. human signs off
§9 set → accept         COMPLETE (frozen)  → Act material, later
         iterate-to-Do  archive Do+Check artifacts → iteration-vN/, rebuild
         iterate-to-Plan archive attempt (incl. brief) → iteration-vN/, re-author
```

Properties: **resumable** (crash resumes from file state), **inspectable**
(`pdca status` is a directory listing), **no model in control flow**.

## Maturity ladder — build in order

1. **L1 — scripted handoff.** Bundles assembled; beats run by hand.
2. **L2 — unattended per-issue body.** One command runs Do→Check→assemble→STOP.
   Requires the deterministic gates to exist and be single-sourced.
3. **L3 — contribution-batch + sign-off queue.** Fan the driver over N issues;
   emit a cheap-first sign-off burn-down.
4. **L4 — Act tooling.** Bundle index across frozen cycles, act-log writer.

This harness ships the **scaffolding for all four rungs**: the L2 driver
(`pdca run`), the L3 batch fan-out + sign-off queue (`pdca batch`, `pdca queue`),
single-sourced gates (`pdca gates`, driver + CI), and the L4 Act tooling
(`pdca act-index`, `pdca act-log`). The leaves and gates run as **stubs** so the
control flow works offline. The long pole that remains is project-specific: the
real gate-tier implementations (until Tiers 1–4 are single-sourced, Check is not
truly automatable) and wiring the real model leaves. Build order:
**gates → driver → batch queue → Act tooling.**

## Full specification

This is the condensed model. The authoritative doc set is vendored under
[quality-cycle/](quality-cycle/) — this harness implements it:

1. [Overview](quality-cycle/00-overview.md)
2. [The Quality Cycle](quality-cycle/01-the-quality-cycle.md) — the model (PDCA, the 5/5/1 inside Check)
3. [Cycle Artifacts](quality-cycle/02-cycle-artifacts.md) — the files each beat produces
4. [Cycle Automation](quality-cycle/03-cycle-automation.md) — the driver as a state machine
5. [Validation Tooling](quality-cycle/04-validation-tooling.md) — what implements Check, and where it lives
6. [Repository Integration](quality-cycle/05-repository-integration.md) — what each repo provides (see [INTEGRATION.md](INTEGRATION.md))
7. [Quality Cycle Guidelines](quality-cycle/06-quality-cycle-guidelines.md) — the per-beat MUST/SHOULD/MAY rules
8. [Case Study — CI Hardening](quality-cycle/07-case-study-ci-hardening.md) — a worked example
9. [Glossary](quality-cycle/08-glossary.md) — terms
10. [Parallel Lanes](quality-cycle/09-parallel-lanes.md) — running cycles concurrently
11. [Adapting the Harness](quality-cycle/10-adapting.md) — render-to-running playbook (worked example: Gramps Testbed v2)

The vendored docs are a snapshot with worked examples drawn from the Gramps
testbed (clearly labelled); your repo's specifics live in INTEGRATION.md, not in
these. Re-vendor when the canonical source changes.
