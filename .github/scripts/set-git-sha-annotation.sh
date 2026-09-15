#!/usr/bin/env bash
# Patches one Deployment manifest's `platform.io/git-sha` pod-template annotation to the given
# value — see ci.yml's own comment on the step that calls this for why it exists: closing the
# mutable `:dev` image tag's biggest downside (Synced/Healthy never proves a new build is actually
# running — docs/known-issues.md) without touching the :dev/:test/:main promotion model itself.
# Changing a pod template's annotation is exactly what `kubectl rollout restart` does under the
# hood (it patches `spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"]`) —
# this does the same trick with a value that's actually informative (the commit whose image this
# now is, so `kubectl get deploy -o jsonpath='{.spec.template.metadata.annotations.platform\.io/git-sha}'`
# answers "is the new build running yet" directly) and commits it to git from CI instead of
# requiring a human to run it by hand against the live cluster after every deploy.
#
# A plain, precise sed substitution rather than a full YAML parse/rewrite (yq or similar) on
# purpose: these manifest files are multi-document, heavily hand-commented, and (gateway.yaml)
# CRLF-terminated — a full parse/serialize round-trip risks exactly the kind of reformatting/
# comment-loss this repo has already been bitten by once (see bootstrap/lib/common.sh's
# replace_sealed_secret_document() docstring for that story, and its own CRLF-tolerance fix). This
# gets away with a plain sed because `platform.io/git-sha: "..."` is deliberately kept unique per
# file — exactly one Deployment per manifests/*.yaml in this repo — so unlike the SealedSecret case
# (which has to disambiguate between multiple documents of the same kind), there's no
# document-boundary detection to get right here, just a single already-unique line to replace.
#
# CRLF safety: sed splits input on `\n` only, so a CRLF file's "lines" keep their trailing `\r` as
# an ordinary trailing character. The pattern below matches up to (and including) the closing `"`
# and stops there — the `\r`, if present, sits after the match and is copied through untouched,
# same as every other character sed didn't touch. Verified against a real copy of gateway.yaml
# (this repo's only CRLF manifest) before this script's first real use — see this branch's own
# writeup in docs/known-issues.md / the READMEs for how that was confirmed.
#
# Usage: set-git-sha-annotation.sh <manifest-file> <git-sha>
set -euo pipefail

file="$1"
sha="$2"

if [[ ! -f "$file" ]]; then
  echo "::error::$file not found" >&2
  exit 1
fi

# Fails loudly rather than silently no-op'ing: if a manifest ever loses this placeholder (a
# careless hand-edit, or this script pointed at the wrong file), that's a real problem worth
# stopping the build for, not a case to quietly skip — the whole point of this script is that a
# missing/wrong annotation update should never pass silently.
if ! grep -q 'platform\.io/git-sha: "' "$file"; then
  echo "::error::$file has no platform.io/git-sha annotation to patch — did manifests/*.yaml's Deployment lose its placeholder annotation (spec.template.metadata.annotations)?" >&2
  exit 1
fi

sed -i "s|platform\.io/git-sha: \"[^\"]*\"|platform.io/git-sha: \"${sha}\"|" "$file"

# Cheap sanity check that the sed didn't produce something structurally broken — this script's
# only real failure mode, given the pattern above is anchored precisely, but worth catching before
# it's committed rather than after Argo CD rejects it.
python3 -c "
import sys
import yaml
with open(sys.argv[1]) as f:
    list(yaml.safe_load_all(f))
" "$file"
