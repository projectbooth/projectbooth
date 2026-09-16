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

A module can also ship a one-time, idempotent setup step that needs to run INSIDE the cluster once
it's installed — something `platform module install` itself can't do, since platform-cli never talks
to the live cluster directly (no network path from wherever it's invoked, and no cluster
credentials; see `module.py`'s own `_print_purge_command` for that same boundary). `<id>/setup/`, if
it exists, is picked up by convention (no `module.yaml` field, same existence-check `../charts/<id>/`
itself already gets) and wired into the generated Application as a SECOND Argo CD source
(`spec.sources:`, multi-source Applications) alongside the chart — a plain directory of raw
manifests, not another chart, expected to contain one Job annotated as an Argo `PostSync` hook.
`trino/setup/job.yaml` is the first real one: it creates Iceberg's own JDBC-catalog bookkeeping
tables in Postgres before Trino ever tries to use them, since Trino deliberately never creates them
itself (trinodb/trino#20419). See that file's own header comment, and
`../platform-cli/platform_cli/manifest.py`'s module docstring (2026-09-16 entry) for the full design.
