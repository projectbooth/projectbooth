#!/usr/bin/env bash
# Automates ARCHITECTURE.md §12's single highest-stakes "day one, not later" reminder — until now,
# apps/core/sealed-secrets.yaml's own bottom-of-file comment was the ENTIRE mechanism for this:
#
#   kubectl -n sealed-secrets get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > \
#     sealed-secrets-private-key.yaml
#
# a one-liner you were expected to remember to run by hand, once, right after first install. Two real
# problems with that as the ONLY mechanism, found while scoping this script (not this repo's opinion —
# confirmed against the bitnami/sealed-secrets project's own README/values.yaml, chart version 2.19.3 /
# appVersion 0.39.1, the exact version apps/core/sealed-secrets.yaml pins):
#
#   1. The controller rotates its own keypair automatically every 30 days by default
#      (`--key-renew-period`, defaults to 720h; this repo's Application sets no override, so the
#      default applies). A single manual export from day one silently goes stale the first time
#      rotation happens — it can still decrypt everything sealed BEFORE that rotation (old keys are
#      never deleted, only superseded — confirmed against the same project), but it can no longer
#      recover the CURRENT active key, meaning a disaster after month 2 restores a cluster that can
#      decrypt old secrets but not re-seal new ones with continuity, and worse, a human has to
#      remember rotation happened at all to know a re-export is even needed.
#   2. "The moment it's generated" doesn't wait for a human to remember — this script exists so
#      nothing has to.
#
# Fix: same shape as bootstrap/snapshot-setup.sh (this repo's other "backup something irreplaceable on
# a schedule" script) — a hand-editable config file, an optional rclone push, a systemd timer — applied
# to this narrower, more urgent target instead of the whole k3s datastore. Deliberately a SEPARATE
# script from snapshot-setup.sh, not a step bolted onto it, for two reasons: (1) snapshot-setup.sh is
# itself independently opt-in ("nothing else depends on this existing") — chaining this to it would
# make the single most irreplaceable artifact in the cluster only as safe as an unrelated, optional
# script someone might not have run yet; (2) the k3s datastore snapshot's own db/ directory technically
# already contains this same Secret (it's stored in the datastore like everything else), but treating
# THAT as sufficient misses the actual point ARCHITECTURE.md's §12 reminder is making — this key is
# categorically different from every other piece of state in §8 (everything else is restorable from a
# backup; lose this and every other backup you have is unreadable ciphertext), and deserves its own
# direct, fast, obviously-named recovery path rather than being buried as one Secret inside a much
# larger datastore archive nobody would think to grep first in an actual emergency.
#
# What it sets up:
#   - installs rclone (optional cloud upload, same as snapshot-setup.sh — a single tool that speaks
#     most cloud storage backends through one config)
#   - /usr/local/bin/sealed-secrets-key-export.sh — the actual export script
#   - /etc/opendataplatform/sealed-secrets-key-export.conf — plain, hand-editable config (destination
#     path, retention count, optional rclone remote), read fresh on every run
#   - sealed-secrets-key-export.service + .timer (systemd)
#
# Run once right now, don't wait for the timer: sudo bootstrap/export-sealed-secrets-key.sh && \
#   sudo /usr/local/bin/sealed-secrets-key-export.sh — the whole point of "day one, not later."

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

CONF_FILE="/etc/opendataplatform/sealed-secrets-key-export.conf"
SCRIPT_DEST="/usr/local/bin/sealed-secrets-key-export.sh"

# Seed from whatever's already configured (if this is a re-run), so a flag you don't pass this time
# doesn't get silently reset to a hardcoded default — same convention snapshot-setup.sh established.
DEST_LOCAL="/var/backups/sealed-secrets-keys"
RETENTION=7
RCLONE_REMOTE=""
RCLONE_PATH=""
SCHEDULE="daily"
# shellcheck disable=SC1090
[[ -f "$CONF_FILE" ]] && source "$CONF_FILE"
[[ -f /etc/opendataplatform/sealed-secrets-key-export.schedule ]] && \
  SCHEDULE="$(cat /etc/opendataplatform/sealed-secrets-key-export.schedule)"

usage() {
  cat <<EOF
Usage: sudo bootstrap/export-sealed-secrets-key.sh [options]

  --dest <path>          Local directory exports land in first, always (default/current:
                          ${DEST_LOCAL}). Same "same-disk is fine for now, a second drive or NAS is
                          better" caveat as snapshot-setup.sh's --dest.
  --retention <N>         How many local exports to keep before pruning the oldest (default/current:
                          ${RETENTION}). Cheap to keep more — this is a few KB of YAML, not a database
                          snapshot — 7 is a "if the newest one is somehow corrupted" margin, not a
                          rotation-coverage window (rotation coverage comes from --schedule instead).
  --schedule <expr>       systemd OnCalendar expression — e.g. "daily", "*-*-* 03:00:00" (default/
                          current: ${SCHEDULE}). Comfortably beats the controller's 30-day default key
                          rotation; there's no real cost to checking daily for a change this cheap to
                          check.
  --rclone-remote <name>  Name of an rclone remote (configured separately via 'sudo rclone config' —
                          this script installs rclone but does NOT configure a remote for you; that
                          needs your actual cloud credentials, entered by you, not scripted). Once set,
                          every export also gets pushed there. Leave unset to stay local-only — valid,
                          but read this script's own header again if you do: "offsite" is the entire
                          point for THIS particular file.
  --rclone-path <path>    Path within that remote (default: sealed-secrets-keys/<hostname>).
  -h, --help              Show this help

Safe to re-run any time — updates the config and systemd units in place.
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
  RCLONE_PATH="sealed-secrets-keys/$(hostname)"
fi

[[ "$EUID" -eq 0 ]] || die "Run this as root (sudo) — it installs packages and writes systemd units."

require_cmd kubectl

# ---- Dependencies ------------------------------------------------------------
info "Checking for rclone..."
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

# ---- Config file --------------------------------------------------------------
info "Writing ${CONF_FILE}..."
mkdir -p "$(dirname "$CONF_FILE")"
cat > "$CONF_FILE" <<EOF
# Written by bootstrap/export-sealed-secrets-key.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ)).
# Safe to hand-edit directly — sealed-secrets-key-export.sh reads this fresh on every run, no
# restart or re-run of the setup script needed for a change here to take effect on the next
# scheduled export.
DEST_LOCAL="${DEST_LOCAL}"
RETENTION=${RETENTION}
RCLONE_REMOTE="${RCLONE_REMOTE}"
RCLONE_PATH="${RCLONE_PATH}"
EOF
chmod 600 "$CONF_FILE"
echo "$SCHEDULE" > /etc/opendataplatform/sealed-secrets-key-export.schedule

mkdir -p "$DEST_LOCAL"
# This directory holds the literal private keys that decrypt every SealedSecret in the cluster — lock
# it down at least as tightly as snapshot-setup.sh locks down its own (arguably more tightly matters
# here, but matching that established convention rather than inventing a stricter one nothing else in
# this repo follows).
chmod 700 "$DEST_LOCAL"

if [[ -n "$RCLONE_REMOTE" ]] && ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
  warn "rclone remote '${RCLONE_REMOTE}' isn't configured yet — exports will stay local-only until you run 'sudo rclone config' and set it up. sealed-secrets-key-export.sh checks for this itself and won't fail, it'll just skip the upload with a clear note each time until the remote exists."
fi

# ---- The export script itself ---------------------------------------------------
info "Installing ${SCRIPT_DEST}..."
cat > "$SCRIPT_DEST" <<'SNAPSHOT_SCRIPT_EOF'
#!/usr/bin/env bash
# sealed-secrets private-key export — installed by bootstrap/export-sealed-secrets-key.sh, run on a
# schedule by sealed-secrets-key-export.timer (systemd). Safe to run manually too:
# sudo /usr/local/bin/sealed-secrets-key-export.sh
#
# See bootstrap/export-sealed-secrets-key.sh for the full design writeup — short version: this is
# ARCHITECTURE.md §12's "export the secrets-encryption key to offsite storage the moment it's
# generated" reminder, automated and re-run on a schedule because the controller rotates this key
# automatically (every 30 days by default) and a single manual export goes stale the first time that
# happens.
#
# -l sealedsecrets.bitnami.com/sealed-secrets-key (no value) matches EVERY key the controller has ever
# generated, not just the currently-active one — confirmed against the bitnami/sealed-secrets project
# that rotation never deletes a superseded key, only adds a new one, specifically so it can keep
# decrypting SealedSecrets that were sealed under an older key. Backing up anything less than the full
# set would silently lose the ability to recover secrets sealed before the most recent rotation.
#
# Quoted heredoc ('SNAPSHOT_SCRIPT_EOF' in the setup script that wrote this file) — no setup-time
# substitution happened here, every $VARIABLE below is this script's own, resolved when IT runs, not
# baked in at install time. NAMESPACE is a plain literal rather than a setup-script variable on
# purpose: it's a fixed constant (the bitnami/sealed-secrets chart's own namespace, apps/core/
# sealed-secrets.yaml), not something export-sealed-secrets-key.sh exposes a --flag for.

set -euo pipefail
umask 077   # every file this creates starts private — no write-then-chmod window for the most
            # sensitive artifact in this cluster to sit briefly world-readable.

CONF_FILE="/etc/opendataplatform/sealed-secrets-key-export.conf"
if [[ ! -f "$CONF_FILE" ]]; then
  echo "Config not found: ${CONF_FILE} — run bootstrap/export-sealed-secrets-key.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONF_FILE"

NAMESPACE="sealed-secrets"
LABEL_SELECTOR="sealedsecrets.bitnami.com/sealed-secrets-key"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DEST_FILE="${DEST_LOCAL}/sealed-secrets-keys-${TIMESTAMP}.yaml"

# Cheap existence check before the real export — fails loudly with a clear, actionable message rather
# than silently writing (and then dutifully backing up, forever) an empty "items: []" file that LOOKS
# like a successful backup of nothing. The most likely real cause: this ran moments after a fresh
# install, before the sealed-secrets controller pod has finished starting and generated its first key.
KEY_COUNT="$(kubectl -n "$NAMESPACE" get secret -l "$LABEL_SELECTOR" -o name 2>/dev/null | wc -l)"
if [[ "$KEY_COUNT" -eq 0 ]]; then
  echo "ERROR: no sealed-secrets key found in namespace '${NAMESPACE}' — has the controller finished" >&2
  echo "starting? Check: kubectl -n ${NAMESPACE} get pods" >&2
  exit 1
fi

mkdir -p "$DEST_LOCAL"
chmod 700 "$DEST_LOCAL"

echo "Exporting ${KEY_COUNT} sealed-secrets key(s) from namespace '${NAMESPACE}'..."
kubectl -n "$NAMESPACE" get secret -l "$LABEL_SELECTOR" -o yaml > "$DEST_FILE"
chmod 600 "$DEST_FILE"

echo "Pruning local exports, keeping the newest ${RETENTION}..."
# shellcheck disable=SC2012
ls -1t "${DEST_LOCAL}"/sealed-secrets-keys-*.yaml 2>/dev/null | tail -n "+$((RETENTION + 1))" | while IFS= read -r old; do
  echo "  removing ${old}"
  rm -f "$old"
done || true

if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  if ! command -v rclone >/dev/null 2>&1; then
    echo "NOTE: RCLONE_REMOTE is set (${RCLONE_REMOTE}) but rclone isn't installed — export stayed local-only this run. Re-run bootstrap/export-sealed-secrets-key.sh to install it." >&2
  elif ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
    echo "NOTE: rclone remote '${RCLONE_REMOTE}' isn't configured yet (run 'sudo rclone config') — export stayed local-only this run."
  else
    echo "Uploading to ${RCLONE_REMOTE}:${RCLONE_PATH}/..."
    if rclone copy "$DEST_FILE" "${RCLONE_REMOTE}:${RCLONE_PATH}/"; then
      echo "Upload OK."
    else
      echo "WARNING: rclone upload failed — the local copy is still safe at ${DEST_FILE}." >&2
    fi
  fi
else
  echo "No RCLONE_REMOTE configured — local-only export (set it in ${CONF_FILE}, or re-run export-sealed-secrets-key.sh with --rclone-remote, once you have a remote). Read this script's own header again if that's not deliberate: 'offsite' is the entire point for this particular file."
fi

echo "Done: ${DEST_FILE}"
SNAPSHOT_SCRIPT_EOF
chmod 755 "$SCRIPT_DEST"

# ---- systemd service + timer --------------------------------------------------
info "Writing systemd units (schedule: ${SCHEDULE})..."
cat > /etc/systemd/system/sealed-secrets-key-export.service <<EOF
[Unit]
Description=sealed-secrets private-key offsite export
# Best-effort ordering only (Wants/After, not Requires) — same "fail loudly via the script's own
# checks, don't block or be blocked by" reasoning as k3s-snapshot.service.
After=k3s.service

[Service]
Type=oneshot
ExecStart=${SCRIPT_DEST}
EOF

cat > /etc/systemd/system/sealed-secrets-key-export.timer <<EOF
[Unit]
Description=Schedule for sealed-secrets-key-export.service

[Timer]
OnCalendar=${SCHEDULE}
# Catch up on a missed run (host was off, etc.) instead of silently skipping straight to the next
# scheduled time — same as k3s-snapshot.timer.
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now sealed-secrets-key-export.timer

success "Done."
echo ""
echo "  Config:            ${CONF_FILE} (hand-editable any time)"
echo "  Local destination:  ${DEST_LOCAL}"
echo "  Retention:          ${RETENTION} exports"
echo "  Schedule:            ${SCHEDULE}  (systemctl list-timers sealed-secrets-key-export.timer)"
if [[ -n "$RCLONE_REMOTE" ]]; then
  echo "  Cloud upload:        ${RCLONE_REMOTE}:${RCLONE_PATH}"
else
  echo "  Cloud upload:        not configured — local-only for now (see this script's own header:"
  echo "                        'offsite' is the whole point for this particular file)"
fi
echo ""
echo "  Run one right now — don't wait for the timer, this is a 'day one, not later' export:"
echo "    sudo ${SCRIPT_DEST}"
echo "  Check recent runs:  sudo journalctl -u sealed-secrets-key-export.service -n 50"
