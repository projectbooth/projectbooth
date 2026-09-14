# modules

Every module the repo knows how to install — the full catalog, `sites-available`-style
(ARCHITECTURE.md §3). Turning one on means putting its manifest in `../modules-enabled/`; nothing
in this folder is running just because it's here.

`_template/module.yaml` is what `platform-cli module scaffold <name>` generates from (real as of
the platform-module-lifecycle branch, 2026-09-03 — see `../platform-cli/README.md`'s "Module
lifecycle" section), alongside `../charts/_template/` for the chart half. Copying it by hand still
works too, same file either way — `scaffold` is a convenience, not the only path.

`hello-module/module.yaml` is the one real module this branch ships — deliberately trivial (stock
nginx, one throwaway PVC), built to prove the whole install → Argo CD reconcile → node-placement →
`--purge-data` chain actually works end to end, not to serve anything anyone would use. See its own
header comment, and `../charts/hello-module/` for the chart it points at.

Schema/validation: `../platform-cli/platform_cli/manifest.py`'s `ModuleManifest` — an unknown field
in a `module.yaml` here fails `platform module install`/`scaffold` with a specific message, not
silently.

## Wrapping a third-party Helm chart (`externalChart`)

Added feature/module-external-chart, 2026-09-14 (Phase 3 kickoff — `trino/module.yaml` is the
first real example). Some modules wrap a chart maintained upstream (Trino, and later Spark/Dask/
Superset/MLflow) rather than one hand-authored in `../charts/`. For those, skip `../charts/<id>/`
entirely and add an `externalChart:` block instead:

```yaml
externalChart:
  repoURL: https://example.invalid/charts   # the chart's own Helm repo, not this repo
  chart: some-chart
  version: "1.2.3"                          # pin a real version; check for newer before installing
values:
  # Free-form Helm values for that chart — an external-chart module has no values.yaml of its own,
  # so whatever the chart needs (beyond `platform module install`'s own `placement:` block, which
  # is merged in automatically) goes here.
  someKey: someValue
```

`scaffold` does NOT generate this shape — it's still local-chart-only by design (its whole framing
is "here's a skeleton, go write the service"; wrapping someone else's chart is a different task).
Hand-write `module.yaml` the way `trino/module.yaml` does, using it as a second worked example
alongside `_template/module.yaml`.

A module can't set `externalChart` AND have a `../charts/<id>/` directory — `platform module
install` refuses that combination outright (ambiguous which chart Argo CD would actually use).

`platform module install` still runs a `helm template` safety check before writing the Application
manifest either way — for an `externalChart` module this pulls straight from `externalChart.repoURL`
(no local chart directory involved), so `helm` still needs to be on `PATH` for the check to run (it
warns and skips, rather than blocking, when it isn't).
