#!/usr/bin/env bash
# Adds a machine to the cluster as a k3s agent, with its role label (and, for
# compute, its taint) applied at join time — ARCHITECTURE.md §7. Run this ON
# the new machine, not on the control node.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

ROLE=""
SERVER=""
TOKEN=""

usage() {
  cat <<EOF
Usage: bootstrap/join-node.sh --role <storage|compute> --server https://<control-ip>:6443 --token <k3s-token>

Get the token from the control node:
  sudo cat /var/lib/rancher/k3s/server/node-token
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

[[ "$ROLE" =~ ^(storage|compute)$ ]] || die "--role must be 'storage' or 'compute' (control nodes are joined by install.sh, not this script). See --help."
[[ -z "$SERVER" ]] && die "--server is required. See --help."
[[ -z "$TOKEN" ]] && die "--token is required. See --help."

require_cmd curl

info "Joining as a ${ROLE} node against ${SERVER}..."
curl -sfL https://get.k3s.io | \
  K3S_URL="${SERVER}" \
  K3S_TOKEN="${TOKEN}" \
  INSTALL_K3S_EXEC="--node-label platform.io/role=${ROLE}" \
  sh -

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
if [[ "$ROLE" == "compute" ]]; then
  info "Tainting this node so only compute-role workloads schedule here..."
  node_name="$(hostname)"
  # Best-effort: on some setups the agent's node name differs from `hostname`;
  # fall back to whatever just joined if the exact-name taint fails.
  kubectl taint nodes "$node_name" platform.io/role=compute:NoSchedule --overwrite 2>/dev/null \
    || warn "Couldn't taint '$node_name' automatically — run manually: kubectl taint nodes <node-name> platform.io/role=compute:NoSchedule"
fi

success "Node joined as ${ROLE}. Give the scheduler a moment, then: kubectl get nodes --show-labels"
