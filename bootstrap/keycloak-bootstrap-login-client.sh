#!/usr/bin/env bash
# One-time: creates a PUBLIC Keycloak client ("platform-cli-login") in the
# "platform" realm, so `platform login` can run the OAuth 2.0 Device
# Authorization Grant (RFC 8628) — the flow a CLI uses to get a real user a
# token without ever handling their password itself, and without needing a
# client secret (public clients don't get one; there's nothing to leak from a
# binary distributed to a user's machine).
#
# This is a SEPARATE client from platform-cli's other one
# (keycloak-bootstrap-cli-client.sh's "platform-cli", confidential,
# service-account-only, used by `workspace invite`). Deliberately not reused:
# that client authenticates as ITSELF via a secret to manage users/groups on
# platform-cli's behalf; this one authenticates as a REAL HUMAN USER via the
# device grant, and must never be able to do so silently or with a client
# secret an attacker could extract from a distributed binary. Mixing the two
# would mean a leaked device-login client secret (if it had one) could also
# manage every user/group in the realm — no reason to accept that blast
# radius when two narrowly-scoped clients cost nothing extra.
#
# Same "why a script, not GitOps" reasoning as keycloak-bootstrap-cli-client.sh
# (see that file's header): src/core/auth/realm-platform.yaml's
# KeycloakRealmImport only creates a realm once, never updates an
# already-imported one, so a new client has to be added via the live Admin
# REST API instead.
#
# Safe to re-run: checks for an existing "platform-cli-login" client (and its
# "groups" protocol mapper) before creating either, and reuses/repairs rather
# than erroring or duplicating.
#
# Requirements — identical to keycloak-bootstrap-cli-client.sh's (see that
# script's header for the per-OS jq install commands): kubectl reaching the
# cluster, curl, base64, jq.
#
# What it needs beyond tools, and where each comes from — identical to
# keycloak-bootstrap-cli-client.sh's: `platform-initial-admin` (namespace
# keycloak, Keycloak Operator's bootstrap-admin Secret) and `platform-ca-secret`
# (namespace cert-manager, the CA manifests/cluster-issuer.yaml issues
# keycloak.platform.local's cert from).
#
# Networking (rewritten 2026-09-02, platform-ingress branch): same as
# keycloak-bootstrap-cli-client.sh's own rewrite — talks straight to
# https://keycloak.platform.local with --cacert now that a real Ingress
# makes that hostname resolve for real, no port-forward or --resolve trick
# needed. Same /etc/hosts requirement; see that script's header for the
# entry to add if you haven't already. Not repeated in full detail here to
# avoid the two copies drifting — see that script if this section needs
# more context.
#
# Device-grant client fields — the one genuinely uncertain detail in this
# script, flagged rather than silently picked (see docs/known-issues.md's
# entry on this, and the platform-gateway plan's design-decision doc for the
# full writeup): Keycloak's ClientRepresentation has a top-level
# `oauth2DeviceAuthorizationGrantEnabled` boolean, but it 400'd with
# "Unrecognized field" on at least one real Keycloak version
# (keycloak/keycloak#19688, reported against v21.0.2 — this cluster's
# Operator is pinned 26.7.2, may or may not still hit it). The underlying,
# always-supported mechanism is the attributes-map key
# `oauth2.device.authorization.grant.enabled`. This script sends BOTH
# together first (cheap, and covers the version where the top-level field is
# required/preferred); if that specific request 400s naming that field, it
# retries with just the attributes key and reports which path it took. If
# creation fails for any OTHER reason, it dies with Keycloak's real error
# body rather than guessing further.
#
# Needs `git update-index --chmod=+x bootstrap/keycloak-bootstrap-login-client.sh`
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
CLIENT_ID="platform-cli-login"

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

# No --resolve and no port-forward needed anymore (2026-09-02,
# platform-ingress branch) — see this script's header comment.
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
  info "Creating client '${CLIENT_ID}' (public, device-grant only — no standard/direct-grant login, no secret)..."

  COMBINED_BODY="$(jq -n --arg id "$CLIENT_ID" '{
    clientId: $id,
    protocol: "openid-connect",
    publicClient: true,
    standardFlowEnabled: false,
    directAccessGrantsEnabled: false,
    serviceAccountsEnabled: false,
    implicitFlowEnabled: false,
    oauth2DeviceAuthorizationGrantEnabled: true,
    attributes: {
      "oauth2.device.authorization.grant.enabled": "true"
    }
  }')"

  # Don't let a 400 here kill the script via set -e — this specific request
  # is allowed to fail once, on purpose, so the fallback below can run.
  set +e
  CREATE_OUTPUT="$("${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/clients" \
    -H "Content-Type: application/json" -d "${COMBINED_BODY}" 2>&1)"
  CREATE_STATUS=$?
  set -e

  if [[ $CREATE_STATUS -ne 0 ]]; then
    if grep -qi 'oauth2DeviceAuthorizationGrantEnabled' <<<"$CREATE_OUTPUT"; then
      warn "This Keycloak build rejected the top-level 'oauth2DeviceAuthorizationGrantEnabled' field \
(unrecognized-property error below) — retrying with just the attributes-map key, which is the \
older/always-supported mechanism for the same setting:"
      warn "$CREATE_OUTPUT"

      ATTRS_ONLY_BODY="$(jq -n --arg id "$CLIENT_ID" '{
        clientId: $id,
        protocol: "openid-connect",
        publicClient: true,
        standardFlowEnabled: false,
        directAccessGrantsEnabled: false,
        serviceAccountsEnabled: false,
        implicitFlowEnabled: false,
        attributes: {
          "oauth2.device.authorization.grant.enabled": "true"
        }
      }')"
      "${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/clients" \
        -H "Content-Type: application/json" -d "${ATTRS_ONLY_BODY}" || die \
        "Client creation failed even with the attributes-only fallback — output above (if any) is \
Keycloak's own error body."
      success "Created '${CLIENT_ID}' via the attributes-only fallback (attributes[\"oauth2.device.authorization.grant.enabled\"])."
    else
      die "Client creation failed for a reason other than the known top-level-field incompatibility \
— output: ${CREATE_OUTPUT}"
    fi
  else
    success "Created '${CLIENT_ID}' with both the top-level field and the attributes key set."
  fi

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

'${CLIENT_ID}' is a PUBLIC client — no secret to store or export. platform-cli's
device-flow login (\`platform login\`) is hardcoded to this client ID
(platform_sdk/config.py's keycloak_login_client_id default), so nothing else
needs configuring on this machine or any other for login to work.

preferred_username and email land in the token via Keycloak's default
"profile"/"email" scopes — not something this script had to add explicitly.
The "groups" claim above is the one non-default addition, and it's what
platform-gateway uses to validate --workspace and derive X-Role.
EOF
