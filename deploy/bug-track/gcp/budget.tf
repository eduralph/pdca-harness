# Alerting only: a budget never stops spend. max_run_duration and the
# orchestrator's reaper are the real limits.

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_billing_budget" "track" {
  count = var.billing_account == "" ? 0 : 1

  billing_account = var.billing_account
  display_name    = "bug-track"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.monthly_budget)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
}
