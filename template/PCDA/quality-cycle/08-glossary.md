---
title: "Quality Cycle — Glossary"
categories: []
managed: false
status: active
---

<!-- Vendored snapshot of the canonical PDCA spec. Obsidian [[wikilinks]] converted to relative Markdown links. The authoritative living source is the project wiki; re-vendor when it changes. -->


> A reference index of the terms used across docs 00–07. Each entry is a one-line
> definition; the doc that *owns* the term (where it is defined and treated in full)
> is named in parentheses. This glossary does not restate the model — it points back
> into it. When a term and a doc disagree, the doc wins.

## The cycle and its four beats

(docs [00](00-overview.md), [01](01-the-quality-cycle.md))

- **PDCA quality cycle** — one contribution turns one cycle: Plan → Do → Check → Act →
  back to Plan with a better baseline.
- **Beat** — one of the cycle's **exactly four** stages: **Plan · Do · Check · Act**.
  There are never more than four. A beat may contain several *steps*.
- **Plan** — author the spec for one contribution; triage (repro-or-close, scope,
  success criterion). Human-owned.
- **Do** — implement the fix (patch + test + rationale). Production work only; no
  adjudication. The builder's beat.
- **Check** — verify the built artifact against the spec (correctness, conformance,
  validation), adjudicate, and contribute it. One beat with several *steps*: gates →
  review → sign-off → publish.
- **Act** — improve the *process* (spec template, ruleset, gates, agent skills) so the
  issues this cycle exposed don't recur. Human-owned; runs **across** cycles, not per
  cycle.
- **Step** — a stage *within* a beat (e.g. Check's review / sign-off / publish; Plan's
  scrape / brief). A step is never a beat; the model leaves are step-level touchpoints.
- **Contribution** vs **process** — Plan + Do + Check operate on the *contribution*
  (this bug, this fix); Act operates on the *process* (the rules and templates). The
  boundary between them is **the seam**: Act is not a re-vote on a contribution.
- **Human touch points** — the three places judgment is irreducible: *Plan-authoring*,
  *Check sign-off*, *Act*. Three touches across the four beats (Do has none) ([03](03-cycle-automation.md)).

## Leaves — the model touchpoints

(doc [03](03-cycle-automation.md))

- **Leaf / model leaf** — a point where a model is invoked. The leaves fill *artifacts*;
  they never decide control flow. There are **six**, all steps within beats:
  - **planner** — Plan (interactive): turns the human's documents into `brief.md`.
  - **builder** — Do (headless): writes `patch.diff` + the named test + `build-notes.md`.
  - **reviewer** — Check review step (headless, advisory): writes `check-review.md`.
  - **signoff** — Check sign-off step (interactive): records the human's decision token.
  - **publisher** — Check publish step (interactive): drafts the contribution artifacts.
  - **act** — Act (interactive): proposes process deltas.
- **Leaf mode** — `stub` (offline placeholder; the vertical-slice default) or `command`
  (a real model wired via `argv` in `pdca.toml`).
- **Interactive vs headless** — interactive leaves hand the terminal to a seeded REPL
  (Plan, sign-off, publish, Act); headless leaves run autonomously and write a doc (Do,
  reviewer). `PDCA_LEAVES_MODE=stub` forces every leaf offline.

## The bundle and its artifacts

(doc [02](02-cycle-artifacts.md))

- **Bundle** — the per-cycle directory `<bundle_root>/issue_<id>/` holding every artifact
  for one contribution. **State is the files present** in it — no database.
- **Bundle root** — the project's base path for bundles (e.g. `results/`).
- **Frozen bundle** — an accepted (COMPLETE) bundle; no longer in flight, and input to
  the next Act review.
- **brief.md** — the Plan artifact: the one-page spec for one logical fix (success
  criterion, repo + branch target, scope/out-of-scope, repro, test file, citations).
- **Design proposal** (a.k.a. **GEPS**-shaped) — a richer Plan artifact for a change big
  enough to warrant a design (architecture / API / UX). Still a brief, not a separate
  track; reserved for the exception ([01](01-the-quality-cycle.md), [07](07-case-study-ci-hardening.md)).
- **iteration-v\<N>/** — a prior rejected attempt, archived intact on iterate (`brief.md` included on iterate-to-Plan); moved here, not deleted.
- **patch.diff** — the Do change. May be pushed to a draft branch; never merged by the
  builder.
- **the shipped test** — the regression test that fails pre-fix and passes post-fix,
  shipped in the same change.
- **build-notes.md** — the builder's rationale, **withheld from the reviewer** by the
  driver; it exists for the human at sign-off.
- **check-gates.json / .md** — the deterministic gate results (the gates component of
  Check). Renders the full 5/5/1 matrix.
- **check-review.md** — the advisory reviewer's per-item verdicts (PASS / FAIL /
  NEEDS-HUMAN).
- **SUMMARY.md** — the assembled result document. Its sections: §1 Spec · §2 Disposition
  · §3 Correctness · §4 Conformance · §5 Advisory review · **§6 NEEDS-HUMAN** · §7
  Proven/unproven · §8 Attachments · **§9 Check sign-off** · §10 Act candidates.
- **§6 NEEDS-HUMAN** — items only a human can clear before sign-off; the C6 guard refuses
  accept while any are open.
- **§9 sign-off** — where the human records the disposition + outcome; completing it
  closes the cycle.
- **§10 Act candidates** — process observations for the next Act review; never gates.
- **Attachments** (§8) — `pr-description.md`, `tracker-comment.md` (always),
  `MANUAL-VERIFICATION.md` (any manual-work outcome), and (for publish) `commit-msg.txt`.
- **process/act-log.md** — the append-only log of process deltas, one dated entry per
  Act review.

## Bundle states

(doc [03](03-cycle-automation.md)) — each is derived from which files exist:

- **UNPLANNED** — no `brief.md` yet (ready for Plan).
- **PLANNED** — `brief.md` present, no `patch.diff` (ready for Do).
- **BUILT** — patch + test + build-notes present (ready for Check gates).
- **CHECKED** — gates + review present, no `SUMMARY.md` (ready to assemble).
- **AWAITING_SIGNOFF** — `SUMMARY.md` assembled, §9 empty; the pipeline **STOPS** here.
- **COMPLETE** — §9 accepted; the bundle is frozen.
- **ITERATE_DO** — §9 = iterate-to-Do; driver clears Do+Check artifacts → PLANNED.
- **ITERATE_PLAN** — §9 = iterate-to-Plan; driver archives the attempt (incl. brief) → UNPLANNED.
- **iterate-to-Do** — the fix was wrong, the spec right: rebuild against the same brief.
- **iterate-to-Plan** — the spec was wrong: re-author the brief (old attempt archived to `iteration-v<N>/`).

## Check — the 5/5/1, gates and judgment

(docs [01](01-the-quality-cycle.md), [02](02-cycle-artifacts.md), [04](04-validation-tooling.md))

- **5/5/1** — the anatomy of Check: **5 correctness** (a chain) + **5 conformance** (a
  stack) + **1 validation** (one indivisible judgment).
- **Correctness (C1–C5)** — ordered chain answering "is it *right* (relative to the
  spec)": C1 Spec · C2 Reproduction · C3 Change · C4 Verification · C5 Causal adequacy.
  C1/C3 are inputs (from Plan/Do); C2/C4 are gates; C5 is judgment.
- **Conformance (T1–T5)** — independent layers answering "is it *well-formed*": T1
  Structure · T2 Shape · T3 Runtime · T4 Contribution · T5 Judgment. T1–T4 are gates; T5
  is judgment.
- **Validation** — the one indivisible act: "is this the right thing to do *at all*"
  (fitness-to-purpose). Judgment, always human-confirmed.
- **Gate / gating** — a deterministic check that **blocks** accept (exits 0 = pass). Only
  gates block. C2/C4 + T1–T4.
- **Unverifiable** — a gate result for a check that genuinely *cannot run* (exit 77 or a
  `PDCA-UNVERIFIABLE:` line). Not a pass, not a fail: routed to §6 NEEDS-HUMAN so C6 makes
  the human clear it (docs 04 §Gate result vocabulary, 06 §C5a).
- **Advisory** — a non-blocking signal (the reviewer, and anything `gating = false`); it
  annotates, never gates.
- **Judgment cell** — C5, T5, and Validation — the three cells no gate can decide; they
  resolve to *advisory reviewer + human sign-off*.
- **Oracle / oracle hierarchy** — every claim is only as strong as what decided it:
  conformance check > written test > existing suite > human/advisory judgment.
- **scope = repo / scope = bundle** — a gate runs against the working tree (what CI
  re-runs) / needs the bundle's patch context (`$PDCA_BUNDLE` exported; local only).
- **Single-sourcing** — one implementation of each gate, invoked identically by the local
  driver and CI — no drift.
- **Reviewer independence / decorrelation** — the reviewer is decorrelated from the
  builder by *file-withholding* (no `build-notes.md`), *tool scope* (execute-only, no
  write), and *vendor split* (a different model family). It protects evidence-integrity,
  not fix-correctness.
- **Collapse rule** — for a conformance-defect fix, correctness collapses into conformance
  (the check is the oracle); for a behavioural-defect fix, correctness needs its own
  evidence chain.
- **C6 accept-guard** — accept is refused while §6 NEEDS-HUMAN has open items. Enforced by
  deterministic code, even when the decision is made in a model+human session.
- **Per-beat rule prefixes** — the guidelines are numbered by beat: **P-** (Plan), **D-**
  (Do), **C-** (Check), **A-** (Act) — e.g. P1, P8, C6, A1. A repo may add prefixed rules
  that *tighten* a generic one, never weaken it ([06](06-quality-cycle-guidelines.md)).

## Sign-off, iteration and disposition

(docs [02](02-cycle-artifacts.md), [03](03-cycle-automation.md), [06](06-quality-cycle-guidelines.md))

- **Sign-off (Check sign-off)** — the human step completing Check: clear §6, weigh the
  advisory review, record §9. The model *proposes* (a one-token decision); the driver
  *records* it under the C6 guard.
- **Sign-off decision** — exactly one of `accept` · `iterate-do` · `iterate-plan`.
- **Disposition** — the cycle's final verdict: fixed / already-fixed / can't-reproduce /
  wontfix / by-design / external / merged-wider / closed-<reason>.
- **Confirm-and-close** — closing a cycle *without* a fix (doesn't reproduce, already
  fixed, wontfix, external). A first-class, successful PDCA outcome.
- **Disposition hint** — the Plan-time triage guess (likely-fix / likely-close /
  POSSIBLY-FIXED → verify first / UPSTREAM / EXTERNAL / NO-NOTES); never binding on Do or
  Check.
- **Sign-off queue** — the cheap-first burn-down of AWAITING_SIGNOFF bundles (empty §6 +
  a close disposition come first; adjudication-heavy ones last).

## Publish — Check's contribution arm

(doc [03](03-cycle-automation.md))

- **Publish** — the closing **step of Check** (not a beat): contribute an accepted fix as
  a **draft PR**. The publisher leaf writes prose; deterministic code does the git/PR
  mechanics, stopping at the draft. In `pdca flow` it runs on every accept (`--no-publish`
  to skip); offline it dry-runs.
- **draft PR** — the contribution artifact publish opens; it is never marked ready or
  merged by the harness — that is the human's sign-off disposition.
- **issue trailer** — the commit/PR line linking to the issue (`[tracker].issue_trailer`,
  e.g. `Fixes #<id>`); the T4 gate enforces it.

## Automation — driver, flow and maturity

(doc [03](03-cycle-automation.md))

- **Driver / orchestrator** — the deterministic state machine over the bundle: read file
  state, run the next beat's leaf, write the artifact, advance, STOP at AWAITING_SIGNOFF.
  No model in the control path. Resumable, idempotent, inspectable.
- **Flow / `pdca flow`** — the continuous orchestrator: Plan → Do → Check(gates → review →
  sign-off → publish) → Act as one run. **`flow_batch`** does it for several issues from
  one Plan session.
- **Batch** — N issues processed together: scrape → draft briefs → one human Plan session →
  `pdca batch` (unattended Do+Check) → `pdca queue` (cheap-first sign-off).
- **Batch selection** — the deliberate, Plan-front choice of *which* issues go in a batch:
  start from a real pool, profile it (status/severity/**age**/component), pick the
  **batch character** (fix-oriented vs hygiene/long-tail), drop non-defects, pre-disposition
  into buckets, size to the natural set.
- **Maturity ladder (L1–L4)** — L1 scripted handoff (project-provided scraper) · L2
  unattended per-issue body (`pdca run`) · L3 batch + sign-off queue (`pdca batch`/`queue`)
  · L4 Act tooling (`pdca act-index`/`act-log`).
- **Maturity tags** — `[built]` (ships and runs) · `[partial]` (ships, needs per-project
  wiring) · `[project-provided]` (not shipped; the project supplies it) · `[planned]`
  (designed, not yet implemented).
- **Heartbeat** — the elapsed-time tick (`… still working (NmSSs elapsed)`) a headless leaf
  or long gate prints so a silent job doesn't look hung.
- **Env overrides** — `PDCA_LEAVES_MODE=stub` (force all leaves offline) · `PDCA_GATES_MODE=stub`
  (stub the gates) · `PDCA_BUNDLE_ROOT` (redirect bundles, so a rehearsal can't collide
  with the real `results/`) · `PDCA_LANES=N` (in-driver lane-pool size; overrides
  `[driver].lanes`, [09](09-parallel-lanes.md)).
- **Lane** — an isolated execution context running cycles concurrently with other lanes,
  realized either as an independent copy / `git worktree` of the workspace (own `results/`
  + own checkout) **or** as a worker slot in the in-driver pool (`[driver].lanes`), where a
  gate scopes its checkout / runner by `$PDCA_LANE`. Several lanes give throughput; the
  bundle is still the unit of isolation ([09](09-parallel-lanes.md)).
- **Lane planning** — assigning issues to lanes by **code locality**: same-area fixes to
  one lane (serial), parallel only across disjoint areas. Partition by *what changes*, not
  by id; a Plan-beat judgment that prevents integration conflicts up front ([09](09-parallel-lanes.md)).
- **Integration re-gate** — validating the *combination* of independently-accepted lane
  patches at the **merge boundary**: the repo-scoped working-tree gate (`gates.run_working_tree`,
  single-sourced with CI) over the merged tree + draft-PR conflict surfacing. Catches what
  a per-fix gate (clean base, blind to other lanes) cannot ([09](09-parallel-lanes.md)).

## Discipline and guardrails

(docs [01](01-the-quality-cycle.md), [03](03-cycle-automation.md), [06](06-quality-cycle-guidelines.md))

- **STOP discipline** — pushing to a draft branch and opening a *draft* PR MAY happen during
  the cycle; the **ready-mark** MUST NOT happen before Check sign-off accepts.
- **Ready-mark** — the explicit "this is ready to merge" transition; the one action reserved
  for the human at sign-off.
- **builder_guard** — the per-subagent PreToolUse hook (`.claude/hooks/builder_guard.py`)
  that mechanically blocks `gh pr ready` / `gh pr merge` for the builder (and publisher),
  not a global deny.
- **Auto-fixable vs decision-level FAIL** — a mechanical FAIL (lint/format/genuinely-red) is
  auto-fixed and re-run; a FAIL needing a human call goes to §6 NEEDS-HUMAN.

## Process, integration and the per-repo spec

(docs [01](01-the-quality-cycle.md), [05](05-repository-integration.md), [06](06-quality-cycle-guidelines.md))

- **Process baseline** — what Plan inherits from prior Act: the brief template, the
  conformance ruleset, the branch-target rules, the agent files. Act improves it.
- **Act review / Act index / act-log** — the cross-cycle pass: `pdca act-index` surfaces
  frozen-bundle §6/§7/§10 + recurring signals; the human decides deltas; `pdca act-log`
  scaffolds the dated, **append-only**, **concrete-and-located** entry.
- **Integration / per-repo specification** — the project's answer to the "which / where /
  how" the generic cycle leaves open (tracker, branches, fixtures, ruleset, templates,
  paths, scripts, governance). Lives in `docs/INTEGRATION.md`; **generic wins on shape,
  the integration wins on instantiation**.
- **Conformance ruleset** — the project's *written* rules each tier consumes (a contributor
  guide, an addon-dev doc, a PEP/RFC). The matrix cites it; a gate that traces to a written
  source is auditable.
- **Tracker** — the bug tracker (Mantis / GitHub Issues / Jira); the integration declares
  its URL, issue-ID format, cross-link form, status→disposition mapping, and comment voice.
- **Reproduction fixture / runner** — the canonical fixture (e.g. `example.gramps`) and the
  scripts that *reproduce* a defect and *verify* a fix; they live in the project's
  verification engine (`engine/`).
- **Branch-target rules** — the per-area map of which fix targets which branch; resolved in
  Plan (P4), never deferred to Do. Symptom-location ≠ fix-location — resolve by reproducing.
- **Upstream-isn't-ahead** — the P8 pre-flight: is the defect already fixed, or being
  addressed? Search by **affected file path**, check merged history, and assess open *and
  closed* PRs (a closed PR is signal, not noise).
- **Success criterion** — the load-bearing brief field: the observable, mechanically
  testable condition that means it's fixed.
- **Scope / out-of-scope** — the brief's explicit boundary (one logical fix); how Check
  flags scope creep against something written.
- **Root cause vs symptom** — the causal-adequacy (C5) distinction; the tracker's
  filed-under location is the symptom, not necessarily the fix location.
- **Two rule families** — **Family A** (project-guideline conformance: the contribution
  against the project's own ruleset — the T1–T5 stack) vs **Family B** (upstream-defect
  analysis: bug-hunting the code the project depends on, whose findings become *upstream*
  PRs). Same tools, different subjects.
