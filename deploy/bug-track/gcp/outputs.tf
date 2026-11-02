output "instance_template" {
  description = "Template the orchestrator creates per-job VMs from."
  value       = google_compute_instance_template.runner.self_link_unique
}

output "runner_service_account" {
  value = google_service_account.runner.email
}

output "orchestrator_service_account" {
  value = google_service_account.orchestrator.email
}

output "cache_writer_service_account" {
  value = google_service_account.cache_writer.email
}

output "artifact_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "cache_bucket" {
  value = google_storage_bucket.cache.name
}

output "subnetwork" {
  value = google_compute_subnetwork.track.id
}
