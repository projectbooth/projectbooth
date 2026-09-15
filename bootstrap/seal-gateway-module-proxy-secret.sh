#!/usr/bin/env bash
# One-time: generates and seals gateway's HS256 module-proxy signing secret into
# src/core/argocd/manifests/gateway.yaml's SECOND SealedSecret document (ui-shell-plan.md item 8,
# feature/module-proxy branch, 2026-09-10) — app/module_proxy.py's short-lived, module-scoped
# proxy tokens (the `?token=` an embedded module UI iframe uses instead of an Authorization header,
# since a plain <iframe src> navigation can't send a custom header — see that file's own module
# docstring for the full "why").
#
# Unlike seal-gateway-github-token.sh's PAT, there's nothing to go create externally first: this is
# a random signing secret gateway both mints AND verifies itself, so this script GENERATES the
# plaintext (`openssl rand -hex 32`) rather than prompting for one. Sibling script, same structure,
# same shared replace_sealed_secret_document helper (lib/common.sh) for finding and replacing only
# ITS OWN SealedSecret document by name — never positionally as "the last one in the file", which
# is exactly the bug that having two such documents in this file exposed (see that helper's own
# comment for the full story).
#
# Requirements (checked via require_cmd — dies immediately if missing, not partway through):
#   - kubectl reaching your cluster (uses sudo, same as every other bootstrap script here)
#   - kubeseal — the sealed-secrets CLI. NOT the same thing as the in-cluster controller
#     (apps/core/sealed-secrets.yaml) — that's already deployed; this is the client tool you run
#     locally. Install: https://github.com/bitnami-labs/sealed-secrets#homebrew (macOS) or the
#     "Installation" section there for Linux — grab a release binary matching the controller's own
#     chart version (2.19.3, appVersion 0.39.1 — see apps/core/sealed-secrets.yaml's own comment).
#   - openssl — used only for `openssl rand -hex 32`, present on essentially every Linux/macOS box.
#
# Safe to re-run: each run generates a FRESH random secret and overwrites the previous SealedSecret
# document — useful for rotating it later, not just the first time. Rotating it invalidates every
# proxy token minted under the old secret (they're stateless JWTs verified against this value, not
# looked up anywhere) — nothing to clean up server-side, the next mint just signs with the new one.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

KUBECTL="sudo /usr/local/bin/kubectl"
SEALED_SECRETS_NAMESPACE="sealed-secrets"
TARGET_NAMESPACE="gateway"
SECRET_NAME="gateway-module-proxy-secret"
GATEWAY_MANIFEST="$(repo_root)/src/core/argocd/manifests/gateway.yaml"

# Same K3S_KUBECONFIG reasoning as seal-gateway-github-token.sh's own comment on this line —
# kubeseal has no k3s-specific config fallback the way `sudo kubectl` does.
K3S_KUBECONFIG="${KUBESEAL_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

require_cmd kubeseal
require_cmd kubectl
require_cmd openssl

[[ -f "$GATEWAY_MANIFEST" ]] || die "Expected to find ${GATEWAY_MANIFEST} — run this from a real checkout."
[[ -f "$K3S_KUBECONFIG" ]] || die "No kubeconfig at ${K3S_KUBECONFIG} — if k3s's config lives \
somewhere else on this box, re-run as: KUBESEAL_KUBECONFIG=/path/to/config bash $0"

info "Looking for the sealed-secrets controller in namespace '${SEALED_SECRETS_NAMESPACE}'..."
CONTROLLER_NAME="$($KUBECTL get deployment -n "$SEALED_SECRETS_NAMESPACE" \
  -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/name=="sealed-secrets")].metadata.name}' \
  2>/dev/null || true)"
if [[ -z "$CONTROLLER_NAME" ]]; then
  warn "Couldn't auto-detect the controller Deployment's name via its app.kubernetes.io/name label."
  info "Listing what's actually there so you can pass it explicitly:"
  $KUBECTL get deployment -n "$SEALED_SECRETS_NAMESPACE" || true
  read -r -p "Controller Deployment name in '${SEALED_SECRETS_NAMESPACE}': " CONTROLLER_NAME
  [[ -n "$CONTROLLER_NAME" ]] || die "No controller name given — can't fetch the right public cert."
fi
success "Using controller '${CONTROLLER_NAME}' in namespace '${SEALED_SECRETS_NAMESPACE}'."

work_dir="$(mktemp -d)"
cleanup() { rm -rf "$work_dir"; }
trap cleanup EXIT

info "Fetching the cluster's real sealed-secrets public cert (kubeseal --fetch-cert)..."
# Same reasoning as seal-gateway-github-token.sh's own comment here: talks to the Kubernetes API
# directly (client-go), and needs KUBECONFIG passed explicitly since kubeseal has no k3s-specific
# fallback the way `sudo kubectl` does.
sudo KUBECONFIG="$K3S_KUBECONFIG" kubeseal --fetch-cert \
  --controller-name "$CONTROLLER_NAME" \
  --controller-namespace "$SEALED_SECRETS_NAMESPACE" \
  > "${work_dir}/pub-cert.pem" \
  || die "kubeseal --fetch-cert failed — confirm the controller name/namespace above are right \
(${KUBECTL} get pods -n ${SEALED_SECRETS_NAMESPACE}), and that ${K3S_KUBECONFIG} is really this \
cluster's kubeconfig."
[[ -s "${work_dir}/pub-cert.pem" ]] || die "Fetched cert came back empty."

info "Generating a fresh 256-bit signing secret (openssl rand -hex 32)..."
# A 64-char hex string — well above HS256's RFC 7518 §3.2 minimum key length (32 bytes), so
# PyJWT never raises InsecureKeyLengthWarning against it. Never echoed to the terminal or written
# anywhere but straight into kubeseal's stdin below.
MODULE_PROXY_SECRET="$(openssl rand -hex 32)"

info "Sealing it for '${SECRET_NAME}' in namespace '${TARGET_NAMESPACE}'..."
$KUBECTL create secret generic "$SECRET_NAME" \
  --namespace "$TARGET_NAMESPACE" \
  --from-literal=secret="$MODULE_PROXY_SECRET" \
  --dry-run=client -o json \
  | kubeseal --cert "${work_dir}/pub-cert.pem" --format yaml \
  > "${work_dir}/sealed.yaml" \
  || die "kubeseal failed to seal the secret."
unset MODULE_PROXY_SECRET

# Replaces only the gateway-module-proxy-secret SealedSecret document, found by its own
# metadata.name — see replace_sealed_secret_document's own comment in lib/common.sh for why this
# has to be name-based rather than "the last SealedSecret document in the file" now that this file
# carries two.
replace_sealed_secret_document "$GATEWAY_MANIFEST" "$SECRET_NAME" "${work_dir}/sealed.yaml"

success "Wrote the real SealedSecret into ${GATEWAY_MANIFEST}."
echo "Next: review the diff, commit, and push — this script never touches git itself. Then roll out"
echo "gateway (sudo kubectl -n gateway rollout restart deployment/gateway) once the SealedSecret has"
echo "synced and the sealed-secrets controller has decrypted it into a real Secret:"
echo "  sudo kubectl -n gateway get secret ${SECRET_NAME}"
