---
created: 31.10.2026 18:45
type: proposal
status: draft
author: Eduard Ralph
tracking-issue: https://github.com/eduralph/pdca-harness/issues/850
tags:
  - proposal
  - triage
  - security
---
# Proposal: Bug track — implementation plan

> Enhancement proposals (KEP/RFC-style) are how larger changes and milestone plans are
> proposed. Move this file from `draft/` to `accepted/` when accepted; an accepted proposal
> is never edited — a later proposal supersedes it, recorded in `../proposals/README.md`.
> If a decision is foundational rather than a change, it may instead warrant an ADR.

## Motivation

ADR-0020 decides what the bug track does, ADR-0021 where its untrusted leaves run, and the
`bug-record/v1` spec what passes between its stages. None of them says in which order to
build it, what can ship before the migration step it depends on (the `LeafRunner` seam,
step 2, #711), or how to know each part works. This proposal does.

The order follows two rules. Nothing that runs a leaf on stranger-shaped input with `Bash`
ships before it can run on a runner with no credentials. And the track meets real public
issues only after it has run on inputs nobody outside wrote: an instance's own failing
simulation seeds.

## Design

Four phases. Each ends with something an instance can turn on.

### Phase 0 — record, intake and bookkeeping (no runner needed)

Nothing in this phase executes code from a bug report.

1. **Triage mark.** ADR-0017's bot sets a visible label for a possible undisclosed
   vulnerability (#852).
2. **Bug record module.** A stdlib-only reader, validator and writer for
   `bug-record/v1` (ADR-0008): structure, ownership, sanitization of summary and
   questions, and the detached HMAC. Its tests run the spec's conformance vectors.
3. **Configuration.** The `pdca.toml` keys for the track: on/off, poll limit per run,
   `runs`, attempts, budgets, protected paths, `bug_root`, the list of internal
   identifiers for the redaction check, and the MAC key's location on the workflow host.
   They move behind the `Policy` protocol when step 5 (#714) lands.
4. **`doctor` checks.** Private vulnerability reporting enabled on the target repository;
   the MAC key present and readable only by the driver's user; the triage labels present.
5. **Poll command.** Lists eligible issues read-only, skips marked, paused and
   needs-triage ones, keeps state in labels, applies the per-run limit, and records job
   claims under `process/` (ADR-0020 sections 2, 11, 12). In this phase it starts only
   Summarize.
6. **Summarize.** A role (data, per ADR-0011) with no tools beyond the issue text it is
   given, run in the existing OS sandbox for read-only leaves. Output through
   `summarize.out.json` only.
7. **Recording.** The `bug_root` layout, the README renderer, the allowlist and the
   redaction check in `pdca record` and publish.

At the end of phase 0 an instance can run the poller and get a sanitized, MAC-protected
record and a readable README for each bug, and nothing else.

### Phase 1 — reproduction on a runner (after step 2, #711)

1. **Runner backend.** The ephemeral-VM `LeafRunner` of ADR-0021 for Google Cloud, talking
   to the provider through `gcloud`; the reference infrastructure in
   `deploy/bug-track/gcp/`.
2. **Runner image.** A Packer template and the `run-job` entry point implementing the job
   contract in `deploy/bug-track/gcp/compute.tf`: read the job from metadata, run the
   stage, upload one result bundle to the signed URL, power off.
3. **Reproduce.** The role, running on the runner; the `PDCA-FAILURE-AT:` marker in the
   instance's gate scripts; repeated runs and the red / green / flaky rule (ADR-0020
   section 9).
4. **Candidate refs.** Running the test on `main`, supported release branches and open
   integration lines; bisection for an existing fix; the target decision (section 4).
5. **Duplicates and answers.** Signature comparison against open tracks; the
   reporter-answer loop with its attempt limit.

At the end of phase 1 every reproduced bug has a red test, a target ref and a routing
decision, posted to the issue, and no fix is built yet.

### Phase 2 — the fix cycle

1. **Brief from the record.** A bug cycle's brief is rendered from the record; the builder
   sees the summary and the test, never the issue.
2. **Gate.** The test-unchanged check against `reproduction.test.sha256`, and the C4 gate
   requiring green on every run.
3. **Cycles without a milestone.** The driver can run a bug cycle that has no milestone,
   in its own lane, labelled `track:bug`.
4. **Conflicts with milestone work.** When a bug cycle's pull request conflicts with a
   milestone wave touching the same files, the milestone's work goes first: the bug cycle
   is rebased onto the wave's integration line (or onto `main` once the wave lands) and
   its gates run again. A bug cycle never holds up a wave.
5. **Backports.** One cycle per supported release branch, with the same test.

### Phase 3 — pilot and widening

1. **Internal sources only.** On getwyrd/wyrd-pdca, the track takes only failing tests
   from the instance's own scheduled runs (`source.kind` `"scheduled-run"`): no stranger
   wrote any of the input.
2. **Public issues, protected paths held.** The poller is opened to public issues, with
   automatic cycles allowed only outside protected paths.
3. **Review.** After the first 20 bugs, or two months, whichever comes first, the
   telemetry of ADR-0020 section 13 is reviewed: reproduction rate, flaky and duplicate
   counts, accepted and reverted fixes, budget overruns. Budgets, `runs` and the protected
   list are adjusted from it.

Once the pattern holds on Wyrd, other instances turn the track on through `copier update`
and their own configuration.

## Alternatives considered

- **Build the whole track before shipping any of it.** Rejected: phase 0 alone gives
  readable, safe bug records and exercises the poller, the labels and the recording rules
  on real issues with nothing executing.
- **Run Reproduce locally until step 2 lands.** Rejected: it would run stranger-shaped
  `Bash` on the workflow host, which ADR-0021 exists to prevent.
- **Pilot on public issues directly.** Rejected: the first runs should test the machinery,
  not the sanitization, on inputs that are hostile by assumption.
- **Let bug cycles take precedence over milestone waves.** Rejected: the milestone carries
  the planned work, and a stream of bug fixes would otherwise keep rebasing it.

## Graduation criteria

- **Phase 0.** The record module passes every conformance vector (valid, invalid,
  sanitization, integrity), in the offline suite and in CI. The poller's tests cover:
  skipped issues (marked, paused, needs-triage, no priority), the per-run limit, and a
  poll killed mid-run that resumes without starting a stage twice (quality scenario Q4).
  The recording tests show that a file outside the allowlist, and a planted secret or
  internal identifier, block a commit with a NEEDS-HUMAN item.
- **Phase 1.** On the runner: a job that exits without its result file fails the stage
  (Q7); a VM deleted mid-job is retried and then stops the track after two failures; a
  flaky test is classified flaky, not red; a duplicate is linked, not rebuilt. Runner
  token use is recorded per job (Q6).
- **Phase 2.** A builder that edits the reproduced test is refused at the gate; a bug
  cycle that conflicts with a milestone wave is rebased and re-gated; a fix exceeding its
  budget is held. Sign-off and C6 behave as for every cycle (Q1, Q2).
- **Phase 3.** The review above is done and its adjustments recorded in an Act report.

Done means all four phases graduated, ADR-0020, ADR-0021 and the spec accepted (the spec
stamped stable), and the living architecture updated: §5 building blocks, §7 deployment
(the workflow host and the runner backend), §8.10 security (the path from an issue to a
runner), §11 risks, and the glossary (bug track, bug record, workflow host).

## Security impact

The track adds one untrusted input path: issue text from anyone, reaching the harness
without a maintainer in between. Architecture §8.10 already treats tracker text as hostile;
this path makes it reachable on a schedule rather than when a maintainer picks an issue.
What keeps it contained is decided in ADR-0020 (summary-only downstream, no write
credentials in leaves, protected paths, vulnerabilities kept out), ADR-0021 (credential-free
ephemeral runners) and the spec (sanitization, ownership, MAC). The repository has no
separate threat-model file; §8.10 is updated in phase 2, when the path exists.

## Backward compatibility

- **Existing data.** None: bug bundles live under a new `bug_root` and existing bundles
  are untouched.
- **Configuration.** The track is off unless an instance turns it on; all new keys have
  defaults that keep it off.
- **Instances.** Delivered through `copier update`. Instances that do not use the track see
  new, unused configuration keys and commands, nothing else.
- **Records across upgrades.** A record written by one driver release is read or refused by
  the next per the spec's versioning rules, so a track open across an update either
  finishes or stops with a NEEDS-HUMAN item.

## Open questions

- **[OPEN]** Whether phase 0 should wait for step 1 (#712), so that bug bundles use the
  bundle-format records from the start, or ship first and be migrated.
- **[OPEN]** Where the MAC key lives on the workflow host and how it is rotated, beyond
  "readable only by the driver's user".
- **[OPEN]** Whether the phase-3 review thresholds (20 bugs, two months) suit instances
  with far fewer bug reports than Wyrd.
