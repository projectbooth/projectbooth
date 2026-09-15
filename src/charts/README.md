# charts

One Helm chart per module + core service, once those services exist to package (ARCHITECTURE.md
§10). Phase 0's infra pieces don't need charts of their own here — they pull upstream charts
directly via the `Application` manifests in `src/core/argocd/apps/`. `gateway`/`catalog-service`
still deploy via plain manifests in `../core/argocd/manifests/`, not a chart of their own — this
folder is specifically for *modules*, and starts filling in for real with the
platform-module-lifecycle branch (2026-09-03):

- `_template/` — what `platform-cli module scaffold <name>` copies and parameterizes
  (`../platform-cli/README.md`'s "Module lifecycle" section). `templates/_helpers.tpl` carries
  ARCHITECTURE.md §7's chart-wrapper node-placement mechanism (`platform.nodeAffinity`/
  `platform.tolerations`, `include`d from `templates/deployment.yaml`, guarded by an `if` at the
  call site) — every module chart scaffolded from this template gets it for free; a module that
  never sets `placement` in its own `module.yaml` renders with no affinity/tolerations at all.
  `platform.nodeAffinity` is a PREFERRED (soft) nodeAffinity, not a hard `nodeSelector` — see
  ARCHITECTURE.md §12's "Single-node placement fallback" decision (2026-09-09).
- `hello-module/` — the one real chart this branch ships, for `../modules/hello-module/`'s trivial
  test module. Also the reference example for the PVC-ownership convention
  `--purge-data` relies on: `templates/pvc.yaml`'s `platform.io/module` label +
  `argocd.argoproj.io/sync-options: Delete=false` annotation.

`platform module install <name>` points the generated Application's `spec.source.path` straight at
`<name>/` here — nothing else resolves which chart a module uses.
