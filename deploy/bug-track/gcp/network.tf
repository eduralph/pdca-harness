# Private network: no public IPs, outbound only through Cloud NAT,
# and only to the FQDN allowlist.

resource "google_project_service" "services" {
  for_each = toset([
    "compute.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "track" {
  name                    = "bug-track"
  auto_create_subnetworks = false

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "track" {
  name                     = "bug-track-${var.region}"
  region                   = var.region
  network                  = google_compute_network.track.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_router" "track" {
  name    = "bug-track"
  region  = var.region
  network = google_compute_network.track.id
}

resource "google_compute_router_nat" "track" {
  name                               = "bug-track"
  router                             = google_compute_router.track.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.track.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Egress policy. Ingress needs no rule: the VPC's implied ingress rule denies
# everything, and runners have no external IP and no SSH.
#
# FQDN matching is a Cloud NGFW feature; verify the tier and its pricing before
# applying. Fallback: replace the FQDN rule with an egress proxy on the trusted
# side and allow only the proxy's address.

resource "google_compute_network_firewall_policy" "egress" {
  name        = "bug-track-egress"
  description = "Runner egress: TCP 443 to an FQDN allowlist, deny everything else."
}

resource "google_compute_network_firewall_policy_association" "egress" {
  name              = "bug-track-egress"
  attachment_target = google_compute_network.track.id
  firewall_policy   = google_compute_network_firewall_policy.egress.name
}

resource "google_compute_network_firewall_policy_rule" "allow_fqdns" {
  firewall_policy = google_compute_network_firewall_policy.egress.name
  rule_name       = "allow-allowlisted-https"
  priority        = 1000
  direction       = "EGRESS"
  action          = "allow"
  enable_logging  = true

  match {
    dest_fqdns = var.egress_fqdns

    layer4_configs {
      ip_protocol = "tcp"
      ports       = ["443"]
    }
  }
}

resource "google_compute_network_firewall_policy_rule" "deny_all_egress" {
  firewall_policy = google_compute_network_firewall_policy.egress.name
  rule_name       = "deny-all-egress"
  priority        = 65000
  direction       = "EGRESS"
  action          = "deny"
  enable_logging  = true

  match {
    dest_ip_ranges = ["0.0.0.0/0"]

    layer4_configs {
      ip_protocol = "all"
    }
  }
}
