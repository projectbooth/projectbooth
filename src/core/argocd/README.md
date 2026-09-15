# Argo CD — the app-of-apps

`root-app.yaml` is always applied by hand (`bootstrap/install.sh`). It points Argo CD at
`apps/core/`, and every `Application` in that folder becomes a reconciled piece of core from then
on. `optional/*-app.yaml` are the same idea, applied conditionally — see "Portability" below.

`apps/core/*.yaml` — one Argo CD `Application` per core infra piece, each pulling from that
project's own upstream chart repo (not Bitnami's general catalog — see the Aug 2025 changes to
Bitnami's free chart/image catalog; Keycloak in particular now goes through the official Operator,
not a chart). Ordered with `argocd.argoproj.io/sync-wave` so things that depend on each other
install in the right order and wait for the previous wave to go healthy first:

| Wave | Apps | Why this order |
|---|---|---|
| 0 | cert-manager, sealed-secrets, reflector, postgres-operator | Foundational, no cross-dependencies |
| 1 | cert-manager-issuers, ingress-nginx, keycloak-operator, postgres-cluster, postgres-backup-plugin | Each needs its wave-0 counterpart's CRDs/controller healthy first — issuers need cert-manager's CRDs, the Postgres Cluster CR needs the operator's CRDs, the Barman Cloud plugin needs cert-manager (its own mTLS certs) and the `cnpg-system` namespace |
| 2 | keycloak-instance, monitoring, postgres-backup, catalog-database | Needs its own operator's CRDs (wave 1) AND postgres-cluster (also wave 1) to exist first — postgres-backup specifically needs the `ObjectStore` CRD that postgres-backup-plugin (wave 1) registers; catalog-database needs `platform-postgres` and its `catalog` role (both wave 1) to exist as something to reference |
| 3 | keycloak-realm, catalog-service | keycloak-realm seeds the workspace-group model (`../auth/realm-platform.yaml`) via a `KeycloakRealmImport` Job against the live admin API — needs the actual `Keycloak` instance (wave 2) running, not just applied. catalog-service needs the `catalog` database (wave 2's `catalog-database`) to actually exist before its PreSync migration Job can run against it |
| 4 | gateway, ui-shell | One after catalog-service — cosmetic Argo CD UI ordering only (avoids a redundant simultaneous-wave Degraded flash while catalog-service's own PreSync migration Job is still running), not a real dependency; see `manifests/gateway.yaml`'s own comment. Needs `platform-ca-secret` mirrored into the `gateway` namespace (Reflector, `manifests/cluster-issuer.yaml`) to actually be present for gateway's readiness probe to pass, same "applied isn't the same as ready" caveat as everywhere else in this table. `ui-shell` (feature/ui-shell-scaffold, 2026-09-04) shares this wave with gateway rather than getting its own — it makes zero backend calls at this scope, so it has no real ordering dependency on anything; see `manifests/ui-shell.yaml`'s own comment |
| 5 | modules-root | One after gateway — best-effort ordering only (dependency-checking on individual modules' `requires:` isn't built yet, see `docs/architecture/module-lifecycle-plan.md`), not enforced. Watches `src/modules-enabled/` and turns whatever `platform module install` puts there into running modules — see `apps/core/modules-root.yaml`'s own comment for the full three-level app-of-apps shape |

`apps/optional/<capability>/*.yaml` follow the same wave numbering independently within their own
capability — MetalLB's controller (wave 0) and its IP pool config (wave 1) still need to install in
that order relative to *each other*, same reasoning as above.

No new wave for real Ingress (platform-ingress branch, 2026-09-02): the `Ingress`/`Certificate`
resources fronting Keycloak and gateway live as extra documents inside `manifests/keycloak-instance.yaml`
and `manifests/gateway.yaml` themselves, not a dedicated `Application` — each of those `Application`s
already syncs its one manifest file whole, so they ride along on waves 2 and 4 above with no table
change. Don't go looking for a wave-5 Ingress entry; there isn't one.

`postgres-operator`/`postgres-cluster` (CloudNativePG) are Phase 1 in ARCHITECTURE.md's build order, pulled forward into Phase 0 here because Keycloak needs a database before Phase 1 would otherwise provide one — see the comments in `manifests/postgres-cluster.yaml` and `keycloak-instance.yaml` for the reasoning. WAL-archive backups (`postgres-backup-plugin`, `postgres-backup`) followed once SeaweedFS existed to archive them to — see `manifests/postgres-backup.yaml`. `catalog-database` (Phase 2 kickoff, 2026-09-01) is the same `platform-postgres` cluster hosting a second database via CNPG's declarative `Database` CRD — see `manifests/catalog-database.yaml` and `src/core/catalog-service/README.md`. `catalog-service` is catalog-lite itself (the FastAPI service, not its database) — see `manifests/catalog-service.yaml` for the PreSync migration Job / Deployment / Service shape and `.github/workflows/ci.yml` for the image that Deployment pulls. That same file also carries a `NetworkPolicy` (`catalog-service-netpol` branch, 2026-09-03) restricting ingress to catalog-service's pods to the `gateway` namespace only, on port 8000 — closing the network-layer half of `docs/known-issues.md`'s "catalog-service's auth was a placeholder" entry. `gateway` (platform-gateway-auth branch, 2026-09-02) verifies Keycloak JWTs and proxies to catalog-service with derived, trustworthy headers — see `manifests/gateway.yaml` for the Deployment/Service shape and `src/core/gateway/README.md` for what it does and doesn't cover yet. `modules-root` (platform-module-lifecycle branch, 2026-09-03, `apps/core/modules-root.yaml`) is the reconciliation engine `docs/architecture/module-lifecycle-plan.md`'s first slice needed before `platform module install/uninstall` could exist at all — a second-level app-of-apps, same mechanism `root-app.yaml` itself uses one level up, watching `../modules-enabled/` instead of `apps/core/`. Every file `platform module install <name>` writes there is itself a complete `Application` manifest generated by `platform_cli/manifest.py`, sourced from `src/charts/<name>/` — see `../modules/README.md` and `../modules-enabled/README.md` for the `modules/` (catalog) vs. `modules-enabled/` (turned on) split this relies on, and `src/modules/hello-module/module.yaml` / `src/charts/hello-module/` for the one real module this branch ships to prove the mechanism end to end.

`manifests/` — plain Kubernetes resources (not Helm releases) that some of the `apps/` Applications
point at directly: the MetalLB IP pool, the cert-manager ClusterIssuer, the Keycloak CR. These are
the pieces with values specific to *your* network/cluster — each has a `TODO` comment marking what
to fill in before it'll actually go healthy. Nothing here is wrong to leave on defaults temporarily;
Argo CD will just show that Application as degraded until the TODO is addressed.

## Portability: core vs. optional capabilities

Added 2026-08-31, prompted by "would this work on EKS?" The honest answer at the time was "the
GitOps pattern and most individual apps, yes — but MetalLB and Longhorn are bare-metal assumptions
baked into what looked like one fixed 'core'." This is the fix: `apps/` splits into two halves.

**`apps/core/`** — environment-agnostic. Works identically whether the cluster is a homelab box, a
self-hosted data centre, cloud VMs you run k8s on yourself, or a managed service like EKS/GKE/AKS.
Nothing in here assumes anything about what's underneath Kubernetes. Always applied, via
`root-app.yaml`.

**`apps/optional/<capability>/`** — everything that depends on what's underneath. Each capability
has its own small root Application (`optional/<capability>-app.yaml`) applied by `bootstrap/install.sh`
only when that capability's flag says so, exactly the same mechanism as `root-app.yaml` itself (a
plain-directory app-of-apps, `__REPO_URL__`/`__REVISION__` substituted by `sed` before `kubectl apply`).
Three capabilities exist today:

- **`metallb`** — hands out real IPs for `LoadBalancer` Services (like ingress-nginx's) by ARPing on
  the local subnet. Only needed where nothing else does this job. Flag: `--skip-metallb` to opt out
  (default: applied).
- **`storage-longhorn`** — replicated block storage across this cluster's own node disks. Only
  needed where nothing else provides durable/replicated storage. Flag: `--enable-longhorn` to opt in
  (default: not applied — CloudNativePG's `Cluster` deliberately leaves `storage.storageClass` unset,
  so it always uses whatever the cluster's default `StorageClass` is, Longhorn or not).
- **`storage-seaweedfs`** — the in-cluster S3-compatible object store (ARCHITECTURE.md §2's storage
  layer, and what `postgres-backup.yaml`'s WAL-archiving points at). Only needed where nothing else
  already provides S3-compatible storage. Flag: `--skip-seaweedfs` to opt out (default: applied),
  requiring `--s3-endpoint` plus `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` env vars pointing at a
  real external endpoint instead (`bootstrap/install.sh --help`). Originally spec'd as MinIO — see
  `apps/optional/storage-seaweedfs/seaweedfs.yaml`'s header for why that changed before anything was
  ever built on it.

Why flags with *independent*, not always matching, defaults instead of one `--profile homelab|cloud`
switch: these are genuinely independent questions ("does something already hand out LoadBalancer
IPs?", "does something already provide durable block storage?", "does something already provide
S3-compatible object storage?"), not one bucket per environment — a cloud-VM cluster you run k8s on
yourself might want cloud block storage but *not* the cloud's LB service (cost), or vice versa.
Flags compose; a fixed enum of named profiles would have forced a guess at every combination anyone
might actually want. The defaults just match what this repo was originally built and tested against
(a self-hosted box with nothing else underneath it providing any of these three things) — so today's
exact zero-flag `bootstrap/install.sh` invocation still does exactly what it always did.

| Environment | `--skip-k3s` | `--skip-metallb` | `--enable-longhorn` | `--skip-seaweedfs` |
|---|---|---|---|---|
| Homelab | no | no | yes, once you have 2+ storage-capable nodes | no |
| Self-hosted data centre | no | no (unless you have real LB hardware) | usually yes | no |
| Cloud VMs, self-managed k8s | no | yes, if using the cloud's LB service | no, if using the cloud's block storage | yes, if using real cloud object storage (S3/R2/B2/etc.) |
| Managed k8s (EKS/GKE/AKS) | yes — point `kubectl`/`KUBECONFIG` at it first | yes | no | yes, point `--s3-endpoint` at real cloud object storage |

What's still a homelab-shaped default even inside `apps/core/`, worth knowing about rather than
hidden: `manifests/cluster-issuer.yaml` ships a self-signed `ClusterIssuer` — that works everywhere,
but isn't what you'd want for anything actually reachable from outside a homelab. Swapping it for a
real ACME/Let's Encrypt issuer is a config change to that one file, not a portability blocker, so it
wasn't pulled into this mechanism — see that file's own comments for the two paths (import
`platform-ca` once, or move to Let's Encrypt when there's a real public domain).

## Self-referencing apps — why `repoURL`/`targetRevision` are hardcoded, not templated

`root-app.yaml` uses `__REPO_URL__`/`__REVISION__` placeholders that `bootstrap/install.sh`
substitutes with `sed` before applying it — that's what lets `install.sh` auto-detect your remote
and current branch instead of hardcoding them. That substitution is a one-time, one-file thing:
it runs only against `root-app.yaml`, right before the script hands it to `kubectl apply`.

Every other `Application` in this folder is read straight from GitHub *by Argo CD itself* once
`root` starts reconciling — there's no script in the loop, and Argo CD has no templating
mechanism for a plain `directory:`-sourced Application. So any Application manifest in here that
points back at this same repo (`postgres-cluster`, `postgres-backup-plugin`, `postgres-backup`,
`catalog-database`, `catalog-service`, `gateway`, `ui-shell`, `cert-manager-issuers`, `metallb-config`,
`keycloak-instance`, `keycloak-realm`, `modules-root` — the ones sourcing `manifests/*.yaml`,
`../auth/*.yaml`, or `../modules-enabled/` from this repo, as opposed to an external chart) has to
use a real, literal `repoURL`/`targetRevision`, not a placeholder. A placeholder left in one of
these isn't "filled in later" the way it is in `root-app.yaml` — it's a permanently broken source
that Argo CD can never resolve, and it fails silently as a stuck `Unknown` sync status with no
obvious symptom pointing at the cause (this bit us in testing — see `docs/known-issues.md`).

Current convention: these hardcode `repoURL: git@github.com:DougallPercival/opendataplatform.git`
and `targetRevision: dev`. If you deliberately bootstrap from a different branch (a feature branch,
say, for isolated testing), `root` itself will track it fine via `--revision`, but these will keep
tracking `dev` until you update them by hand — a known, accepted limitation of this pattern rather
than something `install.sh` can fix for you. Every module Application `platform module install`
generates follows the same `targetRevision: dev` convention, but discovers its `repoURL` live via
`git remote get-url origin` rather than hardcoding it — see `platform_cli/repo.py`'s
`discover_repo_url` docstring for why generated content doesn't need the same "update by hand"
caveat hand-authored files here do.

## The `:dev` image tag and the git-sha rollout trick (2026-09-11)

`catalog-service.yaml`/`gateway.yaml`/`ui-shell.yaml` all hardcode `image: ghcr.io/dougallpercival/
<service>:dev` — a floating tag, deliberately, matching the dev → test → main promotion model
`ci.yml`'s own header comment and `ARCHITECTURE.md` §10 describe (see each manifest's own "self-
referencing apps" image comment). That choice has a real cost `docs/known-issues.md` documents at
length: Argo CD reports `Synced`/`Healthy` the moment the *manifest text* matches git, which is
true the instant `:dev` is merged, whether or not the *running pod* has actually repulled the new
image — `imagePullPolicy: Always` only repulls on a real pod restart, and nothing about a floating
tag's own text changes when the tag's target moves underneath it. This bit `gateway`/`ui-shell`
directly, repeatedly, in one session (`docs/known-issues.md`), always requiring a manual
`kubectl rollout restart` to actually prove out.

**Fixed, not worked around:** `ci.yml`'s three `build-and-push*` jobs each gained a step
(`.github/scripts/set-git-sha-annotation.sh`) that, after a successful push-triggered image build,
patches that service's own manifest — a `platform.io/git-sha` annotation on the Deployment's *pod
template* (`spec.template.metadata.annotations`, not just top-level `metadata`) — to the triggering
commit's SHA, then commits and pushes that one-line change straight back to the branch that
triggered the build. This is the exact same trick `kubectl rollout restart` uses under the hood (it
patches a `kubectl.kubernetes.io/restartedAt` annotation in the same spot) — changing anything in
the pod template forces Kubernetes to actually roll new pods, regardless of whether the image
*reference* text changed. Argo CD's own automated `selfHeal` sync (already `true` on all three
Applications) picks the resulting commit up like any other change, no different from a normal PR
merge.

Kept deliberately narrow: this doesn't touch the `:dev`/`:test`/`:main` promotion model, doesn't add
digest-pinning, and doesn't need Argo CD Image Updater or any new controller — just one more small,
CI-authored commit, following the exact precedent `module-lifecycle.yml` already set for a workflow
pushing straight back to `dev` with `contents: write` on its own `GITHUB_TOKEN`. The commit only ever
touches `manifests/*.yaml`, never anything under `ci.yml`'s own `paths:` filter, so it can't retrigger
itself — confirmed by inspection of that filter, reinforced with `[skip ci]` as defense in depth. See
`.github/scripts/set-git-sha-annotation.sh`'s own comment for why a plain, precise `sed` substitution
was chosen over a full YAML-parsing tool (these files are heavily hand-commented and, in gateway's
case, CRLF-terminated — a full parse/serialize round-trip risks exactly the kind of reformatting this
repo has already been bitten by once, with the SealedSecret boundary-detection bug).

**Status:** confirmed live, 2026-09-11. Merging `feature/git-sha-rollout` (PR #55 + PR #56 — the
`.github/` files landed in a follow-up commit after the remote file-write bridge's protected-path
guard refused to write them directly) touched `ci.yml` itself, which sits in every package's own
`paths:` filter — so that merge's push tripped all three `build-and-push-*` jobs at once, the first
real exercise of all three new steps simultaneously. All three went green and each produced its own
`chore(<service>): bump deployed git-sha annotation ...` commit on `dev`, all stamping the same
`f0c7f1d...` merge-commit SHA. `kubectl get deploy <svc> -n <svc> -o jsonpath='...platform\.io/git-
sha...'` read that exact SHA back for gateway, ui-shell, and catalog-service, and each service's pod
came back with `RESTARTS: 0` and an age far younger than its Deployment object — a real new pod,
rolled by Argo CD's `selfHeal` off the annotation-only commit alone, with no `kubectl rollout restart`
run anywhere in the sequence. See `docs/known-issues.md`'s stale-pod entry for the full writeup.

## RBAC — gateway's Argo CD access (platform-module-deps branch, 2026-09-03; broadened feature/force-cleanup, 2026-09-10)

`manifests/gateway.yaml` now includes this repo's first `ServiceAccount`/`Role`/`RoleBinding` —
everything before this ran as the implicit `default` ServiceAccount with no RBAC at all. It exists
because `GET /modules/check-requirements` (gateway's `app/modules.py`/`app/argocd.py`,
module-lifecycle-plan.md item 6) has to ask the Kubernetes API which module `Application`s exist
and whether Argo CD reports them `Healthy`, to answer "is module X installed and satisfied" for
`platform module install`'s dependency check.

Scoped as tightly as this specific need: a `Role` in the `argocd` namespace (where `Application`
objects actually live, not gateway's own `gateway` namespace) granting `get`/`list`/`watch` on
`applications.argoproj.io` — originally read-only, no other resource types (no Secrets, no Pods,
nothing), no cluster-wide `ClusterRole`. Confirming this actually grants gateway's pod the access
it needs (as opposed to being blocked by a wrong namespace/verb/resource) can only be done live — a
403 from the Kubernetes API is the plausible failure mode if this is ever wrong; see
`docs/known-issues.md` if that's what you're debugging.

**Broadened 2026-09-10** to add the `delete` verb (the `Role`/`RoleBinding` pair renamed
`gateway-read-module-applications` → `gateway-module-applications` to match — it's no longer
read-only). This backs `POST /modules/{id}/force-cleanup`
(`app/modules.py`, `app/argocd.py`'s `delete_module_application()`), the clickable version of the
`kubectl -n argocd delete application <id>` workaround for the `modules-root` prune gap documented
in `docs/known-issues.md`. The endpoint issues a plain Kubernetes API `DELETE` against the same
`Application` object, using this same ServiceAccount token — deliberately not a separate Argo
CD-native REST API credential (that would be a second credential shape, a second SealedSecret, a
second bootstrap script, for no behavioral difference): a `kubectl delete`, this `DELETE`, and Argo
CD's own REST API's delete endpoint all trigger the same
`resources-finalizer.argocd.argoproj.io` finalizer cascade on the object. Still scoped to exactly
one resource type in exactly one namespace — no other verbs, no other resources, no cluster-wide
scope.
