#!/usr/bin/env bash
# Runs the same health checks this session did by hand after every install —
# node readiness, every pod across the cluster, every Argo CD Application,
# Postgres, Keycloak, monitoring, and ingress-nginx's external IP — as one
# report instead of pasting kubectl commands one at a time. Safe to re-run
# any time; it only reads cluster state, never changes anything.
#
# Known-benign states are called out explicitly rather than flagged as
# issues (see docs/known-issues.md for why each one is expected):
#   - metallb: OutOfSync/Healthy — a Helm-generated webhook Secret the chart
#     doesn't track in git, not a real problem.
#   - storage-longhorn: not Synced — expected if you've deliberately deferred
#     Longhorn for single-node testing (disabled its automated sync).

set -uo pipefail
# Deliberately NOT set -e: a single failed check should be reported and
# counted, not abort the rest of the report.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

require_cmd kubectl

ISSUES=0
note_issue() { warn "$*"; ISSUES=$((ISSUES + 1)); }
section() { echo; info "== $* =="; }

# ---- Node ----------------------------------------------------------------
section "Node"
kubectl get nodes
if ! kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}' | grep -q "Ready"; then
  note_issue "No node reporting Ready."
fi

# ---- Every pod, every namespace -------------------------------------------
section "Pods (anything not Running/Completed/Succeeded, across all namespaces)"
BAD_PODS="$(kubectl get pods -A --no-headers 2>/dev/null | awk '$4!="Running" && $4!="Completed" && $4!="Succeeded" {print}')"
if [[ -n "$BAD_PODS" ]]; then
  echo "$BAD_PODS"
  note_issue "Some pods aren't Running/Completed (see above) — check their namespace with 'kubectl describe pod' if this isn't expected."
else
  success "All pods Running/Completed."
fi

# ---- Argo CD Applications, with a short settle-in retry -------------------
# Right after any sync — a fresh install, a resync, adding repo credentials —
# it's completely normal for several Applications to sit at Unknown (first
# comparison pending) or Progressing (resources applied, not all ready yet)
# for a few minutes: cert-manager, keycloak-operator's webhook, and especially
# postgres-operator/postgres-cluster and monitoring have all done this
# repeatedly in testing. Retry through both states before reporting, instead
# of flagging completely expected transient noise the moment this is run
# right after an install finishes — which is exactly when it's most likely
# to be run.
section "Argo CD Applications"
APP_TRIES=0
APP_MAX_TRIES=12   # 12 x 10s = up to 2 minutes. Postgres/Keycloak bootstrap can occasionally
                    # take longer than that on a fresh cluster — if you still see Progressing
                    # after this script moves on, that's not necessarily wrong, just re-run
                    # verify.sh again in another minute or two before treating it as a problem.
while true; do
  APP_ROWS="$(kubectl get applications -n argocd -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.sync.status}{"\t"}{.status.health.status}{"\n"}{end}' 2>/dev/null)"
  STILL_SETTLING=false
  while IFS=$'\t' read -r name sync health; do
    [[ -z "$name" ]] && continue
    case "$name" in
      metallb|storage-longhorn) continue ;;   # never worth waiting on — see exceptions below
    esac
    case "$sync/$health" in
      "Unknown/"*) STILL_SETTLING=true ;;
      *"/Progressing") STILL_SETTLING=true ;;
    esac
  done <<< "$APP_ROWS"
  if [[ "$STILL_SETTLING" == false || $APP_TRIES -ge $APP_MAX_TRIES ]]; then
    break
  fi
  APP_TRIES=$((APP_TRIES + 1))
  info "Some Applications still settling (Unknown or Progressing) — waiting 10s (try ${APP_TRIES}/${APP_MAX_TRIES})..."
  sleep 10
done

kubectl get applications -n argocd
while IFS=$'\t' read -r name sync health; do
  [[ -z "$name" ]] && continue
  case "$name" in
    metallb)
      [[ "$sync" == "OutOfSync" && "$health" == "Healthy" ]] && continue
      ;;
    storage-longhorn)
      # Deliberately not flagged — see header comment. Still shown above via
      # 'kubectl get applications' so you can see its actual state.
      continue
      ;;
  esac
  if [[ "$sync" != "Synced" || "$health" != "Healthy" ]]; then
    note_issue "Application '${name}' is ${sync}/${health}."
  fi
done <<< "$APP_ROWS"

# ---- CloudNativePG operator -------------------------------------------
section "CloudNativePG operator (cnpg-system)"
if kubectl get namespace cnpg-system >/dev/null 2>&1; then
  kubectl -n cnpg-system get pods
  if kubectl -n cnpg-system get pods --no-headers 2>/dev/null | awk '$3!="Running"' | grep -q .; then
    note_issue "cnpg-system has a pod that isn't Running."
  fi
else
  info "cnpg-system namespace not found yet — skipping."
fi

# ---- Postgres cluster -------------------------------------------------
section "Postgres cluster (postgres)"
if kubectl get namespace postgres >/dev/null 2>&1; then
  kubectl -n postgres get cluster 2>/dev/null
  CLUSTER_PHASE="$(kubectl -n postgres get cluster -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")"
  if [[ -z "$CLUSTER_PHASE" ]]; then
    note_issue "No Postgres Cluster resource found in the postgres namespace yet."
  elif ! grep -qi "healthy" <<< "$CLUSTER_PHASE"; then
    note_issue "Postgres cluster phase is '${CLUSTER_PHASE}', not healthy yet (normal for the first few minutes after a fresh install)."
  else
    success "Postgres cluster: ${CLUSTER_PHASE}"
  fi
else
  info "postgres namespace not found yet — skipping."
fi

# ---- Keycloak -----------------------------------------------------------
section "Keycloak"
if kubectl get namespace keycloak >/dev/null 2>&1; then
  kubectl -n keycloak get pods
  if kubectl -n keycloak get pods --no-headers 2>/dev/null | awk '$3!="Running"' | grep -q .; then
    note_issue "keycloak namespace has a pod that isn't Running (can be normal briefly after a fresh install while it waits on Postgres)."
  fi
else
  info "keycloak namespace not found yet — skipping."
fi

# ---- Monitoring -----------------------------------------------------------
section "Monitoring"
if kubectl get namespace monitoring >/dev/null 2>&1; then
  kubectl -n monitoring get pods
  if kubectl -n monitoring get pods --no-headers 2>/dev/null | awk '{print $3}' | grep -q "CrashLoopBackOff"; then
    note_issue "monitoring namespace has a crash-looping pod — if it's node-exporter, check for a port conflict with any native node_exporter on this host (see docs/known-issues.md)."
  fi
else
  info "monitoring namespace not found yet — skipping."
fi

# ---- ingress-nginx external IP -------------------------------------------
section "ingress-nginx external IP"
if kubectl get namespace ingress-nginx >/dev/null 2>&1; then
  EXT_IP="$(kubectl -n ingress-nginx get svc -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].status.loadBalancer.ingress[0].ip}' 2>/dev/null)"
  if [[ -z "$EXT_IP" ]]; then
    note_issue "ingress-nginx has no external IP yet — check MetalLB (metallb-pool.yaml's range vs. this host's subnet, see docs/known-issues.md)."
  else
    success "ingress-nginx external IP: ${EXT_IP}"
  fi
else
  info "ingress-nginx namespace not found yet — skipping."
fi

# ---- Summary ----------------------------------------------------------
section "Summary"
if [[ $ISSUES -eq 0 ]]; then
  success "All checks passed."
  exit 0
else
  warn "${ISSUES} issue(s) found — see warnings above."
  exit 1
fi
