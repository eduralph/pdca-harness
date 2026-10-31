---
created: 31.10.2026 17:55
type: adr
status: Proposed
tags:
  - adr
  - security
  - deployment
---
# 0021. Bug-track leaves and gates run on an ephemeral VM per job, behind the LeafRunner seam, with Google Cloud spot VMs as the first backend

## Context

The bug track (ADR-0020) runs leaves on issues anyone can file. Reproduce writes and runs a
test shaped by a stranger's report, the builder runs `Bash` against the target project, and
the C4 gate executes both. Architecture §8.10 treats a leaf as able to leave any file in any
state, and the claude builder is not confined to its worktree today (#710). On the host
that runs the driver, that host's credentials, mounts and sessions are within reach.

A runner for these jobs needs:

- **a fresh machine per job**, so nothing one job leaves behind reaches the next;
- **no credential that can write to a repository**, and outbound traffic limited to an
  allowlist;
- **many cores**, because instance test suites, Rust builds and simulation seed sweeps are
  CPU-bound;
- **the target project's production architecture.** In getwyrd/wyrd, erasure coding takes
  different SIMD paths on x86_64 and aarch64, so a bug reported on one may not reproduce on
  the other.

The options weighed:

| Option | Verdict |
|---|---|
| The maintainer's desktop or workstation | Rejected: personal credentials and sessions on the same machine |
| Raspberry Pis | Rejected as runners: aarch64 and too slow. Suitable for the driver, orchestration and aarch64 conformance runs |
| An old Intel laptop | Rejected: dual-core, throttles, unsafe as an always-on host |
| Hetzner Cloud | Rejected for now: provisioning of new cloud servers has been restricted since 26 June 2026, which breaks a VM-per-job design, and the dedicated-vCPU lines were repriced sharply in June 2026 |
| An on-premise rack server (Proxmox, a linked clone per job) | Deferred: needs hardware and lead time, and its size cannot be chosen before the load is known |
| OVHcloud, STACKIT | Viable; candidates for later backends |
| Google Cloud spot VMs | Chosen |

## Decision

We will run the bug track's Reproduce leaf, its builder leaf and its gates on **one
ephemeral VM per job**. The driver, the reviewer leaf (already OS-sandboxed, §8.10) and
publishing stay on the host that runs the driver.

1. **Behind the LeafRunner seam.** The VM backend is a `LeafRunner` implementation
   (ADR-0010), so budgets and the token ledger work as for local leaves. Its contract is
   `create(job) → handle`, `status(handle)`, `collect(handle) → bundle`,
   `destroy(handle)`. The driver stays stdlib-only (ADR-0008): a backend talks to its
   provider through the provider's CLI (`gcloud`) or plain HTTPS, never a cloud SDK.
2. **Google Cloud spot VMs as the first backend**, in a dedicated project, in
   `europe-west3`, on x86_64 machine types. Spot interruptions cost a retry: every job is
   restartable from its inputs.
3. **The provider bounds the VM's life.** `max_run_duration` with termination action
   `DELETE` deletes every VM after a fixed time, even if the driver has died. The driver
   also deletes any VM of the track older than its budget.
4. **The VM holds almost nothing.** It runs as a dedicated service account that can only
   read the build cache; the default compute service account is never attached. It has no
   external IP, no SSH and no serial console. Outbound traffic goes through NAT and a
   firewall policy that allows HTTPS to an allowlist (the model APIs the leaves use, the
   target project's package registries, read-only Git, Cloud Storage) and denies the rest.
5. **No repository credential crosses into the VM.** The job arrives as instance metadata;
   the result bundle (diff, test, gate output, token rows) leaves through a signed upload
   URL for that one object. The driver collects it and publishes as in every cycle.
6. **The model credential is the narrowest available.** Never a subscription login, for the
   reason ADR-0017 rejects one: it is a whole personal account exposed to every issue
   author's prompt injection. A key bound to a workspace with its own spending limit is
   the minimum.
7. **The build cache is written only by trusted builds** of the target project's main
   branch; job VMs read it, so a job cannot poison the next one's build.
8. **Infrastructure as code** for the first backend lives in `deploy/bug-track/gcp/`
   (OpenTofu). It is reference infrastructure for an operator, not rendered into instances
   (ADR-0007), because it carries the operator's project, billing and network.

**Sovereignty.** The projects the harness serves aim at digital sovereignty, so running on a
US-controlled provider is a conscious compromise. It is accepted for this track because the
track handles only public data (open-source code and public issues), because the leaves
already call US model providers and the repositories already live on GitHub, so no new
class of dependency is added, and because the LeafRunner seam keeps the exit cost low. It is
not a precedent for any workload with data that is not public. Gaining hands-on experience
with Google Cloud is a stated secondary reason.

**When to revisit.** When the monthly spot spend, averaged over three months, exceeds the
cost of a rack server amortised over three years plus electricity, a Proxmox backend
becomes the default and Google Cloud the overflow.

We reject GitHub-hosted runners for these jobs: workflows triggered by issues sit next to
repository tokens, and keeping the publisher apart from the job inside Actions is much
harder than with a separate VM. We reject a long-lived VM: whatever one job leaves on it
reaches the next.

## Consequences

A hostile issue that steers a leaf reaches a VM that holds no repository credential, can
reach only allowlisted hosts and is deleted within its time limit. At worst it hands the
driver a diff, which the review, the C6 guard and the maintainer still judge. For the bug
track this closes the practical effect of #710; the gap remains for ordinary cycles.

The track can run without buying hardware, and the first months give the numbers (job
hours, sweep sizes, how many bugs reproduce) that the rack-server decision lacks.

A cold VM with no build cache makes the builder slow; the cache must exist before the track
is useful for a large Rust project.

Matching outbound traffic by host name needs Cloud NGFW features whose tier and cost must be
checked when the project is set up; the fallback is an egress proxy on the trusted side.

The backend depends on ADR-0010's runner seam, planned for step 2; until it lands, the track
cannot run its leaves remotely.

An operator now runs a Google Cloud project, with billing and IAM, alongside the instance.

Left open:

- **[OPEN]** Whether the VM can get a short-lived model token by federation from its own
  identity, as ADR-0017's bot does from GitHub's, instead of a workspace key placed in its
  metadata for each job.
- **[OPEN]** Whether reproduction for aarch64-only reports runs on a separate backend (a
  Raspberry Pi or an Arm VM), or such reports always need a maintainer's go.
