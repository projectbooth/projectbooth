#!/usr/bin/env bash
# One-time: seals a fine-grained GitHub Personal Access Token into
# src/core/argocd/manifests/gateway.yaml's SealedSecret (ui-shell-plan.md item 7's mutation
# mechanism, feature/gateway-module-lifecycle-dispatch branch, 2026-09-10) — this repo's FIRST real
# use of sealed-secrets, deployed since day one (apps/core/sealed-secrets.yaml) but never actually
# sealed anything before now.
#
# Before running this: create the PAT on GitHub yourself — Settings -> Developer settings -> Fine-
# grained tokens -> Generate new token, "Only select repositories" -> this repo, Repository
# permissions -> Actions: Read and write, and NOTHING else (no Contents permission — the actual
# git commit/push happens inside the triggered workflow's own ephemeral GITHUB_TOKEN, gateway's PAT
# never touches this repo's contents). See gateway/README.md's own "GitHub PAT for the module-
# lifecycle dispatch" section for the same instructions in one more place.
#
# What this script does: prompts for that PAT (read, never a CLI arg or env var — so it never lands
# in shell history or `ps`), fetches YOUR cluster's real sealed-secrets public cert (kubeseal only
# encrypts correctly against the specific cluster that will decrypt it — nothing this script
# produces on a different cluster, or without this step, would ever actually work), seals it, and
# replaces the placeholder SealedSecret document at the bottom of
# src/core/argocd/manifests/gateway.yaml with the real one. You still commit and push that file
# yourself — this script never touches git, same "one-time cluster setup, not something GitOps
# applies" category as the keycloak-bootstrap-*.sh scripts (see keycloak-bootstrap-cli-client.sh's
# own header for that same reasoning, applied there to a Keycloak client instead of a Secret).
#
# Requirements (checked via require_cmd — dies immediately if missing, not partway through):
#   - kubectl reaching your cluster (uses sudo, same as every other bootstrap script here)
#   - kubeseal — the sealed-secrets CLI. NOT the same thing as the in-cluster controller
#     (apps/core/sealed-secrets.yaml) — that's already deployed; this is the client tool you run
#     locally. Install: https://github.com/bitnami-labs/sealed-secrets#homebrew (macOS) or the
#     "Installation" section there for Linux — grab a release binary matching the controller's own
#     chart version (2.19.3, appVersion 0.39.1 — see apps/core/sealed-secrets.yaml's own comment).
#
# Safe to re-run: each run seals a fresh PAT and overwrites the previous SealedSecret document —
# useful for rotating the token later, not just the first time.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

KUBECTL="sudo /usr/local/bin/kubectl"
SEALED_SECRETS_NAMESPACE="sealed-secrets"
TARGET_NAMESPACE="gateway"
SECRET_NAME="gateway-github-token"
GATEWAY_MANIFEST="$(repo_root)/src/core/argocd/manifests/gateway.yaml"

# k3s's own kubeconfig lives at /etc/rancher/k3s/k3s.yaml, root-readable only — the same reason
# every other script here needs `sudo` for kubectl at all (see lib/common.sh's PATH comment).
# `kubectl` itself, as k3s installs it at /usr/local/bin/kubectl, already knows to fall back to
# that path by default when nothing else is configured. kubeseal is a plain client-go binary with
# no such k3s-specific default — it needs KUBECONFIG pointed there explicitly, or every call
# fails with "no configuration has been provided" even though `sudo kubectl` works fine right next
# to it. Override with KUBESEAL_KUBECONFIG=/path/to/config if your k3s config lives somewhere else.
K3S_KUBECONFIG="${KUBESEAL_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

require_cmd kubeseal
require_cmd kubectl

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
# Talks to the Kubernetes API directly (client-go, not a shell-out to kubectl) — not a network call
# to the controller's own Service, so no port-forward/Ingress concern like the Keycloak bootstrap
# scripts have. KUBECONFIG is passed explicitly for the reason K3S_KUBECONFIG's own comment above
# gives: unlike `sudo kubectl`, kubeseal has no built-in fallback to k3s's config location.
sudo KUBECONFIG="$K3S_KUBECONFIG" kubeseal --fetch-cert \
  --controller-name "$CONTROLLER_NAME" \
  --controller-namespace "$SEALED_SECRETS_NAMESPACE" \
  > "${work_dir}/pub-cert.pem" \
  || die "kubeseal --fetch-cert failed — confirm the controller name/namespace above are right \
(${KUBECTL} get pods -n ${SEALED_SECRETS_NAMESPACE}), and that ${K3S_KUBECONFIG} is really this \
cluster's kubeconfig."
[[ -s "${work_dir}/pub-cert.pem" ]] || die "Fetched cert came back empty."

echo
echo "Paste the fine-grained GitHub PAT (Actions: read/write, this repo only) — input is hidden,"
echo "and this is never written to shell history or a CLI argument:"
read -r -s -p "GitHub PAT: " GITHUB_PAT
echo
[[ -n "$GITHUB_PAT" ]] || die "No token entered."

info "Sealing it for '${SECRET_NAME}' in namespace '${TARGET_NAMESPACE}'..."
$KUBECTL create secret generic "$SECRET_NAME" \
  --namespace "$TARGET_NAMESPACE" \
  --from-literal=token="$GITHUB_PAT" \
  --dry-run=client -o json \
  | kubeseal --cert "${work_dir}/pub-cert.pem" --format yaml \
  > "${work_dir}/sealed.yaml" \
  || die "kubeseal failed to seal the secret."
unset GITHUB_PAT

# Replaces only the gateway-github-token SealedSecret document, found by its own metadata.name —
# not positionally as "the last SealedSecret document in the file" (this file has carried a second
# one, gateway-module-proxy-secret, since ui-shell-plan.md item 8/feature/module-proxy, 2026-09-10;
# see replace_sealed_secret_document's own comment in lib/common.sh for why that shortcut broke and
# what replaced it).
replace_sealed_secret_document "$GATEWAY_MANIFEST" "$SECRET_NAME" "${work_dir}/sealed.yaml"

success "Wrote the real SealedSecret into ${GATEWAY_MANIFEST}."
echo "Next: review the diff, commit, and push — this script never touches git itself. Then roll out"
echo "gateway (sudo kubectl -n gateway rollout restart deployment/gateway) once the SealedSecret has"
echo "synced and the sealed-secrets controller has decrypted it into a real Secret:"
echo "  sudo kubectl -n gateway get secret ${SECRET_NAME}"
