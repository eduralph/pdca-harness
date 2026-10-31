# One runner per batch (ADR-0021 §2). The driver creates one VM from this template
# when a poll found work, writes jobs to the job bucket, and deletes the VM when
# the agent has powered it off or the lifetime has expired.
#
# Batch contract (set per instance by the driver, never in the template):
#   metadata "batch-id"     - the batch this runner serves; the agent reads jobs from
#                             gs://<artifacts>/jobs/<batch-id>/ and writes results to
#                             gs://<artifacts>/results/<batch-id>/<job-id>/ via the
#                             signed URLs each job carries
#   metadata "intake-until" - epoch seconds after which the agent takes no new job
#   metadata "job-limit"    - per-container timeout in seconds
#
# The image's /opt/bug-track/agent runs each job in a fresh container (produce
# under gVisor, verify under plain rootless Podman with no network), uploads the
# result, and powers off when the queue is empty past a grace period or intake
# has closed and the last container exited. max_run_duration is the backstop.

resource "google_compute_instance_template" "runner" {
  name_prefix  = "bug-track-runner-"
  machine_type = var.machine_type
  labels       = var.labels

  disk {
    source_image = var.runner_image
    boot         = true
    auto_delete  = true
    disk_size_gb = var.boot_disk_gb
    disk_type    = "pd-balanced"
  }

  # No access_config block: no external IP.
  network_interface {
    subnetwork = google_compute_subnetwork.track.id
  }

  service_account {
    email  = google_service_account.runner.email
    scopes = ["https://www.googleapis.com/auth/devstorage.read_only"]
  }

  scheduling {
    provisioning_model          = "SPOT"
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    instance_termination_action = "DELETE"

    max_run_duration {
      seconds = var.lifetime_seconds
    }
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    enable-oslogin         = "FALSE" # no SSH at all, not IAM-gated SSH
    job-limit              = tostring(var.job_limit_seconds)
    startup-script         = <<-EOT
      #!/bin/bash
      /opt/bug-track/agent || true
      poweroff
    EOT
  }

  lifecycle {
    create_before_destroy = true
  }
}
