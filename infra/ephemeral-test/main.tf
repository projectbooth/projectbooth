# 2026-09-15, hit live: `local.suffix` originally used `formatdate("YYYYMMDD-hhmmss", timestamp())`.
# Terraform's own docs explicitly warn that `timestamp()` re-evaluates to the CURRENT time on every
# single plan/apply, not just at creation — so every `terraform apply` against an EXISTING droplet
# recomputed a brand-new suffix, which meant `local.name` (and therefore the droplet's and SSH key's
# `name` argument) showed a diff on every re-run of run.sh, forcing a needless rename each time
# (visible live as "digitalocean_ssh_key.droplet_access: Modifying..." and the droplet's own name
# changing between runs while its IP stayed the same — harmless here since DO treats a name change as
# in-place, but still a real bug: a value meant to be a stable identity for the whole life of these
# resources should not silently drift on every apply). `random_id` is the fix — it's an actual
# resource, computed once at creation and then held fixed in state like any other resource, exactly
# the "compute once, keep stable" semantics this suffix needs.
resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  # Two hex chars per byte; keepers not needed since this is created once and never meant to change
  # for the life of this workspace's state (a fresh suffix per `terraform init` in a NEW state dir/
  # workspace is fine and expected — collision avoidance across separate runs, not within one).
  suffix = random_id.suffix.hex
  name   = "${var.name_prefix}-${local.suffix}"
}

# Droplet-access keypair — generated fresh by THIS Terraform run, registered with DO, injected into
# the droplet at boot. Lets the operator SSH in with `-i` against a key that exists nowhere except
# this run's state and whatever run.sh writes to disk locally (gitignored — see README.md). Never
# touches your own personal ~/.ssh key. Destroyed along with everything else on `terraform destroy`
# (DO deletes the registered key too, since it's a Terraform-managed resource, not a pre-existing
# one referenced by ID).
resource "tls_private_key" "droplet_access" {
  algorithm = "ED25519"
}

resource "digitalocean_ssh_key" "droplet_access" {
  name       = "${local.name}-access"
  public_key = tls_private_key.droplet_access.public_key_openssh
}

# Firewall: SSH only, from the one CIDR variables.tf requires you to set explicitly. Nothing else is
# exposed — this environment is reached entirely through SSH (direct commands, or a tunnel/
# port-forward for anything web-based, e.g. Argo CD's UI the same way homelab-dev's is reached).
# See README.md's "Why no Ingress/MetalLB" for why that's a deliberate simplification, not an
# oversight.
resource "digitalocean_firewall" "this" {
  name        = "${local.name}-fw"
  droplet_ids = [digitalocean_droplet.this.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.allowed_ssh_cidr]
  }

  # Outbound: fully open. This box needs to reach GitHub (clone + Argo CD's own polling), GHCR
  # (image pulls), and the DigitalOcean/apt package mirrors — restricting egress here would just
  # mean re-deriving that same allowlist by hand and keeping it in sync with whatever the platform
  # itself needs, for a box that lives a few hours and gets destroyed. Not worth it for this use case.
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

resource "digitalocean_droplet" "this" {
  name     = local.name
  region   = var.region
  size     = var.droplet_size
  image    = var.droplet_image
  ssh_keys = [digitalocean_ssh_key.droplet_access.fingerprint]

  # monitoring/backups both off on purpose — this box doesn't outlive the test it's created for, so
  # DO's monitoring agent and backup snapshots are pure overhead (and backups specifically add cost
  # for a box that's supposed to be free of state worth backing up).
  monitoring = false
  backups    = false
  ipv6       = false

  # Deliberately NOT using user_data/cloud-init to auto-clone-and-bootstrap on first boot. That
  # would mean baking a GitHub credential into cloud-init, which DigitalOcean stores as droplet
  # metadata retrievable via the API for the life of the droplet — a needless exposure window for a
  # credential this design otherwise keeps fully out of any persisted state. run.sh does the
  # clone+bootstrap over the SSH session it already has open instead (see its own comments).

  lifecycle {
    # A DO droplet's `image` only applies at CREATE time in practice (DO doesn't support truly
    # rebuilding in place via this field) — ignoring it here means a stray upstream change to what
    # "ubuntu-24-04-x64" resolves to doesn't produce a spurious replace-in-place plan for an
    # existing droplet between `apply` and `destroy` in the same session.
    ignore_changes = [image]
  }
}
