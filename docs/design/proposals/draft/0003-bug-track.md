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

ADR-0020 decides what the bug track does, ADR-0021 where its jobs run and what the driver
holds, and the `bug-record/v1` spec what passes between its stages. None of them says in
which order to build it, what can ship before the migration steps it depends on (the
`LeafRunner` seam, step 2, #711; the grouped bundle layout, #856), or how to know each part
works. This proposal does.

The order follows three rules. Nothing that runs a leaf on stranger-shaped input ships
before it can run in a container on a runner that holds no credential. No verdict is
produced anywhere but in a verify container, from the first day a verdict exists. And the
track meets real public issues only after it has run on inputs nobody outside wrote: an
instance's own failing simulation seeds.

## Design

Four phases. Each ends with something an instance can turn on. ADR-0020, ADR-0021 and the
spec (as draft) must be Accepted before phase 1 starts; phase 0 may proceed while they are
Proposed, because nothing in it executes a stranger's code.

### Phase 0 — record, intake and the driver container (no runner)

1. **Triage mark.** ADR-0017's bot sets a visible label for a possible undisclosed
   vulnerability (#852).
2. **Bug record module.** A stdlib-only reader, validator and writer for `bug-record/v1`
   (ADR-0008): structure, ownership, leaf-output files, sanitization of summary and
   questions, and the detached HMAC. Its tests run the spec's conformance vectors.
3. **Configuration.** The `pdca.toml` keys of the track: on/off, poll schedule, per-batch
   limit, `track:go` required or not, job limit, intake cutoff and lifetime, `runs`,
   attempts, test directories, the runner command per test kind, protected paths,
   supported releases, the redaction list, the MAC key's location. They move behind the
   `Policy` protocol when step 5 (#714) lands.
4. **`doctor` checks.** Private vulnerability reporting enabled on the target repository;
   `job_limit ≤ lifetime − intake_cutoff`; the `track:*` labels present; the MAC key present
   and readable only by the driver's user.
5. **Poll and batch commands.** Eligibility (labels, `track:go`, the vulnerability mark,
   `track:paused`), label state, the per-batch limit, job claims under `process/` with
   leases, reclaiming and retry counting (ADR-0020 §2, §11, §12). In this phase a batch
   runs Summarize only, in the existing OS sandbox for read-only leaves on the driver's
   host, since Summarize executes nothing; it moves to the runner in phase 1.
6. **Summarize.** A role (data, per ADR-0011 once it exists; a prompt file until then)
   with no tools beyond the issue text it is given. Output through `summarize.out.json`
   only.
7. **Bundles.** The README renderer, the allowlist, the redaction check, the record and
   MAC beside the bundle, under the grouped layout once #856 lands and under `results/`
   before.
8. **Driver image.** `deploy/driver/` rendered into instances: a container image of the
   rendered instance plus the driver, started by a schedule, reporting back by webhook. No
   leaf runs in it.

At the end of phase 0 an instance can run the poller in its driver container and get a
sanitized, MAC-protected record and a readable README for each eligible bug, and nothing
else.

### Phase 1 — reproduction and verification on the runner (after step 2, #711)

1. **Runner backend.** The one-runner-per-batch `LeafRunner` of ADR-0021 for Google Cloud,
   talking to the provider through `gcloud`; the reference infrastructure in
   `deploy/bug-track/gcp/`; the runner image (Packer) with the job agent, gVisor and
   rootless Podman; the dependency-cache job on the target's `main`.
2. **Job kinds.** Produce containers (gVisor, egress to the model endpoint only) and verify
   containers (plain, no network, `git archive` export, fresh tree per run); the fixed
   member list of result bundles and the driver's filtered extraction.
3. **Reproduce.** The role, on the runner; the test-directory rule; the driver takes the
   test bytes by hash.
4. **Verify.** The verify job for the red leg: `runs` per candidate ref, the
   red / green / flaky rule, the `PDCA-FAILURE-AT:` marker and its parser, `check-gates.json`
   and counts written by the driver (ADR-0020 §9, ADR-0021 §1).
5. **Candidate refs.** `main` and declared release branches; bisection within one job
   limit; the target decision (§4).
6. **Links and answers.** Signature links between open tracks; the reporter-answer loop
   with its attempt limit; the taint rule for a batch with an anomaly.
7. **Summarize moves to the runner.**

At the end of phase 1 every eligible bug has a verified red test or a verified reason it
has none, a target ref and a routing decision, and the bundle pull request opens with the
test only (ADR-0020 §7); no fix is built yet.

### Phase 2 — the fix, the bundle pull request and publish

1. **Fix.** The builder as a produce job with the record and the test; the diff taken by
   hash; verify of base plus diff, green on every run; the test-file-unchanged rule as an
   unverifiable C4 row.
2. **Review.** The reviewer as a produce job adjudicating from the verify log (#403 path),
   no re-execution.
3. **Bundle pull request.** Branch `bug/<n>` on the pdca repository, the body rendered from
   the record (description, impact, verdicts, findings, §6 checklist), the required check
   that reads the record and the checklist; `Outcome: accepted` and `rejected` written by
   the driver from the pull request's state at the next batch.
4. **Publish as code.** Apply the recorded diff to the recorded base, commit with the
   publisher leaf's text, push `bugfix/<n>`, open the draft pull request on the target;
   `pdca try` refused for bug bundles. Publish runs in the driver container or, by
   configuration, only from a maintainer's checkout.
5. **Backports.** One cycle per supported release branch, with the same test, id
   `issue_<n>-r<ref>`.
6. **Delegation.** `track:needs-plan` on budget exhaustion and on protected paths; the
   bundle pull request with the test only.

### Phase 3 — pilot and widening

1. **Internal sources only.** On getwyrd/wyrd-pdca, the track takes only failing tests from
   the instance's own scheduled runs (`source.kind` `"scheduled-run"`): no stranger wrote
   any of the input. Cold and warm build times are measured here, before the job limit is
   treated as a budget.
2. **Public issues, `track:go` required.** The poller is opened to public issues the
   maintainer has marked, with automatic Fix allowed only outside protected paths.
3. **Review.** After the first 20 bugs, or two months, whichever comes first, the telemetry
   of ADR-0020 §13 is reviewed: reproduction rate, flaky and linked counts, bundle pull
   requests merged and closed, reverts, delegations, runner hours. Budgets, `runs`, the
   protected list and whether `track:go` stays are decided from it.

Once the pattern holds on Wyrd, other instances turn the track on through `copier update`
and their own configuration.

## Alternatives considered

- **Build the whole track before shipping any of it.** Rejected: phase 0 alone gives
  readable, safe bug records and exercises the poller, the labels and the recording rules
  on real issues with nothing executing.
- **Run Reproduce or Verify on the driver's host until step 2 lands.** Rejected: it would
  run stranger-shaped code where credentials live, which ADR-0021 exists to prevent.
- **Pilot on public issues directly.** Rejected: the first runs should test the machinery,
  not the sanitization, on inputs that are hostile by assumption.
- **Let bug cycles take precedence over milestone waves.** Rejected: the milestone carries
  the planned work. A bug cycle whose published pull request conflicts with `main` goes
  back to the track for a re-verify on the new base in the next batch; it never holds up a
  wave.
- **A separate repository for the track.** Rejected for now: the track reuses the harness's
  roles, gates, records and publish, and a second repository with its own governance and
  release cadence would cost more than the seam it would buy. The provider adapter and the
  driver image keep the parts an outside repository would want portable.

## Graduation criteria

- **Phase 0.** The record module passes every conformance vector (valid, invalid,
  sanitization, integrity), in the offline suite and in CI. The poller's tests cover:
  skipped issues (marked, paused, `needs-triage`, no priority, no `track:go`), the
  per-batch limit, and a batch killed mid-run that resumes without starting a stage twice
  (quality scenario Q4). The bundle tests show that a file outside the allowlist, and a
  planted secret or internal identifier, block a commit with a NEEDS-HUMAN item. The
  driver image builds from a rendered instance and runs a batch against a stub tracker.
- **Phase 1.** On the runner: a produce job that exits without its output file fails the
  stage (Q7); a result bundle with an unexpected member is refused; a job that attempts a
  connection outside the allowlist taints the batch; a test that reads refs sees none
  (`git archive` export); a flaky test is classified flaky, not red; a shared signature
  links and does not stop; runner tokens and hours are recorded per job (Q6); a runner is
  deleted at its lifetime whether or not the driver is alive.
- **Phase 2.** A diff that edits the test file yields an unverifiable C4 row, not a pass; a
  forged `check-gates.json` inside a produce container has no effect on the recorded
  verdict (#854); the required check on a bundle pull request stays red with one unticked
  §6 item (Q2); merging writes `accepted` and closing writes `rejected` (Q1); publish pushes
  exactly the recorded bytes; `pdca try` on a bug bundle is refused; a Fix exceeding its
  limit opens the bundle pull request with `track:needs-plan`.
- **Phase 3.** The review above is done and its adjustments recorded in an Act report.

Done means all four phases graduated, the spec stamped stable, and the living architecture
updated: §5 building blocks, §7 deployment (the driver container and the runner backend),
§8.10 security (the path from an issue to a container, and the produce / verify / decide
rule), §11 risks, and the glossary (bug track, bug record, batch, runner, workflow host).

## Security impact

The track adds one untrusted input path: issue text from anyone, reaching the harness on a
schedule rather than when a maintainer picks an issue. Architecture §8.10 already treats
tracker text as hostile. What keeps it contained is decided in ADR-0020 (record-only
downstream, `track:go`, protected paths, vulnerabilities kept out, the bundle pull request
as the accept), ADR-0021 (produce / verify / decide, credential-free containers, a driver
container with scoped credentials) and the spec (ownership, sanitization as hygiene, MAC).
Three weaknesses of the current cycle that the track would otherwise inherit are filed
against the harness independently: #853, #854, #855. The repository has no separate
threat-model file; §8.10 is updated in phase 2, when the path exists.

## Backward compatibility

- **Existing data.** None: bug bundles are new bundles; existing bundles are untouched.
- **Configuration.** The track is off unless an instance turns it on; all new keys have
  defaults that keep it off.
- **Instances.** Delivered through `copier update`. Instances that do not use the track see
  new, unused configuration keys, commands and a `deploy/driver/` directory, nothing else.
- **Records across upgrades.** A record written by one driver release is read or refused by
  the next per the spec's versioning rules, so a track open across an update either
  finishes or stops with a NEEDS-HUMAN item.
- **Accept path.** For bug bundles the accept is the bundle pull request's merge; `pdca
  signoff` keeps working on them and on every other bundle as today.

## Open questions

- **[OPEN]** Whether phase 0 waits for step 1 (#712) and #856, so that bug bundles use the
  bundle-format records and the grouped layout from the start, or ships first and migrates.
- **[OPEN]** Where the MAC key lives in the driver container and how it is rotated, beyond
  "readable only by the driver's user".
- **[OPEN]** Whether the phase-3 thresholds (20 bugs, two months) suit instances with far
  fewer bug reports than Wyrd.
- **[OPEN]** Whether the required check on the bundle pull request is a workflow in the
  pdca repository that reads the branch, or a status the driver posts (ADR-0020 open item).
