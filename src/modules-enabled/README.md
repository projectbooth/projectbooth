# modules-enabled

Which modules *this* deployment turned on — Argo CD watches this directory, for real, as of the
platform-module-lifecycle branch (2026-09-03): `../core/argocd/apps/core/modules-root.yaml` is a
second-level app-of-apps that turns every file in here directly into a running module. Each file
IS a complete Argo CD `Application` manifest, not a lightweight pointer — see `modules-root.yaml`'s
own header comment for the three-level app-of-apps structure this relies on, and
`../platform-cli/platform_cli/manifest.py`'s `render_application_manifest` for what actually
generates these files.

Populated by whichever of ARCHITECTURE.md §3's three doors you use: `platform module install
<name>` (commits + pushes the generated file automatically — see `../platform-cli/README.md`),
committing one by hand, or (once built — see `docs/architecture/module-lifecycle-plan.md`'s item 7)
the Add-ons page. `platform module uninstall <name>` is the mirror — removes the file, Argo CD
prunes everything the Application owned except any PersistentVolumeClaim marked
`argocd.argoproj.io/sync-options: Delete=false`.

Currently empty on a fresh clone — `src/modules/hello-module/` is the one real module this repo
ships a descriptor for, but installing it is still an operator action
(`platform module install hello-module`), not something that happens automatically on checkout.
