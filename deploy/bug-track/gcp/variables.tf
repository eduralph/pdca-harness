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
  description = "Default machine type. x86_64 only (SIMD-path parity). The orchestrator may override per stage."
  type        = string
  default     = "n2d-standard-16"

  validation {
    condition     = !can(regex("^(t2a|c4a)-", var.machine_type))
    error_message = "Runner must be x86_64; Arm machine types are not allowed."
  }
}

variable "boot_disk_gb" {
  description = "Boot disk size in GB."
  type        = number
  default     = 100
}

variable "max_run_duration_seconds" {
  description = "Hard VM lifetime enforced by GCP; the VM is deleted when it expires."
  type        = number
  default     = 10800
}

variable "egress_fqdns" {
  description = "The only destinations runner VMs may reach (TCP 443)."
  type        = list(string)
  default = [
    "api.anthropic.com",
    "crates.io",
    "index.crates.io",
    "static.crates.io",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "storage.googleapis.com",
  ]
}

variable "bucket_location" {
  description = "Location for the artifact and cache buckets."
  type        = string
  default     = "EUROPE-WEST3"
}

variable "artifact_retention_days" {
  description = "Job artifacts (diffs, test files, logs) are deleted after this many days."
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
