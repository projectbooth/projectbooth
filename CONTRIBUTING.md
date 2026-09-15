# Contributing

## Branches

Three long-lived branches, in promotion order: `main` ← `test` ← `dev`. Everything else is a
short-lived `feature/<name>` or `fix/<name>` branch cut from `dev`.

1. `git checkout -b feature/<name> dev`
2. Do the work, commit, push.
3. Open a PR into `dev`. CI (`.github/workflows/ci.yml`) has to pass.
4. `dev → test` and `test → main` are PRs too, not direct pushes — every promotion gets a diff and
   a CI run, even solo.

Full rationale: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) §10.

## One-time setup after cloning

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push
```

Installs a local backstop that blocks direct commits/pushes to `main`/`test`/`dev` on this
machine. It's a courtesy net, not the real enforcement — that's GitHub branch protection on the
repo (Settings → Branches), which should also be turned on for all three branches: PR required,
status checks required, no direct or force pushes.

## CI

`.github/workflows/ci.yml` exists today for one path: `src/core/catalog-service/**` — ruff + pytest
(against a real `postgres:18` service container, running the actual Alembic migrations, not
`sqlite`), then, on an actual push to `dev`/`test`/`main` (never on a PR), build and push its image
to `ghcr.io/dougallpercival/catalog-service:<branch>`. **One manual step after that workflow's
first successful push:** GHCR packages default to private — flip it to public once in the
package's Settings (see `docs/known-issues.md`).

The rest of the path-filtered matrix ARCHITECTURE.md §10 describes — markdownlint on docs, yamllint
on YAML, ruff + pytest on `platform-sdk`/`platform-cli` once those exist, shellcheck on
`bootstrap/*.sh`, hadolint on Dockerfiles, `helm lint` on charts — isn't built yet. Same "add it
when there's something of that kind to check" reasoning as everywhere else in this repo; add each
job as its corresponding piece of the repo actually shows up.

## Adding a module

`platform-cli module scaffold <name>` (once `platform-cli` exists — Phase 2) generates
`src/modules/<name>/` from `src/modules/_template/`. Until then, copy the template by hand.
