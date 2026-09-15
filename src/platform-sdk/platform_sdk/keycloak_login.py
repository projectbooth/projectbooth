"""Device Authorization Grant (RFC 8628) login flow — `platform login`'s
entire implementation. Gets a real Keycloak-issued token for a real human
without this CLI ever seeing or handling their password: the user approves
the login in their own browser, on Keycloak's own hosted device-verification
page. That's exactly the point of this grant type existing — built for
input-constrained/headless devices, but just as good a fit for "a CLI
running over SSH with no easy way to catch an OAuth redirect."

Talks to the `platform-cli-login` client
bootstrap/keycloak-bootstrap-login-client.sh sets up — a PUBLIC client (no
secret; see that script's header for why a public client is the right shape
here, unlike KeycloakAdminClient's confidential service-account one).

Connects directly to `https://{host}` (2026-09-02, platform-ingress
branch) — `keycloak.platform.local` now resolves for real, off-cluster, via
a real `Ingress` plus a per-device `/etc/hosts` entry, the same way any
browser reaches it, so the verification URL Keycloak hands back is directly
openable with no port-forward involved at all. Reuses
`extract_platform_ca_cert()` from `_keycloak_connection.py` — same as
`KeycloakAdminClient` — to verify Keycloak's self-signed cert; see that
module's docstring for what this replaced (a `kubectl port-forward` plus a
`socket.getaddrinfo` patch, needed only because nothing made
`keycloak.platform.local` actually resolve before this branch).

Poll loop follows RFC 8628 §3.5 exactly:
  - `authorization_pending` — keep polling at `interval` seconds.
  - `slow_down` — back off by adding 5 seconds to `interval` (the RFC's own
    number, not a made-up constant) and keep polling.
  - `access_denied` — the user clicked "Deny" in the browser; stop, raise
    PlatformLoginError.
  - `expired_token` — the device code's own `expires_in` elapsed before
    approval; stop, raise PlatformLoginError. This module also enforces that
    deadline itself (not just relying on Keycloak eventually saying so), so
    a network hiccup right at the boundary can't leave the poll loop
    spinning forever.
  - anything else — unexpected; stop, raise PlatformLoginError naming it
    rather than silently retrying something RFC 8628 doesn't define.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from platform_sdk._keycloak_connection import cleanup_ca_cert, extract_platform_ca_cert
from platform_sdk.config import SDKSettings
from platform_sdk.exceptions import PlatformLoginError
from platform_sdk.models import TokenSet


@dataclass
class DeviceAuthorization:
    """What the device-authorization endpoint returned. `platform login`
    prints `verification_uri_complete` when present (it embeds the user
    code, so it's a single click) and falls back to `verification_uri` +
    `user_code` (typed in by hand) when it isn't — RFC 8628 makes
    `verification_uri_complete` optional, Keycloak includes it in practice,
    but nothing here assumes that without checking."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


class KeycloakLoginFlow:
    """Constructed and used the same one-shot way `KeycloakAdminClient` is
    — see that class's own constructor comment for the explicit-arg >
    settings precedence this mirrors. Deliberately NOT built on top of
    PlatformClient/exposed via `ctx.obj` the way most platform-cli commands
    share one client instance (see `platform_cli/login.py`'s own comment):
    logging in is what *creates* the credentials PlatformClient later reads,
    so it can't depend on PlatformClient already having valid ones.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        realm: str | None = None,
        client_id: str | None = None,
        kubectl_cmd: str | None = None,
        settings: SDKSettings | None = None,
        timeout: float = 10.0,
        _client: httpx.Client | None = None,
    ) -> None:
        settings = settings or SDKSettings()
        self._host = host or settings.keycloak_host
        self._realm = realm or settings.keycloak_realm
        self._client_id = client_id or settings.keycloak_login_client_id
        # kubectl is still needed for exactly one thing now — reading
        # platform-ca-secret's ca.crt (extract_platform_ca_cert(), below) —
        # not for a port-forward, since 2026-09-02's platform-ingress branch.
        self._kubectl_cmd = kubectl_cmd or settings.keycloak_kubectl_cmd
        self._timeout = timeout
        # Same private test-only escape hatch as KeycloakAdminClient's own
        # `_client` arg — see that class's constructor comment.
        self._external_client = _client is not None
        self._client = _client
        self._ca_cert_path: str | None = None

    def __enter__(self) -> KeycloakLoginFlow:
        if self._client is None:
            self._ca_cert_path = extract_platform_ca_cert(self._kubectl_cmd)
            self._client = httpx.Client(
                base_url=f"https://{self._host}",
                verify=self._ca_cert_path,
                timeout=self._timeout,
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        if not self._external_client and self._client is not None:
            self._client.close()
            self._client = None
        cleanup_ca_cert(self._ca_cert_path)
        self._ca_cert_path = None

    # ---- step 1: get a device code -------------------------------------
    def start_device_authorization(self) -> DeviceAuthorization:
        response = self._client.post(
            f"/realms/{self._realm}/protocol/openid-connect/auth/device",
            data={"client_id": self._client_id},
        )
        if response.status_code >= 400:
            raise PlatformLoginError(
                f"Device-authorization request failed: {response.status_code} {response.text}"
            )
        body = response.json()
        try:
            return DeviceAuthorization(
                device_code=body["device_code"],
                user_code=body["user_code"],
                verification_uri=body["verification_uri"],
                verification_uri_complete=body.get("verification_uri_complete"),
                expires_in=int(body["expires_in"]),
                interval=int(body.get("interval", 5)),
            )
        except KeyError as exc:
            raise PlatformLoginError(
                f"Device-authorization response was missing an expected field: {body}"
            ) from exc

    # ---- step 2: poll until the user approves (or doesn't) -------------
    def poll_for_token(self, device_auth: DeviceAuthorization) -> TokenSet:
        interval = device_auth.interval
        deadline = time.monotonic() + device_auth.expires_in
        while True:
            if time.monotonic() >= deadline:
                raise PlatformLoginError(
                    "Device code expired before the login was approved — run `platform login` again."
                )
            time.sleep(interval)

            response = self._client.post(
                f"/realms/{self._realm}/protocol/openid-connect/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_auth.device_code,
                    "client_id": self._client_id,
                },
            )
            if response.status_code == 200:
                return _token_set_from_response(response.json())

            try:
                body = response.json()
            except ValueError:  # body wasn't JSON at all
                body = {}
            error = body.get("error", "")

            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5  # RFC 8628 §3.5's own number, not a guess
                continue
            if error == "access_denied":
                raise PlatformLoginError("Login was denied in the browser.")
            if error == "expired_token":
                raise PlatformLoginError(
                    "Device code expired before the login was approved — run `platform login` again."
                )
            raise PlatformLoginError(
                f"Unexpected response from the token endpoint: {response.status_code} {response.text}"
            )

    # ---- used by PlatformClient's near-expiry check, not by `platform
    # login` itself (a fresh login always goes through the device flow
    # above) ---------------------------------------------------------------
    def refresh(self, refresh_token: str) -> TokenSet:
        response = self._client.post(
            f"/realms/{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
            },
        )
        if response.status_code >= 400:
            raise PlatformLoginError(
                f"Refreshing the access token failed: {response.status_code} {response.text} — "
                "run `platform login` again."
            )
        return _token_set_from_response(response.json())


def _token_set_from_response(body: dict) -> TokenSet:
    access_token = body.get("access_token")
    if not access_token:
        raise PlatformLoginError(f"Token response had no access_token: {body}")
    expires_in = int(body.get("expires_in", 300))
    return TokenSet(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        preferred_username=_extract_preferred_username(body.get("id_token")),
    )


def _extract_preferred_username(id_token: str | None) -> str | None:
    """Pulls `preferred_username` out of the ID token's payload WITHOUT
    verifying its signature — safe here because this is the exact token
    Keycloak just handed back, over a TLS connection this process itself
    authenticated (the CA-pinned httpx.Client built in `__enter__`), not an
    externally-supplied token being trusted for an access-control decision.
    This value is purely cosmetic (`platform login` printing "logged in as
    alice"); nothing anywhere uses it to authorize anything — every real
    authorization decision happens server-side, in gateway, which verifies
    the access_token's signature for real before trusting any claim in it
    (see src/core/gateway/app/auth.py).
    """
    if not id_token:
        return None
    try:
        # JWT = header.payload.signature, each base64url-encoded without
        # padding — the standard "decode the middle segment" trick for
        # reading claims out of a token you already trust the origin of.
        payload_segment = id_token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        return payload.get("preferred_username")
    except Exception:
        # Cosmetic-only value — never let a parsing hiccup here break login.
        return None
