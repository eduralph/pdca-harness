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
# Proposal: Bug-track jobs run in a disposable job VM on a dedicated host

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
properties of where the job's kernel is and what shares a machine with it, and both can be
met on a local machine. This proposal states the boundary backend-neutrally, adds a local
KVM backend as the default, and keeps Google Cloud as one backend for heavy targets such as
wyrd, whose x86_64 SIMD paths and long CPU-bound builds were ADR-0021's main reason.

Two other documents wait on this one. Proposal 0004 (draft,
[#961](https://github.com/eduralph/pdca-harness/pull/961)) decides that the driver's App key
is held by a minter under its own identity, on whichever backend the decision replacing
ADR-0021 allows (its decision Q4). And issue
[#965](https://github.com/eduralph/pdca-harness/issues/965), the loop-host baseline split
out of proposal 0004, asks whether host hardening becomes its own proposal or part of this one
(open question 5).

## Threat model

The design defends against one adversary: **whoever shapes what a job runs**. Anyone can
file an issue; Summarize and Reproduce read it, Reproduce writes a test shaped by it, the
builder runs `Bash` against the target, and verify jobs execute both. So the adversary is
taken to control everything inside the job VM, root in it included: a container escape
inside the VM is assumed, not prevented. In scope is any way for it to:

- reach the host, the driver or anything the driver holds (the App key and its minter, the
  MAC key, a cloud credential, the tracker token);
- forge a verdict: make red or green appear where the recorded inputs did not produce it,
  or reach the place where `check-gates.json` and the counts are written;
- persist past its batch, reach another batch's inputs and results, or reach a verify VM
  from a produce VM;
- reach any network destination beyond the job VM's allowlist, the host's own network and
  the local network included;
- spend beyond the limits of the credentials the VM holds.

Out of scope, each an assumption the design rests on rather than a threat it defends:

- **An escape from the hypervisor** (KVM, or the cloud provider's). The VM boundary is the
  boundary; a host that holds nothing personal bounds what an escape could reach, and the
  driver's placement (open question 4) decides whether it reaches the driver's keys.
- **The cloud provider, the model vendors, GitHub and the maintainer.**
- **The host's own hardening beyond what this design states** (the loop-host baseline,
  #965), and supply-chain compromise of the pinned VM image or container images, whose
  review is the maintainer's.
- **Denial of service.** A job that burns its batch costs time and the VM's budget, not
  integrity.
- **The model credential inside the VM**, while it is there: it spends money and reaches no
  repository, and ADR-0021 §3's gateway question decides whether it stays (open question 6).

Residuals the design accepts within scope are listed under Security impact. A weakness
outside this model, or one already listed there, is recorded there rather than designed
against.

## Design

### 1. The boundary, independent of the backend

Every backend meets these requirements; a backend that cannot meet one is not a backend.

1. **Own kernel.** Every produce and verify job runs inside a job VM: a virtual machine
   with its own kernel behind hardware virtualization. Containers inside it (section 2)
   are a layer within the VM, not a replacement for it.
2. **Verify apart from produce.** Produce and verify never share a VM. A batch's produce
   jobs run in one **produce VM**; each bug's verify jobs run in a fresh **verify VM**,
   created from the pinned image once that bug's inputs are recorded, in which no produce
   job and no other bug's verify job ever ran. The threat model gives the adversary root in
   the produce VM; a shared VM would let it rewrite later verify runs and their exit data
   undetected, which no taint rule can bound.
3. **Disposable.** A job VM is created from a pinned image (by digest) and destroyed when
   its work ends; its disk is never reused. Its maximum lifetime is enforced **outside**
   the VM, by the hypervisor host or the provider, and holds if the driver dies (ADR-0021
   §2's 23 hours by default for the produce VM, the job limit for a verify VM).
4. **Nothing trusted inside.** A job VM holds no repository credential, no MAC key, no
   cloud credential beyond reading its own inputs and writing its own results, and no
   driver code. Jobs and results cross the boundary only as data (item 6).
5. **The driver never shares a job VM's kernel.** The driver runs on the host, in a
   container on the host, or in its own VM, never inside a job VM (open question 4 picks
   the local default).
6. **A narrow channel, parsed in userspace.** The driver writes a job's recorded inputs;
   the VM writes its results as one bounded byte stream (a plain tar, no filesystem) that
   the driver reads after the VM has stopped. The driver never mounts anything a job VM
   wrote and never hands it to a kernel parser: it reads at most a configured number of
   bytes as an unprivileged user and extracts them as ADR-0021 §3 prescribes (a fixed
   member list, `tarfile`'s data filter), discarding the rest. Nothing is writable from
   both sides at once.
7. **Egress allowlist outside the VM.** A verify VM has no network at all. A produce VM
   reaches the configured model endpoints and, where the backend needs them, its input and
   result stores; nothing else, the local network included. The only host-side endpoint a
   produce VM may reach is the egress proxy's single address and port (section 3); every
   other host address, port and protocol is unreachable. The rule is enforced outside the
   VM (the host's firewall on the VM's network, or the provider's firewall) and logged in
   full.
8. **A dedicated host.** No machine that serves personal use runs a job VM or the driver:
   no personal logins, sessions or keys on it (ADR-0021's first rejection, unchanged).

### 2. What carries over from ADR-0021

- **Three roles, three places** (§1): produce jobs under gVisor, verify jobs in rootless
  Podman without network, decide in the driver; nothing a produce job writes is evidence;
  verify runs from recorded inputs only.
- **The batch** (§2): created only when the poll found work; intake cutoff, hard lifetime,
  the job limit as the drain window, power-off when the queue is empty.
- **Containers per job** inside the VM (§2): pinned images, read-only root, no
  capabilities, seccomp, user namespaces, cgroup limits; dependencies from a read-only
  cache a trusted job builds from the target's `main`.
- **The driver's credentials** (§4), with the App key held by a minter under its own
  identity rather than by the driver, as proposal 0004 (draft,
  [#961](https://github.com/eduralph/pdca-harness/pull/961)) decides in its decision Q4.
- **The runner seam** (§5): the backend is reached through the `LeafRunner` seam that
  ADR-0010 is to decide, and talks to its provider through a CLI or plain HTTPS, never a
  cloud SDK (ADR-0008).

Two things change. ADR-0021 §2's "one VM, with a taint rule", which let produce and verify
containers share a kernel and offered separate runners only as an option, is replaced by
section 1 item 2; the taint rule stays for anomalies inside a verify VM, which mark that
bug's verdicts tainted. And ADR-0021 §5's "Google Cloud first" becomes a per-instance
choice, with the local backend below as the default.

### 3. Local KVM backend (the default)

For a dedicated x86_64 host with KVM:

- **Job VMs** are created with QEMU/KVM (libvirt or Firecracker, open question 2) from a
  pinned base image, booted with a throwaway copy-on-write overlay that is deleted with the
  VM: one produce VM per batch, one verify VM per bug. A host-side timer destroys each at
  its hard lifetime whether or not the driver is alive.
- **Inputs and results without network storage.** For each VM the driver builds a
  read-only input disk (for a verify VM: the `git archive` export, the recorded test and
  diff bytes, the runner command from `pdca.toml`) and attaches an empty raw result disk
  of a fixed size. The VM writes one tar stream to that raw device; after the VM has
  stopped, the driver reads the device's bytes up to the stream's end (section 1 item 6).
- **Network.** A verify VM has no network device. A produce VM sits on an isolated bridge
  with no route to the host's networks or the local network. Its only reachable host
  endpoint is a forwarding proxy listening on one address and port on that bridge; the
  proxy runs as its own unprivileged user that holds no driver credential, and admits the
  configured model endpoints by name and nothing else. Host firewall rules (nftables) drop
  every other packet from the bridge, to the host or beyond, and log it.
- **The dependency cache** is a read-only disk built by a trusted job from the target's
  `main`, attached to each job VM.
- **Machine size** is the operator's: the host's cores and memory bound the batch, and the
  job limit (ADR-0021 §2) is the budget.

### 4. Google Cloud backend

ADR-0021 §2, §3 and §5 for the produce VM: one spot VM per batch in a dedicated project, no
external IP, egress through NAT to the model endpoints and the job and cache buckets, a
service account that reads inputs and writes one result prefix, `max_run_duration` with
`DELETE`. Each bug's verify jobs run in a separate small spot VM of the same kind with no
egress beyond its own input and result objects, and a `max_run_duration` of the job limit.
The driver may run outside Google Cloud and reach the project with its cloud credential.
ADR-0021's sovereignty note and its revisit rule (move to a self-hosted default when the
monthly spend exceeds a server's amortised cost) apply to this backend.

### 5. Choosing a backend

`pdca.toml` names the backend per instance (`[bug_track.runner] backend = "local-kvm"` or
`"gcp"`); there is no fallback from one to the other, since that would silently change the
boundary. Other backends (OVHcloud, STACKIT, a rented dedicated server running the local
backend) need only meet section 1 and the seam; adding one is a new ADR, as ADR-0021 §5
already says.

### 6. Proof in `doctor`

`doctor` checks, before every batch, what the backend can show:

- the job VM image and container images are pinned by digest and match the recorded ones;
- the configured lifetime, intake cutoff and job limit satisfy
  `job_limit ≤ lifetime − intake_cutoff` (ADR-0021 §2);
- for the local backend: KVM is available, the job VM's bridge has no route to any
  network but the proxy, the firewall rules are in place, and the host-side lifetime timer
  is armed;
- a **probe produce VM** created the way a batch creates one reaches the proxy's one
  address and port and nothing else on the host (every other port and protocol fails),
  cannot reach a non-allowlisted address through the proxy or the local network, and
  cannot see the driver's files, sockets or processes; a **probe verify VM** has no network
  at all; the probes run when the backend is set up and whenever its image or network
  configuration changes, not before every batch.

## Alternatives considered

- **ADR-0021 as written (Google Cloud first).** Sound for wyrd, but it makes a cloud
  account, its bill and a US provider the price of the bug track for every instance. Kept
  as one backend.
- **Containers on a dedicated host, no VM.** gVisor narrows the kernel surface, but jobs
  and the driver would still share the host's kernel, and a kernel escape would reach the
  driver's keys and the verdicts. Rejected as the boundary; kept as a layer inside the VM.
- **One VM per batch for produce and verify together** (ADR-0021 §2). Rejected: with root
  in the VM assumed, a produce job could forge later verify results undetected.
- **One VM per job.** ADR-0021 rejected it for minutes of cloud provisioning per job. A
  local microVM boots in about a second, so the reason does not carry over; whether
  per-job VMs, stricter than per-bug verify VMs and a per-batch produce VM, are worth
  their cost locally is open question 3.
- **The maintainer's own machines** (the desktop, a laptop): rejected for the reason
  ADR-0021 gives, personal credentials and sessions on the same machine.
- **GitHub-hosted runners:** rejected for the reason ADR-0021 gives, workflows triggered by
  issues next to repository tokens.

## Graduation criteria

1. **Pilot** on one instance with the local backend (open question 10): a batch creates a
   produce VM and one verify VM per bug and destroys each when its work ends; killing the
   driver mid-batch still destroys them at their lifetimes; the probe VMs behave as
   section 6 requires; a deliberately induced anomaly in a verify VM (a container timeout)
   taints that bug's verdicts and they are re-verified in the next batch.
2. **Proposal 0003** (draft) is updated: phase 1's runner backend names the local backend
   first and Google Cloud as the second backend.
3. **An ADR records the decision** and supersedes ADR-0021 by ADR-0019's procedure, or
   ADR-0021 is revised in place while it is still Proposed (open question 8).
4. **The threat models** (`docs/design/architecture/14-threat-model.md`,
   `.oss-scanner/threat_model.md`) gain the job VM boundary in the change that lands the
   local backend.

## Security impact

The untrusted input is the same as ADR-0021's: issue text and everything a job shapes from
it. This proposal moves the boundary that contains it from "a cloud VM per batch" to "a VM
per batch on any dedicated host", separates verify from produce, and adds one new trusted
component on the local backend, the host's egress proxy, which is the only host endpoint a
produce VM reaches and must not be usable to reach anything but the allowlist. The result
disk is the same untrusted input as ADR-0021's result bundle; it is read as bytes after the
VM stops and never mounted.

Accepted residuals:

- **A hypervisor escape** reaches the host. On the local backend, whether it also reaches
  the driver depends on open question 4.
- **The model credential inside the VM**, until a gateway or the host proxy holds it (open
  question 6).
- **Exfiltration through the allowed model endpoints**: a job can send what it sees (public
  code and public issues) to an allowed endpoint. Nothing non-public is in the VM.
- **Produce jobs of one batch share a VM.** A hostile produce job with root there can
  alter another bug's produce output in the same batch (its test or its diff) before the
  driver takes it. Verify then judges those bytes honestly, so no verdict is forged, but a
  bundle pull request can carry a planted change; the maintainer's review of that pull
  request (ADR-0020 §7) is the bound, and open question 3 can remove it.
- **A bug's verify jobs share a verify VM**: a hostile test can tamper with its own bug's
  other verify runs, the same reach it has over its own result; the taint rule applies to
  anomalies there.

## Backward compatibility

Nothing is built yet: proposal 0003's phase 1, which introduces the runner, has not
started, and ADR-0021 is Proposed. No instance runs the bug track. Instances that later
enable it choose a backend in `pdca.toml`; the Google Cloud infrastructure under
`deploy/bug-track/gcp/` remains reference material for that backend.

## Open questions

1. **Which dedicated host?** The Framework Desktop (x86_64, 128 GB) qualifies only if
   nobody uses it personally. The alternatives are a new machine, or a rented dedicated
   server running the local backend. The answer also sets the batch size.
2. **libvirt/QEMU or Firecracker** for the job VM? libvirt is familiar and manages disks
   and networks; Firecracker boots faster, has a smaller device model and no GPU or USB
   passthrough to misconfigure.
3. **Finer VMs on the local backend?** A produce VM per bug, rather than per batch, removes
   the residual that one bug's produce job can alter another's output; a VM per verify
   run removes the shared kernel between a bug's verify runs. Each costs a boot and its
   own input and result disks.
4. **Where does the driver run on the local backend?** On the host as its own user, in a
   container on the host, or in its own VM. Its own VM puts a second hypervisor boundary
   between a job VM escape and the driver's keys; on the host it is simpler and relies on
   the host baseline.
5. **The loop-host baseline (#965):** does its hardening become part of this proposal (the
   dedicated host is the same machine as the loop host), or stay a proposal of its own?
6. **The model key:** on the local backend, the host proxy could hold the key and add it to
   requests, so no spendable credential is ever in the VM, and enforce a per-batch token
   quota. Should that be the default, ahead of ADR-0021 §3's cloud gateway?
7. **aarch64 reports** (ADR-0021's open question): an Arm machine as a second local host,
   an Arm cloud VM, or always the maintainer's go?
8. **Supersede or revise ADR-0021?** It is still Proposed, so it could be rewritten in
   place; superseding it keeps its reasoning on record, revising keeps one document.
9. **Which backend does wyrd use?** Google Cloud as ADR-0021 planned, or the local backend
   on a host large enough for its builds and seed sweeps.
10. **Which instance pilots it?** A small one (nl-shell, depotbuch) proves the boundary
    cheaply; wyrd proves the size.
