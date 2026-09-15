#!/usr/bin/env bash
# One-time: creates a PUBLIC Keycloak client ("platform-ui-shell") in the
# "platform" realm, so ui-shell (the browser SPA) can run a real OAuth 2.0
# Authorization Code + PKCE login — the flow a browser uses to get a real
# user a token via a redirect, without a client secret (public clients
# don't get one; a secret baked into JS shipped to every visitor's browser
# would be visible to anyone who opens devtools, so there's nothing to
# protect by giving this client one).
#
# This is a THIRD, separate Keycloak client — never merged with either of
# the other two. keycloak-bootstrap-cli-client.sh's "platform-cli"
# (confidential, service-account-only) authenticates as ITSELF via a secret
# to manage users/groups on platform-cli's behalf.
# keycloak-bootstrap-login-client.sh's "platform-cli-login" (public,
# device-grant only) authenticates as a real human via RFC 8628's
# poll-a-device-code flow — no browser redirect involved, CLI-shaped.
# ui-shell needs a structurally different flow: authorization code + PKCE,
# a redirect URI, session storage in the browser — see
# docs/architecture/ui-shell-plan.md item 3 for the full "why a third
# client" reasoning. Same blast-radius logic as the other two scripts:
# narrowly-scoped clients cost nothing extra, and there's no reason for a
# browser-facing login client to be able to do anything a device-flow or
# service-account client can.
#
# Same "why a script, not GitOps" reasoning as both other bootstrap
# scripts (see their headers): src/core/auth/realm-platform.yaml's
# KeycloakRealmImport only creates a realm once, never updates an
# already-imported one, so a new client has to be added via the live Admin
# REST API instead.
#
# Safe to re-run: checks for an existing "platform-ui-shell" client (and
# its "groups" protocol mapper) before creating either, and reuses rather
# than erroring or duplicating.
#
# Requirements — identical to the other two scripts' (see
# keycloak-bootstrap-cli-client.sh's header for the per-OS jq install
# commands): kubectl reaching the cluster, curl, base64, jq.
#
# What it needs beyond tools, and where each comes from — identical to the
# other two scripts': `platform-initial-admin` (namespace keycloak,
# Keycloak Operator's bootstrap-admin Secret) and `platform-ca-secret`
# (namespace cert-manager, the CA manifests/cluster-issuer.yaml issues
# keycloak.platform.local's cert from).
#
# Networking: same as the other two scripts' — talks straight to
# https://keycloak.platform.local with --cacert, real Ingress, no
# port-forward or --resolve trick needed. Same /etc/hosts requirement; see
# keycloak-bootstrap-cli-client.sh's header for the entry to add if you
# haven't already.
#
# Two redirect origins registered, not one: ui-shell's deployed Ingress
# (https://app.platform.local) AND Vite's local dev server
# (http://localhost:5173) — there's no port-forward equivalent for an
# interactive browser redirect the way there is for gateway's local dev, so
# local ui-shell development talks to this exact same cluster Keycloak
# directly, over the same /etc/hosts entry. Exact-path redirect URIs, not a
# wildcard `/*` — a public client with no secret is exactly the case where
# a broad open-redirect surface into your own callback path is worth
# avoiding.
#
# pkce.code.challenge.method=S256 is the one non-optional client attribute
# here: without it, a public client with standardFlowEnabled would let a
# code-flow login complete with NO PKCE verification at all, silently
# defeating the entire reason this client exists as its own browser-shaped
# client rather than reusing platform-cli-login's.
#
# post.logout.redirect.uris — confirmed live on this cluster's 26.7.2
# Operator (same "flag genuinely uncertain details, don't silently assume"
# discipline as keycloak-bootstrap-login-client.sh's device-grant-field
# callout, which also turned out to need a fix). The attribute itself is the
# standard OIDC RP-initiated-logout mechanism, but Keycloak's client
# `attributes` map only holds ONE string per key — a multi-valued attribute
# like this one is encoded as its values joined with a literal "##", NOT a
# space or comma. A space-joined value here is silently mis-parsed as ONE
# URI containing a space, and Keycloak's client-creation POST rejects it
# outright: `{"error":"invalid_input","error_description":"A post-logout
# redirect URI is not a valid URI"}`. Caught live on the first real run of
# this script (2026-09-08) — see docs/known-issues.md for the write-up.
#
# Needs `git update-index --chmod=+x bootstrap/keycloak-bootstrap-ui-shell-client.sh`
# after its first commit — see docs/known-issues.md's entry on bootstrap
# scripts losing their executable bit (this repo is edited from Windows,
# which has no executable-bit concept; git re-applies whatever mode its tree
# recorded, so a manual local chmod doesn't survive the next pull that
# touches this file).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

KUBECTL="sudo /usr/local/bin/kubectl"
KEYCLOAK_HOST="keycloak.platform.local"
REALM="platform"
CLIENT_ID="platform-ui-shell"
DEPLOYED_ORIGIN="https://app.platform.local"
LOCAL_DEV_ORIGIN="http://localhost:5173"

require_cmd curl
require_cmd jq

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

CURL=(curl -sS --fail-with-body --cacert "${work_dir}/platform-ca.crt")
BASE_URL="https://${KEYCLOAK_HOST}"

info "Checking ${KEYCLOAK_HOST} is actually reachable before doing anything live-mutating..."
"${CURL[@]}" -o /dev/null "${BASE_URL}/realms/${REALM}" || die \
  "Couldn't reach ${BASE_URL} — is the /etc/hosts entry from keycloak-bootstrap-cli-client.sh's \
header comment in place, and is the 'keycloak-instance' Argo CD Application Synced/Healthy? \
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
  info "Creating client '${CLIENT_ID}' (public, authorization code + PKCE only — no direct-grant, \
no service account, no secret)..."

  CREATE_BODY="$(jq -n --arg id "$CLIENT_ID" \
    --arg deployedRedirect "${DEPLOYED_ORIGIN}/auth/callback" \
    --arg localRedirect "${LOCAL_DEV_ORIGIN}/auth/callback" \
    --arg deployedOrigin "$DEPLOYED_ORIGIN" \
    --arg localOrigin "$LOCAL_DEV_ORIGIN" \
    --arg deployedLogout "${DEPLOYED_ORIGIN}/*" \
    --arg localLogout "${LOCAL_DEV_ORIGIN}/*" \
    '{
      clientId: $id,
      protocol: "openid-connect",
      publicClient: true,
      standardFlowEnabled: true,
      implicitFlowEnabled: false,
      directAccessGrantsEnabled: false,
      serviceAccountsEnabled: false,
      redirectUris: [$deployedRedirect, $localRedirect],
      webOrigins: [$deployedOrigin, $localOrigin],
      attributes: {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": ($deployedLogout + "##" + $localLogout)
      }
    }')"

  "${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/clients" \
    -H "Content-Type: application/json" -d "${CREATE_BODY}" || die \
    "Client creation failed — output above (if any) is Keycloak's own error body."
  success "Created '${CLIENT_ID}'."

  CLIENT_UUID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients?clientId=${CLIENT_ID}" \
    | jq -r '.[0].id // empty')"
  [[ -n "$CLIENT_UUID" && "$CLIENT_UUID" != "null" ]] || die "Client creation looked like it succeeded but a follow-up lookup found nothing."
fi

info "Checking for the 'groups' protocol mapper (emits full workspace group paths into the token, \
e.g. /workspaces/personal/editor — platform-gateway derives X-Workspace/X-Role from this claim)..."
EXISTING_MAPPER_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/clients/${CLIENT_UUID}/protocol-mappers/models" \
  | jq -r '.[] | select(.name == "groups") | .id' | head -n1)"

if [[ -n "$EXISTING_MAPPER_ID" ]]; then
  success "'groups' protocol mapper already present (id=${EXISTING_MAPPER_ID}) — leaving it as-is."
else
  info "Adding the 'groups' protocol mapper (oidc-group-membership-mapper, full.path=true)..."
  "${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/clients/${CLIENT_UUID}/protocol-mappers/models" \
    -H "Content-Type: application/json" \
    -d '{
      "name": "groups",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-group-membership-mapper",
      "consentRequired": false,
      "config": {
        "full.path": "true",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
        "claim.name": "groups"
      }
    }'
  success "Added the 'groups' protocol mapper."
fi

success "Done."
cat <<EOF

'${CLIENT_ID}' is a PUBLIC client — no secret to store or export. ui-shell is
hardcoded to this client ID (src/core/ui-shell/.env.production's
VITE_KEYCLOAK_CLIENT_ID), so nothing else needs configuring on this machine
or any other for browser login to work.

Registered redirect URIs (exact paths, not a wildcard):
  - ${DEPLOYED_ORIGIN}/auth/callback  (the deployed app)
  - ${LOCAL_DEV_ORIGIN}/auth/callback  (npm run dev, against this same cluster's Keycloak)

preferred_username and email land in the token via Keycloak's default
"profile"/"email" scopes — not something this script had to add explicitly.
The "groups" claim above is the one non-default addition, and it's what
platform-gateway uses to validate --workspace and derive X-Role, same as
platform-cli-login's token.
EOF
