# Three identities, strictly separated:
#   runner        - attached to the batch runner; reads the cache bucket and the
#                   job prefix, writes results only through signed URLs.
#   orchestrator  - the driver container (ADR-0021 §4); creates and deletes runners,
#                   signs upload URLs, reads results. Runs no leaf.
#   cache_writer  - trusted builds of main; the only identity that writes the cache.
#
# No service account keys are created here: keys in state are a liability.
# Create them out of band (or use Workload Identity Federation) for the
# orchestrator and cache writer.

resource "google_service_account" "runner" {
  account_id   = "bug-track-runner"
  display_name = "Bug track runner (untrusted workloads)"

  depends_on = [google_project_service.services]
}

resource "google_service_account" "orchestrator" {
  account_id   = "bug-track-orchestrator"
  display_name = "Bug track orchestrator (trusted, off-VM)"

  depends_on = [google_project_service.services]
}

resource "google_service_account" "cache_writer" {
  account_id   = "bug-track-cache-writer"
  display_name = "Bug track build-cache writer (trusted main builds)"

  depends_on = [google_project_service.services]
}

# Orchestrator: manage instances in this project only.
resource "google_project_iam_member" "orchestrator_compute" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

# Orchestrator may attach the runner SA to VMs (and no other SA).
resource "google_service_account_iam_member" "orchestrator_uses_runner" {
  service_account_id = google_service_account.runner.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.orchestrator.email}"
}

# Orchestrator may sign blobs as itself, so signed upload URLs work keylessly.
resource "google_service_account_iam_member" "orchestrator_self_sign" {
  service_account_id = google_service_account.orchestrator.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.orchestrator.email}"
}

# Artifacts: orchestrator writes (via signed URLs it issues) and reads.
resource "google_storage_bucket_iam_member" "orchestrator_artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.orchestrator.email}"
}

# Cache: runner reads, cache writer writes. Nothing else.
resource "google_storage_bucket_iam_member" "runner_cache_read" {
  bucket = google_storage_bucket.cache.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_storage_bucket_iam_member" "cache_writer" {
  bucket = google_storage_bucket.cache.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cache_writer.email}"
}
