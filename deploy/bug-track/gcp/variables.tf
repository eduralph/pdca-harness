variable "project_id" {
  description = "Existing, dedicated GCP project for the bug track. Nothing else should run in it."
  type        = string
}

variable "region" {
  description = "Region for network, NAT and runner VMs."
  type        = string
  default     = "europe-west3"
}

variable "subnet_cidr" {
  description = "Private range for runner VMs."
  type        = string
  default     = "10.80.0.0/24"
}

variable "runner_image" {
  description = "Runner image or image family, e.g. projects/<project>/global/images/family/bug-track-runner (built with Packer)."
  type        = string
}

variable "machine_type" {
  description = "Runner machine type for the batch. x86_64 only (SIMD-path parity); the driver never overrides it."
  type        = string
  default     = "n2d-standard-16"

  validation {
    # Allowlist of x86_64 families, not a denylist of Arm ones.
    condition     = can(regex("^(n2|n2d|c2|c2d|c3|c3d|c4|n4|e2)-", var.machine_type))
    error_message = "Runner must be an x86_64 family (n2, n2d, c2, c2d, c3, c3d, c4, n4, e2)."
  }
}

variable "boot_disk_gb" {
  description = "Boot disk size in GB."
  type        = number
  default     = 100
}

variable "lifetime_seconds" {
  description = "Hard runner lifetime enforced by GCP (max_run_duration); the VM is deleted when it expires. ADR-0021 §2: 23 h."
  type        = number
  default     = 82800
}

variable "intake_cutoff_seconds" {
  description = "After this many seconds the runner's agent takes no new job and powers off when the last one ends. ADR-0021 §2: 20 h."
  type        = number
  default     = 72000
}

variable "job_limit_seconds" {
  description = "Per-container timeout. Must not exceed lifetime - intake cutoff (the drain window), so a job started at the cutoff still ends before the hard stop."
  type        = number
  default     = 10800

  validation {
    condition     = var.job_limit_seconds <= var.lifetime_seconds - var.intake_cutoff_seconds
    error_message = "job_limit_seconds must be <= lifetime_seconds - intake_cutoff_seconds."
  }
}

variable "egress_fqdns" {
  description = "The only destinations the runner may reach (TCP 443): the model endpoint and Cloud Storage. Dependencies come from the cache bucket, never from a registry; nothing on the runner reaches the tracker."
  type        = list(string)
  default = [
    "api.anthropic.com",
    "storage.googleapis.com",
  ]
}

variable "bucket_location" {
  description = "Location for the artifact and cache buckets."
  type        = string
  default     = "EUROPE-WEST3"
}

variable "artifact_retention_days" {
  description = "Job results (test and diff bytes, verify output, token rows; never runner logs) are deleted after this many days."
  type        = number
  default     = 14
}

variable "cache_retention_days" {
  description = "Build-cache objects are deleted after this many days."
  type        = number
  default     = 30
}

variable "billing_account" {
  description = "Billing account ID for the budget alert. Empty string disables the budget."
  type        = string
  default     = ""
}

variable "monthly_budget" {
  description = "Monthly budget amount (alerting only, does not cap spend)."
  type        = number
  default     = 100
}

variable "budget_currency" {
  description = "Must match the billing account's currency."
  type        = string
  default     = "EUR"
}

variable "labels" {
  description = "Labels applied to all labelable resources."
  type        = map(string)
  default = {
    track = "bug"
  }
}
