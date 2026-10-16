---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 8. Cross-cutting concepts

> Living document. The ideas the rest of the design is built on. Terms are also in the
> glossary ([§12](10-quality-risks-glossary.md#12-glossary)). The walkthrough's
> [`docs/07-crosscutting.md`](../../07-crosscutting.md) covers size, iteration and parallel
> execution in operating detail. Open questions are marked **[OPEN]** inline.

## 8.1 The bundle

One contribution is one directory, `results/issue_<id>/`. Its artifacts — `brief.md`,
`patch.diff`, `build-notes.md`, `check-gates.json`, `check-review.md`, `SUMMARY.md`,
`publish.json` and the iteration archives — are the whole state (ADR-0003).

## 8.2 Beats and leaves

Four beats: Plan, Do, Check, Act. Models run only at leaves within them, each configured in
`[leaves.*]` with `mode = stub | command` and `interactive`. Review, sign-off and publish are
steps of Check, not beats. Stub mode runs the whole machine without a model, which is how the
offline suite and `flow --rehearse` work.

## 8.3 Evidence and gates

`[[gates.checks]]` rows run in a reconstructed worktree. Exit 0 passes, 77 or a
`PDCA-UNVERIFIABLE:` line is unverifiable, anything else fails. Only gating rows block; the
per-fix red→green C4 row gates by default (ADR-0006). The Check rubric is an 11-row matrix
(C1–C5 correctness, T1–T5 tests and conformance, V fitness to purpose) laid over the gates.

## 8.4 The sign-off and C6

§6 NEEDS-HUMAN lists what only a person can clear; §9 records the decision. `accept` is refused
while any §6 row is open (ADR-0004). **[OPEN]** roughly 30% of §6 rows are items no leaf can
ever clear (risk R2); the evidence contract in step 1 addresses it (#712).

## 8.5 Decorrelation

The reviewer and advisory leaves run on a different vendor than the builder that actually ran,
without `build-notes.md`, against a disposable copy of base + patch (ADR-0005).

## 8.6 STOP discipline

Leaves may push to a feature branch and open a draft PR. Marking ready, merging and approving
are the human's, enforced by a `gh` shim and the `builder_guard` PreToolUse hook.

## 8.7 Size, iteration and splitting

An a-priori size estimate (and an optional sizer leaf) warns before Do; an empirical size
signal flags oversized patches at Check; the splitter proposes child bundles and `split.accept`
creates them transactionally with lineage. Each iteration archives the previous attempt and
adds a carry-forward block to the brief. **[OPEN]** size controls are advisory; no round has
ever produced a smaller patch than the previous one (risk R6).

## 8.8 Waves, lanes and integration lines

Batches are levelled into waves by declared edges; lanes run bundles in parallel worktrees.
Stack mode folds each accepted wave onto an integration line that later waves are cut from;
merge mode merges in wave order. The 0.60 milestone fixed most of the known gaps here; their
invariants are the input to step 6 (#715).

## 8.9 Contain and degrade

A failing step of one bundle is contained so the batch continues (`flow._isolate`,
`flow.py:52`). **[OPEN]** the same pattern hides real failures: an exception leaves a bundle
silently behind, and a leaf that exits 0 with no output was once read as "0 findings" for nine
bundles. Step 5 replaces it with typed outcomes (`Ok / Held / FailedInfra`, #714).

## 8.10 Security

The trust boundary is the accept path: no leaf output, tracker text or gate output may
release a publish on its own (ADR-0004). Leaves run with `Edit`/`Write`/`Bash` and are
treated as able to leave any file in any state. Sandboxed leaves (reviewer, advisory) run in an
OS sandbox with a memory cap. **[OPEN]** three known gaps: the sign-off session can edit §9,
nothing checks reviewer ≠ builder vendor, and the claude builder is not confined to its
worktree (#710).
