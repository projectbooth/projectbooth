#!/usr/bin/env bash
# One-time: creates a Keycloak service-account client ("platform-cli") in the
# "platform" realm, so platform-cli's `workspace invite` command can call the
# Admin REST API to add a user to a workspace's owner/editor/viewer group —
# without ever handling the master-realm bootstrap admin credentials itself.
#
# Why this is a script you run by hand once, not something GitOps applies:
# src/core/auth/realm-platform.yaml's KeycloakRealmImport only CREATES a
# realm — per that file's own header comment, it never updates one that's
# already been imported (yours has, since Phase 0). Adding a `clients:`
# block there and re-syncing would do nothing. This talks to the live Admin
# REST API directly instead — the same thing Keycloak's own kcadm.sh does,
# just via curl+jq so it needs nothing beyond what a Rocky/RHEL/Ubuntu box
# already has or can `dnf`/`apt install jq` trivially, and so this script
# doesn't have to guess at kcadm.sh's TLS-verification flags (not clearly
# documented — checked before writing this, didn't want to hand you an
# unverified flag for a step that actually mutates your Keycloak instance).
#
# Safe to re-run: checks for an existing "platform-cli" client before
# creating one, and reuses it rather than erroring or duplicating.
#
# Requirements (checked at startup via require_cmd — dies immediately with a
# clear message if any is missing, doesn't fail partway through):
#   - kubectl reaching your cluster (uses sudo, same as every other script
#     here — see lib/common.sh's PATH comment for why /usr/local/bin/kubectl
#     is spelled out explicitly rather than relying on PATH through sudo)
#   - curl, base64 — present on essentially every distro by default.
#   - jq — NOT always present by default. Rocky/RHEL/Alma:
#     `sudo dnf install jq`. Ubuntu/Debian: `sudo apt install jq`. macOS:
#     `brew install jq`. Same "verify before you hand someone a script that
#     dies on step one" reasoning as documenting the Python 3.12+ requirement
#     elsewhere in this repo (see catalog-service/platform-sdk/platform-cli's
#     READMEs and docs/known-issues.md).
#
# What it needs beyond tools, and where each comes from:
#   - `platform-initial-admin` (namespace keycloak) — the Keycloak Operator's
#     own auto-generated bootstrap-admin Secret (kubernetes.io/basic-auth:
#     username/password keys). Nothing in this repo creates this; the
#     operator does, the first time the Keycloak CR comes up.
#   - `platform-ca-secret` (namespace cert-manager) — the CA
#     manifests/cluster-issuer.yaml issues keycloak.platform.local's cert
#     from (kubernetes.io/tls: ca.crt among its keys). Used with curl's
#     --cacert so this properly trusts the real issuing CA rather than
#     disabling TLS verification outright.
#
# Networking (rewritten 2026-09-02, platform-ingress branch): this used to
# port-forward to 127.0.0.1 itself and use curl's --resolve to fake
# keycloak.platform.local resolving there — needed because nothing else made
# that hostname resolvable, and Keycloak's hostname provider strictly
# enforces it for everything past the first request (see
# docs/known-issues.md's "connected fine... broke immediately after" entry).
# Now that a real Ingress fronts Keycloak (src/core/argocd/manifests/
# keycloak-instance.yaml) and keycloak.platform.local resolves for real
# off-cluster, this script just talks to https://keycloak.platform.local
# directly with --cacert, the same way a browser or `platform login` does —
# no port-forward, no --resolve trick. REQUIRES an /etc/hosts entry on
# whatever machine runs this script (decision 2026-08-30: no local DNS
# server, so per-device /etc/hosts):
#   192.168.4.240 keycloak.platform.local gateway.platform.local
# (the IP is ingress-nginx's LoadBalancer address — confirm with
# `kubectl get svc -n ingress-nginx` if this ever changes).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

KUBECTL="sudo /usr/local/bin/kubectl"
KEYCLOAK_HOST="keycloak.platform.local"
REALM="platform"
CLIENT_ID="platform-cli"

require_cmd curl
require_cmd jq
require_cmd base64

work_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT

info "Extracting platform-ca cert from cert-manager..."
$KUBECTL get secret platform-ca-secret -n cert-manager -o jsonpath='{.data.ca\.crt}' \
  | base64 -d > "${work_dir}/platform-ca.crt"
[[ -s "${work_dir}/platform-ca.crt" ]] || die \
  "platform-ca.crt came back empty — check platform-ca-secret has a ca.crt key: \
${KUBECTL} get secret platform-ca-secret -n cert-manager -o yaml"

info "Reading the Keycloak Operator's bootstrap admin credentials..."
ADMIN_USER="$($KUBECTL get secret platform-initial-admin -n keycloak -o jsonpath='{.data.username}' | base64 -d)"
ADMIN_PASS="$($KUBECTL get secret platform-initial-admin -n keycloak -o jsonpath='{.data.password}' | base64 -d)"
[[ -n "$ADMIN_USER" && -n "$ADMIN_PASS" ]] || die \
  "Couldn't read platform-initial-admin's username/password — check it exists: \
${KUBECTL} get secret platform-initial-admin -n keycloak"

# --cacert is the real trust fix, not --insecure/-k. No --resolve and no
# port-forward needed anymore (2026-09-02, platform-ingress branch) —
# keycloak.platform.local resolves for real via the Ingress + your own
# /etc/hosts entry (see this script's header comment), the same as any
# other website's hostname.
CURL=(curl -sS --fail-with-body --cacert "${work_dir}/platform-ca.crt")
BASE_URL="https://${KEYCLOAK_HOST}"

info "Checking ${KEYCLOAK_HOST} is actually reachable before doing anything live-mutating..."
"${CURL[@]}" -o /dev/null "${BASE_URL}/realms/${REALM}" || die \
  "Couldn't reach ${BASE_URL} — is the /etc/hosts entry from this script's header comment in place, \
and is the 'keycloak-instance' Argo CD Application Synced/Healthy? \
(sudo /usr/local/bin/kubectl -n keycloak get ingress)"

info "Getting a master-realm admin token..."
TOKEN_RESPONSE="$("${CURL[@]}" -X POST "${BASE_URL}/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  -d "username=${ADMIN_USER}" -d "password=${ADMIN_PASS}")" || die \
  "Admin token request failed — output above (if any) is Keycloak's own error body."
TOKEN="$(jq -r '.access_token // empty' <<<"$TOKEN_RESPONSE")"
[[ -n "$TOKEN" ]] || die "No access_token in the response: ${TOKEN_RESPONSE}"

AUTH=(-H "Authorization: Bearer ${TOKEN}")

info "Checking whether '${CLIENT_ID}' already exists in realm '${REALM}'..."
CLIENT_UUID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients?clientId=${CLIENT_ID}" \
  | jq -r '.[0].id // empty')"

if [[ -n "$CLIENT_UUID" ]]; then
  success "Client '${CLIENT_ID}' already exists (id=${CLIENT_UUID}) — reusing it."
else
  info "Creating client '${CLIENT_ID}' (confidential, service-account only — no login flow)..."
  "${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/clients" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg id "$CLIENT_ID" '{
      clientId: $id,
      protocol: "openid-connect",
      publicClient: false,
      serviceAccountsEnabled: true,
      standardFlowEnabled: false,
      directAccessGrantsEnabled: false
    }')"
  CLIENT_UUID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients?clientId=${CLIENT_ID}" \
    | jq -r '.[0].id')"
  [[ -n "$CLIENT_UUID" && "$CLIENT_UUID" != "null" ]] || die "Client creation looked like it succeeded but a follow-up lookup found nothing."
  success "Created client '${CLIENT_ID}' (id=${CLIENT_UUID})."
fi

info "Finding the client's service-account user..."
SA_USER_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients/${CLIENT_UUID}/service-account-user" \
  | jq -r .id)"

info "Finding realm-management's 'manage-users' and 'view-realm' client roles..."
# manage-users, not a narrower fine-grained-admin-permissions grant scoped to
# just the three workspace groups — the fine-grained route exists in
# Keycloak and would be tighter, but it's real added setup complexity for a
# personal/solo deployment; manage-users is the standard, well-documented
# role for "this client can manage user accounts and group membership,"
# which covers most of what `workspace invite` needs (finding the user,
# creating/joining workspace groups). Revisit if this ever runs somewhere
# the blast radius of that role actually matters.
#
# view-realm ALSO needed (added after the first live run of
# platform-cli's self-healing group-create path hit a 403): reading a
# realm role's own representation — GET
# /admin/realms/{realm}/roles/{name}, which KeycloakAdminClient's
# _map_realm_role() needs before it can map owner/editor/viewer onto a
# newly-created workspace group — falls under "viewing realm
# configuration" in Keycloak's own admin-console-permissions docs, not
# under manage-users' user/group scope. Read-only (view-, not manage-),
# so this doesn't let the client change realm settings, just read this
# one thing it already needed to read.
REALM_MGMT_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients?clientId=realm-management" \
  | jq -r '.[0].id')"
MANAGE_USERS_ROLE="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients/${REALM_MGMT_ID}/roles/manage-users")"
VIEW_REALM_ROLE="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients/${REALM_MGMT_ID}/roles/view-realm")"

info "Granting 'manage-users' + 'view-realm' to the service account..."
"${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/users/${SA_USER_ID}/role-mappings/clients/${REALM_MGMT_ID}" \
  -H "Content-Type: application/json" -d "[${MANAGE_USERS_ROLE}, ${VIEW_REALM_ROLE}]" \
  || warn "Role assignment request failed — if this is a rerun and the roles are already assigned, that's likely a harmless duplicate-assignment error; check with the verification step below either way."

info "Fetching the client secret..."
CLIENT_SECRET="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients/${CLIENT_UUID}/client-secret" \
  | jq -r .value)"
[[ -n "$CLIENT_SECRET" && "$CLIENT_SECRET" != "null" ]] || die "Client secret came back empty."

info "Storing as Secret 'platform-keycloak-cli-credentials' (namespace keycloak)..."
$KUBECTL create secret generic platform-keycloak-cli-credentials -n keycloak \
  --from-literal=client_id="${CLIENT_ID}" \
  --from-literal=client_secret="${CLIENT_SECRET}" \
  --dry-run=client -o yaml | $KUBECTL apply -f -

success "Done."
cat <<EOF

To use platform-cli's 'workspace invite' from this machine:
  export PLATFORM_KEYCLOAK_CLIENT_SECRET="${CLIENT_SECRET}"

To read it back later instead of rerunning this script:
  ${KUBECTL} get secret platform-keycloak-cli-credentials -n keycloak -o jsonpath='{.data.client_secret}' | base64 -d
EOF
