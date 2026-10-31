---
created: 31.10.2026 17:50
type: adr
status: Proposed
tags:
  - adr
  - process
  - triage
  - security
---
# 0020. Bug track: a reported bug is reproduced as a red C4 test before any fix is built, from a sanitized summary, and only paths outside an instance's protected list are fixed without a maintainer's go

## Context

An instance's cycles centre on the open milestone, so bug reports either wait for it or
interrupt it (architecture §8.8 runs lanes in parallel, but in practice one milestone's work
fills them). Most bugs need a smaller change than milestone work, which makes them a
candidate for a flow that runs alongside it with less human involvement up front.

Four forces shape such a flow.

- **A small diff is not a simple bug.** In the projects the harness runs on, the most
  dangerous bugs are the ones whose fix is one line in a commit protocol or an on-disk
  format, found only through the deepest reasoning in the codebase. Diff size is a poor
  proxy for how safely a bug can be fixed without a human deciding the approach.
- **Issue text is hostile input.** Architecture §8.10 and ADR-0017 already treat tracker
  text as written by strangers. Today a builder leaf reads it with `Edit`, `Write` and
  `Bash` (§8.10, #710). A flow triggered by new issues makes that path reachable by anyone
  who can open an issue.
- **Red-before / green-after is already the blocking evidence (ADR-0006).** The C4 gate
  asks whether the defect existed and is gone. For a bug, the red leg can be produced
  before any fix exists, by a leaf other than the builder.
- **Whoever starts the work matters (ADR-0017).** The milestone stays a maintainer's
  decision because setting it starts the instance's PDCA cycle on an issue. A flow that
  starts itself must not quietly take over that decision.

## Decision

We will add a **bug track** to the harness: a flow for issues labelled `bug` that runs
beside milestone work and reuses the cycle's sign-off unchanged. ("Track", not "lane":
lanes are already the parallel worktrees of §8.8.)

### 1. Stages

1. **Intake.** The priority triage of ADR-0017 runs as today. It adds no labels for this
   track and gains no permissions.
2. **Summarize.** A leaf with no write access and no tools beyond reading the issue
   produces a **sanitized summary**: expected and observed behaviour, the area, and the
   conditions to trigger it, in its own words, under the same quoting rules as ADR-0017
   section 4. Every later stage reads the summary, never the issue.
3. **Reproduce.** A leaf writes a test from the summary that fails on the current head: a
   unit or property test, or for an instance that has one, a deterministic-simulation
   seed. The result is the red leg of the C4 gate (ADR-0006), produced before any fix.
   If no failing test is found within the stage's budget, the track stops and posts the
   open questions to the issue for the reporter. It never proceeds to Build.
4. **Cycle.** The test and summary enter a normal PDCA cycle as the bundle's brief. The
   builder must turn the test green without editing it; the gate checks the test file is
   unchanged since Reproduce. Review (ADR-0005), sign-off and C6 (ADR-0004), and publish
   are unchanged: the track ends at a draft pull request, like every cycle.

Failing tests from an instance's own scheduled runs (a simulation seed sweep, a nightly
conformance run) enter at stage 4: they are reproduced already and were not written by a
stranger.

### 2. Who starts which stage

- **Summarize and Reproduce start on their own** for any open issue with `bug` and a
  priority. They publish no code, only a comment with the result (and, on success, the
  test as a workflow artifact or a branch the driver pushes), so they decide nothing a
  maintainer would.
- **Cycle starts automatically only when all of these hold:** the reproduced test exists;
  the issue has no `needs-triage`; and the bug's area is outside the instance's
  **protected paths** (section 3). Otherwise the track stops with the test attached and
  the issue waits for the maintainer, who starts the cycle by setting the milestone, as
  ADR-0017 already provides.
- The milestone is never set by the track. An automatically started cycle runs without
  one and is visible by its `track:bug` label.

### 3. Protected paths

Each instance lists in `pdca.toml` the paths whose bugs always need a maintainer's go before
a fix is built. For getwyrd/wyrd-pdca, these are at least the on-disk format (the
`chunk-format` crate and `docs/design/specs/`), the commit protocol, the consistency
contract, and anything an accepted ADR of that project declares protected. Matching is
deterministic, on the paths the reproduced test touches and on the paths a later diff
touches; a model may move a bug toward protection, never away from it. A cycle whose diff
reaches a protected path is held as a NEEDS-HUMAN item, even when Reproduce did not.

### 4. Budgets

Each stage has a budget: Summarize and Reproduce in tokens and wall-clock time, Cycle
additionally in files touched and diff size. A fix that exceeds its budget is not treated as
a bug fix: the bundle is held with a NEEDS-HUMAN item proposing that the issue go through
Plan like milestone work. Budgets live in the runner (ADR-0010).

### 5. Credentials

No leaf of the track holds a credential that can write to a repository. The driver, which
runs no model, pushes branches and opens the draft pull request, as in every cycle. Where
the leaves run is decided in ADR-0021.

### 6. Telemetry

Each run records, per instance: how many bugs reproduced, how many automatically started
cycles reached a draft pull request, how many of those the maintainer accepted, how many
were later reopened or reverted, and how many were held for exceeding their budget. The
last number shows whether the protected list and budgets sit in the right place.

We reject letting a fix leaf read the issue directly: a summary loses detail, but it cuts
the path from a stranger's text to a leaf that runs `Bash` on the target project. We reject
classifying by expected diff size: it is not known before the fix and misjudges exactly the
bugs that matter. We reject a separate triage for the track: ADR-0017 already classifies
every issue, and a second classifier would disagree with it.

## Consequences

Bugs get a fix candidate without waiting for a milestone, and every one leaves a regression
test behind. The test that blocks sign-off is written by a different leaf than the fix and
before it, which closes a gap ADR-0006 leaves open for ordinary cycles: a builder writing a
test that passes because it was written to.

Bugs that do not reproduce mechanically are never fixed by the track: environment-specific
failures, races the instance's tests cannot capture, reports too vague to act on. That is
the class where a plausible but wrong fix does most damage, so the cost is accepted.

Summarize and Reproduce run on every bug a stranger files, so the cost of a flood of issues
lands there; the spending limit of ADR-0017 section 5 applies to them as well.

The protected list is a maintained artifact per instance; a stale list widens what is fixed
without a maintainer's go. It should be reviewed whenever an ADR of the target project adds
or changes a protected area. The maintainer's review of draft pull requests remains the
throughput limit, by design.

Follow-on work: the `pdca.toml` keys for protected paths and budgets (they move to the
`Policy` protocol of ADR-0013 when step 5 lands), the `track:bug` label, the summarize and
reproduce roles (data, per ADR-0011), and the gate check that the reproduced test is
unchanged.

Left open:

- **[OPEN]** Whether a reproduced test reaches the cycle as a pushed branch or only as an
  artifact the driver commits into the worktree.
- **[OPEN]** Whether Summarize can be the same model call as ADR-0017's classification, or
  must stay separate because that bot is repository tooling and is not rendered into
  instances.
