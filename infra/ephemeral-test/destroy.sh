#!/usr/bin/env bash
# Tears down everything run.sh created: the deploy key registered on GH_REPO, then the droplet/
# firewall/SSH-key via `terraform destroy`. Always use this instead of a bare `terraform destroy` —
# that alone leaves the read-only deploy key registered on the repo indefinitely, since it was
# created via `gh api`, not by Terraform, and Terraform has no record of it at all.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

_c_red=$'\033[0;31m'; _c_yellow=$'\033[0;33m'; _c_green=$'\033[0;32m'; _c_blue=$'\033[0;34m'; _c_reset=$'\033[0m'
info()    { echo "${_c_blue}==>${_c_reset} $*"; }
success() { echo "${_c_green}==>${_c_reset} $*"; }
warn()    { echo "${_c_yellow}==> warning:${_c_reset} $*" >&2; }
die()     { echo "${_c_red}==> error:${_c_reset} $*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not found on PATH."; }

GH_REPO="projectbooth/projectbooth"
STATE_FILE="$SCRIPT_DIR/.ephemeral-state.json"
ACCESS_KEY_FILE="$SCRIPT_DIR/.droplet-access-key"
DEPLOY_KEY_FILE="$SCRIPT_DIR/.deploy-key"

require_cmd terraform
require_cmd gh

if [[ ! -f "$STATE_FILE" ]]; then
  warn "No ${STATE_FILE} found — either nothing was ever run.sh'd here, or a prior destroy.sh already \
cleaned up. Checking for leftover Terraform-managed resources anyway..."
else
  # 2026-09-15: was a python3 one-liner — dropped for the same reason run.sh's deploy-key-id parsing
  # was (see that file's own dated comment): on Windows/Git Bash, `python3` can resolve on PATH via a
  # Microsoft Store "App Execution Alias" stub with no real Python behind it, so require_cmd wouldn't
  # have caught it. STATE_FILE is a flat, single-level JSON object this script writes itself
  # (`{"deploy_key_id": N, "droplet_ip": "..."}`), so grep is enough — no real parser needed.
  DEPLOY_KEY_ID="$(grep -oE '"deploy_key_id"[[:space:]]*:[[:space:]]*[0-9]+' "$STATE_FILE" | grep -oE '[0-9]+$')"
  if [[ -n "$DEPLOY_KEY_ID" && "$DEPLOY_KEY_ID" != "null" ]]; then
    info "Revoking deploy key id ${DEPLOY_KEY_ID} on ${GH_REPO}..."
    if gh api -X DELETE "repos/${GH_REPO}/keys/${DEPLOY_KEY_ID}" >/dev/null 2>&1; then
      success "Deploy key revoked."
    else
      warn "Couldn't revoke deploy key ${DEPLOY_KEY_ID} via 'gh api' — it may already be gone, or your \
gh token may be missing the admin:public_key scope this DELETE needs (2026-09-15: confirmed live that \
gh's default browser login doesn't request it — 'gh auth refresh -h github.com -s admin:public_key' \
fixes it). Check manually at https://github.com/${GH_REPO}/settings/keys if you want to be sure."
    fi
  else
    warn "${STATE_FILE} didn't contain a deploy_key_id — nothing to revoke via the API. Check \
https://github.com/${GH_REPO}/settings/keys by hand for any stray 'ephemeral-test-*' keys."
  fi
fi

# Terraform's own state is the source of truth for what infrastructure actually exists — destroy
# unconditionally attempts this even if STATE_FILE was missing/malformed above, since the droplet
# itself is the expensive, billable thing to make sure is really gone.
if [[ -f "$SCRIPT_DIR/terraform.tfstate" || -d "$SCRIPT_DIR/.terraform" ]]; then
  : "${TF_VAR_do_token:?Set TF_VAR_do_token to your DigitalOcean API token first — needed to destroy the droplet/firewall/SSH-key.}"
  info "terraform destroy..."
  terraform destroy
else
  warn "No local Terraform state found in ${SCRIPT_DIR} — nothing for Terraform to destroy here. If \
you ran run.sh from a different checkout, cd there instead."
fi

rm -f "$STATE_FILE" "$ACCESS_KEY_FILE" "$DEPLOY_KEY_FILE" "${DEPLOY_KEY_FILE}.pub"
success "Cleaned up. ${STATE_FILE}, ${ACCESS_KEY_FILE}, and ${DEPLOY_KEY_FILE}(.pub) removed."
