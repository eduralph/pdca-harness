terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source = "hashicorp/google"
      # Pinned like every other dependency in this repository; `tofu init` writes
      # .terraform.lock.hash alongside. Bump deliberately.
      version = "~> 6.50"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
