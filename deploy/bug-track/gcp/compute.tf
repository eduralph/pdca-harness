# One template; the orchestrator creates one VM per job from it and may
# override machineType per stage (small for Reproduce, large for Fix/sweeps).
#
# Job contract (set per instance by the orchestrator, never in the template):
#   metadata "job-spec"   - JSON: repo, commit, stage, sanitized summary,
#                           repro test (Fix stage), budget limits
#   metadata "upload-url" - per-job signed PUT URL for the result bundle
#
# The image's /opt/bug-track/run-job reads both, does the work, uploads the
# bundle, and powers off. The orchestrator sees TERMINATED, collects the
# bundle, and deletes the instance. max_run_duration is the backstop.

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
    preemptible                 = true
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    instance_termination_action = "DELETE"

    max_run_duration {
      seconds = var.max_run_duration_seconds
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
    enable-oslogin         = "TRUE"
    startup-script         = <<-EOT
      #!/bin/bash
      /opt/bug-track/run-job || true
      poweroff
    EOT
  }

  lifecycle {
    create_before_destroy = true
  }
}
