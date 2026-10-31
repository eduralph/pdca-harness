# Bug-track runner — GCP adapter (OpenTofu)

Reference infrastructure for the first `LeafRunner` backend of the bug track: the
Reproduce leaf, the builder leaf and the gates run on one Google Compute Engine Spot VM
per job. Decision and rationale:
[ADR-0020](../../../docs/design/adr/0020-bug-track.md) (the track) and
[ADR-0021](../../../docs/design/adr/0021-bug-track-runner.md) (this runner).

This directory is operator infrastructure. It is not rendered into instances by Copier
(ADR-0007): it carries the operator's project, billing and network. The "orchestrator"
below is the host that runs the instance's driver.

## What this creates

| Resource | Purpose |
|----------|---------|
| VPC `bug-track`, private subnet, Cloud NAT | Runners have no public IP; outbound only via NAT |
| Network firewall policy | Egress TCP 443 to `egress_fqdns` only; everything else denied |
| Service accounts `runner`, `orchestrator`, `cache-writer` | Strictly separated identities (see `iam.tf`) |
| Buckets `*-bug-track-artifacts`, `*-bug-track-sccache` | Job results (signed-URL upload, short retention); read-only build cache for runners |
| Instance template `bug-track-runner-*` | Spot, x86_64, shielded, no SSH, hard `max_run_duration` → DELETE |
| Billing budget (optional) | Alert at 50/90/100 % |

## What it deliberately does not create

- **The project.** Bring an existing, dedicated project (`project_id`).
- **The runner image.** Built with Packer into an image family; pass it as
  `runner_image`.
- **Service account keys.** Keep them out of state. Create the orchestrator and
  cache-writer credentials out of band, or use Workload Identity Federation.
- **The orchestrator, the publisher, the model-key gateway.** These run on the
  trusted side (n8n / control Pi), never in this project.

## Usage

```sh
tofu init
tofu plan  -var project_id=<project> \
           -var runner_image=projects/<project>/global/images/family/bug-track-runner
tofu apply ...
```

## Orchestrator flow per job

1. Write the job to `job-spec` metadata; generate a signed PUT URL for
   `gs://<artifacts>/<job-id>/bundle.tar.zst` and set it as `upload-url`.
2. `instances.insert` from the template (`sourceInstanceTemplate`), overriding
   `machineType` for the stage.
3. Poll until `TERMINATED` (job done) or the instance is gone (spot preemption or
   `max_run_duration` → retry the job).
4. Fetch the bundle, delete the instance, hand the bundle to the publisher.
5. Reaper: delete any `track=bug` instance older than its budget.

## Verify before first apply

- **FQDN egress rules** need Cloud NGFW features; check the required tier and
  its cost. Fallback: an egress proxy on the trusted side.
- **`max_run_duration`** on instance templates needs a recent `google` provider;
  if yours rejects it, use `google-beta` for the template.
- **Spot capacity** for the chosen machine type in `europe-west3`.
- **Budget currency** must match the billing account's currency.
- **Egress allowlist** covers everything the runner image fetches at run time
  (crates, git, model API, Cloud Storage for cache and upload).
