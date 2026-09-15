variable "do_token" {
  description = "DigitalOcean API token (Account -> API -> Generate New Token, read+write). Pass via TF_VAR_do_token env var or a gitignored terraform.tfvars — never commit this. See README.md."
  type        = string
  sensitive   = true
}

variable "region" {
  description = "DigitalOcean region slug. nyc1/nyc3/sfo3 are all fine for a throwaway test box — pick whichever is closest to you for less laggy SSH."
  type        = string
  default     = "nyc3"
}

# 2026-09-15: originally defaulted to s-8vcpu-16gb to comfortably exceed homelab-dev's own real,
# measured footprint (that node had ~15.9GB allocatable and only ~6.35GB free once Postgres/
# Keycloak/Argo CD/etc. were already running, before Trino ever entered the picture — see
# src/modules/trino/module.yaml's own dated comments on exactly this).
#
# 2026-09-15 (same day, live test run): reverted the default to s-4vcpu-8gb after hitting DO's own
# account resource-tier system live — s-8vcpu-16gb is $96/mo, which exceeds even Tier 2's $56/mo
# Basic-droplet cap (a brand-new account starts at Tier 1, capped at $48/mo; tiers advance via
# actual cumulative spend/prepayment, not just having a card on file — see DO's own
# docs.digitalocean.com/platform/resource-limits and .../products/droplets/details/limits). $48/mo
# is exactly s-4vcpu-8gb, so that's the size that actually works out of the box on a fresh account
# without first requesting a manual tier increase (Control Panel -> Settings -> Profile ->
# "Increase" — discretionary, not guaranteed, and typically needs real spend history to auto-grant
# for a jump this size). s-4vcpu-8gb is close to homelab-dev's own real day-to-day footprint anyway
# — less headroom than 16GB would give, but it's what a new account can actually provision. Override
# with TF_VAR_droplet_size if your account is already past Tier 1.
variable "droplet_size" {
  description = "DigitalOcean Droplet size slug. s-4vcpu-8gb (~$0.07/hr, ~$48/mo) by default — the largest Basic size a brand-new DO account (Tier 1) can provision without a manual limit increase. s-8vcpu-16gb (~$0.14/hr, ~$96/mo) gives more headroom for Trino/heavier modules if your account is past Tier 2."
  type        = string
  default     = "s-4vcpu-8gb"
}

variable "droplet_image" {
  description = "Base OS image slug. Ubuntu 24.04 LTS — matches what bootstrap/install.sh's own apt-based package steps (lib/common.sh) are written against."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "name_prefix" {
  description = "Prefix for the droplet/firewall/SSH-key names in your DO account, so multiple runs (or multiple people) don't collide. A short timestamp gets appended by run.sh."
  type        = string
  default     = "odp-ephemeral"
}

# Intentionally no default — an open-to-the-world SSH firewall rule on a box that's about to run
# your platform's real credentials (even ephemeral ones) is exactly the kind of thing worth forcing
# a conscious choice on. run.sh fills this in automatically from `curl -s ifconfig.me`; set it
# yourself if you're behind a VPN/proxy that changes your apparent IP, or want to allow a range.
variable "allowed_ssh_cidr" {
  description = "CIDR allowed to reach the droplet on port 22, e.g. \"203.0.113.4/32\". No default — must be set explicitly (run.sh does this for you)."
  type        = string
}
