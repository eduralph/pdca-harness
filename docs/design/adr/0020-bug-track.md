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
   produces a **sanitized summary**: a short plain-language description of the bug and its
   impact for people reading the tracker or the bundle, expected and observed behaviour, the
   area, and the conditions to trigger it, all in its own words, under the same quoting rules as ADR-0017
   section 4. Every later stage reads the summary, never the issue. The summary and
   everything later stages add to it form the **bug record**, specified in the
   `bug-record/v1` spec, which also states the sanitization rules the driver enforces.
3. **Reproduce.** A leaf writes a test from the summary that fails on the ref the report
   names, or on `main` when it names none: a unit or property test, or for an instance
   that has one, a deterministic-simulation seed. The result is the red leg of the C4
   gate (ADR-0006), produced before any fix. The driver then runs the test on the other
   candidate refs to choose where the fix goes (section 4).
   If no failing test is found within the stage's budget, the track stops and posts the
   open questions to the issue for the reporter. It never proceeds to Build.
4. **Cycle.** The test and summary enter a normal PDCA cycle as the bundle's brief. The
   builder must turn the test green without editing it; the gate checks the test file is
   unchanged since Reproduce. Review (ADR-0005), sign-off and C6 (ADR-0004), and publish
   are unchanged: the track ends at a draft pull request, like every cycle.

Failing tests from an instance's own scheduled runs (a simulation seed sweep, a nightly
conformance run) enter at stage 4: they are reproduced already and were not written by a
stranger.

### 2. Trigger and state

The track is started by polling, not by repository events. A workflow engine on a
**workflow host** (a trusted machine that runs the instance's driver and holds its
credentials) runs the track's poll command on a schedule set per instance. Hourly or daily
is enough: a bug report does not need an answer within minutes. Any engine that runs a
command on a schedule will do; the harness does not depend on a particular one.

Each run:

- lists the open issues that carry `bug` and a `priority:*` label, with a token that can
  only read. Requiring a priority means the track starts only after ADR-0017's triage has
  classified the issue.
- starts at most a configured number of new Summarize and Reproduce jobs. This is the
  trusted side's limit against a flood of issues, in addition to the model spending limit.
- keeps each issue's progress in labels (`track:bug` and one stage label), so the state is
  visible in the tracker and the run keeps none of its own, in the spirit of ADR-0003.

We reject a GitHub workflow triggered by `issues` events: it runs in a context any issue
author can trigger, next to repository tokens, would need the runner's and the publisher's
credentials inside Actions, and would race ADR-0017's triage. We reject a webhook: it needs
an endpoint on the workflow host that accepts inbound traffic.

### 3. Who starts which stage

- **Summarize and Reproduce start on their own** for any open issue with `bug` and a
  priority. They publish no code, only a comment with the result (and, on success, the
  test as a workflow artifact or a branch the driver pushes), so they decide nothing a
  maintainer would.
- **Cycle starts automatically only when all of these hold:** the reproduced test exists;
  the issue has no `needs-triage`; and the bug's area is outside the instance's
  **protected paths** (section 5). Otherwise the track stops with the test attached and
  the issue waits for the maintainer, who starts the cycle by setting the milestone, as
  ADR-0017 already provides.
- The milestone is never set by the track. An automatically started cycle runs without
  one and is visible by its `track:bug` label.

### 4. Target ref

A bug is found in something that was built and fixed in something that will ship, so the
track fixes against a ref, never against a milestone. Milestones stay plans; we do not keep
a branch per milestone. The candidate refs are:

- `main`, the default;
- a **release branch** (`release/X.Y`) for each release the instance still supports,
  created when that release ships, not when its milestone opens;
- an open **integration line** (§8.8) carrying accepted milestone work that has not
  reached `main`.

The driver runs the reproduced test on every candidate ref and chooses mechanically:

- **Red on `main`:** the fix is built on `main`. Each supported release branch where the
  test is also red then gets a backport, as its own cycle with the same test as its red
  leg.
- **Red on a release branch, green on `main`:** the bug is already fixed. The driver
  bisects `main` for the commit that turned the test green and backports that commit
  instead of building a new fix.
- **Red only on an integration line:** the fix is built on that line and reaches `main`
  with the milestone's work.
- **Red only on a release that is no longer supported:** no fix is built; the issue is
  answered with the first release in which the test is green.

### 5. Protected paths

Each instance lists in `pdca.toml` the paths whose bugs always need a maintainer's go before
a fix is built. For getwyrd/wyrd-pdca, these are at least the on-disk format (the
`chunk-format` crate and `docs/design/specs/`), the commit protocol, the consistency
contract, and anything an accepted ADR of that project declares protected. Matching is
deterministic, on the paths the reproduced test touches and on the paths a later diff
touches; a model may move a bug toward protection, never away from it. A cycle whose diff
reaches a protected path is held as a NEEDS-HUMAN item, even when Reproduce did not.

### 6. Budgets

Each stage has a budget: Summarize and Reproduce in tokens and wall-clock time, Cycle
additionally in files touched and diff size. A fix that exceeds its budget is not treated as
a bug fix: the bundle is held with a NEEDS-HUMAN item proposing that the issue go through
Plan like milestone work. Budgets live in the runner (ADR-0010).

### 7. Credentials

No leaf of the track holds a credential that can write to a repository. The driver, which
runs no model and runs on the workflow host, pushes branches and opens the draft pull
request, as in every cycle. Where the leaves run is decided in ADR-0021.

### 8. Bug bundles and publication

Instances that record their bundles (`pdca record`) commit them to the instance
repository, and the draft pull request goes to the target repository; either can be
public. Everything the track keeps is therefore written to be published.

**Layout.** Bug bundles live apart from milestone bundles, under a `bug_root` (default
`results/bugs/`), grouped by milestone so no directory grows without bound:

```
results/bugs/
  <milestone>/            the issue's milestone when the bundle is recorded,
                          or `unscheduled` when it has none
    issue_<n>/
      README.md           rendered from the record: description, impact, status,
                          target ref, links to the issue and pull requests
      bug-record.json     the bug record, and its MAC (spec bug-record/v1)
      bug-record.json.mac
      cycles/<ref>/       one normal cycle bundle per fix or backport, `/` in the
                          ref replaced by `-`
```

The milestone only places the directory; the fix still targets a ref (section 4). A bundle
is recorded once, when its track ends, and is not moved if the milestone changes later.
A track that ends without a cycle (not reproduced, answered for an unsupported release, or
withdrawn) is recorded too, as a directory holding only the README and the record.

**Security bugs never enter the track.** A reproducing test and a patch published before a
release are an exploit with instructions. The poller skips any issue that ADR-0017's triage
marks as a possible undisclosed vulnerability, and such reports go through the target
repository's private vulnerability reporting, which MUST be enabled for the track to run
(`doctor` checks it). If the mark appears after Reproduce has run, nothing of that track is
committed or pushed; its files stay on the workflow host until the maintainer decides.

**Contents are allowlisted.** A bug bundle holds only: the README, the record and its MAC,
and per cycle the files of a normal bundle that are already published today (brief,
patch, test, review findings, gate verdicts, `SUMMARY.md`, token rows). Leaf transcripts,
in particular Summarize's, which contains the issue text, raw runner logs, instance
metadata and signed URLs never enter a bundle; they stay on the workflow host or are
deleted with the VM. An allowlist fails closed when a new kind of file appears; a filter
fails open.

**A redaction check runs before every commit and push.** `pdca record` and publish refuse
to commit or push a bug bundle that contains a string matching a secret pattern or an
entry of the instance's list of internal identifiers (host names, absolute paths of the
workflow host, cloud project IDs). A match is a NEEDS-HUMAN item, never a silent scrub.

### 9. Telemetry

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

Every bug leaves a short, readable entry under its milestone, so the bug history of a
release can be read without opening the tracker. Because everything the track keeps is
publishable by construction, an instance can record bug bundles in a public repository
without a review pass for leaks; the allowlist and the redaction check are what make
that safe, and both must be kept current when the bundle gains a new kind of file.

Fixing against refs instead of milestones keeps one line of development and no branch per
plan to keep in sync. Each supported release adds a backport cycle to every fix that
reaches it, so declaring a release supported has a running cost the instance should choose
deliberately. Running the test on several refs and bisecting costs runner time on every
reproduced bug.

Follow-on work: the `pdca.toml` keys for protected paths, budgets, the poll schedule and
the per-run limit (they move to the `Policy` protocol of ADR-0013 when step 5 lands), the
poll command, the `track:bug` and stage labels, the summarize and reproduce roles (data,
per ADR-0011), the gate check that the reproduced test is unchanged, the run of a test
across candidate refs with bisection, a version or commit field in the instances' bug
issue template, the `bug_root` layout and README renderer, the bundle allowlist and
redaction check, a `doctor` check for private vulnerability reporting, and a visible
vulnerability mark in ADR-0017's triage (today the flag only shapes the bot's comment).

Left open:

- **[OPEN]** Which releases an instance supports, and where that list lives: release
  branches only become candidates once an instance declares them.
- **[OPEN]** Whether a reproduced test reaches the cycle as a pushed branch or only as an
  artifact the driver commits into the worktree.
- **[OPEN]** Whether Summarize can be the same model call as ADR-0017's classification, or
  must stay separate because that bot is repository tooling and is not rendered into
  instances.
