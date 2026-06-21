# Introduction — why the PDCA harness exists

[Index](README.md) · next: [01 Render & integrate →](01-render-and-integrate.md)

Before the step-by-step, this page answers the *why*: the problem the harness
solves, the benefits it delivers, the features that deliver them, and the
vocabulary the rest of the walkthrough assumes. If you just want to run something,
skip to [step 01](01-render-and-integrate.md) — but five minutes here makes the
rest land faster.

---

## The problem

An AI model can write a plausible fix for almost any issue in seconds. That is
exactly the danger. A plausible diff is not a *verified* one, "looks done" is not
"is correct," and a fix that passes review can still solve the wrong problem or
quietly regress something else. Left unmanaged, AI-driven contribution produces a
firehose of unreviewable, unverifiable, undifferentiated output — and the
maintainer becomes the bottleneck *and* the only safety net, reading every line
under time pressure.

The instinct is to "let the AI do it and review the PR." That fails at volume:
review is the slowest, most fatiguing step, and a raw diff gives the reviewer
nothing to anchor on — no spec to check against, no proof the bug existed, no
proof it's gone, no statement of what's *not* proven.

## The idea

Treat each contribution as one turn of a **PDCA quality cycle** — the
**P**lan-**D**o-**C**heck-**A**ct improvement loop from manufacturing quality
control — and automate everything in it *except* the few judgments only a human
should make:

- **Plan** — author a precise spec (the *brief*).
- **Do** — implement against that spec, and only that spec.
- **Check** — verify the built artifact against the spec along three axes:
  **correctness** (does it work?), **conformance** (does it fit the project?), and
  **validation** (is it the *right* thing?).
- **Act** — periodically, improve the *process itself* so the gaps one cycle
  exposed don't recur.

A deterministic **driver** moves the work through this cycle and **stops only at
three irreducible human touch points** — authoring the Plan, signing off the
Check, and the cross-cycle Act review. Everything between is automated; nothing
ships without the human sign-off.

> The full reasoning behind the model lives in the vendored spec at
> [`../template/PCDA/quality-cycle/`](../template/PCDA/quality-cycle/) (files
> `00`–`10`). This walkthrough is the applied counterpart.

---

## Benefits

**Verifiable correctness, not vibes.** Each cycle ships proof: a test that is
**red before the fix and green after it**. That red→green check is the one thing
allowed to *block* a sign-off — so "verified" has a concrete, deterministic
meaning instead of being a reviewer's gut feel.

**The human stays in the loop — at three points, not every line.** Plan
authoring, Check sign-off, and Act are reserved for human judgment. Everything
else runs unattended. The maintainer adjudicates a short, specific checklist
(§6 NEEDS-HUMAN) instead of re-reading a raw diff.

**No silent failures.** A check that can't run, or fails for reasons outside the
fix's control, doesn't get swept under the rug *or* block a good fix — it surfaces
as an explicit NEEDS-HUMAN item for a person to adjudicate. The gating path that
decides "can this be accepted?" contains **no LLM at all** — it reads only
deterministic gate results.

**The process gets better over time.** The **Act** beat turns recurring
misses — something a maintainer caught at review that no gate owned — into
permanent improvements: a new gate, a new brief field, a tightened rule. Each
future cycle starts from a better baseline. (The walkthrough shows a real
example: a maintainer's review comment about a translation-manifest rule became a
proposed deterministic gate — see [step 07](07-publish-and-act.md).)

**Scale without losing rigor.** Because state lives in files (not a database), the
driver is **idempotent and resumable**, and one Plan session can brief *several*
issues that then build unattended and queue up for a fast, cheap-first sign-off
burn-down. You review many contributions in one focused sitting.

**Cheap to adopt and cheap to learn.** A full cycle can be **rehearsed entirely
offline** — stub models, stub gates, no network — so you can prove the plumbing
(and learn the state machine) before spending a single token. See
[step 02](02-rehearse-offline.md).

**Project-agnostic, drop-in.** The harness ships the *contract* (state machine,
artifact templates, the gate interface) — not your repo's specifics. You render it
into any project and concretize it once; the model logic is reused, the
project-specific parts are yours.

---

## Features (and where you meet them)

| Feature | What it is | Walkthrough step |
|---------|------------|------------------|
| **Copier template** | Render the harness into any repo; pull template updates later with `copier update` | [01](01-render-and-integrate.md) |
| **`pdca.toml` wiring** | One config file binds the driver to your repo's leaves (the per-beat commands) and gates | [01](01-render-and-integrate.md) |
| **`INTEGRATION.md` (11 items)** | The contract for concretizing the generic model to your tracker, branches, fixtures, ruleset | [01](01-render-and-integrate.md) |
| **File-derived state machine** | A bundle's state is *computed from which files exist* — no DB, fully resumable | [02](02-rehearse-offline.md) |
| **Offline rehearsal** | `pdca flow <id> --rehearse` drives the whole flow with stubs — instant, free | [02](02-rehearse-offline.md) |
| **The brief** | A parsed, field-structured spec every later beat consumes | [03](03-plan.md) |
| **Headless builder, narrow tools** | The Do leaf can edit and run tests but not open PRs — STOP discipline by capability | [04](04-do.md) |
| **Deterministic gates** | Your check commands, each tagged gating (blocks) or advisory (informs); baseline-diffed | [05](05-check.md) |
| **Decorrelated reviewer** | An advisory second-opinion leaf that never sees the builder's notes (ideally a different model family) | [05](05-check.md) |
| **The assembled SUMMARY** | brief + gates + review folded into one 10-section verdict, with a §6 NEEDS-HUMAN checklist | [05](05-check.md) |
| **Sign-off + C6 guard** | Four dispositions; `--accept` is refused while any NEEDS-HUMAN item is open | [06](06-signoff.md) |
| **Iterate carry-forward** | A rejection archives the attempt and folds the *reason* into the next build | [06](06-signoff.md) |
| **Draft-PR publish** | Contribute the accepted fix as a *draft* — a human marks it ready, never the harness | [07](07-publish-and-act.md) |
| **Cross-cycle Act loop** | Periodic review that turns recurring misses into spec/gate/rule deltas | [07](07-publish-and-act.md) |
| **Console-script front door** | `pdca flow <id>` orchestrates the whole cycle; `pdca flow <ids…>` batches, bare `pdca` is status; `make` is bootstrap-only | [README](README.md) |

---

## Vocabulary

Five terms recur throughout. Learn them once:

- **Bundle** — the folder for one contribution, `results/issue_<id>/`. All its
  artifacts (brief, patch, gates, SUMMARY) live here; its **state** is derived
  from which of those files exist.
- **Beat** — one phase of the cycle: Plan, Do, Check, Act.
- **Leaf** — the command a beat runs (e.g. the `builder` leaf, the `reviewer`
  leaf). Configured in `pdca.toml`; may be a real model invocation or an offline
  stub.
- **Gate** — a deterministic Check command with a pass/fail result. **Gating**
  gates block a sign-off; **advisory** ones only inform.
- **Disposition** — the human's §9 verdict: `accept`, `iterate-do`,
  `iterate-plan`, or `discontinue`.

The states a bundle moves through:

```
UNPLANNED → PLANNED → BUILT → CHECKED → AWAITING_SIGNOFF → COMPLETE
  (no brief) (brief)  (patch) (gates+    (SUMMARY ready,    (accepted,
                               review)    driver STOPS)      frozen)
                                              │
                                 iterate-do → PLANNED  (rebuild, same brief)
                               iterate-plan → UNPLANNED (re-spec)
                                discontinue → DISCONTINUED (abandon)
```

The four **halted** states — `UNPLANNED`, `AWAITING_SIGNOFF`, `COMPLETE`,
`DISCONTINUED` — are where the driver hands control back to a human or stops.
Everything else it advances through on its own.

---

## Is this for you?

The harness fits when you are **funnelling AI-generated contributions into a real
codebase** and need each one verified, scoped, and signed off — especially at
volume, across multiple branches, or into a project with real conformance rules
(tests, linters, contribution conventions). The worked example throughout this
guide — [Gramps Testbed v2](README.md), which contributes fixes upstream to the
Gramps genealogy project and its addons — is exactly that shape.

It's heavier than you need for a one-off script or a solo throwaway. The payoff is
in *repeated* contribution where correctness, traceability, and a maintainer's
limited attention all matter.

Ready to set it up against a real repo? → [step 01](01-render-and-integrate.md).

[Index](README.md) · next: [01 Render & integrate →](01-render-and-integrate.md)
