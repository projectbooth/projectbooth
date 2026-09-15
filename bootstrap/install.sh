#!/usr/bin/env bash
# The one unavoidable script (ARCHITECTURE.md §3). Brings a bare machine up
# to "Argo CD is reconciling this repo's core/" — everything after that is
# a choice made in the shell/CLI/git, not another script to run.
#
# What it does:
#   1. Installs k3s if one isn't already running (control-node role,
#      traefik + servicelb disabled — we bring our own ingress + MetalLB).
#   2. Installs Argo CD (official stable manifest).
#   3. Applies the root Application (the "app of apps") pointing Argo CD at
#      src/core/argocd/apps/core in THIS repo, on the branch you're currently
#      on (or --revision, if you want it tracking something else) — this half
#      always applies, on every environment.
#   4. Conditionally applies one small root Application per OPTIONAL
#      capability (MetalLB, Longhorn) — see "Portability" below.
#
# Portability (2026-08-31): apps/core/ is the environment-agnostic half of the
# platform — works identically on a homelab box, a self-hosted data centre,
# cloud VMs you run k8s on yourself, or a managed service like EKS/GKE/AKS.
# apps/optional/<capability>/ holds the pieces that only make sense on SOME
# of those (MetalLB needs a subnet to ARP on; Longhorn needs disks to
# replicate across) — each gated by its own flag below, defaulting to
# whatever this repo was originally built for (a self-hosted box with no
# cloud LB/block-storage underneath it). See src/core/argocd/README.md's
# "Portability" section for the full design and the flag-per-environment
# table.
#
# From here on, adding/removing anything is a git operation, not a script —
# see the Add-ons page / platform-cli / modules-enabled/ in ARCHITECTURE.md §3.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# (common.sh above also fixes up PATH for /usr/local/bin/kubectl — see its comment)

ROLE="control"
REPO_URL=""
REVISION=""
ARGOCD_VERSION="stable"
SKIP_K3S=false
REPO_SSH_KEY=""
SKIP_ISCSI=false
SKIP_METALLB=false
ENABLE_LONGHORN=false
SKIP_SEAWEEDFS=false
S3_ENDPOINT=""
S3_REGION="us-east-1"

usage() {
  cat <<EOF
Usage: bootstrap/install.sh [options]

  --repo-url <url>     Git remote for Argo CD to track (default: this clone's 'origin')
  --revision <branch>  Branch/tag for Argo CD to track (default: current branch)
  --role <role>        Node role for this machine: control|storage|compute (default: control)
  --skip-k3s           Don't touch k3s — use it against an existing cluster (any distro,
                        including managed services like EKS/GKE/AKS: point kubectl/KUBECONFIG
                        at it first, then run this with --skip-k3s --skip-metallb).
  --repo-ssh-key <path> Path to an SSH private key (a read-only deploy key is enough) that
                        Argo CD's repo-server should use to clone REPO_URL. Only needed if
                        REPO_URL is private. Registers it as a repository credential Secret
                        in the argocd namespace, so you don't have to do this by hand after
                        every install (it lives in the argocd namespace, so a teardown that
                        deletes that namespace takes it with it — pass this again on re-install
                        rather than repeating docs/known-issues.md's manual steps).
  --skip-metallb        Don't deploy MetalLB (src/core/argocd/apps/optional/metallb/) — for any
                        environment where a cloud provider already hands out real LoadBalancer
                        IPs (EKS/GKE/AKS, or self-managed k8s on cloud VMs using that cloud's LB
                        integration). Default: MetalLB IS deployed — the common case for a
                        homelab/self-hosted box with no cloud LB underneath it.
  --enable-longhorn      Deploy Longhorn (src/core/argocd/apps/optional/storage-longhorn/) for
                        replicated storage across this cluster's own node disks. Default: off —
                        single-node testing works fine on k3s's built-in local-path-provisioner,
                        and any environment with cloud block storage (EKS/GKE/AKS, or self-managed
                        k8s on cloud VMs) doesn't need this at all. Also installs/enables iscsid
                        (Longhorn's host prerequisite) unless --skip-iscsi.
  --skip-iscsi          When --enable-longhorn is set, don't touch host iSCSI packages/services —
                        useful on a managed/cloud node where you don't want this script running
                        dnf/apt as root on your behalf, or if you'll manage iscsid yourself. Has
                        no effect without --enable-longhorn (see docs/known-issues.md).
  --skip-seaweedfs      Don't deploy SeaweedFS (src/core/argocd/apps/optional/storage-seaweedfs/)
                        — for any environment with real S3-compatible object storage already
                        available (cloud S3/R2/B2/etc.). Requires --s3-endpoint, plus
                        S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY set as environment variables
                        (deliberately not flags — see below) pointing at that real endpoint.
                        Default: SeaweedFS IS deployed — the common case for a homelab/self-hosted
                        box with no object storage underneath it already. Either way, Postgres's
                        WAL-archiving backups (manifests/postgres-backup.yaml) end up pointed at
                        whichever one this run set up.
  --s3-endpoint <url>   Only used with --skip-seaweedfs. The external S3-compatible endpoint to
                        point Postgres's backups at (e.g. https://s3.us-west-002.backblazeb2.com).
  --s3-region <region>  Region value to record alongside the S3 credentials (default: us-east-1 —
                        a harmless placeholder when the endpoint doesn't use real AWS regions,
                        like SeaweedFS; set a real one for an external endpoint that checks it).
  -h, --help            Show this help

S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY (environment variables, only read with --skip-seaweedfs):
credentials for the external endpoint --s3-endpoint points at. Passed as env vars rather than
flags on purpose — a flag value is visible in shell history and process listings
(ps/'kubectl get pods -o yaml' for anything that echoes its own args), an env var passed inline
on the command that invokes this script isn't recorded either place. Example:
  S3_ACCESS_KEY_ID=... S3_SECRET_ACCESS_KEY=... sudo -E bash bootstrap/install.sh --skip-seaweedfs --s3-endpoint https://...

Phase 0 gets you: k3s, Argo CD, and the app-of-apps handoff (core + whichever optional
capabilities you asked for). See src/core/argocd/README.md's "Portability" section for the full
flag-per-environment table. The ingress/cert-manager issuer still needs values filled in for a
real deployment — see the TODOs Argo CD will surface as it syncs (src/core/argocd/manifests/).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --revision) REVISION="$2"; shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    --skip-k3s) SKIP_K3S=true; shift ;;
    --repo-ssh-key) REPO_SSH_KEY="$2"; shift 2 ;;
    --skip-iscsi) SKIP_ISCSI=true; shift ;;
    --skip-metallb) SKIP_METALLB=true; shift ;;
    --enable-longhorn) ENABLE_LONGHORN=true; shift ;;
    --skip-seaweedfs) SKIP_SEAWEEDFS=true; shift ;;
    --s3-endpoint) S3_ENDPOINT="$2"; shift 2 ;;
    --s3-region) S3_REGION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

REPO_ROOT="$(repo_root)"
[[ -z "$REPO_URL" ]] && REPO_URL="$(detect_git_remote_url)"
[[ -z "$REVISION" ]] && REVISION="$(detect_git_branch)"
[[ -z "$REPO_URL" ]] && die "Couldn't detect a git remote URL — pass --repo-url explicitly."

info "Repo:     $REPO_URL"
info "Revision: $REVISION"
info "Role:     $ROLE"

# ---- 0. iSCSI (Longhorn's host prerequisite) ----------------------------
# Only runs at all when Longhorn itself is going to be deployed (--enable-longhorn)
# — Longhorn (apps/optional/storage-longhorn/storage.yaml) needs iscsid running
# on any node that'll host its volumes, or its pods just never go healthy with
# no obvious host-level cause. Best-effort, non-fatal even when it does run.
# Package name and the iscsid-vs-iscsi.service distinction verified against
# Longhorn's own troubleshooting docs, not assumed:
# https://longhorn.io/kb/troubleshooting-open-iscsi-on-rhel/ — iscsid is the
# daemon Longhorn actually talks to; iscsi.service does unrelated boot-time
# node auto-discovery and is deliberately left alone/disabled.
if [[ "$ENABLE_LONGHORN" != true ]]; then
  info "Longhorn not requested (--enable-longhorn not set) — skipping iSCSI setup too."
elif [[ "$SKIP_ISCSI" == true ]]; then
  info "Skipping iSCSI setup (--skip-iscsi) — you'll need iscsid running yourself before Longhorn goes healthy."
elif systemctl is-active --quiet iscsid 2>/dev/null; then
  info "iscsid already running — leaving it as-is."
elif command -v dnf >/dev/null 2>&1; then
  info "Installing iscsi-initiator-utils and enabling iscsid..."
  if dnf install -y iscsi-initiator-utils && systemctl enable --now iscsid; then
    success "iscsid is up."
  else
    warn "iSCSI setup failed — not fatal, but storage-longhorn won't go healthy until this is sorted. See docs/known-issues.md."
  fi
elif command -v apt-get >/dev/null 2>&1; then
  info "Installing open-iscsi and enabling iscsid..."
  if apt-get install -y open-iscsi && systemctl enable --now iscsid; then
    success "iscsid is up."
  else
    warn "iSCSI setup failed — not fatal, but storage-longhorn won't go healthy until this is sorted. See docs/known-issues.md."
  fi
else
  warn "No dnf or apt-get found — skipping iSCSI setup. Install open-iscsi/iscsi-initiator-utils by hand if you plan to use Longhorn."
fi

# ---- 1. k3s -----------------------------------------------------------
if [[ "$SKIP_K3S" == true ]]; then
  info "Skipping k3s install (--skip-k3s) — using the cluster kubectl is already pointed at."
  require_cmd kubectl
elif command -v k3s >/dev/null 2>&1; then
  info "k3s already installed — leaving it as-is."
else
  require_cmd curl
  info "Installing k3s (role: ${ROLE})..."
  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_EXEC="--disable traefik --disable servicelb --node-label platform.io/role=${ROLE}" \
    sh -
  # k3s ships kubectl at /usr/local/bin/k3s; symlink kept by the installer as
  # /usr/local/bin/kubectl normally. Point KUBECONFIG at k3s's own file for
  # the rest of this script either way.
  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  require_cmd kubectl
  info "Waiting for the node to report Ready..."
  _waited=0
  until kubectl get nodes 2>/dev/null | grep -q " Ready"; do
    sleep 2
    _waited=$((_waited + 2))
    if [[ $_waited -ge 180 ]]; then
      die "Node still not Ready after 3 minutes. Check directly: kubectl get nodes / sudo journalctl -u k3s -n 100"
    fi
  done
  success "k3s is up."
fi

# ---- 2. Argo CD ---------------------------------------------------------
require_cmd kubectl
if kubectl get namespace argocd >/dev/null 2>&1; then
  info "Argo CD namespace already exists — leaving the existing install in place."
else
  info "Installing Argo CD (${ARGOCD_VERSION})..."
  kubectl create namespace argocd
  # --server-side: Argo CD's CRDs (applicationsets.argoproj.io especially) are
  # big enough that plain client-side `kubectl apply` — which stashes the
  # whole previous config into a last-applied-configuration annotation for
  # diffing — blows past Kubernetes' 256KiB annotation limit. Server-side
  # apply tracks field ownership on the API server instead, sidestepping the
  # annotation entirely. --force-conflicts so a re-run of this script doesn't
  # error out over field ownership from its own previous run.
  kubectl apply --server-side --force-conflicts -n argocd -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
  info "Waiting for the Argo CD API server to be ready (this can take a couple of minutes)..."
  kubectl -n argocd wait --for=condition=available --timeout=300s deployment/argocd-server
  success "Argo CD is up."
fi

# Captured now, before anything below creates it — used by step 5 to tell a
# genuinely fresh CNPG bring-up (needs the plugin-discovery dance) apart from
# an already-running one (doesn't).
CNPG_SYSTEM_PRE_EXISTED=false
kubectl get namespace cnpg-system >/dev/null 2>&1 && CNPG_SYSTEM_PRE_EXISTED=true

# ---- 2b. Repo credentials (only if REPO_URL is private) -----------------
if [[ -n "$REPO_SSH_KEY" ]]; then
  [[ -f "$REPO_SSH_KEY" ]] || die "--repo-ssh-key path not found: $REPO_SSH_KEY"
  info "Registering repo credentials with Argo CD's repo-server..."
  # Argo CD's repo-server has no credentials of its own by default — cloning
  # this repo from the host shell (e.g. the deploy key used for `git clone`
  # here) does NOT give the repo-server pod any access. Without this, a
  # private REPO_URL fails with something like: "error creating SSH agent:
  # SSH agent requested but SSH_AUTH_SOCK not-specified". Apply is idempotent
  # (dry-run|apply) so re-running install.sh with the same key is a no-op.
  kubectl -n argocd create secret generic gitops-repo-credentials \
    --from-literal=type=git \
    --from-literal=url="${REPO_URL}" \
    --from-file=sshPrivateKey="${REPO_SSH_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n argocd label secret gitops-repo-credentials argocd.argoproj.io/secret-type=repository --overwrite
  success "Repo credentials registered."
else
  info "No --repo-ssh-key given — skipping repo credential setup (fine if REPO_URL is public; see docs/known-issues.md if it isn't)."
fi

# ---- 2c. Sanity-check MetalLB's committed range against this host's subnet
# Only relevant when MetalLB is actually going to be deployed (not --skip-metallb).
# metallb-pool.yaml's IP range has to be real, network-specific values (see
# docs/known-issues.md — there's no way to derive "which slice of your subnet
# is safe" automatically, so it's committed as a concrete range, not a
# placeholder like __REPO_URL__). That means it's silently wrong if this repo
# ever runs against different hardware/network than it was set up on — and
# MetalLB's L2Advertisement mode doesn't just risk a conflict in that case, it
# doesn't work AT ALL, since it ARPs on the local subnet. Warn loudly here
# rather than let that surface later as "ingress-nginx's external IP never
# leaves <pending>" with no obvious cause.
if [[ "$SKIP_METALLB" == true ]]; then
  info "MetalLB not requested (--skip-metallb) — skipping its subnet sanity-check too."
elif command -v ip >/dev/null 2>&1; then
  POOL_FILE="${REPO_ROOT}/src/core/argocd/manifests/metallb-pool.yaml"
  if [[ -f "$POOL_FILE" ]]; then
    POOL_FIRST_IP="$(grep -m1 -oE '([0-9]{1,3}\.){3}[0-9]{1,3}-' "$POOL_FILE" | head -1 | sed 's/-$//')"
    HOST_CIDR="$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | head -1)"
    if [[ -n "$POOL_FIRST_IP" && -n "$HOST_CIDR" ]]; then
      POOL_PREFIX="$(cut -d. -f1-3 <<< "$POOL_FIRST_IP")"
      HOST_PREFIX="$(cut -d. -f1-3 <<< "$HOST_CIDR")"
      if [[ "$POOL_PREFIX" != "$HOST_PREFIX" ]]; then
        warn "metallb-pool.yaml's IP range (${POOL_PREFIX}.x) doesn't match this host's detected subnet (${HOST_CIDR}, ${HOST_PREFIX}.x)."
        warn "MetalLB needs its pool on the SAME subnet as this host to actually work — a mismatch here usually means"
        warn "this repo was set up on different hardware/network than it's running on now. Update"
        warn "src/core/argocd/manifests/metallb-pool.yaml before relying on ingress-nginx getting a working external IP."
        warn "Continuing anyway — this is a warning, not a blocker."
      else
        info "metallb-pool.yaml's IP range matches this host's subnet (${HOST_PREFIX}.x)."
      fi
    fi
  fi
fi

# ---- 2d. Postgres→Keycloak DB credential (cross-namespace via Reflector) --
# Kubernetes Secrets are namespace-scoped, but platform-postgres (postgres-
# operator/postgres-cluster.yaml) is a shared core service and Keycloak lives
# in its own `keycloak` namespace — CNPG's own auto-generated app secret is
# stuck in `postgres` and Keycloak's CR can't see it there (this is exactly
# what left platform-0 in CreateContainerConfigError on 2026-08-30; see
# docs/known-issues.md). The fix: generate this one credential ourselves,
# store it as a plain Secret (never committed to git) in the `postgres`
# namespace, and let apps/core/reflector.yaml mirror it into `keycloak` (and
# any future consuming namespace) automatically. postgres-cluster.yaml's
# managed.roles then reconciles the `keycloak` role's actual password to
# match it. Idempotent — only generates a password the first time; re-running
# this script never rotates an existing credential out from under a running
# cluster.
info "Ensuring the Postgres→Keycloak credential secret exists..."
kubectl create namespace postgres --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if kubectl -n postgres get secret platform-postgres-keycloak-credentials >/dev/null 2>&1; then
  info "platform-postgres-keycloak-credentials already exists — leaving it as-is."
else
  if command -v openssl >/dev/null 2>&1; then
    DB_PASSWORD="$(openssl rand -base64 24)"
  else
    DB_PASSWORD="$(head -c 24 /dev/urandom | base64)"
  fi
  kubectl -n postgres create secret generic platform-postgres-keycloak-credentials \
    --type=kubernetes.io/basic-auth \
    --from-literal=username=keycloak \
    --from-literal=password="${DB_PASSWORD}"
  unset DB_PASSWORD
  # Annotations tell Reflector (apps/reflector.yaml) to auto-mirror this
  # Secret into the keycloak namespace, keeping it in sync if it ever changes.
  kubectl -n postgres annotate secret platform-postgres-keycloak-credentials \
    reflector.v1.k8s.emberstack.com/reflection-allowed=true \
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled=true \
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=keycloak \
    --overwrite
  success "platform-postgres-keycloak-credentials created."
fi

# ---- 2e. S3-compatible object storage credentials -----------------------
# manifests/postgres-backup.yaml's ObjectStore (Postgres WAL archiving +
# scheduled backups, ARCHITECTURE.md §8) and, when deployed, apps/optional/
# storage-seaweedfs/'s S3 gateway both read from this one Secret
# (platform-s3-credentials). Two modes, same as the Longhorn/MetalLB flags:
# generate one in-cluster by default (SeaweedFS), or build one from a real
# external endpoint you already have (--skip-seaweedfs). Idempotent like 2d
# — never rotates an existing credential out from under a running deployment.
info "Ensuring the S3 object-storage credential secret exists..."
if [[ "$SKIP_SEAWEEDFS" == true ]]; then
  [[ -n "$S3_ENDPOINT" ]] || die "--skip-seaweedfs requires --s3-endpoint (see --help)."
  [[ -n "${S3_ACCESS_KEY_ID:-}" && -n "${S3_SECRET_ACCESS_KEY:-}" ]] || \
    die "--skip-seaweedfs requires S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY set as environment variables (see --help)."
  info "Using external S3-compatible endpoint: ${S3_ENDPOINT}"
  if kubectl -n postgres get secret platform-s3-credentials >/dev/null 2>&1; then
    info "platform-s3-credentials already exists in postgres — leaving it as-is."
  else
    kubectl -n postgres create secret generic platform-s3-credentials \
      --from-literal=ACCESS_KEY_ID="${S3_ACCESS_KEY_ID}" \
      --from-literal=ACCESS_SECRET_KEY="${S3_SECRET_ACCESS_KEY}" \
      --from-literal=REGION="${S3_REGION}"
    success "platform-s3-credentials created (external endpoint)."
  fi
  warn "manifests/postgres-backup.yaml's ObjectStore ships with SeaweedFS's in-cluster endpointURL committed as the default — update it to ${S3_ENDPOINT} by hand (see that file's own TODO) before backups will actually reach your external endpoint."
else
  kubectl create namespace storage-seaweedfs --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  if kubectl -n storage-seaweedfs get secret platform-s3-credentials >/dev/null 2>&1; then
    info "platform-s3-credentials already exists in storage-seaweedfs — leaving it as-is."
  else
    if command -v openssl >/dev/null 2>&1; then
      S3_SECRET_KEY="$(openssl rand -base64 30)"
    else
      S3_SECRET_KEY="$(head -c 30 /dev/urandom | base64)"
    fi
    kubectl -n storage-seaweedfs create secret generic platform-s3-credentials \
      --from-literal=ACCESS_KEY_ID="platform" \
      --from-literal=ACCESS_SECRET_KEY="${S3_SECRET_KEY}" \
      --from-literal=REGION="${S3_REGION}"
    unset S3_SECRET_KEY
    # Mirror into postgres — CNPG's ObjectStore and the bucket-creation Job
    # (both in manifests/postgres-backup.yaml) need it there too. Same
    # Reflector mechanism as 2d.
    kubectl -n storage-seaweedfs annotate secret platform-s3-credentials \
      reflector.v1.k8s.emberstack.com/reflection-allowed=true \
      reflector.v1.k8s.emberstack.com/reflection-auto-enabled=true \
      reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=postgres \
      --overwrite
    success "platform-s3-credentials created (in-cluster SeaweedFS)."
  fi
fi

# ---- 2f. Postgres→catalog-service DB credential (cross-namespace via -----
#          Reflector) — same exact pattern as 2d, one workspace-tenancy
#          service later, added 2026-09-01 for Phase 2's catalog-lite.
# platform-postgres-catalog-credentials is what postgres-cluster.yaml's
# managed.roles reconciles the `catalog` role's password from, and what a
# future catalog-service Deployment will read DATABASE_URL out of once one
# exists (see src/core/catalog-service/README.md's "Not yet built" list —
# there's no Deployment yet, so this Secret currently has nowhere to mirror
# TO until that catalog-service namespace exists; Reflector just waits).
# Idempotent, same as 2d — never rotates an existing credential.
info "Ensuring the Postgres→catalog-service credential secret exists..."
if kubectl -n postgres get secret platform-postgres-catalog-credentials >/dev/null 2>&1; then
  info "platform-postgres-catalog-credentials already exists — leaving it as-is."
else
  if command -v openssl >/dev/null 2>&1; then
    CATALOG_DB_PASSWORD="$(openssl rand -base64 24)"
  else
    CATALOG_DB_PASSWORD="$(head -c 24 /dev/urandom | base64)"
  fi
  kubectl -n postgres create secret generic platform-postgres-catalog-credentials \
    --type=kubernetes.io/basic-auth \
    --from-literal=username=catalog \
    --from-literal=password="${CATALOG_DB_PASSWORD}"
  unset CATALOG_DB_PASSWORD
  kubectl -n postgres annotate secret platform-postgres-catalog-credentials \
    reflector.v1.k8s.emberstack.com/reflection-allowed=true \
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled=true \
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=catalog-service \
    --overwrite
  success "platform-postgres-catalog-credentials created."
fi

# ---- 3. Hand Argo CD the core app-of-apps -------------------------------
# Always applied — apps/core/ is the environment-agnostic half of the
# platform (see the portability note at the top of this file).
info "Applying the root Application (src/core/argocd/apps/core)..."
sed \
  -e "s|__REPO_URL__|${REPO_URL}|g" \
  -e "s|__REVISION__|${REVISION}|g" \
  "${REPO_ROOT}/src/core/argocd/root-app.yaml" | kubectl apply -f -

# ---- 4. Hand Argo CD the optional capabilities requested -----------------
# Each of these is its own small root Application, applied the same way as
# core's — only the ones the flags above asked for. See src/core/argocd/
# optional/*.yaml and README.md's "Portability" section.
APPLIED_OPTIONAL=()
if [[ "$SKIP_METALLB" != true ]]; then
  info "Applying optional capability: MetalLB (src/core/argocd/apps/optional/metallb)..."
  sed \
    -e "s|__REPO_URL__|${REPO_URL}|g" \
    -e "s|__REVISION__|${REVISION}|g" \
    "${REPO_ROOT}/src/core/argocd/optional/metallb-app.yaml" | kubectl apply -f -
  APPLIED_OPTIONAL+=("metallb")
fi
if [[ "$ENABLE_LONGHORN" == true ]]; then
  info "Applying optional capability: Longhorn (src/core/argocd/apps/optional/storage-longhorn)..."
  sed \
    -e "s|__REPO_URL__|${REPO_URL}|g" \
    -e "s|__REVISION__|${REVISION}|g" \
    "${REPO_ROOT}/src/core/argocd/optional/storage-longhorn-app.yaml" | kubectl apply -f -
  APPLIED_OPTIONAL+=("storage-longhorn")
fi
if [[ "$SKIP_SEAWEEDFS" != true ]]; then
  info "Applying optional capability: SeaweedFS (src/core/argocd/apps/optional/storage-seaweedfs)..."
  sed \
    -e "s|__REPO_URL__|${REPO_URL}|g" \
    -e "s|__REVISION__|${REVISION}|g" \
    "${REPO_ROOT}/src/core/argocd/optional/storage-seaweedfs-app.yaml" | kubectl apply -f -
  APPLIED_OPTIONAL+=("storage-seaweedfs")
fi

# ---- 5. One-time CNPG operator restart for Barman Cloud plugin discovery -
# See docs/known-issues.md ("CNPG's operator only discovers the Barman Cloud
# plugin at its own startup") for the full story — short version: the
# operator enumerates available CNPG-I plugins once, at its own process
# start, and never again. Argo CD's wave ordering guarantees postgres-operator
# (wave 0) is already healthy before postgres-backup-plugin (wave 1) is even
# created, so on a genuinely fresh Postgres/CNPG bring-up the operator
# reliably starts before the plugin exists — meaning it reliably misses it,
# every time, without this. Only run this dance when cnpg-system didn't
# exist before this run (CNPG_SYSTEM_PRE_EXISTED, captured in step 2) — an
# already-running cluster's operator either already knows about the plugin,
# or this was already handled by hand; restarting it again on every re-run
# of this script would just be needless reconciliation churn for no benefit.
if [[ "$CNPG_SYSTEM_PRE_EXISTED" == true ]]; then
  info "cnpg-system already existed before this run — skipping the one-time plugin-discovery operator restart."
elif [[ "$SKIP_K3S" == true ]]; then
  info "Skipping the plugin-discovery wait+restart against an existing external cluster (--skip-k3s) — do it by hand if Postgres backups ever fail with 'requested plugin is not available' (see docs/known-issues.md)."
else
  info "Waiting for the Barman Cloud plugin to come up (up to 5 minutes) so the CNPG operator can be restarted to discover it..."
  _waited=0
  until kubectl -n cnpg-system get pods -l app=barman-cloud 2>/dev/null | grep -q '1/1.*Running'; do
    sleep 5
    _waited=$((_waited + 5))
    if [[ $_waited -ge 300 ]]; then
      break
    fi
  done
  if kubectl -n cnpg-system get pods -l app=barman-cloud 2>/dev/null | grep -q '1/1.*Running'; then
    info "Barman Cloud plugin is up — restarting the CNPG operator so it discovers it (one-time)..."
    if kubectl -n cnpg-system get deployment postgres-operator-cloudnative-pg >/dev/null 2>&1; then
      kubectl -n cnpg-system rollout restart deployment postgres-operator-cloudnative-pg
      kubectl -n cnpg-system rollout status deployment postgres-operator-cloudnative-pg --timeout=120s \
        || warn "Operator restart didn't report ready in time — check manually: kubectl -n cnpg-system get pods"
      success "CNPG operator restarted — Postgres backups should work without the manual fix in docs/known-issues.md."
    else
      warn "cnpg-system's operator Deployment not found yet — skipping restart. If Postgres backups later fail with 'requested plugin is not available', see docs/known-issues.md for the one-line fix."
    fi
  else
    warn "Barman Cloud plugin didn't come up within 5 minutes — skipping the operator restart. If Postgres backups later fail with 'requested plugin is not available', see docs/known-issues.md for the one-line fix."
  fi
fi

success "Done. Argo CD is reconciling core from ${REPO_URL}@${REVISION}."
if [[ ${#APPLIED_OPTIONAL[@]} -gt 0 ]]; then
  success "Optional capabilities applied: ${APPLIED_OPTIONAL[*]}"
else
  info "No optional capabilities applied (--skip-metallb, --enable-longhorn's default, --skip-seaweedfs) — core only."
fi
echo ""
echo "  Watch it sync:   kubectl get applications -n argocd -w"
echo "  Argo CD UI:       kubectl -n argocd port-forward svc/argocd-server 8080:443"
echo "                     (initial admin password: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
echo ""
echo "  Still TODO before core is fully healthy — see src/core/argocd/manifests/:"
if [[ "$SKIP_METALLB" != true ]]; then
  echo "   - MetalLB's IP address pool (your actual LAN/cloud subnet)"
fi
echo "   - cert-manager's ClusterIssuer (ships self-signed by default)"
echo "   - postgres-backup.yaml's ObjectStore endpointURL (confirm SeaweedFS's real Service name/port once it's synced, or your external endpoint if --skip-seaweedfs)"
