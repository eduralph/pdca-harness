resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-bug-track-artifacts"
  location                    = var.bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = var.labels

  lifecycle_rule {
    condition {
      age = var.artifact_retention_days
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket" "cache" {
  name                        = "${var.project_id}-bug-track-sccache"
  location                    = var.bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = var.labels

  lifecycle_rule {
    condition {
      age = var.cache_retention_days
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}
