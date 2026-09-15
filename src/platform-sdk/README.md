# platform-sdk

The typed Python client for `catalog-service` — ARCHITECTURE.md §4/§10's "connectors, `@platform`
decorators, catalog client." First real consumer of `catalog-service`'s API outside its own test
suite.

Lives at `src/platform-sdk/`, not the top-level `platform-sdk/` ARCHITECTURE.md's original repo
layout diagram shows — this repo already wraps everything under `src/` (`src/core/`, `src/modules/`,
`src/charts/`, `src/examples/`), and this follows that same convention for consistency rather than
matching the diagram literally. Same divergence-for-consistency call as elsewhere in this repo.

## Phase 2, this pass (2026-09-01): workspaces + datasets only

Not a placeholder — real, tested code — but deliberately the minimal slice: `PlatformClient` covers
`/me`, `/workspaces` (list/create/get), and `/datasets` (list/create/get/update/delete). Pipelines,
models, and lineage are the exact same shape of work again, once `platform-cli` or a real
`@platform.*` decorator actually needs them — see `platform_sdk/models.py`'s module docstring for
why they're not speculatively built now.

## Functions: publish + promote (2026-09-03, platform-function-promote branch)

`PlatformClient` also covers `/functions` now — `list`/`create`/`get`/`update`/`delete` (same shape
as `/datasets` above) plus two function-specific endpoints catalog-service's `app/routers/
functions.py` already had before this branch: `publish_function(...)` (bumps `FunctionVersion`,
ARCHITECTURE.md §4's "a version, bumped on each publish") and `promote_function(...)` (one-directional
visibility → public; demoting back is a plain `update_function(..., visibility=...)`). This is the
client side only — the `@platform.function` decorator that would extract a real signature/docstring
from running code (ARCHITECTURE.md §3/§4) still doesn't exist; `publish_function`'s
`signature`/`docstring` are caller-supplied strings for now, same as `location_uri` already is for
datasets.

## Module dependency-checking (2026-09-03, platform-module-deps branch)

`check_module_requirements(requires: list[str]) -> list[ModuleRequirementStatus]` calls gateway's
`GET /modules/check-requirements` (module-lifecycle-plan.md item 6) — for each module id passed
in, whether it's actually installed and Argo CD-healthy right now, not whether *this* module
declares it as a dependency (that list is the caller's own `module.yaml`, already in hand — see
`ModuleRequirementStatus`'s docstring in `models.py`). `platform-cli`'s `module install` is the
first caller (`platform_cli/module.py`'s `_check_requires`), blocking before writing anything if a
declared `requires: [...]` entry isn't satisfied yet.

## Real auth: `platform login` + token-based `PlatformClient` (2026-09-02, platform-gateway-auth branch)

**Breaking change.** `PlatformClient` no longer takes `user`/`role` constructor args, and
`catalog_url`/`PLATFORM_CATALOG_URL` is renamed to `gateway_url`/`PLATFORM_GATEWAY_URL` — see
`client.py`'s and `config.py`'s module docstrings for the full reasoning, but the short version:
identity and role are no longer anything a caller declares. `PlatformClient` now talks to
`platform-gateway` (which proxies to catalog-service), sending a real Keycloak-issued
`Authorization: Bearer <token>` plus `X-Workspace` as a hint gateway validates against the token's
own group membership — `X-User`/`X-Role` are gateway-derived and never sent by this client at all.

Getting that token is `platform_sdk.keycloak_login.KeycloakLoginFlow` — the OAuth 2.0 Device
Authorization Grant (RFC 8628), `platform login`'s entire implementation. Reuses the same
`_PortForward`/`_ResolvePatch` mechanism `KeycloakAdminClient` uses (now factored out into
`platform_sdk/_keycloak_connection.py` for exactly this reuse) to reach Keycloak's real hostname
despite its `hostname-strict` behavior — see that module's docstring, and `keycloak_login.py`'s own,
for the full design.

`platform_sdk.credentials` is where the resulting tokens land — `~/.config/platform/credentials.json`
(respects `XDG_CONFIG_HOME`), written `0600` from the moment the file exists, never write-then-chmod.
`PlatformClient` reads this back lazily, on the first request any instance actually sends, and
refreshes silently (via the same `KeycloakLoginFlow`) if the saved token is within 30 seconds of
expiring — one check per `PlatformClient` instance, matching this SDK's "one-shot-command-then-exit"
CLI-first design. No credentials file yet → `PlatformClient` raises `NotAuthenticatedError` naming
the fix (`platform login`) rather than a bare 401 from gateway.

The `@platform.dataset` / `@platform.pipeline` / `@platform.function` decorators ARCHITECTURE.md §4
describes (self-registration for *code*, the counterpart to the module self-registration pattern in
§3) also aren't built yet — this pass is the client those decorators will eventually call into, not
the decorators themselves.

## Workspace invites (2026-09-01): `KeycloakAdminClient`

A second, separate client — `platform_sdk.keycloak_admin.KeycloakAdminClient` — talks to Keycloak's
own Admin REST API directly, not to catalog-service, to add an existing user to a workspace's
`owner`/`editor`/`viewer` group (`src/core/auth/realm-platform.yaml`'s `/workspaces/<name>/<role>`
model). This is `platform workspace invite`'s entire implementation; see `platform_sdk/keycloak_admin.py`'s
module docstring for the full design, especially *why* it manages its own `kubectl port-forward` and
reproduces curl's `--resolve` trick in Python (Keycloak's hostname provider — see
`docs/known-issues.md`'s 2026-08-31 entry — makes a plain port-forward-to-localhost not work the way
it does for catalog-service).

Requires:

- `PLATFORM_KEYCLOAK_CLIENT_SECRET` set — from `bootstrap/keycloak-bootstrap-cli-client.sh`'s printed
  `export` line, or its read-back command if you've already run that script once.
- `kubectl` reachable the same way every other script in this repo expects (`sudo`, `/usr/local/bin/kubectl`).
- `jq` is NOT needed here (that's the bootstrap script's dependency, for its own bash+curl approach) —
  this is plain Python/httpx.

Self-heals workspaces that only exist in catalog-service's database so far: `platform workspace
create` never touches Keycloak (see that service's `app/routers/workspaces.py` docstring), so the
first `invite` into a workspace besides the seeded `personal` one creates the missing Keycloak group
path (and maps the matching realm role onto it) automatically rather than requiring a manual
admin-console step first.

Does **not** create Keycloak users — `realm-platform.yaml` sets `registrationAllowed: false` and
seeds no users on purpose (see that file's header comment). `invite()` raises a clear
`KeycloakAdminError` naming this if the username doesn't already exist, rather than a bare 404.

**Confirmed working end-to-end (2026-09-01)** against a real cluster, not just the respx-mocked
tests below — `KeycloakAdminClient.invite()` (via `platform workspace invite`) successfully joined a
real Keycloak user to the already-seeded `/workspaces/personal/editor` group: port-forward came up,
the hostname-resolution patch worked, the client-credentials token exchange worked, the group-by-path
lookup and the PUT join all did what the code expects. The self-heal path (creating a missing
workspace/role group from scratch) is still mocked-only — see `platform-cli`'s README.

## Requirements

**Python 3.12+** (`requires-python` in `pyproject.toml`) — most systems' default `python3` is
older (RHEL-family 9.x ships 3.9, Ubuntu 22.04 ships 3.10), so `pip install -e ".[dev]"` fails with
`requires a different Python` against a bare `python3`/`pip`. See `catalog-service/README.md`'s
"Running locally" section for per-OS install commands (same requirement, same fix, verified there
against a real Rocky 9.4 box) — not repeated here to avoid the two copies drifting apart.

## Using it

```python
from platform_sdk import KeycloakLoginFlow
from platform_sdk.credentials import save_credentials

# Run once (this is what `platform login` does) — opens a browser
# verification URL, polls until approved, saves the token for every later
# PlatformClient() to read back. No user/password ever touches this process.
with KeycloakLoginFlow() as flow:
    device_auth = flow.start_device_authorization()
    print(f"Open: {device_auth.verification_uri_complete}")
    token_set = flow.poll_for_token(device_auth)
save_credentials(token_set)
```

```python
from platform_sdk import PlatformClient, Visibility

# No user/role args anymore — identity and role come from the token
# credentials.json already has (see "Real auth" above). Raises
# NotAuthenticatedError if KeycloakLoginFlow hasn't been run yet.
with PlatformClient(workspace="personal") as client:
    me = client.me()
    dataset = client.create_dataset("reddit-sentiment", visibility=Visibility.PRIVATE)
    print(client.list_datasets())
```

```python
from platform_sdk import KeycloakAdminClient, Role

# Needs PLATFORM_KEYCLOAK_CLIENT_SECRET set — see "Workspace invites" above.
# Separate client, separate grant type from KeycloakLoginFlow above — see
# bootstrap/keycloak-bootstrap-login-client.sh's header for why the two
# Keycloak clients (platform-cli vs. platform-cli-login) are deliberately
# never merged into one.
with KeycloakAdminClient() as admin:
    result = admin.invite("alice", workspace="personal", role=Role.EDITOR)
    print(result.group_path, result.group_created)
```

Config resolution (constructor arg > `PLATFORM_*` env var / `.env` > built-in default) is
`platform_sdk/config.py` — see its docstring for the full field list, including why
`PLATFORM_GATEWAY_URL` replaced `PLATFORM_CATALOG_URL` and why `PLATFORM_USER`/`PLATFORM_ROLE` were
removed outright rather than left silently ignored.

## Running its tests

```bash
pip install -e ".[dev]"
ruff check .
pytest -v
```

No live `catalog-service` or Postgres needed — `tests/test_client.py` mocks the HTTP transport
directly with `respx`. That's a real difference from `catalog-service`'s own test suite (which
insists on a real Postgres — see that service's `tests/conftest.py`), not an inconsistency: this
package's job is "does the client send/parse the right thing," not "does the service work."

`tests/test_keycloak_admin.py` does the same for `KeycloakAdminClient` — respx-mocked Admin API
calls, `KeycloakAdminClient`'s private `_client=` constructor arg bypassing the real
port-forward/kubectl/hostname-patch plumbing entirely (that plumbing isn't unit-testable without a
live cluster — see that test file's own docstring, and the real end-to-end confirmation noted in
`platform-cli`'s README once `workspace invite` has actually been run against a live Keycloak).

`tests/test_keycloak_login.py` (2026-09-02) does the same for `KeycloakLoginFlow` — respx-mocked
device-authorization/token-endpoint calls, real signed-enough JWTs for the ID-token-decoding path,
`time.sleep` monkeypatched to a no-op so the RFC 8628 poll loop's tests run in milliseconds regardless
of what `interval` a mocked response specifies. `tests/test_credentials.py` exercises
`platform_sdk.credentials` against a real filesystem, scoped to a pytest `tmp_path` via
`XDG_CONFIG_HOME` — never the real `~/.config/platform/credentials.json`. `tests/test_client.py`'s
own credentials-dependent tests monkeypatch `platform_sdk.client.load_credentials`/`save_credentials`
directly rather than touching a file at all — this suite's job stops at "does PlatformClient use
whatever TokenSet it's handed correctly," not "does credentials.py read/write a file correctly" (that's
`test_credentials.py`'s job).

## Error handling

Every non-2xx response from `PlatformClient` raises `platform_sdk.PlatformAPIError` — `.status_code`
and `.detail` are pulled from the response body (catalog-service's consistent `{"detail": "..."}`
shape), so callers can branch on `.status_code` (e.g. `403` for a viewer trying to write) without
parsing message strings.

`KeycloakAdminClient` raises `platform_sdk.KeycloakAdminError` instead — a single exception type
covering everything from "no client secret configured" to "port-forward never came up" to "no such
Keycloak user" to an unexpected Admin API status, each with a message that says which.

Two more exception types as of 2026-09-02's real-auth work: `platform_sdk.PlatformLoginError` for
anything that goes wrong during `KeycloakLoginFlow`'s device flow (or a silent near-expiry token
refresh) — a denied login, an expired device code, an unexpected token-endpoint response.
`platform_sdk.NotAuthenticatedError` for `PlatformClient` finding no usable saved credentials at all
— the fix is always "run `platform login`," never a retry, which is why it's a distinct type from
`PlatformLoginError` (that one fires during login itself; this one fires when some other command
discovers there's nothing to authenticate with).
