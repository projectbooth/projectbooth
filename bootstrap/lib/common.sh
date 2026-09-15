#!/usr/bin/env bash
# Shared helpers for the bootstrap scripts. Sourced, not executed directly.

set -euo pipefail

# k3s installs its kubectl symlink at /usr/local/bin/kubectl. Some sudo
# configs (a trimmed secure_path, common on hardened RHEL-family systems)
# don't include /usr/local/bin even once the file's right there — which
# silently breaks every bare kubectl/k3s call in scripts that source this
# file, regardless of how they were invoked. Make sure it's findable no
# matter what, once, here, rather than in every script separately.
export PATH="/usr/local/bin:${PATH}"

_c_red=$'\033[0;31m'; _c_yellow=$'\033[0;33m'; _c_green=$'\033[0;32m'; _c_blue=$'\033[0;34m'; _c_reset=$'\033[0m'

info()    { echo "${_c_blue}==>${_c_reset} $*"; }
success() { echo "${_c_green}==>${_c_reset} $*"; }
warn()    { echo "${_c_yellow}==> warning:${_c_reset} $*" >&2; }
err()     { echo "${_c_red}==> error:${_c_reset} $*" >&2; }
die()     { err "$*"; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not found on PATH."
}

# Prompt for exact-text confirmation before a destructive action.
# Usage: confirm_destructive "teardown" "This will delete the cluster's config."
confirm_destructive() {
  local word="$1" msg="${2:-}"
  [[ -n "$msg" ]] && warn "$msg"
  read -r -p "Type '${word}' to confirm: " reply
  [[ "$reply" == "$word" ]] || die "Confirmation text didn't match — aborting."
}

# Repo root = two directories up from this file (bootstrap/lib/common.sh -> repo root)
repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

# Auto-detect the git remote URL and current branch, so nothing has to be
# hardcoded into a script that different people will run from different
# clones/forks. Both are overridable via flags in the calling script.
detect_git_remote_url() {
  git -C "$(repo_root)" remote get-url origin 2>/dev/null || echo ""
}

detect_git_branch() {
  # symbolic-ref (not rev-parse) so this also works on a brand new repo
  # before the first commit exists — same fix as .githooks/pre-commit.
  git -C "$(repo_root)" symbolic-ref --short HEAD 2>/dev/null || echo "main"
}

# Replaces ONE SealedSecret document inside a manifest file that can contain
# more than one back to back — src/core/argocd/manifests/gateway.yaml has
# carried two since ui-shell-plan.md item 8/feature/module-proxy, 2026-09-10
# (gateway-github-token, item 7, and gateway-module-proxy-secret, item 8).
#
# Locates the document by its own metadata.name (unique per Secret), never
# positionally. seal-gateway-github-token.sh's first version found "the last
# bitnami.com/v1alpha1 document in the file" via `grep | tail -1` — a
# shortcut that only ever worked because there was exactly one such document
# at the time; a second SealedSecret in the same file makes it ambiguous,
# and would silently make a re-run (e.g. rotating the GitHub PAT) overwrite
# the WRONG document. This function is the fix, shared so both
# seal-gateway-github-token.sh and seal-gateway-module-proxy-secret.sh apply
# it identically rather than each carrying their own copy to keep in sync.
#
# Usage: replace_sealed_secret_document <manifest-file> <secret-name> <new-document-file>
#   <new-document-file> must contain exactly one SealedSecret document with
#   no leading/trailing '---' — exactly what `kubeseal --format yaml` prints.
replace_sealed_secret_document() {
  local manifest="$1" secret_name="$2" new_doc_file="$3"

  # gateway.yaml's working-tree line endings depend on what checked it out
  # (a Windows clone with core.autocrlf leaves real CRLFs on disk; a Linux
  # one doesn't) — every comparison below strips a trailing \r before
  # matching, so this works either way instead of silently finding nothing
  # on a CRLF checkout. cut -d: -f1 below is unaffected (it stops at the
  # first ':', which grep -n always emits plain).

  local doc_start=""
  while IFS= read -r line_no; do
    # Every SealedSecret document in this file has the same fixed shape
    # (apiVersion / kind / metadata: / name: ...), so the top-level
    # metadata.name always sits exactly 3 lines after its own apiVersion
    # line. Checked positionally like this, rather than just grepping
    # "name: $secret_name" anywhere in the file, because that same string
    # also appears deeper in the SAME document, indented under
    # spec.template.metadata.name.
    local name_line
    name_line="$(sed -n "$((line_no + 3))p" "$manifest")"
    name_line="${name_line%$'\r'}"
    if [[ "$name_line" == "  name: ${secret_name}" ]]; then
      doc_start="$line_no"
      break
    fi
    # Dropped the trailing '$' anchor deliberately (see the file-wide note
    # above) — a prefix match on this literal, distinctive string is
    # unambiguous on its own without needing to also anchor the end.
  done < <(grep -n '^apiVersion: bitnami\.com/v1alpha1' "$manifest" | cut -d: -f1)

  [[ -n "$doc_start" ]] || die \
    "Couldn't find a SealedSecret document named '${secret_name}' in ${manifest} — has this \
file's structure changed? Check it by hand before re-running."

  # Every document here is preceded by its own '---' separator, which is
  # kept as-is; only the document body itself is replaced — from its
  # apiVersion line through the line before the NEXT '---' (another
  # document follows), or through EOF if this is the last document in the
  # file. Anything after that next boundary (another document, plus its own
  # explanatory comment block) is preserved untouched.
  local keep_through=$((doc_start - 1))
  local next_boundary
  next_boundary="$(awk -v start="$doc_start" \
    'NR > start { line = $0; sub(/\r$/, "", line); if (line == "---") { print NR; exit } }' \
    "$manifest")"

  local work_file
  work_file="$(mktemp)"
  head -n "$keep_through" "$manifest" > "$work_file"
  cat "$new_doc_file" >> "$work_file"
  if [[ -n "$next_boundary" ]]; then
    tail -n "+${next_boundary}" "$manifest" >> "$work_file"
  fi
  mv "$work_file" "$manifest"
}
