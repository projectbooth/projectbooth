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
