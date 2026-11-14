---
created: 15.11.2026 01:10
type: adr
status: Proposed
tags:
  - adr
  - orchestration
---
# 0022. A bundle in flight is claimed by a leased ref in the instance repository; its progress is read from that claim

## Context

An instance's bundles are driven from more than one place. `pdca flow` runs on an operator's
workstation, in remote Claude Code sessions started from a phone, and, with the bug track
(ADR-0020, ADR-0021), from a driver container on the operator's NAS that sends jobs to a
disposable cloud runner. Each of these places has its own clone of the instance repository.
Nothing stops two of them from driving the same bundle, and nothing tells an operator on
one of them which bundles the others are driving, or how far each has got.

What exists today is local to one host, and correctly so:

- **Drive claims** (`template/src/pdca_harness/drive_claim.py:1-48` at `2024ac5`) give one
  live driver per bundle *per host*: a non-blocking `flock` on a file under
  `process/.drive-claims/`, dropped by the OS when the run ends however it ends. No claim
  can go stale, so no stale claim needs cleaning up. No other host can see the lock.
- **Bug-track job claims** (ADR-0020 §12) are leases under `process/` in the driver
  container. They are per stage and seen only by that driver.
- **Bundle state** is a pure function of the bundle's files (ADR-0003, `state.state`,
  `state.py:348`). It is exact on the host that is driving the bundle and invisible
  everywhere else until `pdca record` commits the bundle, which happens after the fact.
- **`Conflicts with` edges** in a brief keep two bundles *of one batch* out of the same
  wave. They say nothing about a bundle another host is driving.

The one place every driving location already reaches is the instance's git remote. Any
cross-host claim has to work with what that remote offers: an atomic compare-and-swap on
refs (`git push --force-with-lease`). It must not depend on one forge's API, because
instances may live on GitHub, Codeberg or a self-hosted Forgejo. And it must keep the
driver stdlib-only (ADR-0008).

## Decision

### 1. A flight claim is a leased ref

We will claim a bundle across hosts with a **flight claim**: the ref
`refs/pdca/flight/<bundle>` (for example `refs/pdca/flight/issue_412`) on the instance
repository's remote.

- **Take.** A run takes a claim by pushing a parentless commit to the ref with
  `--force-with-lease=refs/pdca/flight/<bundle>:` (empty expectation: the ref must not
  exist). Exactly one of two racing runs succeeds. The run takes the flight claim *after*
  the local drive claim and *before* the first write to the bundle.
- **Heartbeat.** The holder replaces the commit at a fixed interval and at every state
  transition, each time with a lease on its own last commit. A heartbeat that is rejected
  means the claim was taken over: the run is **fenced**.
- **Fence.** A fenced run MUST NOT make any further remote write for that bundle: no
  branch push, no draft PR, no `pdca record`, no tracker change. It stops the bundle with a
  line naming the new holder. Local writes in its own worktree are harmless and are left
  for the operator to inspect. Before every remote write for the bundle, the driver
  confirms that it still holds the claim (a cheap `ls-remote` against its last commit).
- **Expire and take over.** A claim whose `lease_until` has passed MAY be taken over with a
  lease on the stale commit. If two runs race to take it over, one wins.
- **Release.** When the run lets the bundle go (it reaches a halted or terminal state, or
  the run decides not to drive it), it deletes the ref with a lease on its own commit. It
  never deletes a successor's claim.
- **Fail closed.** If the run cannot take the claim (remote unreachable, ref namespace
  refused), it refuses the bundle, exactly as `drive_claim` refuses one it cannot lock.
  `--rehearse` runs take no flight claims.
- **Opt-in.** The feature is enabled per instance with `[driver].flight_claims` (default
  `false` in `template/pdca.toml.jinja` until one instance has run it through a milestone).
  When it is off, behaviour is exactly as today.

Refs under `refs/pdca/` are outside the default fetch refspec, so clones do not fetch
them, branch lists do not show them, and pushing them triggers no workflow. A branch
would do all three.

### 2. What the claim holds

The commit's tree holds one file, `claim.json`:

| Field | Meaning |
|---|---|
| `schema` | `pdca-flight-claim/1` |
| `bundle` | the bundle directory name |
| `holder` | host, run id, lane, harness version, mode (`interactive`, `flow`, `bug-track`) |
| `taken`, `heartbeat`, `lease_until` | UTC timestamps |
| `state` | `state.state(d)` at the last heartbeat (ADR-0003) |
| `step` | what is running now: the leaf role or gate name, as the `progress` status probe already reports it |
| `touches` | reserved: path globs the bundle expects to change; empty until the issue-selection advisory consumes it |

Every field is written by deterministic driver code. No leaf writes or edits the claim, in
keeping with the rule ADR-0004 sets for §9. A leaf holds no credential that can push to the
instance remote (ADR-0021 §3), and the push guard (#962) judges ref-deleting pushes.

A reader MUST ignore fields it does not know. A claim with an unknown major `schema`
version, or one that does not parse, counts as **live** until its ref's commit is older
than the longest configured lease. It is reported as unreadable, and it is never taken
over sooner.

### 3. The claim is a view, never the state

The bundle's state stays in its files (ADR-0003). The claim records *who* is driving and
*since when*. Its `state` and `step` are a progress snapshot for other hosts, not an input
to any transition. If every flight ref were lost, the only thing lost would be the
knowledge of which bundles are in flight right now. The next run rediscovers that, or the
leases expire.

### 4. Seeing what is in flight

`pdca inflight` lists every flight claim on the remote: bundle, holder, state, step, age
and heartbeat age, with each marked LIVE, STALE or UNREADABLE (`--json` for automation).
`doctor` warns about stale claims. `pdca inflight --release <bundle>` lets a human delete
a claim with a lease on the commit they were shown. It is a human verb, and no leaf is
given it.

### 5. Lease timing

The heartbeat runs on its own thread, independently of the leaf. A leaf that is silent for
an hour therefore does not lose its claim, and a host that sleeps or dies does. Defaults:
heartbeat every 5 minutes, lease of 3 heartbeats (15 minutes). Both are configurable, and
`doctor` checks `lease ≥ 2 × heartbeat`.

### 6. The bug track

The bug-track driver takes one flight claim per issue for the whole time it has jobs in
flight for that issue, renewed from the driver container (not from the runner). Its job
claims (ADR-0020 §12) stay as they are, per stage and local. An interactive run and the
track can therefore never drive the same bug. If a spot runner disappears, the driver
keeps renewing the claim while it reclaims the job. If the driver itself dies, the claim
expires.

### Rejected

| Option | Why not |
|---|---|
| Tracker labels or assignees as the lock | No compare-and-swap: two runs read "free" and both claim. Forge-specific. |
| A forge's project board field | Forge-specific API, not atomic, and a second source of state beside ADR-0003. |
| A lock service (etcd, Redis) | A new stateful dependency that every driving location must reach across sites. It is the database ADR-0003 rules out. |
| A claim branch, or a status file committed to a branch | Pollutes branch lists, triggers workflows on every heartbeat, and a committed status file conflicts with itself. |
| A sticky tracker comment as the lock | No compare-and-swap. As a *projection* it remains possible (see [OPEN] below). |
| Stretching `drive_claim`'s `flock` across hosts (NFS) | Does not reach the runner or remote sessions. `drive_claim.py` already notes that NFS narrows the lock. |

**[OPEN]** Whether the driver also mirrors the claim into the target's tracker (a sticky
status comment or a label on the issue). Many target repositories are public, so this
would announce work in progress, and it adds a write the driver would have to fence.

**[OPEN]** The CSV batch's Plan session runs before the batch takes any claim
(`drive_claim.py:42-46`). Decide whether Plan takes flight claims on the ids it is about to
plan, or whether this gap stays documented as it is for drive claims.

## Consequences

- Two hosts can no longer drive one bundle. An operator can see, from any clone or none,
  what is in flight, where, and how far each bundle has got, without a new service.
- It works on any git remote that accepts custom refs and honours `--force-with-lease`.
  The implementation must prove this with a live test against GitHub and against
  Codeberg/Forgejo before the default changes. A remote that refuses `refs/pdca/*` leaves
  the feature unusable on that instance, and `doctor` must say so rather than fail open.
- Every remote write for a bundle gains a holder check: one round-trip per push, PR or
  record. Every heartbeat is a push. Old claim commits become unreachable and are
  garbage-collected by the forge.
- A host that sleeps past its lease loses its bundle, and a successor may already have
  redone the work. That is the price of not leaving a dead host's claim in place, and the
  fence keeps it from double-publishing.
- The `claim.json` format becomes a contract between harness versions running on
  different hosts. Once a second consumer reads it (the issue-selection advisory, the bug
  track), it is promoted to a spec, `flight-claim/v1`, under the proposal and spec process
  (ADR-0018).
- `touches` reserves the place for the overlap advisory, which compares a candidate's
  expected paths with every live claim. That advisory is separate work. This ADR only
  guarantees that the data has a place to live.
- Reversal is cheap while the feature is opt-in: turn the key off and delete
  `refs/pdca/flight/*`.
