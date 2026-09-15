# catalog-service

`catalog-lite` — the Unity-Catalog analog. Datasets, functions, pipelines, models, workspaces;
every entry carries `workspace_id` + `visibility` (private/workspace/public). Metadata only — code
stays in `platform-sdk`/git. ARCHITECTURE.md §2 (layer 3), §4, §12 ("Catalog: build vs adopt" —
picked lightweight custom over OpenMetadata/DataHub).

Phase 2 kickoff (2026-09-01): the data model and a thin FastAPI service over it now exist. Not a
placeholder — this is real, runnable code — but it's the first piece of Phase 2, not the whole
thing. See "Not yet built" below for what's deliberately left for the next steps.

## What's here

- `app/models.py` — the schema: `workspaces`, `datasets`, `pipelines`, `models`, `functions` +
  `function_versions` (full publish history), `lineage_edges` (polymorphic — a function's lineage
  can point at a dataset, another function, or a pipeline). Every entity table carries
  `workspace_id` + `visibility`, per ARCHITECTURE.md §4 — see that file's module docstring for why
  it's written out per-table rather than through a mixin.
- `app/visibility.py` — the ONE place the read/write rule lives (`private`/`workspace`/`public`,
  and the "public means readable, not writable" asymmetry from §4). Every router goes through this.
- `app/deps.py` — reads `X-Workspace`/`X-User`/`X-Role` headers; as of the platform-gateway-auth
  branch (2026-09-02), that's the correct permanent shape, not a placeholder — `platform-gateway`
  verifies every caller's Keycloak JWT and is the only thing that ever sets these headers on a
  request that reaches this service (see its own README, and this module's docstring for the full
  trust-boundary writeup, including the one gap that's still open at the network layer).
- `app/routers/` — CRUD for each entity type, plus `functions.py`'s `/publish` and `/promote`
  (the `platform-cli function promote --public` §4 calls out by name) and `lineage.py`.
- `migrations/` — Alembic, hand-written (no live DB in the environment that built this to
  autogenerate against). `0001_initial_schema.py` seeds the `personal` workspace to match
  `../auth/realm-platform.yaml`'s seeded Keycloak group. `0002_function_versions_cascade.py`
  (2026-09-03, `platform-function-promote` branch) adds `ON DELETE CASCADE` to
  `function_versions.function_id` — see `docs/known-issues.md`'s "Deleting a published `Function`
  500'd" entry for the real bug this fixed and why the constraint name is looked up dynamically
  rather than hardcoded.
- `tests/` — `test_visibility.py` (pure logic, no DB) and `test_datasets_api.py` (integration,
  needs a real Postgres — see that file's docstring for a one-line `docker run` to get one).
- `Dockerfile` — builds and runs today (`docker build . && docker run -p 8000:8000 ...` against any
  reachable Postgres with migrations applied). Now also what `.github/workflows/ci.yml` builds and
  pushes to `ghcr.io/dougallpercival/catalog-service`, and what
  `../argocd/manifests/catalog-service.yaml`'s Deployment and migration Job actually run in-cluster.

## Running locally

**Requires Python 3.12+** (`requires-python` in `pyproject.toml`). Most systems' default `python3`
is older than that — notably RHEL-family (Rocky/Alma/RHEL) 9.x, whose default is 3.9, and Ubuntu
22.04's, which is 3.10 — so `pip install -e ".[dev]"` will fail with `requires a different Python`
if you just run it against whatever `python3`/`pip` resolves to by default. Get 3.12+ first, then
use it explicitly:

- **RHEL-family 9.x (Rocky/Alma/RHEL):** `sudo dnf install python3.12` — ships directly from
  AppStream as a non-modular package (installs alongside the system Python, doesn't replace it), no
  EPEL or third-party repo needed. Then `python3.12 -m venv .venv && source .venv/bin/activate`
  before the commands below.
- **Ubuntu/Debian:** `sudo apt install python3.12` if your release ships it, otherwise the
  deadsnakes PPA (`sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12`).
- **macOS:** `brew install python@3.12`.

```bash
cp .env.example .env   # edit DATABASE_URL if not using the default local Postgres
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
# http://localhost:8000/docs — FastAPI's generated Swagger UI
```

## Database provisioning (in-cluster)

`../argocd/manifests/postgres-cluster.yaml`'s `managed.roles` now includes a `catalog` role, and
`../argocd/manifests/catalog-database.yaml` declares a `catalog` database on `platform-postgres` via
CNPG's `Database` CRD (`postgresql.cnpg.io/v1`) — the same declarative pattern
`postgres-backup.yaml`'s `ObjectStore` uses, not a manual `CREATE DATABASE` Job, because the CNPG
operator (1.30, well past the CRD's 1.24 introduction) already offers it. `bootstrap/install.sh`
generates `platform-postgres-catalog-credentials` the same way it generates the Keycloak one, and
Reflector mirrors it into `catalog-service` for whenever a Deployment reads it.

## Deployment (in-cluster)

`.github/workflows/ci.yml` tests, then builds and pushes the image to `ghcr.io/dougallpercival/
catalog-service` on every push to `dev`/`test`/`main` — tag matches the branch name, per
ARCHITECTURE.md §10's promotion model. **One manual, one-time step after the first successful
push:** GHCR packages default to private regardless of the source repo's visibility — go to the
package's Settings → Change visibility → Public (irreversible — can't go back to private once
public). See `docs/known-issues.md`.

`../argocd/manifests/catalog-service.yaml` (Argo CD Application `catalog-service`, wave 3) runs a
PreSync `alembic upgrade head` Job against the `catalog` database, then a 1-replica Deployment +
ClusterIP Service — deliberately no Ingress. `platform-gateway` (wave 4, `../gateway/`) is the
intended way in from outside this service's own namespace; see `docs/known-issues.md`'s auth entry
below for what's still open even with gateway in front (network-layer isolation, not application-layer
trust).

## Roles (2026-09-01)

`app/visibility.py` now enforces owner/editor/viewer (ARCHITECTURE.md §4's table) on every write:
a viewer can read anything visibility already lets them read, but `can_write`/`can_create` block
them from creating, updating, deleting, publishing, or promoting — even something they created
themselves. Role arrives via the same `X-Role` header workspace/user identity already do — as of
the platform-gateway-auth branch (2026-09-02), gateway-derived from the caller's verified Keycloak
token's `groups` claim, never client-declared (see `app/deps.py`'s docstring) — and still defaults
to `owner` when absent, so nothing that predates this header (every existing test, every curl
example above) needed to change. That default matters more now than it used to: gateway must
always send an explicit `X-Role` for every request it forwards, never omit it — see `DEFAULT_ROLE`'s
own comment in `app/deps.py` for why an omitted `X-Role` would be a real privilege-escalation path,
not a hypothetical one. `GET /me` reflects the resolved workspace/user/role back for a caller to
self-check.

What this **isn't**: membership storage. Nothing here decides *who's* a workspace's owner/editor/
viewer — that's entirely Keycloak-group territory (`src/core/auth/realm-platform.yaml`'s
`/workspaces/<name>/<role>` groups). `platform workspace invite` (`platform-cli`, calling Keycloak's
Admin API directly via `platform_sdk.keycloak_admin.KeycloakAdminClient`) now does that actual
membership write — see `platform-sdk`'s README — but it talks to Keycloak, not to this service; this
service still only decides what a *given, already-resolved* role is allowed to do once it has one.

## Not yet built

- **Network-layer isolation.** `platform-gateway` (2026-09-02) closed the *application-layer* auth
  gap — see `app/deps.py`'s docstring — but nothing yet stops another in-cluster pod from reaching
  this service's `ClusterIP` directly, bypassing gateway. A `NetworkPolicy` restricting ingress to
  gateway's namespace only would close it; tracked in `docs/known-issues.md`, not built this pass.
- **`@platform.dataset` etc. self-registration decorators, `platform-cli publish`/`promote`, and
  platform-sdk/platform-cli coverage for the other four resource types** (pipelines, models,
  functions, lineage) — `platform-sdk`/`platform-cli` now exist and are real, tested consumers of
  this service (workspaces + datasets, plus `platform workspace invite` — Keycloak-only, doesn't
  touch this service at all, see its own README), just not the whole surface yet.
