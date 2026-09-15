# Ephemeral cloud test environment

Stands up a throwaway DigitalOcean droplet, installs the **real, unmodified** `bootstrap/install.sh`
on it (same script homelab-dev uses — full platform: Postgres, Keycloak, SeaweedFS, gateway,
ui-shell, Argo CD, and whatever modules you install afterward), and tears the whole thing down again
— droplet, firewall, SSH keys, and the short-lived GitHub deploy key it registers for the clone — when
you're done. The point is a fresh, disposable platform to test against without needing access to
homelab-dev (e.g. no Cloudflare WARP required), and without leaving anything billable or any stray
credentials behind afterward.

Not CI-integrated — this is a manual, on-demand tool you run from your own machine when you want a
test box.

## Prerequisites

- A DigitalOcean account and an API token with read+write scope: **Account → API → Generate New
  Token**. Export it as `TF_VAR_do_token` (never commit it, never put it in `terraform.tfvars`).
- [`terraform`](https://developer.hashicorp.com/terraform/install) (`>= 1.5`), `ssh`/`scp`, `curl`,
  `ssh-keygen` — all standard.
- [`gh`](https://cli.github.com/), authenticated (`gh auth login`) against an account with permission
  to add deploy keys on `projectbooth/projectbooth` (Settings → Deploy keys on the repo, or org-level
  equivalent). `run.sh` uses this to register and later revoke a short-lived, read-only deploy key —
  it never touches your own personal SSH key or GitHub PAT. **`gh auth login`'s default browser/OAuth
  flow does not request the scope this needs** (confirmed live, 2026-09-15 — the deploy-key API 403s
  with just the default `repo`/`read:org`/`gist` scopes). After logging in, also run:
  ```bash
  gh auth refresh -h github.com -s admin:public_key
  ```
  `run.sh` checks for this scope up front and tells you to run the above if it's missing, before it
  creates anything billable.

## Usage

```bash
cd infra/ephemeral-test
export TF_VAR_do_token=dop_v1_...
./run.sh
```

`terraform apply` runs interactively (not auto-approved) — review the plan before typing `yes`, since
this creates a real, billable droplet. Everything else after that is automatic: waiting for SSH,
generating and registering the deploy key, cloning, and running `bootstrap/install.sh`. The whole
thing takes several minutes, most of it the platform's own install step (visible in the streamed
output).

Want a specific branch instead of `dev`? `REVISION=my-branch ./run.sh`.

When you're done:

```bash
./destroy.sh
```

**Always use `destroy.sh`, not a bare `terraform destroy`.** The deploy key is registered via `gh api`,
not by Terraform — Terraform has no record of it and won't clean it up. `destroy.sh` revokes the
deploy key first, then destroys the droplet/firewall/SSH-key, then removes the local state files
(`.ephemeral-state.json`, `.droplet-access-key`, `.deploy-key*`).

## What you get / how to reach it

`run.sh` prints the connection details at the end, but in short:

```bash
ssh -i .droplet-access-key root@<droplet-ip>                       # direct shell
ssh -i .droplet-access-key root@<droplet-ip> 'kubectl get applications -n argocd -w'
```

For anything web-based (Argo CD's UI, a module's UI via the gateway proxy, etc.), tunnel it over SSH —
the same pattern already used to reach homelab-dev's Argo CD UI:

```bash
ssh -i .droplet-access-key -L 8080:localhost:443 root@<droplet-ip>
# in that same SSH session, on the droplet:
kubectl -n argocd port-forward svc/argocd-server 8080:443
# then from YOUR machine, browse https://localhost:8080
```

## Why no Ingress/MetalLB

`ingress-nginx`'s Service is `type: LoadBalancer`. On homelab-dev, MetalLB hands that a real LAN IP.
A single cloud droplet has no LAN to ARP on and no cloud load balancer wired up, so that Service would
just sit `Pending` forever without one or the other. Rather than provisioning (and paying for) a real
DigitalOcean Load Balancer for a box that lives a few hours, `run.sh` passes `--skip-metallb` and this
environment is reached entirely through SSH — direct `kubectl`, or a tunnel/port-forward for anything
with a web UI, exactly as shown above. If you specifically need to test Ingress/TLS/DNS behavior
itself, this environment isn't the right tool for that — it's for testing the platform's own
components against a fresh cluster.

## Before installing gateway/ui-shell/catalog-service

Same requirement as homelab-dev right now: their GHCR images live at `ghcr.io/projectbooth/*`, which
default to private for an org-owned package. Either make those packages public, or configure a real
`imagePullSecret` — this isn't specific to this environment, it's a standing prerequisite until that's
resolved once, upstream.

## Gateway secrets

`run.sh` provisions two Kubernetes Secrets in the `gateway` namespace directly on the droplet, after
`bootstrap/install.sh` finishes — `gateway-module-proxy-secret` (a freshly generated HS256 signing
key, no external credential involved) and `gateway-github-token` (a placeholder, unless you set
`GATEWAY_GITHUB_PAT` yourself beforehand). This is necessary, not optional: `gateway.yaml` commits
both as `SealedSecret`s, but a `SealedSecret` is encrypted specifically for the sealed-secrets
controller key on ONE cluster — the ones committed in git were sealed for homelab-dev's key, and this
freshly bootstrapped cluster generates its own, different keypair, so it can never decrypt them.
Without this step, gateway sits in `CreateContainerConfigError` waiting on Secrets that will never
materialize.

Re-sealing and committing a version for this cluster would be the wrong fix — `bootstrap/seal-gateway-
*.sh` both overwrite the same shared `gateway.yaml`, so that would break homelab-dev's already-working
secrets. Since this cluster is throwaway, creating the underlying Secrets directly here, never
touching git, is the right scope for the fix.

If you specifically want to exercise the module-lifecycle-dispatch feature (not needed for Trino
verification or most other testing) against this cluster, export a real fine-grained PAT first — same
requirements as `bootstrap/seal-gateway-github-token.sh`'s own header (Actions: read/write, this repo
only, nothing else):
```bash
export GATEWAY_GITHUB_PAT=github_pat_...
./run.sh
```

## Cost

DigitalOcean Basic Droplet pricing (checked 2026-09-15, confirm current pricing before a long-running
test — https://www.digitalocean.com/pricing/droplets):

| Size | vCPU / RAM | Hourly | Monthly (for reference — this is billed hourly, not monthly) |
|---|---|---|---|
| `s-4vcpu-8gb` (default here) | 4 / 8GB  | ~$0.07143/hr | ~$48/mo |
| `s-8vcpu-16gb` | 8 / 16GB | ~$0.14286/hr | ~$96/mo |

You're only billed for the hours the droplet actually exists — a typical test session (provision, run
some checks, destroy) measured in an hour or two costs well under a dollar.

**A brand-new DigitalOcean account can't use `s-8vcpu-16gb` by default.** DO gates droplet sizes by an
account resource tier (Tier 1 for new accounts, capped at $48/mo Basic droplets — exactly
`s-4vcpu-8gb`) that advances with actual cumulative spend/prepayment, not just having a card on file;
requesting `s-8vcpu-16gb` on a fresh account fails with `422 ... this size is currently restricted`.
That's why `s-4vcpu-8gb` is the default — it's what actually provisions out of the box. It's also
close to homelab-dev's own real day-to-day footprint (~15.9GB allocatable, ~6.35GB free once the core
platform was already up, before Trino even entered the picture), just with less headroom than 16GB
would give. If your account is already past Tier 1 (or you request a manual limit increase via
Control Panel → Settings → Profile → "Increase" — discretionary, and usually needs real spend
history to auto-grant a jump this size), bump it back up with
`TF_VAR_droplet_size=s-8vcpu-16gb ./run.sh`, or edit `variables.tf`'s default. There's no separate
charge for the firewall or SSH key resources.

## Design notes

- **Two separate SSH keypairs, both ephemeral, neither ever your own.** `main.tf` generates a
  droplet-access keypair entirely within Terraform state (`tls_private_key`) for logging into the box
  itself; `run.sh` generates a second, separate keypair on your machine purely to register as a
  read-only GitHub deploy key for the clone. Neither is your personal `~/.ssh` key or a long-lived
  credential. Both are destroyed/revoked by `destroy.sh`.
- **No cloud-init/`user_data`.** The clone-and-bootstrap step runs over the SSH session `run.sh`
  already has open, not via cloud-init, specifically so the deploy key's private half is never baked
  into droplet metadata (which DigitalOcean stores API-retrievable for the droplet's whole lifetime) —
  it only ever touches the droplet's actual filesystem, scoped to the life of that filesystem.
- **`monitoring`/`backups` off, `ipv6` off.** Pure overhead for a box that doesn't outlive its test.
- Firewall allows inbound SSH only, from the one IP `run.sh` auto-detects (`curl ifconfig.me`) — never
  open to `0.0.0.0/0`. Outbound is fully open (needs GitHub, GHCR, and apt/package mirrors; not worth
  hand-deriving a tighter allowlist for a box that lives a few hours).
- `REPO_URL` in `run.sh` is hardcoded to the plain `github.com` hostname rather than auto-detected via
  `git remote get-url origin` (the pattern `bootstrap/install.sh` itself uses elsewhere) — that
  auto-detection is a known, separately-tracked issue because it can bake in a local-only SSH host
  alias that means nothing on a brand-new box with no such alias configured.
