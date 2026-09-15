#!/usr/bin/env bash
# Sets up scheduled backups of k3s's own datastore (ARCHITECTURE.md §8/§12,
# Phase 0's "k3s datastore snapshot schedule"). Separate from install.sh on
# purpose — nothing else depends on this existing, it's independently
# configurable any time (before install.sh, right after, or months later
# when you add real storage to point it at), and re-running it any time
# just updates the config in place rather than starting over.
#
# Why this exists as its own thing instead of using k3s's built-in
# `etcd-snapshot` feature (which even has S3 upload flags built in): that
# feature ONLY applies to embedded-etcd clusters (multi-server, installed
# with --cluster-init). install.sh doesn't pass that flag, so this cluster
# runs on k3s's OTHER datastore option — a plain SQLite database at
# /var/lib/rancher/k3s/server/db/ — which has no built-in backup mechanism
# at all. Confirmed against k3s's own docs, not assumed:
# https://docs.k3s.io/datastore/backup-restore
#
# Scope: this is for a SELF-MANAGED k3s control plane specifically —
# homelab, self-hosted data centre, or cloud VMs you run k3s on yourself
# (see src/core/argocd/README.md's "Portability" section). On managed k8s
# (EKS/GKE/AKS), there's no host-level datastore to back up this way at all
# — the control plane and its storage are the cloud provider's problem, not
# this repo's. Skip this script entirely there.
#
# What it sets up:
#   - installs sqlite3 (for a consistent online backup of the live database)
#     and rclone (optional cloud upload — a single tool that speaks most
#     cloud storage backends through one config, so this script doesn't need
#     provider-specific logic)
#   - /usr/local/bin/k3s-snapshot.sh — the actual backup script
#   - /etc/opendataplatform/k3s-snapshot.conf — plain, hand-editable config
#     (destination path, retention count, optional rclone remote) that
#     k3s-snapshot.sh reads fresh on every run. Change your mind about where
#     backups go later? Edit this file directly — no need to re-run this
#     script or touch systemd at all.
#   - k3s-snapshot.service + .timer (systemd) — the schedule

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

CONF_FILE="/etc/opendataplatform/k3s-snapshot.conf"
SCRIPT_DEST="/usr/local/bin/k3s-snapshot.sh"

# Seed from whatever's already configured (if this is a re-run), so a flag
# you don't pass this time doesn't get silently reset to a hardcoded
# default — only flags you actually pass override the current setting.
DEST_LOCAL="/var/backups/k3s-snapshots"
RETENTION=7
RCLONE_REMOTE=""
RCLONE_PATH=""
SCHEDULE="daily"
# shellcheck disable=SC1090
[[ -f "$CONF_FILE" ]] && source "$CONF_FILE"
[[ -f /etc/opendataplatform/k3s-snapshot.schedule ]] && SCHEDULE="$(cat /etc/opendataplatform/k3s-snapshot.schedule)"

usage() {
  cat <<EOF
Usage: sudo bootstrap/snapshot-setup.sh [options]

  --dest <path>          Local directory snapshots land in first, always (default/current:
                          ${DEST_LOCAL}). Point this at a second drive or NAS mount if you have
                          one — same-disk is fine for now (protects against accidental deletion,
                          bad config changes, etc.) but not a drive failure, worth upgrading once
                          you have separate storage.
  --retention <N>         How many local snapshots to keep before pruning the oldest (default/
                          current: ${RETENTION}).
  --schedule <expr>       systemd OnCalendar expression — e.g. "daily", "*-*-* 03:00:00" (default/
                          current: ${SCHEDULE}). See: man systemd.time
  --rclone-remote <name>  Name of an rclone remote (configured separately via 'sudo rclone config'
                          — this script installs rclone but does NOT configure a remote for you;
                          that step needs your actual cloud credentials, entered by you, not
                          scripted). Once set, every snapshot also gets pushed there. Leave unset
                          to stay local-only — completely valid, not a degraded mode.
  --rclone-path <path>    Path within that remote (default: k3s-snapshots/<hostname>).
  -h, --help              Show this help

Safe to re-run any time — updates the config and systemd units in place. Nothing here depends on
install.sh having been run first or vice versa; this cluster's Postgres/Keycloak/etc. couldn't care
less whether this script has been run.
EOF
}

RCLONE_PATH_SET=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST_LOCAL="$2"; shift 2 ;;
    --retention) RETENTION="$2"; shift 2 ;;
    --schedule) SCHEDULE="$2"; shift 2 ;;
    --rclone-remote) RCLONE_REMOTE="$2"; shift 2 ;;
    --rclone-path) RCLONE_PATH="$2"; RCLONE_PATH_SET=true; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done
if [[ -z "$RCLONE_PATH" && "$RCLONE_PATH_SET" == false ]]; then
  RCLONE_PATH="k3s-snapshots/$(hostname)"
fi

[[ "$EUID" -eq 0 ]] || die "Run this as root (sudo) — it installs packages and writes systemd units."

# ---- Dependencies ----------------------------------------------------------
info "Checking for sqlite3 and rclone..."
if ! command -v sqlite3 >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y sqlite
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get install -y sqlite3
  else
    warn "No dnf or apt-get found — install sqlite3 by hand. Backups will fall back to a raw file copy without it (less safe — see k3s-snapshot.sh's own comments)."
  fi
fi
if ! command -v rclone >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y rclone || warn "rclone install failed — cloud upload won't be available until it's installed by hand (see https://rclone.org/install/)."
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get install -y rclone || warn "rclone install failed — cloud upload won't be available until it's installed by hand (see https://rclone.org/install/)."
  else
    warn "No dnf or apt-get found — install rclone by hand if you want cloud upload: https://rclone.org/install/"
  fi
fi
success "Dependencies OK."

# ---- Config file ------------------------------------------------------------
info "Writing ${CONF_FILE}..."
mkdir -p "$(dirname "$CONF_FILE")"
cat > "$CONF_FILE" <<EOF
# Written by bootstrap/snapshot-setup.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ)).
# Safe to hand-edit directly — k3s-snapshot.sh reads this fresh on every
# run, no restart or re-run of the setup script needed for a change here to
# take effect on the next scheduled snapshot.
DEST_LOCAL="${DEST_LOCAL}"
RETENTION=${RETENTION}
RCLONE_REMOTE="${RCLONE_REMOTE}"
RCLONE_PATH="${RCLONE_PATH}"
EOF
chmod 600 "$CONF_FILE"
echo "$SCHEDULE" > /etc/opendataplatform/k3s-snapshot.schedule

mkdir -p "$DEST_LOCAL"
# This directory holds the same sensitive content as the live datastore
# (Secrets, etc., once combined with the token file inside each snapshot) —
# lock it down the same way the datastore itself already is.
chmod 700 "$DEST_LOCAL"

if [[ -n "$RCLONE_REMOTE" ]] && ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
  warn "rclone remote '${RCLONE_REMOTE}' isn't configured yet — snapshots will stay local-only until you run 'sudo rclone config' and set it up. This is expected if you haven't done that yet; k3s-snapshot.sh checks for this itself and won't fail, it'll just skip the upload with a clear note each time until the remote exists."
fi

# ---- The backup script itself -----------------------------------------------
info "Installing ${SCRIPT_DEST}..."
cat > "$SCRIPT_DEST" <<'SNAPSHOT_SCRIPT_EOF'
#!/usr/bin/env bash
# k3s SQLite datastore snapshot — installed by bootstrap/snapshot-setup.sh,
# run on a schedule by k3s-snapshot.timer (systemd). Safe to run manually
# too: sudo /usr/local/bin/k3s-snapshot.sh
#
# See bootstrap/snapshot-setup.sh for the full design writeup (why this
# exists instead of k3s's built-in etcd-snapshot feature — short version:
# that feature is etcd-only, this cluster runs on SQLite instead).
#
# Backs up exactly what k3s's own restore docs say is needed: the whole
# db/ directory PLUS the server token (without the token, a restored
# snapshot is permanently unusable — it's what encrypts confidential
# datastore content, e.g. Secrets:
# https://docs.k3s.io/datastore/backup-restore). The live state.db gets a
# SQLite-native ".backup" (an online, consistent snapshot via SQLite's own
# backup API) rather than trusting the raw copy taken alongside the rest of
# the directory, since this one file is being actively written to by k3s
# continuously and a plain cp/tar risks grabbing it mid-write.

set -euo pipefail

CONF_FILE="/etc/opendataplatform/k3s-snapshot.conf"
if [[ ! -f "$CONF_FILE" ]]; then
  echo "Config not found: ${CONF_FILE} — run bootstrap/snapshot-setup.sh first." >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$CONF_FILE"

K3S_DB_DIR="/var/lib/rancher/k3s/server/db"
K3S_TOKEN="/var/lib/rancher/k3s/server/token"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT_NAME="k3s-snapshot-${TIMESTAMP}.tar.gz"

if [[ ! -d "$K3S_DB_DIR" ]]; then
  echo "No k3s datastore found at ${K3S_DB_DIR} — is k3s installed and running on this host?" >&2
  exit 1
fi
if [[ ! -f "$K3S_TOKEN" ]]; then
  echo "No k3s token found at ${K3S_TOKEN} — a snapshot without it would be unrestorable, refusing to continue." >&2
  exit 1
fi

mkdir -p "$DEST_LOCAL"
chmod 700 "$DEST_LOCAL"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Copying ${K3S_DB_DIR}..."
cp -a "$K3S_DB_DIR" "${WORKDIR}/db"

if command -v sqlite3 >/dev/null 2>&1 && [[ -f "${K3S_DB_DIR}/state.db" ]]; then
  echo "Taking a consistent online backup of state.db (safe while k3s keeps running)..."
  rm -f "${WORKDIR}/db/state.db" "${WORKDIR}/db/state.db-wal" "${WORKDIR}/db/state.db-shm"
  sqlite3 "${K3S_DB_DIR}/state.db" ".backup '${WORKDIR}/db/state.db'"
else
  echo "WARNING: sqlite3 not found — using the raw copy of state.db taken above as-is." >&2
  echo "This snapshot may be inconsistent if k3s was writing to it at the same moment." >&2
  echo "Install sqlite3 (re-run bootstrap/snapshot-setup.sh) so future snapshots use the safe online backup instead." >&2
fi

cp "$K3S_TOKEN" "${WORKDIR}/token"

echo "Archiving to ${DEST_LOCAL}/${SNAPSHOT_NAME}..."
tar -czf "${DEST_LOCAL}/${SNAPSHOT_NAME}" -C "$WORKDIR" .
chmod 600 "${DEST_LOCAL}/${SNAPSHOT_NAME}"

echo "Pruning local snapshots, keeping the newest ${RETENTION}..."
# shellcheck disable=SC2012
ls -1t "${DEST_LOCAL}"/k3s-snapshot-*.tar.gz 2>/dev/null | tail -n "+$((RETENTION + 1))" | while IFS= read -r old; do
  echo "  removing ${old}"
  rm -f "$old"
done || true

if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  if ! command -v rclone >/dev/null 2>&1; then
    echo "NOTE: RCLONE_REMOTE is set (${RCLONE_REMOTE}) but rclone isn't installed — snapshot stayed local-only this run. Re-run bootstrap/snapshot-setup.sh to install it." >&2
  elif ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
    echo "NOTE: rclone remote '${RCLONE_REMOTE}' isn't configured yet (run 'sudo rclone config') — snapshot stayed local-only this run."
  else
    echo "Uploading to ${RCLONE_REMOTE}:${RCLONE_PATH}/..."
    if rclone copy "${DEST_LOCAL}/${SNAPSHOT_NAME}" "${RCLONE_REMOTE}:${RCLONE_PATH}/"; then
      echo "Upload OK."
    else
      echo "WARNING: rclone upload failed — the local copy is still safe at ${DEST_LOCAL}/${SNAPSHOT_NAME}." >&2
    fi
  fi
else
  echo "No RCLONE_REMOTE configured — local-only snapshot (set it in ${CONF_FILE}, or re-run snapshot-setup.sh with --rclone-remote, once you have a remote)."
fi

echo "Done: ${DEST_LOCAL}/${SNAPSHOT_NAME}"
SNAPSHOT_SCRIPT_EOF
chmod 755 "$SCRIPT_DEST"

# ---- systemd service + timer ------------------------------------------------
info "Writing systemd units (schedule: ${SCHEDULE})..."
cat > /etc/systemd/system/k3s-snapshot.service <<EOF
[Unit]
Description=k3s datastore snapshot
# Best-effort ordering only (Wants/After, not Requires) — a snapshot attempt
# right as k3s is starting/stopping should fail loudly via k3s-snapshot.sh's
# own checks, not block or be blocked by k3s's own unit.
After=k3s.service

[Service]
Type=oneshot
ExecStart=${SCRIPT_DEST}
EOF

cat > /etc/systemd/system/k3s-snapshot.timer <<EOF
[Unit]
Description=Schedule for k3s-snapshot.service

[Timer]
OnCalendar=${SCHEDULE}
# Catch up on a missed run (host was off, etc.) instead of silently skipping
# straight to the next scheduled time.
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now k3s-snapshot.timer

success "Done."
echo ""
echo "  Config:            ${CONF_FILE} (hand-editable any time)"
echo "  Local destination:  ${DEST_LOCAL}"
echo "  Retention:          ${RETENTION} snapshots"
echo "  Schedule:            ${SCHEDULE}  (systemctl list-timers k3s-snapshot.timer)"
if [[ -n "$RCLONE_REMOTE" ]]; then
  echo "  Cloud upload:        ${RCLONE_REMOTE}:${RCLONE_PATH}"
else
  echo "  Cloud upload:        not configured — local-only for now"
fi
echo ""
echo "  Run one right now to test it:  sudo ${SCRIPT_DEST}"
echo "  Check recent runs:              sudo journalctl -u k3s-snapshot.service -n 50"
