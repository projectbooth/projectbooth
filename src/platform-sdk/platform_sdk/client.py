"""PlatformClient — a thin, synchronous wrapper over platform-gateway's REST
API (which itself proxies to catalog-service — see gateway's own README).
"Thin" on purpose: this does exactly what a `curl` command hitting the same
endpoint would do, plus typed request/response shapes and one consistent
error type (PlatformAPIError) instead of raw httpx exceptions — no caching,
no retries, no batching. Add those only once something using this actually
needs them, not speculatively.

Synchronous, not async: platform-cli (this SDK's first real consumer) is a
one-shot-command-then-exit CLI, where async buys nothing — every command
does one thing and quits. Revisit if something long-lived and concurrent
ever needs it; httpx supports both, so this isn't a one-way door.

Auth (rewritten 2026-09-02, platform-gateway-auth branch): this used to send
client-declared `X-Workspace`/`X-User`/`X-Role` headers straight through
with zero verification on the other end — see this file's git history, or
catalog-service's app/deps.py docstring, for that placeholder shape. Real
auth replaces it: `PlatformClient` now sends a real Keycloak-issued
`Authorization: Bearer <access_token>` (from `platform login`'s saved
credentials — see credentials.py) plus `X-Workspace` as a *hint*.
`X-User`/`X-Role` are no longer sent at all — gateway derives both itself
from the verified token (the `sub`/`preferred_username` and `groups` claims)
and validates the `X-Workspace` hint against `groups` before trusting it
(403 if there's no matching membership). Nothing this class sends is trusted
on faith anymore; that trust boundary moved to gateway, which is the whole
point of this branch — see src/core/gateway/app/auth.py.

No `user`/`role` constructor args or PLATFORM_USER/PLATFORM_ROLE settings
exist anymore (config.py's docstring covers why removing them, not just
ignoring them, was the right call). Not logged in? `PlatformClient` raises
`NotAuthenticatedError` the first time it needs to send a request, naming
the fix (`platform login`) rather than a bare 401 from gateway.

TLS trust (added 2026-09-02, platform-ingress branch): `gateway_url`'s
default changed from `http://localhost:8080` (a plain-HTTP port-forward, no
TLS at all) to the real `https://gateway.platform.local` Ingress — found
live the moment this shipped: every request failed with
`CERTIFICATE_VERIFY_FAILED`, because gateway's Ingress cert is signed by
this cluster's self-signed `platform-ca`, which isn't in any system trust
store. Fixed the same way `KeycloakAdminClient`/`KeycloakLoginFlow` already
handle this — `extract_platform_ca_cert()` (`_keycloak_connection.py`) reads
`platform-ca-secret` via `kubectl` and pins it as this client's own
`verify=`, but only when `gateway_url` is actually `https://` (the test
suite's mocked `http://gateway.test` base_url skips this entirely, so
`kubectl` is never invoked in tests). This does mean `platform` commands now
assume `kubectl` access on whatever machine runs them — true of every other
Keycloak-touching path in this repo already (`platform login` itself has
needed it since before this branch), so not new scope, just extended to
this class too. The documented alternative — importing `platform-ca` into
the machine's own OS/browser trust store once (see
`manifests/cluster-issuer.yaml`'s header comment) — would let this default
back off; worth revisiting if `platform-cli` ever needs to run somewhere
without `kubectl` configured.

CA extraction is LAZY, not done in `__init__` — found live immediately
after the first version of this fix: `platform_cli/main.py`'s Typer
callback constructs a `PlatformClient` unconditionally for every command,
including `login` and `workspace invite`, which build their own separate
`KeycloakLoginFlow`/`KeycloakAdminClient` and never touch this one at all.
An eager `extract_platform_ca_cert()` call in `__init__` meant `platform
login` itself broke if `kubectl` wasn't reachable — exactly backwards, since
`login` is the one command that shouldn't need to already be talking to
gateway. `_ensure_http()` below defers building the actual `httpx.Client`
(CA extraction included) until the first real request, same "checked once,
on first use" shape `_ensure_token()` already has — never called at all by
a command that never sends one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from platform_sdk._keycloak_connection import cleanup_ca_cert, extract_platform_ca_cert
from platform_sdk.config import SDKSettings
from platform_sdk.credentials import load_credentials, save_credentials
from platform_sdk.exceptions import NotAuthenticatedError, PlatformAPIError
from platform_sdk.keycloak_login import KeycloakLoginFlow
from platform_sdk.models import (
    Dataset,
    Function,
    FunctionVersion,
    ModuleRequirementStatus,
    Principal,
    TokenSet,
    Visibility,
    Workspace,
)

# How close to actual expiry counts as "near-expiry" and triggers a silent
# refresh before the request goes out, rather than sending a token that's
# likely to 401 mid-flight. 30s is generous relative to how long a single
# `platform` command takes to run (well under a second of actual gateway
# round-trip time) — this isn't tuned finer than that because it doesn't
# need to be: too-small only risks an occasional avoidable 401-then-retry
# gateway would otherwise have to handle, too-large only means refreshing a
# few requests earlier than strictly necessary.
_TOKEN_REFRESH_BUFFER = timedelta(seconds=30)


class PlatformClient:
    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        workspace: str | None = None,
        settings: SDKSettings | None = None,
        timeout: float = 10.0,
    ) -> None:
        # Explicit constructor args win over settings (env vars) over
        # hardcoded fallbacks — same precedence order platform-cli's own
        # --workspace/--gateway-url flags rely on without this class needing
        # to know CLI flags exist.
        settings = settings or SDKSettings()
        self._settings = settings
        self._base_url = (gateway_url or settings.gateway_url).rstrip("/")
        self._workspace = workspace or settings.workspace
        self._timeout = timeout
        # Built lazily by _ensure_http() on the first actual request, not
        # here — see this module's docstring for why an eager kubectl call
        # in __init__ was a real bug, not just premature work.
        self._http: httpx.Client | None = None
        self._ca_cert_path: str | None = None
        # Loaded (and refreshed, if needed) at most once per PlatformClient
        # instance — see _ensure_token()'s own comment for why this is a
        # deliberate "one check per invocation," not a re-check-every-call
        # or a check-once-per-process cache.
        self._token_set: TokenSet | None = None
        self._token_checked = False

    def _ensure_http(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        # Only pin a CA when there's actually TLS to verify — the test
        # suite's mocked http://gateway.test base_url (and anyone who
        # deliberately overrides gateway_url back to a plain-HTTP
        # port-forward) never touches kubectl at all.
        if self._base_url.startswith("https://"):
            self._ca_cert_path = extract_platform_ca_cert(self._settings.keycloak_kubectl_cmd)
        self._http = httpx.Client(
            base_url=self._base_url, timeout=self._timeout, verify=self._ca_cert_path or True
        )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        cleanup_ca_cert(self._ca_cert_path)
        self._ca_cert_path = None

    def __enter__(self) -> PlatformClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- auth -----------------------------------------------------------
    def _ensure_token(self) -> TokenSet:
        # A one-shot-command-then-exit CLI only ever needs to decide "is my
        # token still good enough for this run" once — there's no long-lived
        # process here where a token could go stale mid-lifetime the way a
        # server's would. So this checks (and refreshes, if needed) the
        # first time any command actually sends a request, then reuses that
        # same TokenSet for every remaining request this instance makes.
        if self._token_checked:
            assert self._token_set is not None
            return self._token_set

        token_set = load_credentials()
        if token_set is None:
            raise NotAuthenticatedError("Not logged in — run `platform login` first.")

        if token_set.expires_at - datetime.now(UTC) < _TOKEN_REFRESH_BUFFER:
            if not token_set.refresh_token:
                raise NotAuthenticatedError(
                    "Saved credentials have expired and there's no refresh token to renew them with "
                    "— run `platform login` again."
                )
            # KeycloakLoginFlow's own port-forward/hostname-patch machinery
            # (see that module's docstring) — a real but short-lived cost,
            # paid only on the rare invocation that lands within
            # _TOKEN_REFRESH_BUFFER of expiry, not on every command.
            with KeycloakLoginFlow(settings=self._settings) as flow:
                token_set = flow.refresh(token_set.refresh_token)
            save_credentials(token_set)

        self._token_set = token_set
        self._token_checked = True
        return token_set

    def _headers(self) -> dict[str, str]:
        token_set = self._ensure_token()
        # X-Workspace is a HINT, not a trust boundary — gateway validates it
        # against the token's own `groups` claim and 403s if there's no
        # matching membership (see gateway/app/auth.py). Sending it at all
        # is what preserves the existing --workspace-flag UX; nothing about
        # its presence here makes it authoritative.
        return {
            "Authorization": f"Bearer {token_set.access_token}",
            "X-Workspace": self._workspace,
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._ensure_http().request(method, path, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:  # body wasn't JSON at all (e.g. a raw 502 from something in front)
                detail = response.text
            raise PlatformAPIError(response.status_code, detail, method=method, url=str(response.url))
        return response

    # ---- Principal ---------------------------------------------------
    def me(self) -> Principal:
        return Principal.model_validate(self._request("GET", "/me").json())

    # ---- Workspaces ----------------------------------------------------
    def list_workspaces(self) -> list[Workspace]:
        return [Workspace.model_validate(w) for w in self._request("GET", "/workspaces").json()]

    def create_workspace(self, name: str, display_name: str) -> Workspace:
        body = {"name": name, "display_name": display_name}
        return Workspace.model_validate(self._request("POST", "/workspaces", json=body).json())

    def get_workspace(self, workspace_id: UUID | str) -> Workspace:
        return Workspace.model_validate(self._request("GET", f"/workspaces/{workspace_id}").json())

    # ---- Datasets --------------------------------------------------------
    def list_datasets(self) -> list[Dataset]:
        return [Dataset.model_validate(d) for d in self._request("GET", "/datasets").json()]

    def create_dataset(
        self,
        name: str,
        *,
        visibility: Visibility = Visibility.PRIVATE,
        description: str | None = None,
        location_uri: str | None = None,
    ) -> Dataset:
        body = {
            "name": name,
            "visibility": visibility.value,
            "description": description,
            "location_uri": location_uri,
        }
        return Dataset.model_validate(self._request("POST", "/datasets", json=body).json())

    def get_dataset(self, dataset_id: UUID | str) -> Dataset:
        return Dataset.model_validate(self._request("GET", f"/datasets/{dataset_id}").json())

    def update_dataset(self, dataset_id: UUID | str, **fields: Any) -> Dataset:
        return Dataset.model_validate(self._request("PATCH", f"/datasets/{dataset_id}", json=fields).json())

    def delete_dataset(self, dataset_id: UUID | str) -> None:
        self._request("DELETE", f"/datasets/{dataset_id}")

    # ---- Functions (platform-function-promote branch, 2026-09-03) --------
    # Same shape as the Datasets block above, mirrored onto a different
    # resource — catalog-service's app/routers/functions.py already has the
    # full CRUD plus /publish and /promote; this is purely the client side.
    def list_functions(self) -> list[Function]:
        return [Function.model_validate(f) for f in self._request("GET", "/functions").json()]

    def create_function(
        self,
        name: str,
        *,
        visibility: Visibility = Visibility.PRIVATE,
        description: str | None = None,
        module_path: str | None = None,
    ) -> Function:
        body = {
            "name": name,
            "visibility": visibility.value,
            "description": description,
            "module_path": module_path,
        }
        return Function.model_validate(self._request("POST", "/functions", json=body).json())

    def get_function(self, function_id: UUID | str) -> Function:
        return Function.model_validate(self._request("GET", f"/functions/{function_id}").json())

    def update_function(self, function_id: UUID | str, **fields: Any) -> Function:
        response = self._request("PATCH", f"/functions/{function_id}", json=fields)
        return Function.model_validate(response.json())

    def delete_function(self, function_id: UUID | str) -> None:
        self._request("DELETE", f"/functions/{function_id}")

    def list_function_versions(self, function_id: UUID | str) -> list[FunctionVersion]:
        return [
            FunctionVersion.model_validate(v)
            for v in self._request("GET", f"/functions/{function_id}/versions").json()
        ]

    def publish_function(
        self,
        function_id: UUID | str,
        *,
        signature: str,
        module_path: str,
        docstring: str | None = None,
        published_by: str | None = None,
    ) -> FunctionVersion:
        # published_by is omitted from the body when not given, not sent as
        # an explicit null — app/routers/functions.py's own fallback is
        # `body.published_by or principal.user_id`, which a JSON null
        # satisfies fine, but leaving the key out entirely is the more
        # honest "I'm not asserting a value" shape for an optional field.
        body: dict[str, Any] = {"signature": signature, "module_path": module_path, "docstring": docstring}
        if published_by is not None:
            body["published_by"] = published_by
        return FunctionVersion.model_validate(
            self._request("POST", f"/functions/{function_id}/publish", json=body).json()
        )

    def promote_function(self, function_id: UUID | str) -> Function:
        # No body — app/routers/functions.py's promote_function is
        # unconditional (sets visibility=public, one-directional by design;
        # demoting back is a plain update_function(..., visibility=...)).
        return Function.model_validate(self._request("POST", f"/functions/{function_id}/promote").json())

    # ---- Modules (platform-module-deps branch, 2026-09-03) ---------------
    def check_module_requirements(self, requires: list[str]) -> list[ModuleRequirementStatus]:
        """Calls gateway's GET /modules/check-requirements — see that
        endpoint's docstring (gateway/app/modules.py) and
        ModuleRequirementStatus's own docstring for what's actually being
        asked. `requires` is sent as repeated `?requires=` query params
        (httpx does this automatically for a list value); an empty list is
        a legal, if pointless, call — returns an empty list back."""
        response = self._request("GET", "/modules/check-requirements", params={"requires": requires})
        return [ModuleRequirementStatus.model_validate(r) for r in response.json()["results"]]
