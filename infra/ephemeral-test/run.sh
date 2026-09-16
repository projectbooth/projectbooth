#!/usr/bin/env bash
# Stands up a throwaway DigitalOcean droplet, clones this repo onto it via a short-lived GitHub
# deploy key (never your own personal key, never a long-lived one), and runs the REAL
# bootstrap/install.sh unmodified — same script homelab-dev uses. See README.md for the full
# picture; this is just the orchestration `terraform apply` alone can't do (SSH-based clone +
# bootstrap, deploy-key lifecycle).
#
# Deliberately its own small helper set rather than sourcing bootstrap/lib/common.sh — this
# directory is a cloud-provisioning concern, bootstrap/ is an in-cluster one, and pulling one into
# the other for four echo wrappers isn't worth the cross-directory coupling.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

_c_red=$'\033[0;31m'; _c_yellow=$'\033[0;33m'; _c_green=$'\033[0;32m'; _c_blue=$'\033[0;34m'; _c_reset=$'\033[0m'
info()    { echo "${_c_blue}==>${_c_reset} $*"; }
success() { echo "${_c_green}==>${_c_reset} $*"; }
warn()    { echo "${_c_yellow}==> warning:${_c_reset} $*" >&2; }
die()     { echo "${_c_red}==> error:${_c_reset} $*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not found on PATH."; }

# REPO_URL is hardcoded to the plain github.com hostname on purpose, not auto-detected via
# `git remote get-url origin` the way bootstrap/install.sh defaults elsewhere — that auto-detection
# is a known, still-open issue (flagged 2026-09-15, Trino live-verification) precisely because it
# can bake in a LOCAL SSH host alias (e.g. "github.com-projectbooth") that means nothing inside a
# repo-server pod, or here, a brand-new droplet with no such alias configured. The plain hostname
# always resolves the same way everywhere.
REPO_URL="git@github.com:projectbooth/projectbooth.git"
GH_REPO="projectbooth/projectbooth"
REVISION="${REVISION:-dev}"
STATE_FILE="$SCRIPT_DIR/.ephemeral-state.json"
ACCESS_KEY_FILE="$SCRIPT_DIR/.droplet-access-key"
DEPLOY_KEY_FILE="$SCRIPT_DIR/.deploy-key"

require_cmd terraform
require_cmd ssh
require_cmd scp
require_cmd gh
require_cmd curl
require_cmd ssh-keygen

gh auth status >/dev/null 2>&1 || die "gh isn't authenticated — run 'gh auth login' first (needed to register a short-lived read-only deploy key on ${GH_REPO})."

# 2026-09-15, hit live: 'gh auth login's default browser/OAuth flow only requests repo/read-only/gist
# scopes — it does NOT request the admin:public_key (or write:public_key) scope the "Add a deploy
# key" REST API endpoint needs, so the gh api call further down fails with a 403 'Resource not
# accessible by personal access token'. Checking for it here, before terraform ever creates anything
# billable, rather than discovering it only after the droplet's already up (which is exactly what
# happened the first time this script ran end-to-end).
if ! gh auth status 2>&1 | grep -qE "admin:public_key|write:public_key"; then
  die "Your 'gh' auth token is missing the scope deploy-key registration needs. Run: \
'gh auth refresh -h github.com -s admin:public_key', then re-run this script. (admin:public_key is \
requested rather than the narrower write:public_key because destroy.sh's teardown needs to DELETE \
the key too, which write:public_key alone doesn't cover.)"
fi

if [[ -f "$STATE_FILE" ]]; then
  die "Found an existing ${STATE_FILE} — looks like a prior run wasn't torn down. Run ./destroy.sh first, or remove that file by hand if you're sure nothing's left running."
fi

: "${TF_VAR_do_token:?Set TF_VAR_do_token to your DigitalOcean API token first (Account -> API -> Generate New Token). See README.md.}"

info "Detecting your public IP for the SSH firewall rule..."
# 2026-09-16, hit live: a dual-stack machine (IPv6 available and preferred by its resolver — common
# on home ISPs and increasingly the default) makes a bare `curl https://ifconfig.me` come back with
# an IPv6 address, which the IPv4-CIDR regex below correctly rejects — but the die message's own
# advice ("set TF_VAR_allowed_ssh_cidr yourself and re-run") used to be a dead end: this block ran
# unconditionally and stomped any pre-set value with the freshly (re-)detected one, contradicting
# variables.tf's own doc comment for allowed_ssh_cidr ("set it yourself if you're behind a
# VPN/proxy..."). Fixed two ways: `-4` forces the detection itself onto IPv4 (fixes this exact case
# with no manual step at all), and a pre-set TF_VAR_allowed_ssh_cidr is now honored and skips
# detection entirely, so the escape hatch the variable's own docs already promised actually works.
if [[ -n "${TF_VAR_allowed_ssh_cidr:-}" ]]; then
  info "TF_VAR_allowed_ssh_cidr already set to ${TF_VAR_allowed_ssh_cidr} — using it as-is, skipping auto-detection."
else
  MY_IP="$(curl -4 -s https://ifconfig.me)"
  [[ "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Couldn't determine a plausible public IPv4 (got: '${MY_IP}'). Set TF_VAR_allowed_ssh_cidr yourself (e.g. export TF_VAR_allowed_ssh_cidr=\"203.0.113.4/32\") and re-run, or check your network."
  export TF_VAR_allowed_ssh_cidr="${MY_IP}/32"
fi
info "Allowing SSH from ${TF_VAR_allowed_ssh_cidr} only."

info "terraform init..."
terraform init -input=false

info "terraform apply — this creates real, billable DigitalOcean resources (a droplet, ~\$0.07-0.14/hr while it exists). Review the plan:"
terraform apply

DROPLET_IP="$(terraform output -raw droplet_ip)"
terraform output -raw ssh_private_key_pem > "$ACCESS_KEY_FILE"
chmod 600 "$ACCESS_KEY_FILE"
success "Droplet up at ${DROPLET_IP}."

ssh_droplet() {
  ssh -i "$ACCESS_KEY_FILE" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "root@${DROPLET_IP}" "$@"
}

info "Waiting for SSH to come up..."
for _ in $(seq 1 30); do
  if ssh_droplet true 2>/dev/null; then
    break
  fi
  sleep 5
done
ssh_droplet true 2>/dev/null || die "SSH never came up after 150s. Check the droplet in the DO control panel — it may just need more time; re-run this script once it's reachable (delete ${STATE_FILE} first if it was written)."
success "SSH is up."

info "Generating a short-lived, read-only GitHub deploy key for this run (never your own key, revoked on teardown)..."
ssh-keygen -t ed25519 -f "$DEPLOY_KEY_FILE" -N "" -C "ephemeral-test-$(date +%s)" -q
DEPLOY_KEY_TITLE="ephemeral-test-$(date +%s)"

# 2026-09-15, hit live, twice: the original `gh api ... --jq '.id'` form, run inside `$(...)`, hid the
# real failure reason both times a registration failed. gh sends the response body (the JSON with the
# actual error message) to stdout, which command substitution captures into the variable; only the
# terse "gh: <summary> (HTTP nnn)" line goes to stderr and is what you actually see on screen. Worse,
# `set -e` kills the script the instant that `gh api` call exits non-zero — command substitution never
# finishes assigning, so the die() message that used to sit on the next line never ran either. Fetching
# the response as plain text first (the `|| true`-equivalent `{ ... } ||` block below keeps set -e from
# short-circuiting before we can inspect it) fixes both: the real reason is always visible, and the two
# causes actually hit live (missing gh scope; deploy keys disabled at the org level) get an actionable
# hint instead of a bare HTTP status.
DEPLOY_KEY_RESPONSE="$(gh api "repos/${GH_REPO}/keys" -f "title=${DEPLOY_KEY_TITLE}" -f "key=$(cat "${DEPLOY_KEY_FILE}.pub")" -F "read_only=true" 2>&1)" || {
  if grep -q "Deploy keys are disabled for this repository" <<<"$DEPLOY_KEY_RESPONSE"; then
    die "GitHub rejected the deploy key: deploy keys are disabled at the ORG level for ${GH_REPO%%/*} \
— a separate setting from the gh token scope, and off by default on newer orgs. An org OWNER needs to \
enable it: org page -> Settings -> Access -> Member privileges -> Deploy keys section -> \"Allow \
members to create deploy keys\". Then re-run this script (delete ${DEPLOY_KEY_FILE}* first — it'll \
otherwise hit an interactive overwrite prompt)."
  elif grep -qi "Resource not accessible by personal access token" <<<"$DEPLOY_KEY_RESPONSE"; then
    die "GitHub rejected the deploy key: your gh token is missing the admin:public_key scope. Run \
'gh auth refresh -h github.com -s admin:public_key', then re-run this script. (This should have been \
caught by the scope check earlier in this script — seeing it here instead means the scope was revoked \
or changed after that check ran.)"
  else
    die "Failed to register the deploy key via 'gh api'. Full response:
${DEPLOY_KEY_RESPONSE}"
  fi
}
# 2026-09-15, hit live: a python3 one-liner was here originally, but on Windows/Git Bash `python3`
# can resolve on PATH via a Microsoft Store "App Execution Alias" stub even with no real Python
# installed — require_cmd's `command -v python3` check passes against that stub (it genuinely IS on
# PATH), so this wasn't caught until the actual invocation, which prints a Store-install prompt
# instead of doing anything. Rather than add a real python3 dependency just for one integer field,
# extract it with grep against GitHub's known, flat, single-level response shape (id/key/url/title/
# verified/created_at/read_only/added_by/last_used — "id" only ever appears once, at top level).
DEPLOY_KEY_ID="$(grep -oE '"id"[[:space:]]*:[[:space:]]*[0-9]+' <<<"$DEPLOY_KEY_RESPONSE" | head -1 | grep -oE '[0-9]+$')"
[[ -n "$DEPLOY_KEY_ID" ]] || die "Deploy key registration looked like it succeeded but couldn't find \
an \"id\" field in the response to confirm it. Full response:
${DEPLOY_KEY_RESPONSE}"
success "Deploy key registered on ${GH_REPO} (id ${DEPLOY_KEY_ID}, read-only)."

# Persisted so destroy.sh can revoke it even if this shell exits before reaching that point.
cat > "$STATE_FILE" <<EOF
{"deploy_key_id": ${DEPLOY_KEY_ID}, "droplet_ip": "${DROPLET_IP}"}
EOF

info "Copying the deploy key up and cloning ${GH_REPO}@${REVISION}..."
scp -i "$ACCESS_KEY_FILE" -o StrictHostKeyChecking=accept-new "$DEPLOY_KEY_FILE" "root@${DROPLET_IP}:/root/deploy_key"
ssh_droplet "chmod 600 /root/deploy_key"
ssh_droplet "export GIT_SSH_COMMAND='ssh -i /root/deploy_key -o StrictHostKeyChecking=accept-new'; apt-get update -qq && apt-get install -y -qq git >/dev/null && git clone --quiet --branch '${REVISION}' '${REPO_URL}' /root/projectbooth"
success "Cloned onto the droplet."

info "Running bootstrap/install.sh on the droplet — this takes several minutes (full platform: Postgres, Keycloak, SeaweedFS, gateway, ui-shell, Argo CD). Streaming output:"
# --skip-metallb: see README.md's "Why no Ingress/MetalLB" — this single droplet has no LAN to ARP
# on, so ingress-nginx's LoadBalancer Service would just sit Pending forever without it. Everything
# is reached via SSH (kubectl directly, or port-forward/tunnels for anything web-based) instead.
ssh_droplet "cd /root/projectbooth && ./bootstrap/install.sh --repo-url '${REPO_URL}' --revision '${REVISION}' --repo-ssh-key /root/deploy_key --skip-metallb"

# 2026-09-15, hit live: gateway.yaml carries two SealedSecrets (gateway-github-token,
# gateway-module-proxy-secret) committed to git — but a SealedSecret is encrypted specifically for
# the sealed-secrets controller's key on ONE cluster (see bootstrap/seal-gateway-*.sh's own headers).
# They were sealed for homelab-dev's key; THIS cluster generated its own, different keypair during
# bootstrap, so it can never decrypt them, and gateway sits in CreateContainerConfigError forever
# waiting on Secrets that will never materialize. Re-sealing and committing a version for this
# cluster is the WRONG fix — both seal scripts overwrite the same shared gateway.yaml, so that would
# break homelab-dev's already-working secrets. Since this cluster is throwaway, the right fix is
# creating the underlying Secrets directly here, out-of-band, never touching git — see README.md's
# "Gateway secrets" section for the full reasoning.
info "Waiting for the gateway namespace (core, sync-wave 4) to exist..."
for _ in $(seq 1 30); do
  ssh_droplet "kubectl get ns gateway" >/dev/null 2>&1 && break
  sleep 5
done
ssh_droplet "kubectl get ns gateway" >/dev/null 2>&1 || die "gateway namespace never appeared after \
150s. bootstrap/install.sh returning doesn't mean Argo CD finished reconciling core — check \
'kubectl get applications -n argocd' on the droplet and re-run this script once gateway exists \
(nothing before this point re-runs destructively)."

info "Provisioning gateway's cluster-local secrets..."
# gateway-module-proxy-secret: gateway mints AND verifies this itself (HS256 JWT signing key for
# module-proxy tokens, app/module_proxy.py) — no external credential involved, so generating it
# fresh here is exactly equivalent to what seal-gateway-module-proxy-secret.sh does, minus the
# sealing (nothing to commit to git for a cluster this ephemeral).
if ssh_droplet "kubectl -n gateway get secret gateway-module-proxy-secret" >/dev/null 2>&1; then
  info "gateway-module-proxy-secret already exists (probably a re-run) — leaving it as-is."
else
  ssh_droplet "kubectl -n gateway create secret generic gateway-module-proxy-secret --from-literal=secret=\"\$(openssl rand -hex 32)\""
fi

# gateway-github-token: only exercised by the module-lifecycle-dispatch feature (gateway/README.md's
# "GitHub PAT for the module-lifecycle dispatch" section), which this ephemeral-test workflow never
# uses — a placeholder is enough to satisfy the volume mount and clear CreateContainerConfigError.
# Set GATEWAY_GITHUB_PAT yourself before running this script if you specifically want to exercise
# that feature against this cluster (same fine-grained-PAT requirements as
# seal-gateway-github-token.sh's own header: Actions read/write, this repo only, nothing else).
GATEWAY_GITHUB_PAT="${GATEWAY_GITHUB_PAT:-ephemeral-cluster-placeholder-not-a-real-pat}"
if ssh_droplet "kubectl -n gateway get secret gateway-github-token" >/dev/null 2>&1; then
  info "gateway-github-token already exists (probably a re-run) — leaving it as-is."
else
  ssh_droplet "kubectl -n gateway create secret generic gateway-github-token --from-literal=token='${GATEWAY_GITHUB_PAT}'"
fi

info "Rolling out gateway to pick up its secrets..."
ssh_droplet "kubectl -n gateway rollout restart deployment/gateway"
ssh_droplet "kubectl -n gateway rollout status deployment/gateway --timeout=120s" || warn \
  "gateway didn't report Ready within 120s. Not fatal to the rest of this script, but the module-proxy \
UI (e.g. Trino's) won't work until it is — check 'kubectl -n gateway get pods' / 'describe pod' on the droplet."
success "Gateway secrets provisioned for this cluster."

# 2026-09-15, hit live: bootstrap/keycloak-bootstrap-ui-shell-client.sh registers the
# "platform-ui-shell" OAuth client ui-shell is hardcoded to use (.env.production's
# VITE_KEYCLOAK_CLIENT_ID) — without it, login fails with Keycloak's "Client not found". Same
# "one-time, cluster-local setup, not GitOps-applied" category as the gateway secrets above: the
# KeycloakRealmImport CR only ever creates the realm once (see realm-platform.yaml's own header), so
# a client added after that has to go through the live Admin REST API instead, on every fresh
# cluster, not just this one. The script itself expects to reach https://keycloak.platform.local
# directly — real Ingress+DNS+MetalLB on homelab-dev, neither of which this droplet has, so a
# temporary /etc/hosts entry + a SEPARATE dedicated port-forward (its own local port 443, distinct
# from whatever you're using for browser access) stand in for both, entirely inside one ssh_droplet
# call so the port-forward's lifetime is naturally scoped to just this one setup step.
info "Provisioning ui-shell's Keycloak OAuth client..."
ssh_droplet "apt-get update -qq && apt-get install -y -qq jq >/dev/null"
ssh_droplet "grep -q 'keycloak.platform.local' /etc/hosts || echo '127.0.0.1 keycloak.platform.local' >> /etc/hosts"
ssh_droplet "cd /root/projectbooth && kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 443:443 >/tmp/kc-client-pf.log 2>&1 & PF_PID=\$!; sleep 3; bash bootstrap/keycloak-bootstrap-ui-shell-client.sh; STATUS=\$?; kill \$PF_PID 2>/dev/null || true; exit \$STATUS" || die \
  "Failed to provision ui-shell's Keycloak client — see output above; \
bootstrap/keycloak-bootstrap-ui-shell-client.sh's own die() messages explain the likely cause \
(common one: keycloak-realm/keycloak-instance not yet Synced/Healthy — check \
'kubectl get applications -n argocd' on the droplet and re-run this script once they are; nothing \
before this point re-runs destructively)."
success "ui-shell's Keycloak client provisioned."

cat <<EOF

${_c_green}==> Ready.${_c_reset}

  SSH in:        ssh -i ${ACCESS_KEY_FILE} root@${DROPLET_IP}
  Watch apps:     ssh -i ${ACCESS_KEY_FILE} root@${DROPLET_IP} 'kubectl get applications -n argocd -w'
  Argo CD UI:     ssh -i ${ACCESS_KEY_FILE} -L 8080:localhost:443 root@${DROPLET_IP}
                  then: kubectl -n argocd port-forward svc/argocd-server 8080:443   (run ON the droplet, in that same session)
                  then browse https://localhost:8080 from YOUR machine
  Any other Service the same way: forward it on the droplet, tunnel that port over SSH from here.

  Before installing gateway/ui-shell/catalog-service modules: their GHCR packages need to be
  public (ghcr.io/projectbooth/*) or imagePullSecrets configured — see today's known-issues.md
  entry on this. Same requirement as homelab-dev, not specific to this environment.

  ${_c_yellow}When you're done: ./destroy.sh${_c_reset}   (tears down the droplet AND revokes the deploy key —
  don't just 'terraform destroy' by hand, that skips the deploy-key cleanup and leaves it on
  ${GH_REPO} indefinitely.)
EOF
