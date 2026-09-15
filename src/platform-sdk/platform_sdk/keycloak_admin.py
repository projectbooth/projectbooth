"""KeycloakAdminClient — talks to Keycloak's own Admin REST API to add an
existing user to a workspace's owner/editor/viewer group
(src/core/auth/realm-platform.yaml's `/workspaces/<name>/<role>` model).
This is `platform workspace invite`'s entire implementation.
catalog-service isn't involved at all — see that service's
app/routers/workspaces.py docstring: membership is Keycloak-group
territory, not catalog data, so there's nothing here for catalog-service to
proxy or store.

Reaches Keycloak directly at `https://{host}` (2026-09-02, platform-ingress
branch) — `keycloak.platform.local` now resolves for real, off-cluster, via
a real `Ingress` (`src/core/argocd/manifests/keycloak-instance.yaml`) plus a
per-device `/etc/hosts` entry, so no port-forward or hostname-resolution
trick is needed to satisfy Keycloak's `hostname-strict` enforcement of
`spec.hostname.hostname: keycloak.platform.local` (see
docs/known-issues.md's 2026-08-31 entry for what that enforcement actually
does). What this class still owns for itself: reading `platform-ca-secret`'s
`ca.crt` via `kubectl` (`extract_platform_ca_cert()` in
`_keycloak_connection.py`) so it can verify Keycloak's cert — self-signed,
not in any public trust store — the same way `--cacert` does for
`bootstrap/keycloak-bootstrap-cli-client.sh`. Net effect unchanged:
`platform workspace invite alice --role editor` stays one command.

Until 2026-09-02 this reached Keycloak through a `kubectl port-forward` plus
a process-wide `socket.getaddrinfo` patch (`_PortForward`/`_ResolvePatch`,
shared with `keycloak_login.py`) — that mechanism, and the reasoning behind
it, is described in `_keycloak_connection.py`'s own module docstring, kept
there rather than repeated here since it no longer applies to either
caller.

Authenticates as the `platform-cli` Keycloak client itself (client_credentials
grant, PLATFORM_KEYCLOAK_CLIENT_SECRET from config.py) — never the master-realm
bootstrap admin. The bootstrap script used the master admin once, to create
this narrower client in the first place; everything from here on uses that
narrower client, scoped to exactly `manage-users` in the `platform` realm.

Scope boundary worth restating here, not just in realm-platform.yaml: this
does NOT create Keycloak users. `registrationAllowed: false` and no seeded
users are deliberate (see that file's header comment) — `invite()` raises a
clear KeycloakAdminError naming that if the username doesn't already exist,
rather than a bare 404.
"""
from __future__ import annotations

import httpx

from platform_sdk._keycloak_connection import cleanup_ca_cert, extract_platform_ca_cert
from platform_sdk.config import SDKSettings
from platform_sdk.exceptions import KeycloakAdminError
from platform_sdk.models import InviteResult, Role


class KeycloakAdminClient:
    def __init__(
        self,
        *,
        host: str | None = None,
        realm: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        kubectl_cmd: str | None = None,
        settings: SDKSettings | None = None,
        timeout: float = 10.0,
        _client: httpx.Client | None = None,
    ) -> None:
        # Same explicit-arg > settings precedence as PlatformClient — see
        # that class's constructor comment.
        settings = settings or SDKSettings()
        self._host = host or settings.keycloak_host
        self._realm = realm or settings.keycloak_realm
        self._client_id = client_id or settings.keycloak_client_id
        self._client_secret = client_secret or settings.keycloak_client_secret
        if not self._client_secret:
            raise KeycloakAdminError(
                "No Keycloak client secret configured. Set PLATFORM_KEYCLOAK_CLIENT_SECRET — see "
                "bootstrap/keycloak-bootstrap-cli-client.sh's printed 'export' line, or read it back "
                "with the command that script also prints if you've already run it."
            )
        # kubectl is still needed for exactly one thing now — reading
        # platform-ca-secret's ca.crt (extract_platform_ca_cert(), below) —
        # not for a port-forward, since 2026-09-02's platform-ingress branch.
        self._kubectl_cmd = kubectl_cmd or settings.keycloak_kubectl_cmd
        self._timeout = timeout
        # Tests construct this with `_client` already set to a mocked
        # httpx.Client (respx) — skips the real kubectl/CA-extraction
        # plumbing entirely, same "inject a transport for tests" shape
        # PlatformClient's own tests use, just via a private constructor arg
        # instead since PlatformClient never needed one. __exit__ only tears
        # down what __enter__ actually built.
        self._external_client = _client is not None
        self._client = _client
        self._token: str | None = None
        self._ca_cert_path: str | None = None

    def __enter__(self) -> KeycloakAdminClient:
        if self._client is None:
            self._ca_cert_path = extract_platform_ca_cert(self._kubectl_cmd)
            self._client = httpx.Client(
                base_url=f"https://{self._host}",
                verify=self._ca_cert_path,
                timeout=self._timeout,
            )
        self._token = self._fetch_token()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if not self._external_client and self._client is not None:
            self._client.close()
            self._client = None
        cleanup_ca_cert(self._ca_cert_path)
        self._ca_cert_path = None

    # ---- token + low-level request plumbing ---------------------------
    def _fetch_token(self) -> str:
        # client_credentials against the *platform* realm's own token
        # endpoint — this authenticates AS the platform-cli client (its own
        # secret), not as the master-realm bootstrap admin the bootstrap
        # script used once to create that client in the first place.
        response = self._client.post(
            f"/realms/{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if response.status_code >= 400:
            raise KeycloakAdminError(
                f"Failed to get a Keycloak admin token for client {self._client_id!r}: "
                f"{response.status_code} {response.text}"
            )
        token = response.json().get("access_token")
        if not token:
            raise KeycloakAdminError(f"Token response had no access_token: {response.text}")
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, path, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            raise KeycloakAdminError(f"{method} {path} -> {response.status_code}: {response.text}")
        return response

    # ---- groups ---------------------------------------------------------
    def _group_by_path(self, path: str) -> str | None:
        # GET /admin/realms/{realm}/group-by-path/{path} — {path} takes the
        # segments literally (no leading slash, no URL-encoding of the
        # internal slashes): confirmed against Keycloak's own Admin REST API
        # docs rather than assumed, same "verify before a live-mutating
        # script depends on it" discipline as the bootstrap script's header
        # comment describes for kcadm.sh.
        response = self._client.get(
            f"/admin/realms/{self._realm}/group-by-path/{path}", headers=self._headers()
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise KeycloakAdminError(f"GET group-by-path/{path} -> {response.status_code}: {response.text}")
        return response.json()["id"]

    def _get_or_create_group(self, path: str, parent_id: str | None, name: str) -> str:
        group_id = self._group_by_path(path)
        if group_id is not None:
            return group_id
        create_path = (
            f"/admin/realms/{self._realm}/groups"
            if parent_id is None
            else f"/admin/realms/{self._realm}/groups/{parent_id}/children"
        )
        response = self._client.post(create_path, headers=self._headers(), json={"name": name})
        # 409 = another invite (or this same call, retried) created it
        # between the lookup above and this POST — not fatal, just look it
        # up again rather than treating a race as an error.
        if response.status_code not in (201, 409):
            raise KeycloakAdminError(f"Creating group {path!r} -> {response.status_code}: {response.text}")
        group_id = self._group_by_path(path)
        if group_id is None:
            raise KeycloakAdminError(
                f"Created (or hit a conflict creating) group {path!r} but a follow-up lookup found nothing."
            )
        return group_id

    def _map_realm_role(self, group_id: str, role_name: str) -> None:
        role_repr = self._request("GET", f"/admin/realms/{self._realm}/roles/{role_name}").json()
        self._request(
            "POST", f"/admin/realms/{self._realm}/groups/{group_id}/role-mappings/realm", json=[role_repr]
        )

    def _ensure_role_group(self, workspace: str, role: Role) -> tuple[str, bool]:
        # The common case: `platform workspace invite` against "personal"
        # (or any workspace someone already invited into before) — the
        # group already exists, one lookup, done.
        role_path = f"workspaces/{workspace}/{role.value}"
        existing = self._group_by_path(role_path)
        if existing is not None:
            return existing, False

        # Self-healing path: `platform workspace create` (catalog-service's
        # own endpoint) never touches Keycloak — see that service's
        # app/routers/workspaces.py docstring — so a workspace created that
        # way has no matching group yet. Rather than fail with "no such
        # group" and leave a manual Keycloak admin-console step as the only
        # fix, build the missing part of the path here. "workspaces" itself
        # is always present (seeded by realm-platform.yaml at import time),
        # so only the workspace subgroup and/or the role subgroup can
        # actually be missing.
        workspaces_id = self._group_by_path("workspaces")
        if workspaces_id is None:
            raise KeycloakAdminError(
                "No top-level 'workspaces' group in the Keycloak realm — realm-platform.yaml should "
                "have seeded this when the realm was first imported. This isn't just a missing "
                "workspace; check the realm itself in Keycloak's admin console before retrying."
            )
        workspace_id = self._get_or_create_group(f"workspaces/{workspace}", workspaces_id, workspace)
        role_id = self._get_or_create_group(role_path, workspace_id, role.value)
        self._map_realm_role(role_id, role.value)
        return role_id, True

    # ---- users ------------------------------------------------------------
    def _find_user_id(self, username: str) -> str:
        response = self._request(
            "GET", f"/admin/realms/{self._realm}/users", params={"username": username, "exact": "true"}
        )
        users = response.json()
        if not users:
            raise KeycloakAdminError(
                f"No Keycloak user named {username!r} in realm {self._realm!r}. `platform workspace "
                "invite` doesn't create users — src/core/auth/realm-platform.yaml sets "
                "registrationAllowed: false and seeds no users on purpose (see that file's header "
                "comment). The user has to already exist in Keycloak some other way first."
            )
        return users[0]["id"]

    # ---- the actual command -------------------------------------------
    def invite(self, username: str, *, workspace: str = "personal", role: Role = Role.VIEWER) -> InviteResult:
        user_id = self._find_user_id(username)
        group_id, created = self._ensure_role_group(workspace, role)
        # PUT is idempotent here — re-inviting someone who's already a
        # member just succeeds again (204), so `invite()` doesn't need its
        # own "already a member" check first.
        self._request("PUT", f"/admin/realms/{self._realm}/users/{user_id}/groups/{group_id}")
        return InviteResult(
            username=username,
            workspace=workspace,
            role=role,
            group_path=f"/workspaces/{workspace}/{role.value}",
            group_created=created,
        )
