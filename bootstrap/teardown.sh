#!/usr/bin/env bash
# The mirror of install.sh (ARCHITECTURE.md §3, "Tearing it all down"):
# uninstalls every module, removes core, removes Argo CD, then runs k3s's
# own uninstaller. The machine ends up where it was before install.sh ran —
# whether that's a spare box at home or a cloud VM you're about to reclaim.
#
# What this deliberately does NOT do: terminate a cloud instance. That's one
# layer above this script — pair cloud test clusters with Terraform (or
# equivalent) and run `terraform destroy` after this, so nothing lingers.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

YES=false
for arg in "$@"; do
  [[ "$arg" == "--yes" ]] && YES=true
done

if [[ "$YES" != true ]]; then
  confirm_destructive "teardown" \
    "This removes every Argo CD-managed module and core service, then uninstalls k3s itself. There's no undo."
fi

if kubectl get namespace argocd >/dev/null 2>&1; then
  info "Deleting modules-enabled Applications (if any)..."
  kubectl delete applications -n argocd -l platform.io/tier=module --ignore-not-found --timeout=60s \
    || warn "Timed out deleting module-tier Applications — continuing anyway (k3s-uninstall.sh below wipes the node regardless)."

  info "Deleting core Applications..."
  kubectl delete applications -n argocd -l platform.io/tier=core --ignore-not-found --timeout=60s \
    || warn "Timed out deleting core-tier Applications — continuing anyway (k3s-uninstall.sh below wipes the node regardless)."

  # platform.io/tier=optional catches whichever of metallb/metallb-config/
  # storage-longhorn/storage-seaweedfs actually got applied (see
  # src/core/argocd/README.md's "Portability" section — install.sh only
  # applies the ones its flags asked for, so this is a no-op for whichever
  # weren't).
  info "Deleting optional-capability Applications (if any)..."
  kubectl delete applications -n argocd -l platform.io/tier=optional --ignore-not-found --timeout=60s \
    || warn "Timed out deleting optional-tier Applications — continuing anyway (k3s-uninstall.sh below wipes the node regardless)."

  # root, optional-metallb, and optional-storage-longhorn all carry the
  # cascade-delete finalizer resources-finalizer.argocd.argoproj.io (they're
  # each their own app-of-apps root — see root-app.yaml and optional/*.yaml),
  # so Argo CD tears down everything each one manages before it actually
  # finalizes. Unlike the label-selector deletes above, a plain `kubectl
  # delete` on one of these has NO default timeout — so a stalled cascade
  # (seen in testing: a crash-looping operator that can't finish processing
  # one of its own custom resources' finalizers) blocks forever with zero
  # output, no error, nothing to Ctrl-C into. Bound each one, and if it
  # doesn't finish in time, force it through: we're about to wipe the whole
  # node with k3s-uninstall.sh anyway, so nothing is lost by dropping the
  # finalizer rather than tracking down why the cascade got stuck. Only
  # root is guaranteed to exist — the others are only present if their
  # capability was ever applied, hence --ignore-not-found.
  for root_app in root optional-metallb optional-storage-longhorn optional-storage-seaweedfs; do
    if ! kubectl get application "$root_app" -n argocd >/dev/null 2>&1; then
      continue
    fi
    if ! kubectl delete application "$root_app" -n argocd --ignore-not-found --timeout=90s; then
      warn "${root_app} Application's cascade delete didn't finish in 90s (likely a stuck finalizer on a crashing operator's own custom resource) — forcing it through."
      kubectl -n argocd patch application "$root_app" --type=merge -p '{"metadata":{"finalizers":null}}' 2>/dev/null || true
    fi
  done

  info "Waiting for Argo CD to finish pruning (best-effort, 60s)..."
  sleep 60 || true

  info "Removing Argo CD itself..."
  kubectl delete namespace argocd --ignore-not-found --timeout=120s || warn "argocd namespace didn't fully clean up in time — continuing."
else
  info "No argocd namespace found — nothing to prune, moving straight to k3s removal."
fi

if command -v k3s-uninstall.sh >/dev/null 2>&1; then
  info "Running k3s's own uninstaller..."
  k3s-uninstall.sh
  success "k3s removed. This machine is back to bare metal."
elif command -v k3s-agent-uninstall.sh >/dev/null 2>&1; then
  info "Running k3s agent's own uninstaller..."
  k3s-agent-uninstall.sh
  success "k3s agent removed."
else
  warn "No k3s-uninstall.sh found — either k3s isn't installed here, or --skip-k3s was used at install time (in which case this script never touched the underlying cluster, on purpose)."
fi
