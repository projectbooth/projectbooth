# Ephemeral cloud test environment (2026-09-15) — a throwaway DigitalOcean droplet that runs the
# FULL platform (bootstrap/install.sh, unmodified) so you can test something real without homelab
# access. Built the day the Trino module-proxy investigation turned up a real bug
# (ghcr.io/dougallpercival/* image paths surviving the org migration to projectbooth — see
# src/core/argocd/manifests/gateway.yaml's own 2026-09-15 comment) that only a live cluster could
# have caught — this exists so "away from homelab" doesn't mean "can't test against a real
# cluster." See README.md in this directory for the actual workflow; this file just pins the
# providers so `terraform init` gets reproducible versions.
#
# Three providers: `digitalocean` creates the droplet + firewall; `tls` generates the droplet-access
# keypair entirely inside this Terraform run, for the operator to SSH into the droplet (this is the
# ONLY keypair Terraform itself generates — the GitHub deploy key is a separate one run.sh generates
# on your own machine via `ssh-keygen`, deliberately outside Terraform state, since it only needs to
# live on the droplet's filesystem for the duration of one `git clone` and gets revoked via `gh api`
# on teardown, not destroyed as a Terraform resource; see run.sh's own comments — an earlier version
# of this comment incorrectly described both as Terraform-managed, corrected 2026-09-15). `random`
# provides a naming suffix that's generated once and stays stable in state (see main.tf's own comment
# on why `timestamp()` was wrong for this).
terraform {
  required_version = ">= 1.5"
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "digitalocean" {
  token = var.do_token
}
