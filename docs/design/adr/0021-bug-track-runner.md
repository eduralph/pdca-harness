---
created: 31.10.2026 17:55
type: adr
status: Proposed
tags:
  - adr
  - security
  - deployment
---
# 0021. Bug-track jobs run in containers on one disposable runner per batch; the driver runs in an operator's container and holds no leaf credential

## Context

The bug track (ADR-0020) runs leaves on issues anyone can file. Reproduce writes a test
shaped by a stranger's report, the builder runs `Bash` against the target project, and the
C4 gate executes both. Architecture §8.10 treats a leaf as able to leave any file in any
state, and the claude builder is not confined to its worktree today (#710). Three weaknesses
of the current cycle matter here and exist independently of the track: the publisher is a
model leaf holding the operator's repository credentials (#853); the state machine reads
evidence from files a leaf or a build script can write (#854); the reviewer re-executes
bundle code on the host inside a sandbox that confines writes, not reads (#855).

A place to run the track's jobs needs:

- **a fresh environment per job**, so nothing one job leaves behind reaches the next, and
  **a fresh kernel often enough** that a container escape does not become a foothold;
- **no credential on the job's side** that can write to a repository, spend without a
  limit, or read another job's data;
- **evidence that the job cannot forge**: whatever decides red or green must not run where
  the stranger's code ran;
- **many cores**, because instance test suites, Rust builds and simulation seed sweeps are
  CPU-bound;
- **the target project's production architecture.** In getwyrd/wyrd, erasure coding takes
  different SIMD paths on x86_64 and aarch64, so a bug reported on one may not reproduce on
  the other.

The options weighed for where the jobs run:

| Option | Verdict |
|---|---|
| Machines that also serve personal use | Rejected: personal credentials and sessions on the same machine |
| Small Arm boards and other low-power machines | Rejected as runners: the wrong architecture, or too slow for builds and seed sweeps |
| One virtual machine per job | Rejected: minutes of provisioning per job, one spot interruption per job, and a provisioning lottery on every job when a provider rations capacity |
| GitHub-hosted runners | Rejected: workflows triggered by issues sit next to repository tokens, and keeping the publisher apart from the job inside Actions is hard |
| Hetzner Cloud | Rejected for now: provisioning of new cloud servers has been limited since the capacity incident of 26 June 2026 (status.hetzner.com), and dedicated-vCPU lines were repriced in June 2026 |
| A self-hosted server | Deferred: needs hardware and lead time, and its size cannot be chosen before the load is known |
| OVHcloud, STACKIT | Viable; candidates for later backends |
| Google Cloud, one spot VM per batch, containers per job | Chosen |

## Decision

### 1. Three roles, three places

Every job of the track is one of three kinds, and the kinds never share a place:

| Role | What runs | Where | What it may produce |
|---|---|---|---|
| **Produce** | Summarize, Reproduce, the builder, the reviewer (model leaves) | a container on the runner, under gVisor | bytes: a summary, a test file, a diff, review findings |
| **Verify** | the recorded test against a recorded tree, N times | a container on the runner, plain rootless Podman, no network | exit codes and captured output |
| **Decide** | the driver: validation, verdicts, routing, records, bundle PRs | the driver container (section 4) | every file the state machine reads |

Nothing a produce job writes is evidence. The driver takes from it exactly the bytes its
stage owns (spec `bug-record/v1` §7 and §9), by hash, and discards the rest with the
container. Verify jobs run from recorded inputs only: the target commit as a `git archive`
export (no `.git`, so a test cannot read which ref it is on), the recorded test bytes, the
recorded diff bytes (empty for the red leg), and the runner command from the instance's
`pdca.toml`, never from leaf output. Each run gets a fresh copy of the tree. The driver
parses the verify output and writes `check-gates.json`, the red/green counts and the
routing itself, in the decide place. This is the rule that makes ADR-0006's evidence hold
for code a stranger shaped: the gate's bookkeeping is produced where the stranger's code
never ran.

The reviewer therefore does not re-execute the red and green legs (ADR-0005's stash and
unstash) for bug bundles; it adjudicates from the verify job's log, the path docs 05 already
provides for rows a reviewer cannot re-run (#403).

### 2. One runner per batch

A **batch** is one scheduled run of the track (ADR-0020 §2). The driver creates one runner
VM when, and only when, the poll found work; a quiet day creates nothing.

- **Intake stops at 20 hours, the VM is deleted at 23.** `max_run_duration` of 23 h with
  termination action `DELETE` is enforced by the provider and holds if the driver dies. The
  runner's own agent takes no new job after 20 h, finishes what runs, and powers off when
  the last container exits; the driver deletes the instance.
- **The job limit is the drain window.** No job may run longer than
  `lifetime − intake_cutoff` (3 h by default), so a job started at the cutoff still ends
  before the hard stop. `doctor` checks `job_limit ≤ lifetime − intake_cutoff`. The limit is
  per job, not per issue; what exhausting it means is decided in ADR-0020 §6.
- **The runner powers off when the queue is empty** for a short grace period, so a batch
  with two bugs does not pay for 23 hours.
- **Containers per job.** Each job runs in a fresh container from a pinned image: read-only
  root, a tmpfs working directory, no capabilities, seccomp, user namespaces, cgroup
  limits, a timeout of the job limit. Produce containers run under gVisor (`runsc`) and
  reach one network destination, the model endpoint; verify containers run under plain
  rootless Podman with no network. Dependencies come from a read-only cache volume that a
  trusted job writes from the target's `main`; no container fetches from a registry.
- **One VM, with a taint rule.** Produce and verify containers share the runner's kernel.
  If any anomaly occurs during a batch (a seccomp kill, a container timeout, an attempted
  connection the proxy refused, an out-of-memory kill), every verdict of that batch is
  marked tainted and verified again in the next batch before anything is published. A
  kernel escape is thereby bounded to one batch's verdicts, which never reach a pull
  request unverified. Separate runners for produce and verify remain the stricter option
  an operator may configure.
- **Spot.** The runner is a spot VM; an interruption ends the batch. Claimed jobs expire
  (ADR-0020 §12) and the next batch picks them up.

### 3. What the runner holds

- A service account that can read the job bucket and the cache bucket and write to one
  result prefix; never the default compute service account, never a role on the project.
- No external IP, no SSH (OS Login off), no serial console; outbound through NAT to the
  model endpoint, the job and cache buckets, and nothing else. The egress rule is by
  destination, logged in full, not errors only.
- The model credential for produce containers: a key bound to a workspace with its own
  spending limit, placed in the runner's metadata for the batch, never a subscription
  login (for the reason ADR-0017 rejects one). **[OPEN]** whether a small gateway in the
  project holds the key instead and enforces a per-batch token quota, so no spendable
  credential is on the runner at all; the pilot's spend data decides.
- Nothing that can write to a repository. Jobs and results move through object storage
  with URLs the driver signs per job.
- Result bundles are plain tar archives with a fixed member list; the driver extracts them
  with `tarfile`'s data filter, rejects any member outside the list, and never decompresses
  a format outside the standard library (ADR-0008).

### 4. The driver container

The driver runs as a scheduled container the operator deploys ("the workflow host" in
ADR-0020): a rendered instance plus the driver, started by the operator's workflow engine,
reporting back when the batch ends. It is the only trusted place and it runs no leaf.

| Credential | Scope |
|---|---|
| Cloud credential | create and delete runners in the dedicated project; read the result prefix |
| Tracker token | read issues and comments; write labels and comments; the target repository only, no code scopes |
| Deploy key | push `bug/*` branches to the pdca repository and open pull requests there |
| MAC key | the bug records (spec §11) |
| Target push credential | push `bugfix/*` branches to the target and open draft pull requests; present only if publish runs here (ADR-0020 §7), absent if the operator publishes from their own machine |

No personal login of the maintainer is among them. The container keeps two volumes, the
checkout of the pdca repository and `process/` for job claims; everything else is Git.
Its image definition is rendered into instances under `deploy/driver/` (ADR-0007); how an
operator runs it is the operator's.

### 5. Backend and seam

- **Google Cloud first**, in a dedicated project, in `europe-west3`, on x86_64 machine
  types; the runner image is built with Packer and the infrastructure is OpenTofu under
  `deploy/bug-track/gcp/`, reference infrastructure for an operator and not rendered into
  instances, because it carries the operator's project, billing and network.
- **Requirements on the runner seam.** The runner is reached through the `LeafRunner` seam
  that ADR-0010 will decide. This ADR requires of that seam: a job is created from
  recorded inputs and a job kind; its result is a bundle of named members collected by
  hash; budgets and token rows are recorded per job; a backend talks to its provider through
  the provider's CLI or plain HTTPS, never a cloud SDK (ADR-0008). **[OPEN]** the seam's
  exact contract is ADR-0010's to decide; this ADR does not fix it.
- **Other backends.** Any host that runs containers from a queue satisfies the seam: a
  self-hosted server, OVHcloud, STACKIT. The decision for one of them is a new ADR.

**Sovereignty.** The projects the harness serves aim at digital sovereignty, so running on a
US-controlled provider is a conscious compromise. It is accepted for this track because the
track handles only public data (open-source code and public issues), because the leaves
already call US model providers and the repositories already live on GitHub, so no new
class of dependency is added, and because the seam keeps the exit cost low. It is not a
precedent for any workload with data that is not public. Gaining hands-on experience with
Google Cloud is a stated secondary reason.

**When to revisit.** When the monthly spend, averaged over three months, exceeds the cost of
a self-hosted server amortised over three years plus running costs, a self-hosted backend
becomes the default and Google Cloud the overflow.

## Consequences

A hostile issue that steers a leaf reaches a container that holds no repository credential,
can reach at most the model endpoint, and is gone with its batch. At worst it hands the
driver a diff and a test, whose red and green are established on a container the stranger's
code never touched, and which a human then judges in a bundle pull request (ADR-0020 §7).
For the bug track this removes the practical effect of #710, #853, #854 and #855; the gaps
remain for ordinary cycles and are tracked there.

A batch that reproduces and fixes several bugs costs one VM for the hours it needs;
provisioning happens once a day at most, and a quiet day costs nothing.

Every verdict costs a verify container in addition to the produce container, and every
reproduced test runs N times per candidate ref; this is the price of evidence that means
something, and the runner's cores are what pay it.

A cold cache makes the builder slow; the cache job on the target's `main` must exist before
the track is useful for a large Rust project, and the pilot measures cold against warm
build time before the job limit is treated as a budget rather than a ceiling.

The taint rule can delay a batch's results by a day when an unrelated container times out;
a separate verify runner removes the delay at the cost of a second VM.

An operator now runs a Google Cloud project and a driver container alongside the instance.

Left open:

- **[OPEN]** The model-key gateway (section 3).
- **[OPEN]** The runner seam's contract (section 5, ADR-0010).
- **[OPEN]** Whether reproduction for aarch64-only reports runs on a separate backend (an
  Arm machine or an Arm VM), or such reports always need a maintainer's go.
