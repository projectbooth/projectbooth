# platform-cli

The `platform` command — ARCHITECTURE.md §4/§10. A thin wrapper over `platform-sdk`: every command
here does what `PlatformClient`'s matching method does, plus flag parsing and readable output/error
formatting. See `platform-sdk`'s own README for the client itself.

Lives at `src/platform-cli/`, same `src/`-wrapping convention as `platform-sdk` — see that package's
README for why.

## Phase 2, this pass (2026-09-01): the same minimal slice platform-sdk covers

```text
platform login
platform me
platform workspace list
platform workspace create NAME [--display-name TEXT]
platform workspace get WORKSPACE_ID
platform dataset list
platform dataset create NAME [--visibility private|workspace|public] [--description TEXT] [--location-uri TEXT]
platform dataset get DATASET_ID
platform dataset update DATASET_ID [--description TEXT] [--visibility ...] [--location-uri TEXT]
platform dataset delete DATASET_ID
```

Not built yet: commands for the other three catalog-service resource types (pipelines, models,
lineage) — same "add it when platform-cli actually needs it" reasoning as everywhere else in this
repo. `platform module scaffold/install/uninstall` (ARCHITECTURE.md §3/§7) is a separate, unrelated
slice — module lifecycle, not catalog data — see "Module lifecycle" below; it's built now, not
future scope.

## Functions: publish + promote (2026-09-03, platform-function-promote branch)

```text
platform function list
platform function create NAME [--visibility private|workspace|public] [--description TEXT] [--module-path TEXT]
platform function get FUNCTION_ID
platform function update FUNCTION_ID [--description TEXT] [--visibility ...]
platform function delete FUNCTION_ID
platform function versions FUNCTION_ID
platform function publish FUNCTION_ID --signature TEXT --module-path TEXT [--docstring TEXT] [--published-by TEXT]
platform function promote FUNCTION_ID
```

`publish`/`promote` were the two items ARCHITECTURE.md §11's Phase 2 row named explicitly
(`platform-cli function promote`) — catalog-service's backend for both already existed
(`app/routers/functions.py`); this branch is purely the CLI/SDK side. `promote` takes no flags —
the endpoint always sets visibility to `public` (one-directional by design); demoting back is
`function update FUNCTION_ID --visibility workspace`, the same generic `update` command every other
resource here already has.

## Module lifecycle: install/uninstall/scaffold (2026-09-03, platform-module-lifecycle branch)

```text
platform module scaffold NAME
platform module install NAME [--dry-run] [--skip-requires-check]
platform module uninstall NAME [--purge-data] [--dry-run]
```

Genuinely different from every other command in this file: `uninstall`/`scaffold` don't talk to
`platform-gateway` at all — no login needed, no `PLATFORM_GATEWAY_URL`. They read/write files in
whatever git checkout the CLI is invoked from and commit + push directly, per
docs/architecture/module-lifecycle-plan.md's "Recommended first slice" (items 1-5 — the Argo CD
reconciliation mechanism, `module.yaml` schema/validation, and the chart-wrapper node-placement
convention all live in `platform_cli/manifest.py`, `platform_cli/repo.py`, and
`../core/argocd/apps/core/modules-root.yaml`, not in this package alone). `install` is the one
exception, and only conditionally — see its own bullet below.

- `scaffold NAME` generates `../modules/NAME/module.yaml` and `../charts/NAME/` from
  `_template/` — writes files only, commits nothing. Edit the chart, then commit it yourself
  (`git add` + `git commit`) before running `install` — `install` refuses to run against a dirty
  working tree, so the scaffolded chart has to actually be committed first, same as any other
  change you'd make by hand. The generated `module.yaml`'s `requires: []` means "no other module
  needed first" — fill in other module ids only (never a core service like `auth`) if this module
  genuinely depends on one; see that file's own comment.
- `install NAME` first checks `module.yaml`'s `requires: [...]` (module-lifecycle-plan.md item 6,
  platform-module-deps branch, 2026-09-03): if it's non-empty, this is the one case `install` talks
  to `platform-gateway` at all, calling `GET /modules/check-requirements` to confirm every
  dependency is actually installed and Argo CD-healthy right now — blocks with a clear
  per-dependency message (not installed vs. installed-but-not-Healthy-yet) before writing or
  committing anything if not. A module with an empty `requires` (the template's own default, and
  `hello-module`'s) never makes this call at all — install stays exactly as login-free as before
  this branch. `--skip-requires-check` bypasses the check entirely (prints a visible warning) —
  for bootstrapping the first module of a dependency chain, or installing while gateway/the
  cluster is unreachable.

  Once that passes (or was skipped), `install` validates `module.yaml`, renders a complete Argo CD
  `Application` manifest (repo URL discovered live via `git remote get-url origin`; placement
  wired from `module.yaml`'s own `placement` block), writes it to `../modules-enabled/NAME.yaml`,
  and commits + pushes it — from there, `modules-root` picks it up and Argo CD reconciles the
  module in. `--dry-run` prints the generated manifest and stops before writing or committing
  anything (the requires check above still runs first, same as every other validation `install`
  does before `--dry-run`'s stop point).
- `uninstall NAME` removes that file and commits + pushes the removal — Argo CD prunes the
  Deployment/Service, but leaves any PersistentVolumeClaim the chart marked
  `argocd.argoproj.io/sync-options: Delete=false` alone (ARCHITECTURE.md §3's documented default:
  reinstalling gets your data back). `--purge-data` additionally prints the `kubectl delete pvc`
  command to run yourself — platform-cli never runs it for you; it has no cluster credentials for
  anything beyond git, and PVC deletion has no undo. It also prints a check to confirm the
  module's Application is actually gone *before* you run that delete — found live (see
  `docs/known-issues.md`) that deleting the PVC too early just gets it recreated by that
  Application's own still-live `selfHeal`.

Item 7 — the Add-ons page and gateway's own module registry/`ui-shell` — is still explicitly **not**
built; item 6 (above) is. See `docs/architecture/module-lifecycle-plan.md`.

## Real auth: `platform login` (2026-09-02, platform-gateway-auth branch)

```text
platform login
```

**Breaking change** — the flags every other command used to accept for identity/role are gone:
`--user`/`--role`/`PLATFORM_USER`/`PLATFORM_ROLE` no longer exist at all, and `--catalog-url`/
`PLATFORM_CATALOG_URL` are renamed to `--gateway-url`/`PLATFORM_GATEWAY_URL`. This is the actual
point of this branch, not an oversight: identity and role are no longer anything `platform-cli`
declares — `platform-gateway` derives both from a real, verified Keycloak token, and every command
that talks to catalog-service now goes through gateway instead of reaching it directly (see
`platform_sdk/client.py`'s and `../core/catalog-service/app/deps.py`'s module docstrings for the full
story). Running `platform --user alice ...` now fails with "no such option" rather than silently
doing nothing.

`platform login` is how you get that token: opens (or prints, if you're on a headless/SSH session) a
Keycloak verification URL, waits for you to approve it in a browser, and saves the result to
`~/.config/platform/credentials.json` for every other `platform` command to read back automatically.
No password ever touches this CLI. Safe to re-run any time — always replaces whatever was saved
before. Every other command now needs this run at least once first; without it, they fail fast with
"Not logged in — run `platform login` first" rather than a confusing 401 from gateway.

Uses a separate, public Keycloak client (`platform-cli-login`,
`bootstrap/keycloak-bootstrap-login-client.sh`) from the one `workspace invite` below uses — see that
script's header comment for why the two are deliberately never merged into one.

## Workspace invites (2026-09-01)

```text
platform workspace invite USERNAME [--workspace NAME] [--role owner|editor|viewer]
```

Adds an EXISTING Keycloak user to a workspace's `owner`/`editor`/`viewer` group. Defaults: `--role
viewer` (least privilege), `--workspace` from `PLATFORM_WORKSPACE`/`personal` like every other
command. Doesn't create the user — see `platform_sdk`'s README ("Workspace invites") for why, and for
the required `PLATFORM_KEYCLOAK_CLIENT_SECRET` setup via `bootstrap/keycloak-bootstrap-cli-client.sh`.

Different from every other command here in one way worth knowing before you run it: it does NOT talk
to catalog-service or need `PLATFORM_CATALOG_URL` at all — it talks to Keycloak directly, and manages
its own `kubectl port-forward` to do it (needs `kubectl` + the same `sudo` access every bootstrap
script in this repo assumes). No separate port-forward to set up first.

**Confirmed working end-to-end (2026-09-01)**, not just respx-mocked: `platform workspace invite
dougall --role editor` against a real user created by hand in Keycloak's admin console (see
`bootstrap/keycloak-bootstrap-cli-client.sh` for how `PLATFORM_KEYCLOAK_CLIENT_SECRET` gets set up)
correctly joined the already-seeded `/workspaces/personal/editor` group and printed `'dougall' added
to /workspaces/personal/editor (Reused that Keycloak group)`. That proves the "join an existing
group" path against a live cluster — the "self-heal a missing workspace/role group" path (invite into
a workspace besides `personal`, which `platform workspace create` never wires up in Keycloak) is
still only covered by `platform-sdk`'s mocked test suite, not yet exercised live. Worth trying once
there's a second workspace to invite into.

## Requirements

**Python 3.12+** (`requires-python` in `pyproject.toml`, matching `platform-sdk` and
`catalog-service`). Confirmed the hard way, not hypothetically: installing this on `homelab-dev`
(Rocky Linux 9.4) against its default `python3` (3.9.19) failed with `requires a different Python`.
Rocky/Alma/RHEL 9.x ship Python 3.12 directly from AppStream, no EPEL needed:

```bash
sudo dnf install python3.12
python3.12 -m venv ~/.venvs/platform
source ~/.venvs/platform/bin/activate
```

(Ubuntu 22.04 ships 3.10, same problem, different fix — `sudo apt install python3.12` or the
deadsnakes PPA; macOS — `brew install python@3.12`. See `catalog-service/README.md`'s "Running
locally" section for the full per-OS list; not repeated here to avoid the two copies drifting.)

**The venv isn't active by default in a new shell** — `source ~/.venvs/platform/bin/activate`
first, every time, or `platform: command not found` even though it's installed. Easy to forget
after switching terminals/reconnecting SSH; if `platform` isn't found and you're sure it's
installed, this is almost always why.

**New as of `platform module install/uninstall/scaffold` (2026-09-03):** these need a real git
checkout with push access to `origin` on whatever branch is checked out — the first commands in
this package that touch git at all (every other command only ever needed HTTP + saved
credentials). `helm` is checked for but not required: if it's on `PATH`, `install` runs
`helm template` against the chart before committing, as a pre-push safety check; if it isn't,
`install` prints one warning and skips the check rather than failing. Whether `helm` is actually
on `homelab-dev` hasn't been confirmed as of this branch — check with `which helm` before relying
on the safety check being active there.

## Local dev setup

Two editable installs, in this order — `platform-cli` depends on `platform-sdk` by name with no
version pin (see this package's `pyproject.toml` for why: no published package to resolve against,
just a sibling in this monorepo):

```bash
pip install -e ../platform-sdk
pip install -e ".[dev]"
```

Then log in once and point the CLI at a running `platform-gateway` (see `../gateway/README.md` for
`uvicorn app.main:app --reload`, and that service's own env vars for pointing it at Keycloak +
catalog-service in turn) via env vars — no CLI flag or config file needed for the common case, though
`--workspace`/`--gateway-url` override them per-invocation:

```bash
export PLATFORM_GATEWAY_URL=http://localhost:8080
export PLATFORM_WORKSPACE=personal

platform login          # once — opens a browser verification URL, saves credentials
platform me
platform dataset create reddit-sentiment --visibility public --description "raw pulls"
platform dataset list
```

Verified working end-to-end against a real running `catalog-service` + Postgres during development,
before this branch's gateway/token-auth rewrite — `platform dataset create` / `list` / `get` /
`update` / `delete`, `platform workspace list`, and a `PLATFORM_ROLE=viewer` run correctly getting
rejected with `catalog-service error (403): Viewers cannot create entries.` (that specific env var no
longer exists — see "Real auth" above; a viewer-role rejection now depends on real Keycloak group
membership instead, confirmed the same way once `platform-gateway` is live — see this branch's plan
verification steps).

## Running its tests

```bash
pip install -e ../platform-sdk
pip install -e ".[dev]"
ruff check .
pytest -v
```

`tests/test_cli.py` uses Typer's `CliRunner` with `platform_cli.main.PlatformClient` monkeypatched to
a fake — no network, no live `catalog-service` needed, same reasoning as `platform-sdk`'s own
transport-mocked tests (see that package's README): this suite's job is "does the CLI wire flags,
output, and errors correctly around *a* client," not "does `PlatformClient` talk HTTP correctly"
(that's `platform-sdk`'s job) or "does `catalog-service` work" (that's `catalog-service`'s job).

As of 2026-09-02's real-auth work, the same file also covers `platform login` the same way — a
`FakeLoginFlow` monkeypatched onto `platform_cli.login.KeycloakLoginFlow` (no real device flow, no
network) — verifying the verification-URL/code output, the success/failure paths, and, separately,
that `--user`/`--role`/`--catalog-url` now fail with Typer's own "no such option" rather than being
silently accepted and ignored (`test_removed_flags_are_rejected_not_silently_ignored`) — the actual
proof that this branch's breaking change took effect, not just that the new command works.

`tests/test_module.py` (2026-09-03) is different from every other test file here on purpose: it
uses real temporary git repos (a bare "origin" plus a working clone, both `subprocess`-driven, set
up fresh per test) rather than mocking git calls — the same "exercise the real thing" preference
this repo applies elsewhere (catalog-service's migration tests run real Alembic against a real
Postgres). `install`/`uninstall`'s commits are asserted by actually diffing local `HEAD` against
`origin/main` after the push, not by trusting the CLI's own exit code. The one thing genuinely
mocked is `helm` itself (`shutil.which`/`subprocess.run`, both monkeypatched) — this sandbox has no
`helm` binary to run for real, so both the "helm absent" and "helm present" paths are exercised
explicitly rather than only ever hitting whichever one happens to match a given machine.
