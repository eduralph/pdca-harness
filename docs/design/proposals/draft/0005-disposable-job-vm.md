---
created: 20.11.2026 14:12
type: proposal
status: draft
author: Eduard Ralph
tracking-issue: https://github.com/eduralph/pdca-harness/issues/969
tags:
  - proposal
  - security
  - deployment
---
# Proposal: Bug-track jobs run in disposable job VMs on a dedicated host

> Enhancement proposals (KEP/RFC-style) are how larger changes and milestone plans are
> proposed. Move this file from `draft/` to `accepted/` when accepted; an accepted proposal
> is never edited — a later proposal supersedes it, recorded in `../proposals/README.md`.
> If a decision is foundational rather than a change, it may instead warrant an ADR.

## Motivation

ADR-0021 (Proposed) decides where the bug track's jobs run: containers on one spot VM per
batch in Google Cloud, with the driver in an operator's container. Its reasons are sound,
but its answer is a provider. For most instances (autobook, depotbuch, the ofxstatement
plugins, stemma, nl-shell) a Google Cloud project is not a given: it is an account, a bill
and a sovereignty compromise that the instance's own work does not need. ADR-0021 already
calls its backend replaceable ("any host that runs containers from a queue") and names a
self-hosted server as deferred, not rejected.

What ADR-0021 actually needs is a **boundary**, not a provider. It rejected machines that
also serve personal use because personal credentials and sessions sit there, and it wanted a
fresh kernel often enough that a container escape does not become a foothold. Both are
properties of where a job's kernel is and what shares a machine with it, and both can be
met on a local machine. This proposal states the boundary backend-neutrally: every job runs
in its own microVM on a dedicated job host. The job host is a local machine by default, or a
cloud VM created per batch for heavy targets such as wyrd, whose x86_64 SIMD paths and long
CPU-bound builds were ADR-0021's main reason for Google Cloud.

Two other documents wait on this one. Proposal 0004 (draft,
[#961](https://github.com/eduralph/pdca-harness/pull/961)) decides that the driver's App key
is held by a minter under its own identity, on whichever backend the decision replacing
ADR-0021 allows (its decision Q4). And issue
[#965](https://github.com/eduralph/pdca-harness/issues/965), the loop-host baseline split
out of proposal 0004, asks whether host hardening becomes its own proposal or part of this
one (open question 5).

## Threat model

The design defends against one adversary: **whoever shapes what a job runs**. Anyone can
file an issue; Summarize and Reproduce read it, Reproduce writes a test shaped by it, the
builder runs `Bash` against the target, and verify jobs execute both. So the adversary is
taken to control everything inside a job VM, root in it included: a container escape
inside the VM is assumed, not prevented. In scope is any way for it to:

- reach the job host, the driver or anything the driver holds (the App key and its minter,
  the MAC key, a cloud credential, the tracker token);
- forge a verdict: make red or green appear where the recorded inputs did not produce it,
  or reach the place where `check-gates.json` and the counts are written;
- reach another job's VM, inputs or results, or persist past its own job;
- reach any network destination beyond its job's allowlist, the job host's own services and
  the local network included;
- spend beyond the limits of the credentials a job VM holds.

Out of scope, each an assumption the design rests on rather than a threat it defends:

- **An escape from the hypervisor** (KVM on the job host, or the cloud provider's). The VM
  boundary is the boundary; a job host that holds nothing personal bounds what an escape
  could reach, and the driver's placement (open question 4) decides whether it reaches the
  driver's keys.
- **The cloud provider, the model vendors, GitHub and the maintainer.**
- **The job host's own hardening beyond what this design states** (the loop-host
  baseline, #965), and supply-chain compromise of the pinned VM image or container images,
  whose review is the maintainer's.
- **Denial of service.** A job that burns its time limit costs time, not integrity.
- **The model credential inside a produce VM**, while it is there: it spends money and
  reaches no repository, and open question 6 decides whether it stays.

Residuals the design accepts within scope are listed under Security impact. A weakness
outside this model, or one already listed there, is recorded there rather than designed
against.

## Design

### 1. The boundary, independent of the backend

Every backend meets these requirements; a backend that cannot meet one is not a backend.

1. **One job, one VM.** Every produce and verify job runs in its own job VM: a microVM with
   its own kernel behind hardware virtualization, created from a pinned image (by digest)
   for that job and destroyed when it ends. No two jobs share a VM, so no job can alter
   another's inputs, run or results, and nothing a job leaves behind outlives it.
   Containers inside the VM (section 2) are a layer within it, not a replacement for it.
2. **A lifetime enforced outside.** Each job VM's lifetime is the job limit (ADR-0021 §2),
   enforced by the job host, not by anything inside the VM, and it holds if the driver
   dies. A batch has a hard lifetime (ADR-0021 §2's 23 hours by default) enforced the same
   way: when it ends, every job VM of the batch is destroyed, whatever the driver does.
3. **Launch fails closed.** The job agent starts a job VM only after its network rules (or,
   for a verify VM, the absence of any network device) and its external timer are in
   place and verified; if any of that fails, the VM is not started and the job fails as
   an infrastructure failure.
4. **Nothing trusted inside.** A job VM holds no repository credential, no MAC key, no
   cloud credential and no driver code. It has no access to any store; its inputs and
   results cross the boundary only as disks (item 6).
5. **The driver never shares a job VM's kernel.** The driver runs on the job host, in a
   container there, in its own VM, or on another machine, never inside a job VM (open
   question 4 picks the local default).
6. **A narrow channel, parsed in userspace.** For each job, the job host attaches a
   read-only input disk the driver built (for a verify job: the `git archive` export, the
   recorded test and diff bytes, the runner command from `pdca.toml`) and an empty raw
   result disk of fixed size. The job writes one plain tar stream to the raw disk, with no
   filesystem. After the VM has stopped, the job host hands the driver at most the disk's
   bytes; the driver never mounts anything a job VM wrote and never hands it to a kernel
   parser. It extracts the stream as an unprivileged user as ADR-0021 §3 prescribes (a
   fixed member list, `tarfile`'s data filter) and rejects the whole stream if any member
   falls outside that list; then it validates and persists what its stage owns before it
   queues the next job. Stages are chained by the
   driver, one job at a time per bug, never inside a VM.
7. **Egress allowlist outside the VM.** A verify VM has no network device. A produce VM
   has one network path: to the job host's egress proxy, on a single address and port,
   which admits the configured model endpoints by name and nothing else. Every other
   address, port and protocol, on the job host, the local network or beyond, is
   unreachable; the job host enforces this outside the VM and logs every refusal.
8. **A dedicated job host.** No machine that serves personal use is a job host or runs the
   driver: no personal logins, sessions or keys on it (ADR-0021's first rejection,
   unchanged). The job host runs job VMs, the egress proxy and the job agent that creates
   VMs and moves disks, and nothing a job controls.

### 2. What carries over from ADR-0021, and what changes

Carried over:

- **Three roles, three places** (§1): produce jobs under gVisor, verify jobs in rootless
  Podman without network, decide in the driver; nothing a produce job writes is evidence;
  verify runs from recorded inputs only.
- **The batch** (§2): a batch starts only when the poll found work; intake cutoff, hard
  lifetime, the job limit as the drain window. A cloud job host exists only for its batch
  and powers off when the queue is empty; a local job host is a standing machine that runs
  nothing between batches.
- **Containers per job** inside the VM (§2): pinned images, read-only root, no
  capabilities, seccomp, user namespaces, cgroup limits; dependencies from a read-only
  cache disk a trusted job builds from the target's `main`.
- **The driver's credentials** (§4), with the App key held by a minter under its own
  identity rather than by the driver, as proposal 0004 (draft,
  [#961](https://github.com/eduralph/pdca-harness/pull/961)) decides in its decision Q4.
- **The runner seam** (§5): the backend is reached through the `LeafRunner` seam that
  ADR-0010 is to decide, and talks to its provider through a CLI or plain HTTPS, never a
  cloud SDK (ADR-0008).

Changed:

- **One VM per job instead of one VM per batch.** ADR-0021 §2 ran all of a batch's
  containers on one VM and bounded a kernel escape with a taint rule. With root in the VM
  assumed, a produce job there could rewrite later verify runs undetected, which no taint
  rule bounds. ADR-0021 rejected per-job VMs for minutes of cloud provisioning each; a
  microVM on a running job host boots in about a second, so that reason no longer holds.
  The taint rule is no longer needed: a job that hits its limit is handled by ADR-0020's
  stage rules (a Fix job that does not turn the test green opens the bundle with the
  partial diff and `track:needs-plan`, §6), and any other anomaly (a seccomp kill, a
  refused connection, a VM that failed to launch) fails the job as an infrastructure
  failure, which ADR-0020 §12 retries in a later batch without counting it as an attempt.
- **A stop before the limit.** So that a job cut off by its limit still leaves its partial
  output (the Fix diff above), the job agent signals the VM a grace period before the job
  limit (5 minutes by default); the job's supervisor then stops the leaf and writes what it
  has to the result stream. The host-side timer still destroys the VM at the limit. A
  stream that is empty or does not parse is an infrastructure failure, never a partial
  diff.
- **"Google Cloud first"** (§5) becomes a per-instance choice of where the job host is.

### 3. Local job host (the default)

A dedicated x86_64 machine with KVM is the job host for every batch:

- **Job VMs** are created with QEMU/KVM, through libvirt or Firecracker (open question 2),
  from a pinned base image, booted with a throwaway copy-on-write overlay that is deleted
  with the VM. A job VM is never defined persistently, so nothing restarts it after a
  host reboot. Its deadline is written to disk when it starts, and a host-side timer
  destroys it at its job limit.
- **Reconciliation at start.** When the job agent starts (after a crash or a reboot of the
  host), it destroys every job VM still running, deletes every overlay, input and result
  disk left from an earlier run, and reports the jobs concerned to the driver as
  infrastructure failures, before it accepts new work.
- **The batch on a standing machine.** When the driver starts a batch, the job agent arms a
  host-side watchdog (a systemd timer, not the driver) for the batch's hard lifetime; the
  agent takes no new job after the intake cutoff, and the watchdog destroys every job VM
  still running when the lifetime ends.
- **Disks** are files on the host: the driver writes input disks into a directory the job
  agent attaches read-only, and reads each result disk's bytes from the agent once the VM
  has stopped.
- **Network.** A produce VM sits on its own isolated bridge with no route to the host's
  networks or the local network. Its only reachable endpoint is the egress proxy listening
  on one address and port on that bridge; the proxy runs as its own unprivileged user that
  holds no driver credential. Host firewall rules (nftables) drop every other packet from
  the bridge and log it.
- **The dependency cache** is a read-only disk built by a trusted job from the target's
  `main`, attached to each job VM.
- **Machine size** is the operator's: the host's cores and memory bound how many job VMs
  run at once.

### 4. Cloud job host (Google Cloud first)

For targets too heavy for a local machine, the job host is a cloud VM created per batch:
ADR-0021's spot VM, now running the local job host's software rather than the jobs
themselves. It runs job VMs under nested virtualization, and meets section 1 like any job
host: no external IP and no inbound connection at all, a hard lifetime that deletes the
instance and its disks, a pinned image with nothing else executed at boot, egress only
to the model endpoints (for the proxy) and the storage service (for the agent), and a job
agent whose cloud identity reaches only the current batch's inputs and results, while
the job VMs never reach the storage service. The driver runs outside the cloud. ADR-0021's
sovereignty note and its revisit rule (move to a self-hosted default when the monthly
spend exceeds a server's amortised cost) apply here.

How those properties are established and verified in a cloud project (the template and
instance configuration, project-wide metadata, per-batch storage access, the IAM bindings
an identity holds) is designed and reviewed when this backend is built, not in this
proposal (open question 11). Until then `doctor` refuses a configuration that names the
cloud backend, so no batch runs on an unverified cloud host.

### 5. Choosing a backend

`pdca.toml` names the job host per instance (`[bug_track.runner] backend = "local"` or
`"gcp"`); there is no fallback from one to the other, since that would silently change the
boundary. Other backends (OVHcloud, STACKIT, a rented dedicated server running the local
job host) need only meet section 1 and the seam; adding one is a new ADR, as ADR-0021 §5
already says.

### 6. Proof in `doctor`

`doctor` checks, before every batch, what the backend can show:

- the job VM image and container images are pinned by digest and match the recorded ones;
- `job_limit + collect_margin ≤ lifetime − intake_cutoff`: ADR-0021 §2's drain rule plus a
  margin (15 minutes by default) to collect and upload the last job's result before the
  batch ends, so with ADR-0021's 3-hour job limit and 23-hour lifetime the intake cutoff
  moves to 19 h 45 min; and `grace < job_limit` for the stop before the limit (section 2);
- on a local job host: KVM is available, the firewall rule set and the proxy's
  configuration match their recorded versions, and the batch watchdog can be armed;
- on a cloud job host: the checks open question 11 defines; until they exist, the
  backend is refused.

Each job VM's own network rules and timer are created with it, so `doctor` cannot see them
in advance; the job agent's fail-closed launch (section 1 item 3) is what checks those, and
the agent refuses to start any job VM where hardware virtualization (`/dev/kvm`) is
missing, never falling back to software emulation.

Probe VMs, created the way the job agent creates real ones, run when a job host is set up
and whenever its image or network configuration changes: a probe produce VM reaches the
proxy's one address and port and nothing else on the job host (every other port and
protocol fails), cannot reach a non-allowlisted address through the proxy or the local
network, and cannot see the driver's files, sockets or processes; a probe verify VM has no
network at all.

## Alternatives considered

- **ADR-0021 as written (Google Cloud first, one VM per batch).** Sound for wyrd's size,
  but it makes a cloud account, its bill and a US provider the price of the bug track for
  every instance, and its shared batch VM lets a produce job forge later verify results.
  Its cloud VM is kept as a job host.
- **Containers on a dedicated host, no VM.** gVisor narrows the kernel surface, but jobs
  and the driver would share the host's kernel, and a kernel escape would reach the
  driver's keys and the verdicts. Rejected as the boundary; kept as a layer inside the VM.
- **One VM per batch, or per bug.** Cheaper in boots, but jobs in the same VM can alter each
  other's inputs and results, and a per-bug VM must outlive the fix stage between red and
  green. One VM per job is simpler to reason about and cheap on a running job host.
- **A cloud VM per job.** ADR-0021's reason holds there: minutes of provisioning and a spot
  interruption per job. The cloud job host runs microVMs instead.
- **The maintainer's own machines** (the desktop, a laptop): rejected for the reason
  ADR-0021 gives, personal credentials and sessions on the same machine.
- **GitHub-hosted runners:** rejected for the reason ADR-0021 gives, workflows triggered by
  issues next to repository tokens.

## Graduation criteria

1. **Pilot** on one instance with a local job host (open question 10): each job runs in its
   own VM, which is destroyed when the job ends; killing the driver mid-batch still
   destroys running job VMs at their job limit and the batch watchdog destroys any left at
   the hard lifetime; after a reboot of the job host mid-job, no job VM restarts and no
   disk of it remains; a launch whose network rules cannot be installed starts no VM; the
   probe VMs behave as section 6 requires; a Fix job made to time out leaves its partial
   diff through the grace-period stop, and the bundle opens with it and
   `track:needs-plan`.
2. **Proposal 0003** (draft) is updated throughout phase 1 to this design: the runner
   backend is a job host running one VM per job (local first, cloud second), not one
   runner VM per batch; job kinds run in produce and verify VMs with the disk channel of
   section 1; anomalies follow section 2 and the batch-wide taint rule is removed. Phase 1
   does not start until that update is merged.
3. **An ADR records the decision** and supersedes ADR-0021 by ADR-0019's procedure, or
   ADR-0021 is revised in place while it is still Proposed (open question 8).
4. **Architecture §8.10** (`docs/design/architecture/08-crosscutting-concepts.md`, the
   repository's threat model) gains the job VM boundary in the change that lands the
   local job host.

## Security impact

The untrusted input is the same as ADR-0021's: issue text and everything a job shapes from
it. This proposal moves the boundary that contains it from "a cloud VM per batch, shared by
its jobs" to "a microVM per job on a dedicated job host", and adds two trusted components on
the job host: the egress proxy, the only endpoint a produce VM reaches, which must not be
usable to reach anything but the allowlist; and the job agent, which creates VMs and moves
disks and is reachable from no job VM. The result disk is the same untrusted input as
ADR-0021's result bundle; it is read as bytes after the VM stops and never mounted.

Accepted residuals:

- **A hypervisor escape** reaches the job host. On a local job host, whether it also
  reaches the driver depends on open question 4.
- **The model credential inside a produce VM**, until the proxy or a gateway holds it
  (open question 6).
- **Exfiltration through the allowed model endpoints**: a job can send what it sees (public
  code and public issues) to an allowed endpoint. Nothing non-public is in a job VM.
- **A hostile test is judged by its own runs**: a verify job executes the recorded test,
  and a test can make its own result whatever it likes; ADR-0020's red-before /
  green-after rule and the maintainer's review of the bundle pull request are the bound,
  as before.

## Backward compatibility

Nothing is built yet: proposal 0003's phase 1, which introduces the runner, has not
started, and ADR-0021 is Proposed. No instance runs the bug track. Instances that later
enable it choose a job host in `pdca.toml`; the Google Cloud infrastructure under
`deploy/bug-track/gcp/` becomes reference infrastructure for the cloud job host.

## Open questions

1. **Which dedicated machine is the local job host?** The Framework Desktop (x86_64,
   128 GB) qualifies only if nobody uses it personally. The alternatives are a new
   machine, or a rented dedicated server. The answer also sets how many jobs run at once.
2. **libvirt/QEMU or Firecracker** for job VMs? Firecracker boots faster and has a smaller
   device model with no GPU or USB passthrough to misconfigure; libvirt is more familiar
   and manages disks and networks itself.
3. **Nested virtualization in the cloud.** It limits the machine types (Google Cloud offers
   it only on some machine types) and costs some performance; does it suit wyrd's builds and
   seed sweeps, or does wyrd need a large local or rented host instead (see also 9)?
4. **Where does the driver run?** On the job host as its own user, in a container there,
   in its own VM, or on another machine. Its own VM or another machine puts a second
   boundary between a hypervisor escape and the driver's keys; on the job host it is
   simpler and relies on the host baseline.
5. **The loop-host baseline (#965):** does its hardening become part of this proposal (the
   job host may be the same machine as the loop host), or stay a proposal of its own?
6. **The model key:** the egress proxy could hold the key and add it to requests, so no
   spendable credential is ever in a job VM, and enforce a per-batch token quota. Should
   that be the default, ahead of ADR-0021 §3's cloud gateway?
7. **aarch64 reports** (ADR-0021's open question): an Arm machine as a second job host, an
   Arm cloud VM, or always the maintainer's go?
8. **Supersede or revise ADR-0021?** It is still Proposed, so it could be rewritten in
   place; superseding it keeps its reasoning on record, revising keeps one document.
9. **Which job host does wyrd use?** A cloud job host as ADR-0021 planned, or a local or
   rented host large enough for its builds and seed sweeps.
10. **Which instance pilots it?** A small one (nl-shell, depotbuch) proves the boundary
    cheaply; wyrd proves the size.
11. **Verifying a cloud job host.** How the cloud backend proves section 1 before a batch:
    comparing the created instance with a recorded configuration (normalized, since the
    provider adds fields), project-wide metadata that could run at boot, per-batch
    storage access for the agent (per-batch identities, downscoped credentials or signed
    URLs), and IAM bindings held on individual resources, which project and bucket
    policies do not show. This is designed with the cloud backend, and its review is part
    of that work.
