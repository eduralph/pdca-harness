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
# 0020. Bug track: reported bugs are reproduced as a red test before any fix, verified where the stranger's code never ran, and accepted by merging a bundle pull request

## Context

An instance's cycles centre on the open milestone, so bug reports either wait for it or
interrupt it. Most bugs need a smaller change than milestone work, which makes them a
candidate for a flow that runs alongside it with less human involvement up front.

Four forces shape such a flow.

- **A small diff is not a simple bug.** In the projects the harness runs on, the most
  dangerous bugs are the ones whose fix is one line in a commit protocol or an on-disk
  format, found only through the deepest reasoning in the codebase. Diff size is a poor
  proxy for how safely a bug can be fixed without a human deciding the approach.
- **Issue text is hostile input.** Architecture §8.10 and ADR-0017 already treat tracker
  text as written by strangers. Today a builder leaf reads it with `Edit`, `Write` and
  `Bash` (§8.10, #710). A flow triggered by new issues makes that path reachable by anyone
  who can open an issue, and it makes three weaknesses of the current cycle matter: the
  publisher leaf holds the operator's credentials (#853), evidence files can be written by
  the thing under test (#854), and the reviewer executes bundle code on the host (#855).
- **Red-before / green-after is already the blocking evidence (ADR-0006).** The C4 gate
  asks whether the defect existed and is gone. For a bug, the red leg can be produced
  before any fix exists, by a leaf other than the builder, and verified by nobody's leaf.
- **Whoever starts the work matters (ADR-0017).** The milestone stays a maintainer's
  decision because setting it starts the instance's PDCA cycle on an issue. A flow that
  starts itself must not quietly take over that decision, and ADR-0004 keeps the accept a
  human's and publish after it.

## Decision

We will add a **bug track** to the harness: a flow for issues labelled `bug` that runs
beside milestone work in daily batches, produces a red test before any fix, has every
verdict computed where no stranger's code ran (ADR-0021 §1), and ends in a **bundle pull
request** on the pdca repository whose merge is the maintainer's accept. Publish follows
accept, unchanged. ("Track", not "lane": lanes are the parallel worktrees of §8.8.)

### 1. Stages

1. **Intake.** The priority triage of ADR-0017 runs as today. It adds no labels for this
   track and gains no permissions.
2. **Summarize.** A leaf with no tools beyond the issue text it is given produces the
   **bug record**'s summary (spec `bug-record/v1`): a plain-language description and
   impact, expected and observed behaviour, the area and the conditions to trigger it, in
   its own words, under the spec's sanitization rules. Every later stage reads the record,
   never the issue.
3. **Reproduce.** A leaf writes a test from the summary. The test file MUST lie in one of
   the instance's configured test directories; the command that runs it comes from
   `pdca.toml`, not from the leaf. The driver takes the test's bytes and nothing else.
4. **Verify.** The driver runs the recorded test against the recorded commit of each
   candidate ref (section 4), N times each (section 9), in verify containers (ADR-0021 §1),
   and writes the outcome itself. Red on the ref the report names, or on `main` when it
   names none, is the red leg of the C4 gate (ADR-0006), produced before any fix.
   Otherwise the track stops and posts the open questions to the issue for the reporter.
5. **Fix.** The builder gets the record and the test, never the issue, and returns a diff.
   The driver verifies the diff the same way: the test green N times on the recorded base
   plus the diff, in a fresh verify container. The diff may not touch any file under the
   instance's test directories (the test itself, or a helper it depends on), or the C4 row
   is unverifiable and goes to §6 under C6: a fix changes source, not tests.
6. **Review.** The reviewer (ADR-0005) adjudicates from the verify logs; it does not
   re-execute them for bug bundles (ADR-0021 §1).
7. **Sign-off: the bundle pull request.** The driver opens a draft pull request on the
   pdca repository with the bundle (section 7). Merging it is the accept.
8. **Publish.** After accept, the regular publish: a `bugfix/<n>` branch and a draft pull
   request on the target repository, pushed by the driver as code (section 7).

Failing tests from an instance's own scheduled runs (a simulation seed sweep, a nightly
conformance run) enter at stage 4: they are reproduced already and were not written by a
stranger.

### 2. Trigger, batches and state

The track is started by polling, not by repository events. A workflow engine on the
**workflow host** (the driver container of ADR-0021 §4) runs the track's poll on a schedule
set per instance; daily is enough, since a bug report does not need an answer within
minutes. Any engine that runs a command on a schedule will do.

A poll run is the start of a **batch**:

- it lists the open issues that carry `bug`, a `priority:*` label and, for issues filed by
  anyone but the instance's own scheduled runs, the **`track:go`** label a maintainer sets.
  Requiring a priority means ADR-0017's triage has classified the issue; requiring
  `track:go` keeps the decision to spend on a stranger's issue a human's, one click per
  bug. An instance MAY turn `track:go` off once the pilot has shown the spend is bounded
  without it;
- it fetches the pdca repository first, so merged and closed bundle pull requests
  (section 7) are seen before anything new starts;
- it collects finished jobs, reclaims expired ones, then starts at most a configured
  number of new Summarize jobs. If nothing is eligible, no runner is created;
- it creates one runner (ADR-0021 §2) and drives the stages for every issue in the batch
  until the queue is empty or intake closes;
- it keeps each issue's progress in labels (`track:bug` and one stage label), so the state
  is visible in the tracker, and in the bug record, which is the authoritative state when
  the two disagree. Labels are the maintainer's switches (section 11); the record is what
  the driver believes.

We reject a GitHub workflow triggered by `issues` events: it runs in a context any issue
author can trigger, next to repository tokens, and would race ADR-0017's triage. We reject
a webhook: it needs an endpoint on the workflow host that accepts inbound traffic.

### 3. What may start without a maintainer

- **Summarize, Reproduce and Verify start on their own** for eligible issues. They publish
  no code: a comment on the issue with the result, and the test as a file in the bundle.
- **Fix starts automatically** when the test is red, the issue has no `needs-triage`, and
  neither the test file nor, once known, the diff touches a protected path (section 5).
  Otherwise the track stops with the test attached, opens the bundle pull request with the
  test only and the label `track:needs-plan`, and waits for the maintainer to decide how
  the bug is fixed.
- **Nothing reaches the target repository without a merged bundle pull request.** An
  automatically started fix still ends at a human's merge. The milestone is never set by
  the track.

### 4. Target ref

A bug is found in something that was built and fixed in something that will ship, so the
track fixes against a ref, never a milestone. The candidate refs are:

- `main`, the default;
- a **release branch** (`release/X.Y`) for each release the instance declares supported in
  `pdca.toml`, created when that release ships, not when its milestone opens.

Integration lines (§8.8) are run-scoped and rewritten by the next run, so they are not
candidates; a bug in milestone work not yet on `main` is reproduced when it lands there.

The driver verifies the test on every candidate ref and chooses mechanically:

- **Red on `main`:** the fix is built on `main`. Each supported release branch where the
  test is also red then gets a backport, as its own cycle with the same test as its red
  leg.
- **Red on a release branch, green on `main`:** the bug is already fixed. The driver
  bisects `main` for the commit that turned the test green, within one job limit and
  treating `error` outcomes as unknown, and backports that commit. If bisection does not
  finish, the fix is built on the release branch instead.
- **Red only on a release that is no longer supported:** no fix is built; the issue is
  answered with the first supported release in which the test is green.

### 5. Protected paths

Each instance lists in `pdca.toml` the paths whose bugs always need a maintainer's go before
a fix is built: typically its on-disk formats and specifications, its commit protocol, and
whatever its own accepted decisions declare protected; the list is the instance's, not the
harness's. Matching is deterministic on two inputs only: the path of the reproduced test
file, and the paths the diff touches once a diff exists. Both are files, not model
judgement; the summary's `areas` are informational and never route. A fix whose diff reaches
a protected path is held as a NEEDS-HUMAN item even when the test did not.

### 6. Budgets and delegation

Every job has the same wall-clock limit, the runner's drain window (3 h by default,
ADR-0021 §2), and token budgets per leaf. The per-issue budget is counted in jobs: one
Summarize, one Reproduce per attempt (section 11), one Fix with no automatic iteration
round, one backport per supported release. A Fix job that does not turn the test green
within its limit is not treated as a bug fix: the bundle pull request opens with the test
and the partial diff, labelled `track:needs-plan`, for the issue to go through Plan like
milestone work. A test that exists is the asset; the fix is optional.

### 7. Sign-off and publish

**The bundle pull request is the accept.** When a track has a verified test (and a
verified diff, if Fix ran), the driver commits the bundle to a branch `bug/<n>` of the pdca
repository and opens a draft pull request:

- the body carries the README's description and impact, the verdicts, the review findings
  and the §6 NEEDS-HUMAN items as a checklist;
- a required status check reads the record and stays red until a maintainer has ticked
  every §6 item: this is the C6 guard, enforced by the tracker;
- the bundle is data in a directory; the pdca repository's CI compiles and runs none of it;
- **merging the pull request is the §9 decision.** The driver records `Outcome: accepted`
  from the merge event at the next batch, into `SUMMARY.md` and the record; closing it
  unmerged records `rejected`. Code writes §9, never a model, so ADR-0004 holds as written.
  `pdca signoff` remains available as the interactive path and ends in the same §9.

**Publish follows accept, unchanged in order and changed in means.** The driver, as code,
applies the recorded diff to the recorded base commit, commits with the text the publisher
leaf wrote into the bundle, pushes `bugfix/<n>` and opens the draft pull request on the
target repository; Codex review and the maintainer's ready-mark and merge follow as for every
cycle. The target's CI runs the code only after a human accepted those exact bytes. The
credential that pushes is scoped to `bugfix/*` and lives in the driver container (ADR-0021
§4), or publish runs from the maintainer's machine if the operator prefers no target
credential there. `pdca try` is refused for bug bundles; the verify log and the reviewer's
adjudication are what the maintainer signs off on, and a bug the maintainer wants to run by
hand is a NEEDS-HUMAN item.

### 8. Bundles and publication

Bug bundles are ordinary bundles under the grouped layout of #856: `results/<group>/`,
grouped by the issue's milestone at creation or `unscheduled`, with ids `issue_<n>` for the
fix on `main` and `issue_<n>-r<ref>` for each backport (whose trailer is `Refs #<n>`, so
only the main fix closes the issue). The bug record, its MAC and a README rendered from the
record sit in the `issue_<n>` bundle. A track that ends without a fix (not reproduced,
flaky, duplicate, answered, delegated, withdrawn) needs no decision, so the driver commits
its bundle directly, as `pdca record` does today.

Everything the track keeps is written to be published: the pdca repository and the target
repository may both be public.

- **Security bugs never enter the track.** A reproducing test and a patch published before a
  release are an exploit with instructions. The poller skips any issue carrying the
  vulnerability mark ADR-0017's triage sets (#852), and such reports go through the target
  repository's private vulnerability reporting, which `doctor` requires to be enabled. The
  mark is a model's judgement over hostile text and will miss cases; it is a filter, not a
  guarantee, and the maintainer's `track:go` is the second filter. If the mark appears after
  a bundle exists, nothing further of that track is committed or pushed.
- **Contents are allowlisted.** A bug bundle holds only: the README, the record and its MAC,
  and the files of a normal bundle that are already published today (brief, patch, test,
  review findings, gate verdicts, `SUMMARY.md`, token rows). Leaf transcripts, in particular
  Summarize's, which contains the issue text, runner logs, job specifications and signed
  URLs never enter a bundle; they stay in the driver container or die with the runner.
- **A redaction check runs before every commit and push.** The driver refuses to commit or
  push a bug bundle that contains a string matching a secret pattern or an entry of the
  instance's list of internal identifiers (host names, absolute paths of the workflow host,
  cloud project IDs). A match is a NEEDS-HUMAN item, never a silent scrub.

### 9. Reliable reds

A test that fails only some of the time is not evidence. Verify runs a test `runs` times
per ref (default 5, set per instance), each run on a fresh copy of the tree, and records the
counts. The test is **red** on a ref only if every run fails, **green** only if every run
passes, **flaky** otherwise. A flaky result on the ref that matters stops the track with a
NEEDS-HUMAN item and the test attached; on another candidate ref it removes that ref. After
a fix, the C4 row requires green on every one of the same number of runs; anything less is
not a pass, and the row goes to §6 under C6 as today's confirm-once rule does for a
once-failing gate. A deterministic-simulation seed runs once, since the same seed gives the
same result.

### 10. Duplicates

The gate reports where a failing test fails with a marker line, `PDCA-FAILURE-AT:
<path>:<line>`, in the marker protocol of `PDCA-UNVERIFIABLE:`. Two open tracks with the
same location on the same ref are **linked** in their READMEs and in a comment on the newer
issue. A link never stops a track: simulation seeds and property tests fail at shared
invariant checkers, so a shared location does not mean one bug, and the location is printed
by the test itself. Closing one issue as a duplicate of another is the maintainer's call.

### 11. Issue lifecycle

The track follows the issue, but never lets a change to it restart work silently.

- **Edits.** The record keeps a digest of the issue text Summarize read (title, body and the
  reporter's comments it was given). An edit after that does not change the record or
  restart a stage. While the track waits on the reporter, an edit counts as an answer.
- **Answers.** After "not reproduced", the record stays open and the track waits: an edit,
  or a comment by the reporter after the track's questions, lets the next batch run
  Summarize and Reproduce once more, on the issue and the reporter's comments since the
  questions. The number of attempts is limited (default 2, set per instance); after the
  last, the record is closed as not reproduced.
- **Closure and triage.** An issue that is closed ends its track as withdrawn. An issue that
  gets `needs-triage`, or loses `bug`, its priority or `track:go`, pauses the track until it
  has them again. When a record closes, the driver removes `track:go`; reopening the issue
  starts a new record only once a maintainer sets it again, so a reporter cannot restart
  the track by closing and reopening.
- **The maintainer's switches.** `track:paused` stops the track for one issue until it is
  removed; removing `track:bug` ends it as handed over. An instance setting turns the whole
  track off; the poller then starts nothing new and lets the running batch end.
- **Hand-over.** Setting a milestone on an issue whose track is held starts an ordinary
  cycle under the maintainer, as ADR-0017 provides; the test stays in the bundle for it.

Every ending (accepted, rejected, answered, not reproduced, flaky, delegated, withdrawn,
handed over) is written to the record and rendered into the bundle's README.

### 12. Claims and retries

A batch runs for hours and a runner can disappear mid-job, so the drive claims of
`process/.drive-claims/` (user documentation, chapter 07), which last only as long as the
process holding them, are not enough on their own.

- For each issue and stage, the driver records a **job claim** under `process/` in the
  driver container: the job's handle, when it started, and a lease of one job limit plus a
  margin. Job claims are never committed. They are the same kind of scratch as the drive
  claims ADR-0003 already allows beside the bundles: the state of a bug is in its record and
  its bundle, and a lost `process/` directory loses at most the knowledge of which jobs are
  running, which the next batch rediscovers from the runner or lets expire.
- A batch first collects finished jobs, then reclaims jobs whose lease has expired, and only
  then starts new ones. It never starts a stage for an issue that holds a live claim for it.
- A stage is retried in at most two later batches for infrastructure reasons (a lost
  runner, a container error). After that the track stops with a NEEDS-HUMAN item.
  Infrastructure failures are not attempts in the sense of section 11.

### 13. Telemetry

Each batch records, per instance: how many bugs reproduced, how many were flaky or linked,
how many fixes were verified green, how many bundle pull requests the maintainer merged and
how many they closed, how many fixes were later reverted, how many issues were delegated
with `track:needs-plan`, and the runner hours spent. The delegation count shows whether the
job limit and the protected list sit in the right place.

### Rejected

- **Letting a fix leaf read the issue.** A summary loses detail, but it cuts the path from a
  stranger's text to a leaf that runs `Bash` on the target project.
- **Classifying by expected diff size.** It is not known before the fix and misjudges
  exactly the bugs that matter.
- **A separate triage for the track.** ADR-0017 already classifies every issue; a second
  classifier would disagree with it.
- **Trusting anything a produce job writes besides its bytes.** Gate output, counts and
  signatures from the job's own container are what #854 shows to be forgeable.
- **Publishing the fix before accept, from a bot-owned fork.** It would keep the target's
  CI from running stranger code unapproved, but it moves the accept out of the harness and
  needs a bot identity per target; the bundle pull request gives the same human decision
  without either.
- **Auto-closing duplicates by failure location.** It lets a deliberately failing test
  suppress a genuine report.

## Consequences

Bugs get a verified red test and, where the model manages it, a verified fix, without
waiting for a milestone, and nothing reaches the target repository that a maintainer has
not accepted in a pull request they can review from anywhere. The test that blocks sign-off
is written by one leaf, verified by no leaf, and the fix by another: this closes a gap
ADR-0006 leaves open for ordinary cycles, a builder writing a test that passes because it
was written to.

Bugs that do not reproduce mechanically are never fixed by the track: environment-specific
failures, races the instance's tests cannot capture, reports too vague to act on. That is
the class where a plausible but wrong fix does most damage, so the cost is accepted. Bugs
whose fix needs more than one job go to Plan with a red test already written.

The accept moves, for bug bundles, from the interactive sign-off session to a pull request
review, which is a thinner ritual; the pull request body has to carry what the session would
have shown. ADR-0004's rule is unchanged, and so is ADR-0006's: verify is the C4 gate, run
by the driver on the engine's containers, and a test the diff edits makes the row
unverifiable rather than adding a blocking gate.

`track:go` costs the maintainer one click per public bug during the pilot and removes the
class of attacks that start with a flood of issues; the phase-3 review of proposal 0003
decides whether it stays.

Every bug leaves a readable entry under its milestone; because everything the track keeps is
publishable by construction, an instance can record bug bundles in a public repository
without a review pass for leaks, as long as the allowlist and the redaction check are kept
current when the bundle gains a new kind of file.

Verify multiplies runner time by `runs` per candidate ref and adds a container per verdict;
it is the price of evidence that means something.

Follow-on work, sequenced in proposal 0003: the `pdca.toml` keys (test directories, runner
command, protected paths, supported releases, `runs`, attempts, the poll schedule, the
per-batch limit, the job limit, the redaction list), the poll and batch commands, the
`track:*` labels and the required check for the bundle pull request, the summarize and
reproduce roles, the verify job and its parser, the `PDCA-FAILURE-AT:` marker, the README
renderer, the allowlist and redaction check, `doctor` checks (private vulnerability
reporting, the job-limit inequality, the labels), the driver image under `deploy/driver/`,
and the visible vulnerability mark in ADR-0017's triage (#852). The grouped bundle layout is
#856. The `pdca.toml` keys move behind the `Policy` protocol if ADR-0013 lands as planned.

Left open:

- **[OPEN]** Where the list of supported releases lives and who declares one: release
  branches only become candidates once an instance declares them.
- **[OPEN]** Whether Summarize can be the same model call as ADR-0017's classification, or
  must stay separate because that bot is repository tooling not rendered into instances.
- **[OPEN]** Whether the required check on the bundle pull request reads the record from
  the branch or from a status the driver posts; the former needs no credential, the latter
  no code in the pdca repository's CI.
