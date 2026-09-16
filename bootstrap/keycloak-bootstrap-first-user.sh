#!/usr/bin/env bash
# One-time (per realm, per user): creates a real human user in the "platform" realm and puts them
# in one of the workspace-role groups realm-platform.yaml's KeycloakRealmImport already seeded
# (/workspaces/<workspace>/<owner|editor|viewer> — see that file's own header for the group-path
# model). Without this, a brand-new realm import has groups/roles/clients but genuinely nobody who
# can log in — ui-shell-plan.md's own deferred backlog flagged this gap (2026-09-15, Trino
# live-verification: hit live on a fresh ephemeral-test cluster with no users at all, blocking
# every other verification step behind it), and the fix has been "do it by hand in the admin
# console" until now. This script is the "do it right" version of that, matching the other three
# keycloak-bootstrap-*.sh scripts' own shape and safety discipline instead of a one-off manual
# workaround that leaves no record of how it was done.
#
# Same "why a script, not GitOps" reasoning as the other three: KeycloakRealmImport only ever
# creates the realm once — see realm-platform.yaml's own header, and its explicit "no users seeded
# here... a seeded user is a git-committed credential nobody needs" scope boundary. This script is
# how that gap gets closed WITHOUT ever putting a real credential in git: the password is supplied
# at run time only (an env var you set yourself, or an interactive prompt that doesn't echo) — never
# a script argument (would land in shell history) and never a default baked in here.
#
# Safe to re-run: looks up the username first. If it already exists, this only ensures group
# membership (adding it if missing) and leaves the existing password alone — re-running this is NOT
# how you reset somebody's password (use Keycloak's own admin console or `kcadm.sh set-password`
# for that; this script's job ends at "the user exists and can log in").
#
# Requirements — identical to the other three scripts (see keycloak-bootstrap-cli-client.sh's
# header for per-OS jq install commands): kubectl reaching the cluster, curl, base64, jq.
#
# What it needs beyond tools, and where each comes from — identical to the other three:
# `platform-initial-admin` (namespace keycloak, Keycloak Operator's bootstrap-admin Secret) and
# `platform-ca-secret` (namespace cert-manager, the CA manifests/cluster-issuer.yaml issues
# keycloak.platform.local's cert from).
#
# Networking: same as the other three — talks straight to https://keycloak.platform.local with
# --cacert, real Ingress, no port-forward needed on homelab-dev. On a droplet with no real
# Ingress/DNS (infra/ephemeral-test), run this the same way you ran
# keycloak-bootstrap-ui-shell-client.sh there: over an SSH session with the /etc/hosts entry and a
# `kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 443:443` tunnel already up.
#
# Usage:
#   KEYCLOAK_NEW_USER_PASSWORD='...' bootstrap/keycloak-bootstrap-first-user.sh --username alice [--email alice@example.com] [--workspace personal] [--role owner]
# Or, to be prompted (password never touches your shell history or this process's argv either way):
#   bootstrap/keycloak-bootstrap-first-user.sh --username alice
#
# --workspace/--role default to "personal"/"owner" — realm-platform.yaml only ever seeds the
# "personal" workspace's three role-groups (ARCHITECTURE.md: personal workspaces need no invite
# flow), and "owner" is the natural default for the first, and usually only, person in it. Pass
# --role editor/viewer, or a different --workspace once more than "personal" exists, as needed.
#
# Needs `git update-index --chmod=+x bootstrap/keycloak-bootstrap-first-user.sh` after its first
# commit — see docs/known-issues.md's entry on bootstrap scripts losing their executable bit (this
# repo is edited from Windows, which has no executable-bit concept).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

KUBECTL="sudo /usr/local/bin/kubectl"
KEYCLOAK_HOST="keycloak.platform.local"
REALM="platform"

USERNAME=""
EMAIL=""
WORKSPACE="personal"
ROLE="owner"

usage() {
  cat <<EOF
Usage: $(basename "$0") --username <name> [--email <address>] [--workspace <name>] [--role owner|editor|viewer]

Creates (or, if already present, just group-assigns) a real human user in the "platform" realm's
/workspaces/<workspace>/<role> group. The password is read from \$KEYCLOAK_NEW_USER_PASSWORD if set,
otherwise prompted for interactively — never a flag, never hardcoded.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --username) USERNAME="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (see --help)." ;;
  esac
done

[[ -n "$USERNAME" ]] || { usage; die "--username is required."; }
case "$ROLE" in
  owner|editor|viewer) ;;
  *) die "--role must be one of owner, editor, viewer (got '${ROLE}')." ;;
esac

require_cmd curl
require_cmd jq

# Read the new user's password without ever letting it touch argv (visible to anyone on the box via
# `ps`) or shell history. An env var is the non-interactive path (scripting, CI); the prompt below
# is the interactive fallback — either way this script itself never picks or defaults a password.
if [[ -n "${KEYCLOAK_NEW_USER_PASSWORD:-}" ]]; then
  NEW_PASSWORD="$KEYCLOAK_NEW_USER_PASSWORD"
else
  read -r -s -p "Password for new user '${USERNAME}': " NEW_PASSWORD
  echo
  [[ -n "$NEW_PASSWORD" ]] || die "Empty password — aborting."
fi

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
  "Couldn't reach ${BASE_URL} — is the /etc/hosts entry in place, and is the 'keycloak-instance' \
Argo CD Application Synced/Healthy? (sudo /usr/local/bin/kubectl -n keycloak get ingress)"

info "Getting a master-realm admin token..."
TOKEN_RESPONSE="$("${CURL[@]}" -X POST "${BASE_URL}/realms/master/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=admin-cli \
  -d "username=${ADMIN_USER}" -d "password=${ADMIN_PASS}")" || die \
  "Admin token request failed — output above (if any) is Keycloak's own error body."
TOKEN="$(jq -r '.access_token // empty' <<<"$TOKEN_RESPONSE")"
[[ -n "$TOKEN" ]] || die "No access_token in the response: ${TOKEN_RESPONSE}"

AUTH=(-H "Authorization: Bearer ${TOKEN}")

GROUP_PATH="/workspaces/${WORKSPACE}/${ROLE}"
info "Looking up group '${GROUP_PATH}'..."
# Keycloak's group-search endpoint matches by NAME, not full path, and can return multiple/nested
# hits (e.g. searching "owner" would also match a same-named group under a different workspace, if
# one existed) — walking workspaces -> WORKSPACE -> ROLE by subGroups is unambiguous regardless of
# how many workspaces or same-named role-groups exist elsewhere.
WORKSPACES_GROUP_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/groups?search=workspaces&exact=true" \
  | jq -r '.[] | select(.name == "workspaces") | .id')"
[[ -n "$WORKSPACES_GROUP_ID" ]] || die \
  "No top-level 'workspaces' group in realm '${REALM}' — has realm-platform.yaml's \
KeycloakRealmImport actually finished importing? (${KUBECTL} -n keycloak get keycloakrealmimport)"

WORKSPACE_GROUP_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/groups/${WORKSPACES_GROUP_ID}/children" \
  | jq -r --arg n "$WORKSPACE" '.[] | select(.name == $n) | .id')"
[[ -n "$WORKSPACE_GROUP_ID" ]] || die \
  "No 'workspaces/${WORKSPACE}' group — realm-platform.yaml only seeds 'personal' today. Add a new \
workspace group there first if you need one that isn't 'personal' (see that file's own header on \
why a re-sync won't pick up an edit to an already-imported realm)."

ROLE_GROUP_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/groups/${WORKSPACE_GROUP_ID}/children" \
  | jq -r --arg n "$ROLE" '.[] | select(.name == $n) | .id')"
[[ -n "$ROLE_GROUP_ID" ]] || die "No '${GROUP_PATH}' group found under workspace '${WORKSPACE}'."
success "Found '${GROUP_PATH}' (id=${ROLE_GROUP_ID})."

info "Checking whether user '${USERNAME}' already exists..."
USER_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/users?username=${USERNAME}&exact=true" \
  | jq -r '.[0].id // empty')"

if [[ -n "$USER_ID" ]]; then
  success "User '${USERNAME}' already exists (id=${USER_ID}) — leaving their password as-is, only \
checking group membership below."
else
  info "Creating user '${USERNAME}'..."
  CREATE_BODY="$(jq -n --arg username "$USERNAME" --arg email "$EMAIL" \
    --arg password "$NEW_PASSWORD" \
    '{
      username: $username,
      enabled: true,
      emailVerified: ($email != ""),
      email: (if $email != "" then $email else null end),
      credentials: [{type: "password", value: $password, temporary: false}]
    }')"
  # Keycloak's user-creation endpoint returns 201 with an empty body and the new user's id in the
  # Location response header, not in the body — has to be read back via a follow-up lookup, same as
  # keycloak-bootstrap-ui-shell-client.sh does for its own client id.
  "${CURL[@]}" "${AUTH[@]}" -X POST "${BASE_URL}/admin/realms/${REALM}/users" \
    -H "Content-Type: application/json" -d "${CREATE_BODY}" || die \
    "User creation failed — output above (if any) is Keycloak's own error body."

  USER_ID="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/users?username=${USERNAME}&exact=true" \
    | jq -r '.[0].id // empty')"
  [[ -n "$USER_ID" && "$USER_ID" != "null" ]] || die "User creation looked like it succeeded but a follow-up lookup found nothing."
  success "Created '${USERNAME}' (id=${USER_ID})."
fi
# NEW_PASSWORD isn't needed past this point (already sent, or the user already existed and this
# script deliberately leaves an existing password alone) — dropped so it isn't sitting in this
# process's memory/environment for the remaining, unrelated group-membership steps below.
unset NEW_PASSWORD

info "Checking group membership..."
ALREADY_MEMBER="$("${CURL[@]}" "${AUTH[@]}" "${BASE_URL}/admin/realms/${REALM}/users/${USER_ID}/groups" \
  | jq -r --arg id "$ROLE_GROUP_ID" 'any(.[]; .id == $id)')"

if [[ "$ALREADY_MEMBER" == "true" ]]; then
  success "'${USERNAME}' is already a member of '${GROUP_PATH}'."
else
  info "Adding '${USERNAME}' to '${GROUP_PATH}'..."
  "${CURL[@]}" "${AUTH[@]}" -X PUT "${BASE_URL}/admin/realms/${REALM}/users/${USER_ID}/groups/${ROLE_GROUP_ID}" || die \
    "Failed to add '${USERNAME}' to '${GROUP_PATH}' — output above (if any) is Keycloak's own error body."
  success "Added '${USERNAME}' to '${GROUP_PATH}'."
fi

success "Done."
cat <<EOF

'${USERNAME}' can now log in at https://app.platform.local (or http://localhost:5173 for local
ui-shell dev) with the password you just set, as a member of '${GROUP_PATH}' — platform-gateway
will derive X-Workspace=${WORKSPACE}/X-Role=${ROLE} from that group's realm role the same way it
does for any other user (see app/auth.py's derive_headers()).
EOF
