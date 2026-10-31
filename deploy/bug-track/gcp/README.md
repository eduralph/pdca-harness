# Bug-track runner — GCP backend (OpenTofu)

Reference infrastructure for the first runner backend of the bug track: one spot VM per
batch, containers per job. Decision and rationale:
[ADR-0020](../../../docs/design/adr/0020-bug-track.md) (the track) and
[ADR-0021](../../../docs/design/adr/0021-bug-track-runner.md) (this runner and the driver
container).

This directory is operator infrastructure. It is not rendered into instances by Copier
(ADR-0007): it carries the operator's project, billing and network. The "orchestrator"
below is the driver container of ADR-0021 §4, which an instance renders under
`deploy/driver/` and the operator runs on a schedule.

## What this creates

| Resource | Purpose |
|----------|---------|
| VPC `bug-track`, private subnet, Cloud NAT | The runner has no public IP; outbound only via NAT, every connection logged |
| Network firewall policy | Egress TCP 443 to `egress_fqdns` (model endpoint, Cloud Storage) only; everything else denied |
| Service accounts `runner`, `orchestrator`, `cache-writer` | Strictly separated identities (see `iam.tf`) |
| Buckets `*-bug-track-artifacts`, `*-bug-track-sccache` | Jobs and results (signed-URL upload, short retention); read-only dependency and build cache for the runner |
| Instance template `bug-track-runner-*` | Spot, x86_64 allowlist, shielded, no SSH, `max_run_duration` = `lifetime_seconds` → DELETE |
| Billing budget (optional) | Alert at 50/90/100 %; alerting only |

## What it deliberately does not create

- **The project.** Bring an existing, dedicated project (`project_id`).
- **The runner image.** Built with Packer into an image family (job agent, gVisor, rootless
  Podman, the container images for produce and verify); pass it as `runner_image`.
- **Service account keys.** Keep them out of state. The orchestrator's credential is a key
  file in the driver container, rotated; the cache writer's lives with the trusted cache
  job.
- **The driver container, the model-key gateway, publish.** These run on the trusted side,
  never in this project.

## Usage

```sh
tofu init
tofu plan  -var project_id=<project> \
           -var runner_image=projects/<project>/global/images/family/bug-track-runner
tofu apply ...
```

## Batch flow

1. The driver's poll finds eligible work (ADR-0020 §2); if none, nothing is created.
2. It writes the first jobs to `gs://<artifacts>/jobs/<batch-id>/`, each with a signed
   PUT URL for its result, and creates one VM from the template with `batch-id` and
   `intake-until` in metadata.
3. The agent on the runner takes jobs, runs each in a fresh container (produce under
   gVisor with egress to the model endpoint only; verify under plain Podman with no
   network), and uploads the result. The driver reads results, writes verdicts itself,
   and queues the next stages.
4. After `intake-until` the agent takes nothing new; it powers off when the queue is empty
   past a grace period or the last container exits. The driver deletes the instance; the
   provider deletes it at `lifetime_seconds` regardless.
5. A spot interruption ends the batch; claimed jobs expire and the next batch retries them.

## Verify before first apply

- **FQDN egress rules** need Cloud NGFW features; check the required tier and its cost.
  FQDN rules match resolved addresses, so the runner image's own name-based filter is the
  second line.
- **`max_run_duration`** on instance templates needs a recent `google` provider; the pin in
  `versions.tf` is a floor.
- **Spot capacity** for the chosen machine type in `europe-west3`.
- **Budget currency** must match the billing account's currency.
- **The cache bucket** must be populated by the trusted cache job before the first batch, or
  every build is cold.
