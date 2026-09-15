"""Where PlatformClient's defaults come from when a caller doesn't pass them
explicitly — env vars (optionally via a `.env` file), same
pydantic-settings pattern catalog-service/app/config.py already uses, for
the same reason: one typed, validated place instead of scattered
`os.environ.get(...)` calls.

Deliberately NOT a config *file* (no `~/.platform/config.toml`) yet — env
vars are enough for "one person, one workspace, running this from a shell
or a CI job," which is everything this SDK/CLI needs to support today. A
config file becomes worth it once there's something that actually needs
persisted-across-sessions state a `PLATFORM_*` env var can't reasonably
hold — that line already got crossed once (credentials.py's
~/.config/platform/credentials.json, for `platform login`'s tokens), but
that's deliberately its own dedicated file, not folded into this settings
object: settings here are re-resolved fresh from the environment on every
`SDKSettings()` construction, while credentials are read-write state a
login command creates and a completely separate later process reads back.
Mixing the two would make this class's "just env vars, resolved fresh"
contract a lie.

`user`/`role` fields REMOVED (2026-09-02, platform-gateway-auth branch):
identity and role are no longer anything PlatformClient declares — they're
derived server-side, by gateway, from the caller's verified Keycloak token
(see client.py's module docstring, and src/core/gateway/app/auth.py). A
`PLATFORM_USER`/`PLATFORM_ROLE` env var that no longer did anything would be
actively misleading, not harmlessly unused — better to remove them and let
`platform_cli`'s `--user`/`--role` flags fail with "no such option" than
leave either silently ignored.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SDKSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=".env", env_file_encoding="utf-8")

    # RENAMED from catalog_url/PLATFORM_CATALOG_URL (2026-09-02,
    # platform-gateway-auth branch): the old name became actively
    # misleading once PlatformClient stopped talking to catalog-service
    # directly — every request now goes through platform-gateway, which
    # verifies the caller's token and forwards a derived, trustworthy
    # request to catalog-service on the client's behalf (see client.py's
    # module docstring). Default changed again (2026-09-02, platform-ingress
    # branch) from gateway's old port-forward address
    # (`http://localhost:8080`) to its real Ingress hostname — see
    # src/core/argocd/manifests/gateway.yaml's own Ingress/Certificate
    # resources — now that nothing needs a manual port-forward to reach it.
    gateway_url: str = "https://gateway.platform.local"
    workspace: str = "personal"

    # --- Keycloak Admin API (platform_sdk.keycloak_admin, `platform
    # workspace invite`) --- Separate from gateway_url/workspace above: this
    # talks to Keycloak directly, not through catalog-service or gateway
    # (see catalog-service/app/routers/workspaces.py's own docstring on why
    # membership is Keycloak-group territory, not catalog data). See
    # keycloak_admin.py's module docstring for the full "why a port-forward
    # plus a hostname-resolution patch, and not just a URL" design.
    keycloak_host: str = "keycloak.platform.local"
    keycloak_realm: str = "platform"
    keycloak_client_id: str = "platform-cli"
    # No default, on purpose: KeycloakAdminClient refuses to start rather
    # than silently doing nothing, with an error naming exactly where to get
    # this (bootstrap/keycloak-bootstrap-cli-client.sh's printed `export`
    # line, or its read-back command for a rerun).
    keycloak_client_secret: str | None = None
    # Still needed (2026-09-02, platform-ingress branch) for exactly one
    # thing: extract_platform_ca_cert() in _keycloak_connection.py reads
    # platform-ca-secret's ca.crt via `kubectl get secret`, so
    # KeycloakAdminClient/KeycloakLoginFlow can verify Keycloak's
    # self-signed cert. NOT for a port-forward anymore — keycloak_namespace/
    # keycloak_service_name/keycloak_service_port/keycloak_local_port/
    # keycloak_login_local_port are gone; nothing port-forwards to Keycloak
    # now that keycloak.platform.local resolves for real, off-cluster, via
    # a real Ingress (see src/core/argocd/manifests/keycloak-instance.yaml).
    # Same PATH-under-sudo reasoning as bootstrap/lib/common.sh's own
    # require_cmd comments — spelled out explicitly rather than trusting
    # sudo's secure_path to include /usr/local/bin.
    keycloak_kubectl_cmd: str = "sudo /usr/local/bin/kubectl"

    # --- Keycloak device-flow login (platform_sdk.keycloak_login,
    # `platform login`) --- A separate client from keycloak_client_id above
    # on purpose (see bootstrap/keycloak-bootstrap-login-client.sh's header):
    # that one is confidential/service-account-only and authenticates AS
    # platform-cli itself; this one is public and authenticates AS a real
    # human via the device grant, and must never carry a secret a leaked CLI
    # binary could expose.
    keycloak_login_client_id: str = "platform-cli-login"
