# Personal Data Platform — Architecture

**Status:** living document, revisit as pieces get built
**Last updated:** 2026-08-30
**Target:** k3s, 2–4 nodes — on a spare box at home, a handful of cloud VMs, or a mix of both
**Tenancy:** single-user default, multi-team ready

A modular data platform — stocks/news/reddit, in-season NFL, and Kaggle March Madness running through the same catalog, compute, and shared code. Runs on whatever k3s cluster you point it at: a machine on your own network today, cloud instances tomorrow, no assumption baked in about which. The tenancy model is load-bearing from day one so handing it to a small team later is an invite, not a rebuild.

---

## 1. Principles

1. **Core stays tiny.** Auth, catalog, gateway, and the UI shell are the only mandatory pieces. Everything else — Spark, Jupyter, Trino, dashboards — is opt-in.
2. **Metadata is the seam.** Modules don't call each other directly. They read and write the catalog. That's what lets you add or remove a piece without rewiring the rest.
3. **Compute is ephemeral.** Spark and Dask clusters spin up for a job and disappear. Nothing idles 24/7 just to exist — that's wasted power on a home rack and a wasted bill on cloud, and ephemeral compute avoids both the same way.
4. **One identity, one door.** Keycloak SSO in front of every module. The shell's nav is the only URL you need to remember.
5. **Every module ships as a chart.** `helm install`, register a manifest, appear in nav. Uninstall it and core doesn't notice.
6. **Tenancy from day one.** Every catalog entry, namespace, and bucket prefix hangs off a workspace — even when there's only one of them.

---

## 2. The stack

Seven layers, foundation at the bottom. The shell sits on top of all of them as the front door, not as a peer layer — it's what makes the rest feel like one platform instead of eight bookmarks.

**Legend:** `core` = install this first, it doesn't come out · `module` = opt in when a use case needs it

### Platform Shell — `core`

Unified nav built from module manifests, catalog browser, pipeline & run status, deep-links into each module's own UI.

| # | Layer | Description | Components |
|---|-------|-------------|------------|
| 7 | Analytical & Serving | Where you actually look at things — notebooks for exploration, small apps for "give me this week's bets," BI for everything else. | JupyterHub `module` · Streamlit `module` · Superset `module` · MLflow `module` |
| 6 | Compute | Spun up per job, torn down after. Spark when a job genuinely needs distributed shuffle; Dask/Ray when it's Python-native parallelism — which covers most of your three use cases. | Spark (Kubernetes Operator) `module` · Dask / Ray `module` |
| 5 | Orchestration | DAGs for ingestion, transforms, scoring. Asset-centric so a "pipeline" and a "catalog entry" are the same concept. | Dagster `module` · Argo Workflows (k8s-native fallback) `module` |
| 4 | Query & Virtualization | Your Denodo analog — one SQL surface federated across Postgres, SeaweedFS/Iceberg, and anything else with a connector, without copying data around. | Trino `module` · dbt `module` |
| 3 | Catalog & Shared Code | The registry that makes datasets, functions, pipelines, and models discoverable — this is your Unity Catalog. Every entry carries a workspace + visibility flag. The shared-code repo stops you rewriting the same Reddit scraper three times. | catalog-lite service `core` · platform-sdk `core` · platform-cli `core` · Gitea/GitHub `module` |
| 2 | Storage | One object store as the lake, one relational store for anything transactional or small. Table format is optional until you need time travel or schema evolution. | SeaweedFS `core` · Postgres (CloudNativePG) `core` · Iceberg `module` · Redis `module` |
| 1 | Infrastructure | The boring bedrock everything else assumes exists, including the workspace/group model in Keycloak and the GitOps engine that installs everything above this layer. Get this reliable before anything else. | k3s `core` · MetalLB `core` · ingress-nginx `core` · cert-manager `core` · Longhorn/NFS `core` · Keycloak `core` · Argo CD `core` · sealed-secrets/Vault `core` · Prometheus+Grafana+Loki `module` |

> **SeaweedFS, not MinIO (updated 2026-08-31).** This doc originally specified MinIO for layer 2 —
> swapped for SeaweedFS before it was ever built, because MinIO's Community Edition was archived
> upstream Feb 13, 2026 (paid features carved out of the console mid-2025, maintenance mode Dec
> 2025, `minio/minio` archived Feb 2026), with the company steering everyone toward its commercial
> "AIStor" product instead. SeaweedFS was picked over the other self-hosted options considered
> (Garage; pinning an unmaintained MinIO image) for having an official Helm chart maintained by the
> project itself and being light enough for a single homelab node. Same S3 API surface either way —
> nothing above this layer (§4's Trino federation, §9's `s3://bronze/...` landing paths) had to
> change, and `endpointURL`-style config means a real cloud S3/R2/B2/etc. bucket is a straight swap
> too, not a rewrite. See `src/core/argocd/apps/optional/storage-seaweedfs/seaweedfs.yaml` in the
> repo for the actual deployment and `docs/known-issues.md` for the operational fallout.

---

## 3. How modules plug in

### From clone to running platform

Two steps are unavoidably scripted — nothing exists yet to click on:

1. `./bootstrap/install.sh` provisions k3s (or verifies an existing cluster) and applies `core/` as an Argo CD "app of apps": MetalLB, ingress-nginx, cert-manager, storage, Keycloak (seeding the `personal` workspace), secrets, and the platform services themselves (gateway, catalog-lite, ui-shell). It prints the shell URL when it's done.
2. You log in to the shell. Everything from here is a choice, not a script.

From that point, every module — JupyterHub, Trino, Dagster, Spark, whatever — can be turned on through **any of three doors, all converging on the same mechanism**, which is the direct answer to "does someone pick pieces from a config UI, or implement each by hand": both, and neither is the "real" way — they're front ends on the same install path.

| Door | How | Best for |
|---|---|---|
| Add-ons page, in the shell | Click "Install" on a module card | Point-and-click, no terminal needed |
| `platform-cli` | `platform module install jupyterhub` | Scripting, remote/headless boxes |
| Git, directly | Commit a file into `modules-enabled/` | GitOps purists, code review on infra changes |

All three do the same thing underneath: write (or commit) the module's manifest into `modules-enabled/` — named the way nginx names `sites-enabled/`, deliberately, because the idea is identical. `modules/` holds every module the repo knows how to install; `modules-enabled/` holds only the ones *this* deployment actually turned on. Argo CD watches `modules-enabled/` and reconciles the cluster to match it, whether that file arrived by hand, by CLI, or by a UI click — so there's exactly one source of truth (git), and no "the UI thinks it's installed but the cluster disagrees" drift.

The Add-ons page is thin by design: `platform-gateway` reads a static module index built from every `modules/*/module.yaml` at release time (so it can list modules that aren't installed yet, not just ones already running), overlays it with the live `PlatformModule` registrations it already watches for nav, and shows each module's state — Not installed / Installing / Healthy / Failed — by reading Argo CD's `Application` status. Clicking "Install" doesn't call Helm directly; it commits the manifest and lets Argo CD reconcile it, which is exactly what keeps the UI and the CLI from becoming two code paths that quietly drift apart. A module that declares `requires: [trino]` and isn't satisfied shows a disabled button with why, in the UI, and a clear error from the CLI — the dependency check lives once, at the API layer both doors call through.

`modules/notebook-jupyterhub/module.yaml`

```yaml
id: notebook-jupyterhub
displayName: Notebooks
icon: notebook
navPath: /notebooks
proxyTo: http://jupyterhub.jupyterhub.svc:80
healthCheck: /hub/health
requires:
  - auth        # registers a Keycloak OIDC client on install
  - catalog     # notebooks can pull dataset refs via platform-sdk
optional: true
```

Decorators in `platform-sdk` (`@platform.dataset`, `@platform.pipeline`, `@platform.function`) are the equivalent self-registration mechanism for *code* rather than infrastructure — how a function or a table gets into the catalog without a separate manual registration step, once its module is running.

### Building a new module

This is the one part that's still genuinely hands-on, and should be: `platform-cli module scaffold <name>` generates the `modules/<name>/` skeleton (chart + `module.yaml`) from the Phase 7 template, you write the actual service, and once it's committed to `modules/`, it shows up on the Add-ons page like any built-in module. Installing it stops being a coding problem and goes back to being a one-click problem — building it is the only step nobody can automate for you.

### Tearing it all down

Testing this on a cluster you intend to destroy afterward — a cloud VM set spun up just to validate a release, or a box you want back to bare metal — needs the exact inverse of every install path, not just "delete the manifests and hope":

- `platform module install <name>` / `platform module uninstall <name>` are real as of the
  `platform-module-lifecycle` branch (2026-09-03): `install` writes a generated Argo CD
  `Application` manifest into `modules-enabled/`, commits, and pushes; `uninstall` removes it the
  same way, and Argo CD (via `modules-root`, `src/core/argocd/apps/core/modules-root.yaml`) prunes
  the release. By default this leaves the module's PersistentVolumeClaims in place (an
  `argocd.argoproj.io/sync-options: Delete=false` annotation each module's chart sets, not special
  CLI logic), so reinstalling the same module gets its data back — pass `--purge-data` and the CLI
  prints the `kubectl delete pvc` command to also drop its PVCs yourself (it doesn't run this for
  you — no cluster credentials, no undo). Bucket/schema ownership for `--purge-data` is **still
  not built** — PVCs only this pass; see `docs/architecture/module-lifecycle-plan.md`.
  Dependency-checking on `requires: [...]` **is now built** (`platform-module-deps` branch,
  2026-09-03, `module-lifecycle-plan.md` item 6): gateway's `GET /modules/check-requirements`
  checks live Argo CD `Application` health, and `platform module install` blocks on it before
  writing anything (`--skip-requires-check` bypasses it). The Add-ons page's own "Remove & delete
  data" button, and the rest of gateway's module registry/nav aggregation, are still **not built
  yet** (`module-lifecycle-plan.md` item 7) — those need `ui-shell` to exist at all, which it
  doesn't yet, a separate and later piece of work than either of the two branches above.
- `bootstrap/teardown.sh` is the mirror of `install.sh`: uninstalls every module in `modules-enabled/` (with data purged, since the goal is a clean slate), removes core, removes Argo CD, then runs k3s's own `k3s-uninstall.sh` — so the machine ends up exactly where it was before `install.sh` ever ran, whether that machine is a spare box at home or a cloud VM. What it deliberately doesn't touch is the compute itself: on hardware you own, that's a machine you still have; on a cloud VM, actually reclaiming cost means terminating the instance, which is one layer above this script.
- For cloud test clusters specifically, it's worth provisioning them through a minimal Terraform (or equivalent) module that spins the VMs up *and* tears them down — pairing `terraform apply` with `bootstrap/install.sh`, and `terraform destroy` as the last step regardless of whether `teardown.sh` ran first. A full test cycle then costs exactly as long as the test took, and nothing lingers to show up on a bill.

---

## 4. Tenancy & isolation

The unit of ownership everywhere in this platform is the **workspace**, not the user. A single-user install provisions exactly one — `personal` — and you never see the concept again. A team install provisions one per project or team, and the same primitives that kept your NFL data separate from your March Madness scratch space now keep two people's data separate from each other.

A workspace is a label that four other systems key off of, so isolation is enforced by infrastructure that already isolates things, not by application code bolted on after the fact:

| Workspace maps to | Gives you |
|---|---|
| Keycloak group | Membership + role (owner / editor / viewer), enforced by SSO everywhere, not per-app |
| k8s namespace | Resource quotas, network policy, RBAC — a runaway Spark job in one workspace can't starve another |
| SeaweedFS bucket prefix | `s3://lake/<workspace>/...` with bucket policy scoped per prefix |
| Postgres schema | `<workspace>.*` tables, one database role per workspace |

The catalog carries a `workspace_id` and a visibility flag (`private` / `workspace` / `public`) on every dataset, function, pipeline, and model — that's the whole mechanism for "my Reddit sentiment tweak is mine, but the platform-sdk connector everyone uses is public." JupyterHub already spawns per-user pods natively; the addition is a KubeSpawner profile list keyed to workspace membership, so a notebook launches with the right storage mounts and quota without anyone picking anything by hand. Dagster's code locations get the same treatment — a workspace's pipelines are their own code location, visible to its members, not one shared blob of DAGs.

What this buys a solo user: nothing, on purpose — one workspace, one owner, no extra screen to click through. What it buys later: inviting someone is `platform workspace invite alice --role editor`, not a schema migration.

**Publishing code to the catalog.** A function decorated with `@platform.function(name=..., visibility=...)` registers into catalog-lite with an owner workspace, a version (bumped on each `platform-cli publish`), an extracted signature/docstring, and lineage — which pipelines call it, which datasets it reads or writes. The catalog stores that metadata, not the code itself: the code stays in `platform-sdk`/git, so the catalog is a discovery and governance layer on top of it, not a package host. `visibility: public` makes a function callable and browsable from every workspace on your deployment without membership in the workspace that owns it — `platform-cli function promote <name> --public` is the one-line way to do that. Editing rights stay with the owning workspace regardless of visibility: "public" means readable by everyone else, not writable.

That's public *within your instance*. A different question — publishing a function so someone else's separate installation of this platform could pull it in, the way `pip install` reaches PyPI — isn't in scope for the MVP, but it's worth designing for now rather than retrofitting later. See §12 for that as an explicit deferred decision.

---

## 5. Scheduling & triggers

Every one of your three use cases has a different rhythm — stocks want to run continuously, NFL wants a weekly cadence, March Madness wants to run once, fast, on demand. Dagster (§2, layer 5) covers all three with its built-in primitives, so scheduling isn't a separate system bolted on top of orchestration — it's a property of how a pipeline is defined.

- **Cron schedules** — a `ScheduleDefinition` on a job: the stocks ingestion pipeline runs nightly after market close; the NFL pull runs every Tuesday morning once the prior week's games are final.
- **Sensors** — event-driven, not time-driven: an asset sensor fires the stocks transform the moment a new file lands in `s3://bronze/stocks/`, instead of waiting for a fixed clock time. A freshness policy can flag a dataset that hasn't been updated when it should have been — useful for catching a silently-broken API key before the dashboard just looks stale.
- **Manual & backfill runs** — March Madness is triggered by hand when the Kaggle dataset drops, not on a schedule. Partitioned assets (NFL data by week, or historical seasons) can be backfilled on demand if you need to reprocess.

**One asterisk on Principle 3 ("compute is ephemeral"):** the Dagster daemon itself has to run continuously to evaluate schedules and sensors — it's a small, cheap, always-on control-plane process, not compute. It wakes up ephemeral Spark/Dask/Python jobs; it doesn't do the work itself.

**Open gap, worth flagging:** nothing in the current design alerts you when a scheduled run fails. A silently-broken Tuesday NFL pull is exactly the kind of thing you won't notice until Sunday. Worth adding a lightweight optional module — a Dagster failure hook wired to a webhook or push notification — before Phase 4 goes live with a real schedule.

---

## 6. Cluster topology

A reference layout for three nodes — the sweet spot for "small cluster." With two, fold storage onto the control node; with four, split compute across two workers and let the scheduler balance Spark/Dask pods between them. Node roles here are physical and don't change with tenant count — workspaces are a logical slice on top, each with a `ResourceQuota` scheduled onto the compute node(s), not a node of its own. That's what keeps three workspaces from needing three clusters.

| Node | Role | Runs |
|---|---|---|
| `node-a` | Control | k3s server, platform-gateway, catalog-lite + Postgres (metadata), Keycloak, UI shell |
| `node-b` | Storage | SeaweedFS, Longhorn/NFS volumes, Postgres (warehouse, if split from node-a), Trino coordinator |
| `node-c` (+ `node-d`) | Compute — tainted | JupyterHub kernels, ephemeral Spark/Dask workers, Dagster job runners, Streamlit/Superset pods |

Label and taint the compute node(s) so a Spark job can't starve the shell or the catalog of CPU during a Sunday-afternoon NFL scoring run.

---

## 7. Node placement & scale

Every physical node gets one label — `platform.io/role: control|storage|compute` — applied once, at join time, by the same script that adds it to the cluster. Module charts don't hardcode a machine name; they request a role, so a module's `module.yaml` carries an optional placement hint that the chart wrapper turns into a *preferred* node affinity plus a `tolerations` block — a soft ask, not a hard `nodeSelector` (see §12's "Single-node placement fallback" decision for why):

```yaml
placement:
  role: compute
  tolerations:
    - key: platform.io/role
      operator: Equal
      value: compute
      effect: NoSchedule
```

That's what makes the taint in §6 real: `kubectl taint nodes node-c platform.io/role=compute:NoSchedule` means nothing except compute-labeled workloads lands there — the shell, gateway, and catalog stay off it entirely, even under load.

On a cluster with only one node — nothing in this repo assumes at least two, but §6's reference topology does — that node can only ever carry one `platform.io/role` value, so it will never exactly match every module's requested role. A preferred (not hard) affinity is what keeps that from being a hard failure: the module still schedules on the only node available, it just doesn't get the placement preference honored until a real role-labeled node exists.

**Adding a node.** On a machine you own, growing the cluster is a physical act: plug it in, then `bootstrap/join-node.sh --role compute --token <k3s-token>` installs the k3s agent, applies the role label and taint, and the scheduler picks it up immediately. No chart changes anywhere — existing compute-role workloads simply get more room, and the next ephemeral Spark/Dask job can land there. Removing a node is the same in reverse: cordon, drain, then physically pull it (or terminate it, on cloud).

On cloud compute, `join-node.sh` still works the same way against a freshly provisioned instance, but there's a second option bare metal doesn't have: point a cluster-autoscaler (or your cloud's managed node group) at the `compute`-role node pool and let it add and remove instances on its own under load — the ephemeral-compute principle already assumes workloads come and go, so it composes cleanly with infrastructure that comes and goes too. Worth adding once compute cost is metered; not worth the complexity before then.

---

## 8. Backup & disaster recovery

Most of this platform doesn't need backing up — it needs to be *reconstructible*. Every core service and every module is declared in git (`core/`, `modules/`, `modules-enabled/`); Argo CD can rebuild the entire cluster's configuration from a fresh checkout of that repo. What can't be regenerated is data and secrets, so that's where backup effort actually concentrates:

| What | Where it lives | How it's backed up |
|---|---|---|
| Catalog + workspace metadata | Postgres (catalog-lite schema) | CloudNativePG's built-in WAL archiving + base backups, continuously, to a dedicated SeaweedFS bucket — point-in-time recovery, not just nightly snapshots |
| Keycloak realm (users, groups, workspaces) | Postgres (Keycloak schema) | Same CloudNativePG mechanism, same bucket |
| The actual data lake | SeaweedFS | On-cluster: Longhorn/NFS replication protects against one disk or one node dying. Off-cluster: a scheduled `rclone sync` of critical buckets to an external drive or offsite location — on-cluster redundancy alone doesn't survive a fire, theft, or a bad `rm` |
| Secrets encryption key (sealed-secrets private key, or Vault unseal keys) | Nowhere reproducible | `bootstrap/export-sealed-secrets-key.sh` (2026-09-11, timer's automated firings confirmed live 2026-09-14 — see the script's own systemd unit and `sealed-secrets.yaml`'s comment) — a systemd timer exporting every sealed-secrets key Secret (the controller rotates its own keypair every 30 days by default; old keys are kept, never deleted, so the export has to cover all of them, not just the active one), with optional `rclone` push to any cloud remote (not yet configured on homelab-dev — exports are local-only for now). The one thing that must be exported and stored *outside* the cluster the moment it's generated — lose this and every other backup is unreadable ciphertext |
| k3s cluster datastore (SQLite by default; etcd if `--cluster-init`) | `node-a` | `bootstrap/snapshot-setup.sh` — a systemd timer running a SQLite online `.backup` (not k3s's built-in `etcd-snapshot`, which is etcd-only and doesn't apply to this repo's single-server SQLite datastore), with optional `rclone` push to any cloud remote |
| Argo CD's own state | Git + a few in-cluster repo-credential secrets | Effectively already backed up — it's git. Only the repo credentials need to ride along with the secrets-key backup above |

**What recovery actually looks like** if `node-a` dies outright: provision a replacement, restore the k3s datastore snapshot and the secrets encryption key, point it at the git repo, and Argo CD reconciles `core/` and `modules-enabled/` back into existence on its own — that's the payoff of everything being declarative. Then restore the two Postgres databases from their CloudNativePG backups. SeaweedFS's data was never on `node-a` to begin with (it lives on `node-b`, per §6), so it's untouched throughout. Total recovery time is bounded by how fast two databases restore and one script re-runs, not by how much configuration you remember by hand.

**What this design doesn't give you: zero-downtime failover.** A single control node means a control-node failure is an outage, not a seamless handoff — acceptable for a single low-stakes deployment, less so for a team depending on this daily. See §12 for the upgrade path.

---

## 9. Your three use cases, through the same pipe

Same five-step shape every time — ingest, land, transform, catalog, serve. What changes is the content, not the plumbing, which is the whole point of building this once. Written here as one person's workspace; in a team install each of these could just as easily be its own workspace with its own members.

### Stocks, news & Reddit mentions

*Continuous ingestion, ad hoc exploration, a standing dashboard.*

| Ingest | Land | Transform | Catalog | Serve |
|---|---|---|---|---|
| Dagster job hits price API, news API, Reddit via PRAW on a schedule | Raw JSON/parquet → `s3://bronze/stocks/` | Polars job joins price + mentions, scores sentiment → Postgres/Iceberg | Tables + the sentiment-scorer function registered | Streamlit ticker dashboard, ad hoc SQL in Jupyter via Trino |

### In-season NFL betting ideas

*Weekly cadence, a trained model, a "what should I look at" UI.*

| Ingest | Land | Transform | Catalog | Serve |
|---|---|---|---|---|
| Weekly job pulls odds + team/player stats APIs | `s3://bronze/nfl/` | Feature pipeline scores edges; model version pulled from MLflow | Dataset + model version registered | Streamlit "this week's value bets" app, reads Postgres directly |

> **Reuse ready:** feature/elo functions live in `platform-sdk`, not in this pipeline — so March Madness below doesn't reinvent them.

### March Madness Kaggle contest

*Bursty, deadline-driven, needs a fast turnaround from dataset to submission.*

| Ingest | Land | Transform | Catalog | Serve |
|---|---|---|---|---|
| Manual trigger pulls the Kaggle dataset | `s3://bronze/march-madness/` | Reuses NFL's feature functions + existing model from the registry | Run + submission logged, so you can compare seasons later | `submission.csv` in the UI, results browsable in Jupyter |

---

## 10. Repo layout

Shaped so someone cloning this later can install `core/` and stop, or keep going through `modules/` one at a time.

```text
platform/
  core/
    gateway/              # module registry, nav aggregation, Add-ons page API
    catalog-service/       # datasets, functions, pipelines, models, workspaces
    ui-shell/               # the one front door + workspace switcher
    auth/                    # Keycloak realm + client + workspace-group config
  modules/                    # every module the repo KNOWS how to install (the catalog)
    storage-seaweedfs/
    storage-postgres/
    query-trino/
    orchestration-dagster/
    compute-spark-operator/
    compute-dask/
    notebook-jupyterhub/
    serving-streamlit-template/
    serving-superset/
    ml-mlflow/
  modules-enabled/            # which of the above THIS deployment turned on — Argo CD watches this
  platform-sdk/             # python: connectors, @platform decorators, catalog client
  platform-cli/              # scaffold / install / register
  charts/                     # one helm chart per module + core
  bootstrap/
    install.sh                 # the one unavoidable script: k3s + core as an Argo CD app-of-apps
  examples/
    stocks-pipeline/
    nfl-betting/
    march-madness/
  docs/
```

### Branching & CI

Three long-lived branches, each a step of promotion: `main` (stable, what you'd stand a deployment up from) ← `test` (integration-validated, candidate for `main`) ← `dev` (where feature branches land first). Everything else is a short-lived `feature/<name>` (or `fix/<name>`) branch cut from `dev`, merged back into `dev` via PR, then never touched again.

Promotion between the three long-lived branches is also a PR, never a direct push — `dev → test` and `test → main` — even solo, so every promotion gets a diff and a CI run instead of trusting memory. `main`, `test`, and `dev` are all protected: no direct pushes, PR required, CI must pass before merge. This also happens to line up with how the platform itself is deployed (§3, §6) — if you ever run separate dev/test/prod clusters, Argo CD can point each one at the matching branch, so branch promotion and environment promotion become the same motion instead of two.

CI (GitHub Actions, `.github/workflows/ci.yml`) runs on every PR into `dev`/`test`/`main`, path-filtered so it only lints what actually changed and stays green while most of the repo is still empty:

| Changed paths | Check |
|---|---|
| `**/*.md` | markdownlint |
| `**/*.yml`, `**/*.yaml` | yamllint |
| `src/**/*.py`, `platform-sdk/**`, `platform-cli/**` | ruff + pytest |
| `**/*.sh` | shellcheck |
| `**/Dockerfile*` | hadolint |
| `charts/**`, `**/Chart.yaml` | `helm lint` |

Each check is a no-op until there's something of that kind to lint, so the pipeline doesn't start red on a docs-only repo — it grows teeth as `src/`, `charts/`, and `bootstrap/*.sh` fill in.

---

## 11. Build order

Each phase is usable on its own — you're not blocked on finishing the whole thing before any use case works. Tenancy rides along in phases 0 and 2 rather than getting its own phase, because retrofitting it later is the expensive path. Node placement and datastore backups ride along in Phase 0 for the same reason; off-cluster data backup rides along in Phase 1, right after there's data worth losing.

| Phase | Focus | You get | Adds |
|---|---|---|---|
| 0 | Infrastructure bedrock | A cluster that stays up | k3s, MetalLB, ingress, cert-manager, Longhorn/NFS, Keycloak realm + workspace-group model (seeds one `personal` workspace), Argo CD, secrets, node role labels/taints + `join-node.sh`, `bootstrap/teardown.sh`, k3s datastore snapshot schedule, Prometheus/Grafana/Loki |
| 1 | Storage | Somewhere to land data | SeaweedFS, Postgres (CloudNativePG) with WAL-archive backups to SeaweedFS, offsite `rclone sync` for critical buckets |
| 2 | Catalog & shared code | Things become findable and reusable | catalog-lite (with `workspace_id` + visibility on every table), function publish/versioning (`platform-cli function promote`), `platform module uninstall --purge-data`, platform-sdk, platform-cli, `platform workspace` commands, git remote |
| 3 | Query + exploration | You can actually look at your data | Trino, JupyterHub — bundled together since one needs the other |
| 4 | Orchestration | Ingestion stops being a cron job on your laptop | Dagster + daemon, cron schedule + asset sensor for the stocks pipeline, failure-hook alerting |
| 5 | Compute scale-out | Room to grow when data volume asks for it | Spark Kubernetes Operator, Dask |
| 6 | Serving | The NFL/March Madness payoff — dashboards and model tracking | Streamlit, Superset, MLflow, shell polish |
| 7 | Open-source prep | Someone else can stand this up | Docs, module template, one-command bootstrap, seeded demo dataset, `install.sh`/`teardown.sh` validated against a clean cloud environment (not just home hardware) |

**Do this on day one, not later:** export the secrets-encryption key to offsite storage the moment it's generated in Phase 0. Every other piece of state in §8 can be restored from backups; that key can't be regenerated, and losing it makes every other backup unreadable ciphertext. `bootstrap/export-sealed-secrets-key.sh` (§8's table, above) automates this, including catching future key rotations — still worth running it once by hand right after first install rather than waiting for its own timer's first scheduled fire, per its own name: day one, not later. Confirmed live 2026-09-14: the timer itself (not just that first manual run) has now fired automatically three days running, each producing a fresh, correctly-permissioned export — the mechanism is proven, not just installed.

**Phase 3 kicked off, 2026-09-14** (feature/module-external-chart): Trino's half is built — `src/modules/trino/module.yaml`, wired to a real Iceberg catalog (Postgres JDBC + SeaweedFS S3), plus the general external-chart module mechanism it's the first real consumer of (`docs/architecture/module-lifecycle-plan.md` items 8-9 have the full writeup). Not yet live-verified against a real cluster — see that doc's own note on what was researched vs. hands-on-confirmed. JupyterHub, the other half of this phase, is deliberately not started yet: it needs its own Keycloak OAuthenticator SSO work.

---

## 12. Open calls

Defaults picked to keep this moving — worth revisiting once real usage tells you otherwise.

**Tenancy: model it now vs bolt it on later.**
*Picked:* workspace as a first-class concept from Phase 0, even for a single user.
The cost today is one foreign key and a seeded default row. The alternative — retrofitting multi-tenancy into a catalog schema, a bucket layout, and a Postgres role structure that were designed for one user — is a rewrite, not a migration.

**Catalog: build vs adopt.**
*Picked:* a lightweight custom service (Postgres + FastAPI) over OpenMetadata or DataHub.
Those tools are excellent but heavy for three personal projects. Switch once you actually want lineage graphs or multi-user governance, not before.

**Orchestrator: Dagster vs Argo Workflows.**
*Picked:* Dagster.
Its asset model matches the catalog-first thinking here, and the local dev loop (no cluster needed to iterate on a pipeline) matters when you're the only user. Argo is the fallback if you'd rather stay purely k8s-native with one fewer moving process.

**Module lifecycle: GitOps.**
*Picked:* Argo CD (or Flux) watching a `modules-enabled/` directory.
Makes "decide what pieces you want" a literal file operation instead of a runbook, and gives you a rollback path when a module misbehaves.

**Node roles.**
*Picked:* the 3-node reference in §6, as a starting assumption.
Actual specs will move things around — swap in your real node count and RAM/CPU and the control/storage/compute split should adjust accordingly.

**Single-node placement fallback.**
*Picked:* `platform.nodeAffinity` renders a preferred (soft) `nodeAffinity`, not a hard `nodeSelector` (2026-09-09).
Hit for real installing `hello-module` on a single-node homelab cluster: its one node is labeled `control` (§7's join-time label can only ever hold one value), so a hard `nodeSelector: platform.io/role: compute` left the pod `Pending` forever — no node could ever match. §6's node-role model is written around at least two physical nodes and never actually says what one node should do. Rather than requiring an explicit `singleNode` flag or auto-detecting node count from inside a Helm template (fragile under Argo CD's rendering, and one more thing to keep in sync), the chart wrapper just asks for a role-labeled node without demanding one: `preferredDuringSchedulingIgnoredDuringExecution` schedules onto a matching node when one exists — the real placement behavior a multi-node cluster still gets, since tolerations are unchanged and still gate landing on a tainted compute node — and falls back to whatever's available when nothing matches. Trades a small amount of placement precision on a real multi-node cluster (a `Pending`/unscheduled pod would have been a loud, obvious signal that placement is misconfigured; a silently-ignored preference is quieter) for not hard-failing the common single-box case this repo is actually developed and tested against day to day. Worth revisiting if a real multi-node cluster ever wants that hard-failure signal back — e.g. a lint/CI check that flags a `role` with no matching node label, rather than baking the strictness into the scheduler decision itself.

**Function sharing: local-only vs a cross-instance hub.**
*Picked:* local-only for now — visibility scoping (private/workspace/public) inside your own catalog, nothing shared outside it.
A community registry (`platform function install community/<name>`, pulling in someone else's published connector or model the way `pip install` reaches PyPI) is a genuinely good idea once this is open-sourced — but it's a different system, a package index rather than a metadata catalog, and building it before anyone else is running this platform solves a problem that doesn't exist yet. Keep the function-metadata schema (id, version, signature) stable so it's addable later without a rewrite.

**Pipeline failure alerting.**
*Picked:* a lightweight optional module (Dagster failure hook → webhook/push notification), not built into core.
A single-user deployment doesn't need a heavy notification system, and it shouldn't be mandatory for someone who doesn't want it — but it's cheap enough that it belongs in Phase 4, not "someday."

**DR strategy: backup-and-restore vs a true HA control plane.**
*Picked:* backup-and-restore (§8), with a single control-plane node, as the default.
A genuinely HA control plane means running k3s server on 3 nodes (an odd number, for etcd quorum) plus HA'ing Postgres and Keycloak themselves — real operational weight for a small deployment, buying you minutes of avoided downtime a few times a year at best. Worth revisiting once this is actually serving a team that notices an outage; the git-native design doesn't block that upgrade later, it just isn't the default.

---

*This is meant to be argued with as you build — not a spec to implement literally end to end. A reasonable next step once Phase 0–2 are running: pick whichever of the three use cases has the most annoying manual step today and let it be the first thing that moves onto the platform.*
