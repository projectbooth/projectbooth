# Personal Data Platform

A modular, GitOps-managed data platform — catalog, shared code, pipeline orchestration, on-demand
compute, and an analytical/serving layer, installable core-first and grown one module at a time.
Runs on any k3s cluster you point it at, home or cloud.

**Start here:** [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) — the
living design doc. [`docs/architecture/capability-map.html`](docs/architecture/capability-map.html)
is a visual overview of core vs. optional pieces.

## Repo layout

| Path | What's in it |
|---|---|
| `bootstrap/` | The scripts that bring a cluster up (`install.sh`), tear it back down (`teardown.sh`), and add a node (`join-node.sh`) |
| `src/core/` | The mandatory pieces — infrastructure bedrock (as Argo CD `Application` manifests under `core/argocd/`) plus the platform services (`gateway/`, `catalog-service/`, `ui-shell/`, `auth/`) |
| `src/modules/` | Every module the repo knows how to install — the catalog, `sites-available`-style |
| `src/modules-enabled/` | Which modules *this* deployment turned on — Argo CD watches this |
| `src/platform-sdk/`, `src/platform-cli/` | Python: `@platform.*` decorators, the `platform` CLI |
| `src/charts/` | One Helm chart per module + core service |
| `src/examples/` | The three reference use cases, wired through the same pipe |
| `docs/architecture/` | `ARCHITECTURE.md`, the styled HTML mirror, the capability-map diagram |

## Getting a cluster up

```bash
./bootstrap/install.sh
```

Provisions k3s if one isn't already running, installs Argo CD, and hands it the app-of-apps in
`src/core/argocd/` — everything from there on is Argo CD reconciling git, not a script doing work.
See `bootstrap/install.sh --help` for flags (role, branch/revision to track, etc).

To tear a test deployment back down to nothing: `./bootstrap/teardown.sh`.

## Backing up the cluster's own datastore

`sudo ./bootstrap/snapshot-setup.sh` sets up scheduled snapshots of k3s's own SQLite datastore
(separate from Postgres/CNPG backups, which are an in-cluster concern) — local by default, with
optional `rclone` push to any cloud remote. Independent of `install.sh`: run it before, right
after, or months later; re-running it or hand-editing `/etc/opendataplatform/k3s-snapshot.conf`
just updates the schedule/destination in place. See `bootstrap/snapshot-setup.sh --help` and the
script's own header comments for the full design (why k3s's built-in `etcd-snapshot` feature
doesn't apply here). Not applicable on managed k8s (EKS/GKE/AKS) — there's no host-level datastore
to back up.

## Contributing

Branch workflow, local hooks, and CI expectations are in [`CONTRIBUTING.md`](CONTRIBUTING.md).
