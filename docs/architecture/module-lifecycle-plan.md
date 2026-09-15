# Module lifecycle: a properly-scoped build plan

## Context

ARCHITECTURE.md §11's Phase 2 build-order row lists `platform module uninstall --purge-data`
alongside `platform-cli function promote` and "git remote," as if the three were comparably-sized
leftover items. Researching that row for the `platform-function-promote` branch (2026-09-03) found
otherwise: `function promote` was a well-scoped, purely client-side gap (catalog-service's backend
already existed) — but `module uninstall --purge-data` isn't buildable in isolation at all. As of
that same date, `src/modules/` and `src/modules-enabled/` are still exactly what §3 describes them
as on paper and nothing more: `src/modules/_template/module.yaml` plus two placeholder READMEs. No
Argo CD `Application` watches `modules-enabled/` yet, `platform module install` doesn't exist,
there's no module registry, and `ui-shell` doesn't exist at all (confirmed independently in
`src/core/gateway/README.md`'s own "What's NOT built yet" section). Building *uninstall* before
*install* exists doesn't make sense — the real task hiding behind that one bullet point is standing
up the whole module lifecycle mechanism ARCHITECTURE.md §3 already designs, which is its own
multi-branch, Phase-3-plus-sized piece of work.

This doc exists so that work is tracked as a real, scoped plan — not silently dropped, and not
re-derived from scratch by whoever eventually picks it up. It intentionally does not re-explain
design decisions ARCHITECTURE.md §3 already made (the three-doors model, `modules/` vs.
`modules-enabled/`, the `module.yaml` shape); it names which section covers each piece and focuses
on build order and what's genuinely still open.

**Status (updated 2026-09-03, platform-module-lifecycle branch): the "Recommended first slice"
below — items 1-5 — is now built.** `src/core/argocd/apps/core/modules-root.yaml`,
`src/platform-cli/platform_cli/{manifest,repo,module}.py`, `src/charts/_template/` +
`src/charts/hello-module/`, and `src/modules/hello-module/module.yaml` are all real, not
placeholders — see each item below for exactly where. Items 6-7 remain open, unchanged from the
original plan. The rest of this document is left as originally written (including "What already
exists," now historical) except where a status note marks something as resolved.

## What already exists

- `src/modules/README.md`, `src/modules-enabled/README.md` — description only, matching what §3
  says these directories are for.
- `src/modules/_template/module.yaml` — the skeleton `platform-cli module scaffold` will eventually
  generate from, per §3's "Building a new module" subsection. Nothing consumes it yet.
- Nothing else. No Argo CD `Application` watches `modules-enabled/`. No `platform module` Typer
  subcommand group in `platform-cli` (contrast with `dataset`/`function`/`workspace`, which exist).
  No module registry, no `PlatformModule` CRD/registration mechanism, no Add-ons page, no `ui-shell`.

## Dependency-ordered build list

Each item names the ARCHITECTURE.md section that already specifies its design, where one exists.

1. **An Argo CD `Application` watching `modules-enabled/`.** ✅ **Built** —
   `src/core/argocd/apps/core/modules-root.yaml`, wave 5. Exactly the app-of-apps shape predicted
   here: same mechanism `root-app.yaml` uses one level up, `directory.recurse: false`, pointed at
   `src/modules-enabled/` instead of `apps/core/`. Each file it finds there is itself a complete
   Argo CD `Application` (see item 4's manifest.py note), not a lightweight pointer.

2. **`module.yaml` schema + validation**, per §3's own `notebook-jupyterhub` example: `id`,
   `displayName`, `icon`, `navPath`, `proxyTo`, `healthCheck`, `requires`, `optional`. ✅ **Built** —
   `src/platform-cli/platform_cli/manifest.py`'s `ModuleManifest` (Pydantic, `extra="forbid"`),
   plus one additive field, `namespace` (defaults to `id`) — resolves the "open questions" section's
   validation-failure-mode question below.

3. **The chart-wrapper layer §7 references** — `module.yaml`'s placement hint (`platform.io/role:
   control|storage|compute`) turned into a real `nodeSelector`/`tolerations` block on whatever the
   module's chart deploys. ✅ **Built** — resolved as a Helm-template convention, not a separate
   wrapper tool: every chart scaffolded from `src/charts/_template/` gets `templates/_helpers.tpl`'s
   `platform.nodeSelector`/`platform.tolerations` named templates for free, and
   `platform module install` computes the actual `placement` values from `module.yaml` and writes
   them into the generated Application's `spec.source.helm.values` block
   (`manifest.py`'s `render_application_manifest`). No separate values file, no post-render step.

4. **`platform-cli module install <name>` / `module uninstall <name> [--purge-data]`.** ✅ **Built**
   — `src/platform-cli/platform_cli/module.py`. `install` validates + renders + writes
   `modules-enabled/<id>.yaml` + commits + pushes (via `repo.py`'s git helpers); `uninstall` mirrors
   it for removal. The PVC-ownership question below is resolved for v1: PVCs only (bucket/schema
   ownership stays open, see below), via a label + `Delete=false` annotation convention
   (`src/charts/hello-module/templates/pvc.yaml`), and `--purge-data` prints the `kubectl delete
   pvc` command rather than running it — platform-cli has no cluster credentials for anything
   beyond git, by design.

5. **`platform-cli module scaffold <name>`.** ✅ **Built** — same file, `module.py`'s `scaffold`
   command. Generates both `modules/<name>/module.yaml` and `charts/<name>/` from their respective
   `_template/` directories; deliberately doesn't commit (see `module.py`'s own docstring for why
   that's different from install/uninstall).

6. **Dependency checking (`requires: [...]`) "at the API layer both doors call through"** (§3's own
   phrasing — "the dependency check lives once, at the API layer both doors call through," referring
   to the CLI door and the Add-ons-page door sharing one check). ✅ **Built** (platform-module-deps
   branch, 2026-09-03) — `GET /modules/check-requirements` (`src/core/gateway/app/modules.py`,
   `app/argocd.py`), which answers "is module X installed and healthy" by listing Argo CD
   `Application`s via the Kubernetes API (new RBAC: a `gateway` ServiceAccount read-only-scoped to
   `applications.argoproj.io` in `argocd`, see `argocd/README.md`), not "what does module X
   require" — the caller (`platform module install`, `platform_cli/module.py`'s `_check_requires`)
   already has its own `requires: [...]` list from the manifest it just validated, so no static
   module index was needed for this slice (that index is still future work — see item 7 below).
   `--skip-requires-check` is the escape hatch. A module with `requires: []` (the template's own
   default now, and `hello-module`'s) never calls gateway at all — `install` stays exactly as
   login-free as it was before this branch.

7. **gateway's module registry / `PlatformModule` registrations / the Add-ons page API**, and
   `ui-shell` itself. Both explicitly out of scope today: `src/core/gateway/README.md`'s "What's NOT
   built yet" section already names the registry/nav-aggregation/Add-ons-page piece as future work.
   `ui-shell` itself now exists as a deployed static-placeholder scaffold (feature/ui-shell-scaffold
   branch, 2026-09-04, `ui-shell-plan.md` item 1 — see `src/core/ui-shell/README.md`) — but there's
   still nothing to serve real nav *to*: no registry, no live data, no auth. Needed for the Add-ons-
   page door specifically — the CLI door (item 4) and the git door (committing straight into
   `modules-enabled/` by hand) don't depend on either of these. **Scoped in full, 2026-09-04
   (docs/ui-shell-plan branch): see `docs/architecture/ui-shell-plan.md`** — item 7 turned out to be
   several separate subsystems (frontend stack choice, browser OAuth, a static release-time module
   index, a git-push-credentials trust-boundary question, reverse-proxying into module UIs),
   not one bullet's worth of work, the same discovery that split items 6/7 apart in the first place.

## Recommended first slice

Not "build the whole system" — a concrete, buildable starting point for whoever picks this up next,
following the same "usable on its own" phasing principle ARCHITECTURE.md §11 already applies between
its numbered phases:

**Items 1-5, against one real, deliberately simple module** (a trivial test module, not
`notebook-jupyterhub` — proving the mechanism shouldn't be coupled to also standing up a real
JupyterHub deployment on the first pass). Concretely: the `modules-enabled/`-watching Application,
`module.yaml` validation, the chart-wrapper's node-placement piece, `platform module
install/uninstall/scaffold`, and a first real end-to-end live verification — install a module via
CLI, confirm Argo CD reconciles it, uninstall it, confirm it's gone, confirm `--purge-data` actually
drops what it claims to.

**Done (2026-09-03):** all of the above is built, against `src/modules/hello-module/` — stock
nginx, `placement: {role: compute}`, one throwaway PVC — per items 1-5's ✅ markers above. Live
verification (install → Argo reconciles → placement lands on the compute node → uninstall leaves
the PVC → `--purge-data` prints the removal command) is tracked separately as this branch's own
live-verification pass, not re-described here — see the branch's own plan file / commit history for
that record rather than duplicating it in this doc.

**Items 6-7 explicitly deferred to their own later branch** — the dependency-check endpoint, the
module registry, the Add-ons page, and `ui-shell` are a substantial, separable piece of work in their
own right (arguably the harder half, since `ui-shell` doesn't exist at all), and nothing in items 1-5
requires them to already exist. Splitting here mirrors how `platform-ingress` and
`catalog-service-netpol` each shipped independently useful, narrowly-scoped pieces rather than one
large branch.

**Item 6 done (2026-09-03, platform-module-deps branch):** turned out smaller than it first looked
once recognized that the caller already has the `requires` list locally — see item 6's own ✅ marker
above for the full design. Item 7 (gateway's module registry, the Add-ons page, `ui-shell`) stayed
scoped out again, same "properly scope, don't bundle" call this doc already made once — `ui-shell`
was still a five-line README with zero frontend tooling anywhere in the repo when this branch was
scoped, confirmed via a repo-wide search for `package.json`/npm/React/`.tsx`/etc. that came back
empty.

## Open questions this doc deliberately doesn't resolve

Resolved by the platform-module-lifecycle branch (2026-09-03) — kept here, marked, rather than
deleted, so the reasoning stays visible next to the question it answers:

- ~~The chart-wrapper mechanism (item 3)~~ — **resolved**: a Helm-template convention
  (`_helpers.tpl`'s named templates) plus values computed by `platform module install` and passed
  through the generated Application's `spec.source.helm.values`. See item 3 above.
- ~~The machine-readable "what does this module own" convention `--purge-data` needs (item 4)~~ —
  **resolved for v1, PVC-only**: a `platform.io/module: <id>` label plus an
  `argocd.argoproj.io/sync-options: Delete=false` annotation on every PVC a module's chart creates
  (`src/charts/hello-module/templates/pvc.yaml`). Bucket/schema ownership is a separate, harder
  problem, not a smaller version of this same answer — see "Bucket/schema data ownership" below.
- ~~`module.yaml` validation's exact failure mode (item 2)~~ — **resolved**: rejected at both
  `scaffold`-time (a bad module *name*, not module.yaml content, since scaffold generates the file)
  and `install`-time (a bad module.yaml, including unknown fields — `ModuleManifest`'s
  `extra="forbid"`) — never silently, and never left for Argo CD to discover as a Degraded sync.

Resolved by the platform-module-deps branch (2026-09-03):

- ~~Dependency checking (`requires: [...]`) enforcement (item 6)~~ — **resolved**: see item 6's own
  ✅ marker above.

Still open, unresolved by either branch — item 7's own scope:

- Everything in item 7: gateway's module registry, the Add-ons page, `ui-shell`'s real nav (a static
  placeholder scaffold exists as of feature/ui-shell-scaffold — see above — but nothing behind it
  yet), and the static `modules/*/module.yaml` index the Add-ons page will need to list modules that
  *aren't* installed yet — item 6's live-Argo-CD-query approach deliberately didn't need that index,
  but item 7 still will.

### Bucket/schema data ownership for `--purge-data` — informed deferral (2026-09-04, docs-only pass)

Split out from item 4's bullet above because research this pass (a docs-only branch — no code
changed) found it isn't a smaller version of the same answer: PVC ownership had a real convention
to mirror, this doesn't, and the two halves of "bucket/schema" turned out to be two structurally
different problems, not one.

**What "schema" actually meant here, clarified:** shorthand for "a module's own Postgres
database," not a literal SQL `CREATE SCHEMA` — no per-module, or even per-workspace, real SQL
schema convention exists anywhere in this repo (a repo-wide grep for `CREATE SCHEMA`/`search_path`
returns nothing). ARCHITECTURE.md §4's tenancy table describes "Postgres schema |
`<workspace>.*` tables" as the workspace-isolation mechanism, but the real implementation
(catalog-service) uses one whole database with a `workspace_id` column instead — a separate,
pre-existing gap in the workspace tenancy model, not something this pass resolves either.

**Postgres (module-owned database):** CNPG's `Database` CRD is real, already used by
catalog-service (`src/core/argocd/manifests/catalog-database.yaml` — `name: catalog`, `owner:
catalog`, `cluster: {name: platform-postgres}`, `databaseReclaimPolicy: retain`), namespaced, and
in principle just as label-selectable/deletable as a PVC — `metadata.labels: {platform.io/module:
<id>}` plus `kubectl delete database -n postgres -l platform.io/module=<id>` would mirror the PVC
UX exactly. But two real wrinkles PVC never had: (1) a `Database` CR must live in the *same*
namespace as the CNPG `Cluster` it targets — `postgres`, per `postgres-cluster.yaml` — so a
module's own chart would have to write into a shared namespace it doesn't own, something no module
manifest does today (every module only ever gets `CreateNamespace=true` into its own namespace);
(2) `databaseReclaimPolicy` is CNPG's own field, not an Argo CD sync-option like PVC's
`Delete=false` — `retain` (the only value used anywhere in this repo today) means deleting the CR
does *not* drop the database, so a `--purge-data`-style printed delete command would only actually
work if the policy were `delete`, a distinction PVC's convention doesn't have to make.

**Buckets:** SeaweedFS is a real, deployed core service (`storage-seaweedfs`), but the only thing
that creates a bucket today is `postgres-backup.yaml`'s own Job — a hand-rolled `aws s3 mb` against
SeaweedFS's S3-compatible endpoint (confirmed: this repo's SeaweedFS deployment runs S3-gateway
only, no CRD/operator mode). There is **no Kubernetes object representing a bucket at all** — unlike
a PVC or a CNPG `Database`, nothing exists to attach a `platform.io/module` label to. A module
wanting bucket ownership tracked would need a synthetic marker object invented purely for this
(e.g. a labeled `ConfigMap` recording the bucket name), which is a real design decision with zero
precedent anywhere in this repo — not a mechanical extension of anything that exists.

**Why this stays deferred rather than built now:** neither storage type has a real consumer today
(`hello-module` is stateless nginx with one throwaway PVC; no module needs a database or a bucket
yet), and the two problems don't share a solution the way "extend the PVC label to two more
resource types" implied. A future implementation needs to decide, concretely: whether module
Applications get cross-namespace write permission into `postgres` (and what that does to the
one-namespace-per-module isolation model every other module manifest assumes today); what shape a
bucket-marker convention takes if built, and who's actually responsible for creating/deleting the
bucket itself (still nobody, today — even catalog-service's own bucket is a hand-rolled Job, not a
generic mechanism a module could reuse). Same "properly scope, don't bundle" call this doc has made
twice already (function-promote vs. module-lifecycle; items 6 vs. 7) — the difference here is the
smaller half doesn't get built at all this pass, just written down accurately so whoever picks this
up next isn't re-deriving this research from scratch.
