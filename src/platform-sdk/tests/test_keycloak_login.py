"""Unit tests for KeycloakLoginFlow — same transport-mocked-via-respx
approach test_keycloak_admin.py uses for KeycloakAdminClient, and for the
same reason: this suite's job is "does the device-flow poll loop follow
RFC 8628 correctly," not "does a live Keycloak/kubectl port-forward work"
(not unit-testable without a live cluster — see test_keycloak_admin.py's own
docstring).

Every test here constructs KeycloakLoginFlow with `_client=` already set to
a real httpx.Client pointed at a fake base_url, bypassing
`extract_platform_ca_cert()` entirely — the same private test-only escape
hatch KeycloakAdminClient's own tests use.

`time.sleep` is monkeypatched to a no-op in every test that exercises the
poll loop, so these tests run in milliseconds regardless of what `interval`
Keycloak's mocked response specifies — nothing here is actually waiting on a
wall-clock interval, and asserting real sleep durations would just make the
suite slow for no test-correctness benefit.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from platform_sdk import KeycloakLoginFlow, PlatformLoginError
from platform_sdk.keycloak_login import DeviceAuthorization

BASE_URL = "https://keycloak.test"
REALM = "platform"
CLIENT_ID = "platform-cli-login"


def _flow(**overrides) -> KeycloakLoginFlow:
    kwargs = {
        "host": "keycloak.test",
        "realm": REALM,
        "client_id": CLIENT_ID,
        "_client": httpx.Client(base_url=BASE_URL),
    }
    kwargs.update(overrides)
    return KeycloakLoginFlow(**kwargs)


def _fake_id_token(preferred_username: str = "alice") -> str:
    # header.payload.signature — only the payload is ever read by
    # _extract_preferred_username, and it's read unverified (see that
    # function's docstring for why that's fine here), so header/signature
    # can be anything base64url-ish.
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": preferred_username}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("platform_sdk.keycloak_login.time.sleep", lambda _seconds: None)


@respx.mock
def test_start_device_authorization_parses_full_response():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/auth/device").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://keycloak.test/device",
                "verification_uri_complete": "https://keycloak.test/device?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 5,
            },
        )
    )
    with _flow() as flow:
        result = flow.start_device_authorization()

    assert result == DeviceAuthorization(
        device_code="dc-1",
        user_code="ABCD-EFGH",
        verification_uri="https://keycloak.test/device",
        verification_uri_complete="https://keycloak.test/device?user_code=ABCD-EFGH",
        expires_in=600,
        interval=5,
    )


@respx.mock
def test_start_device_authorization_missing_field_raises_clear_error():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/auth/device").mock(
        return_value=httpx.Response(200, json={"device_code": "dc-1"})
    )
    with _flow() as flow, pytest.raises(PlatformLoginError, match="missing an expected field"):
        flow.start_device_authorization()


@respx.mock
def test_start_device_authorization_http_error_raises():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/auth/device").mock(
        return_value=httpx.Response(400, json={"error": "invalid_client"})
    )
    with _flow() as flow, pytest.raises(PlatformLoginError):
        flow.start_device_authorization()


def _device_auth(expires_in: int = 600, interval: int = 5) -> DeviceAuthorization:
    return DeviceAuthorization(
        device_code="dc-1",
        user_code="ABCD-EFGH",
        verification_uri="https://keycloak.test/device",
        verification_uri_complete="https://keycloak.test/device?user_code=ABCD-EFGH",
        expires_in=expires_in,
        interval=interval,
    )


@respx.mock
def test_poll_pending_then_success_returns_token_set_with_expiry_and_username():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 300,
                    "id_token": _fake_id_token("alice"),
                },
            ),
        ]
    )
    with _flow() as flow:
        token_set = flow.poll_for_token(_device_auth())

    assert token_set.access_token == "at-1"
    assert token_set.refresh_token == "rt-1"
    assert token_set.preferred_username == "alice"
    assert token_set.expires_at is not None


@respx.mock
def test_poll_slow_down_then_success():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        side_effect=[
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 300}),
        ]
    )
    with _flow() as flow:
        token_set = flow.poll_for_token(_device_auth())

    assert token_set.access_token == "at-1"
    # No id_token in this response — preferred_username stays None, not an error.
    assert token_set.preferred_username is None


@respx.mock
def test_poll_access_denied_raises():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(400, json={"error": "access_denied"})
    )
    with _flow() as flow, pytest.raises(PlatformLoginError, match="denied"):
        flow.poll_for_token(_device_auth())


@respx.mock
def test_poll_expired_token_error_from_server_raises():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(400, json={"error": "expired_token"})
    )
    with _flow() as flow, pytest.raises(PlatformLoginError, match="expired"):
        flow.poll_for_token(_device_auth())


@respx.mock
def test_poll_unexpected_error_raises():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with _flow() as flow, pytest.raises(PlatformLoginError, match="Unexpected response"):
        flow.poll_for_token(_device_auth())


def test_poll_local_deadline_expires_without_any_request():
    # expires_in=0 means the local deadline check fires before the loop
    # ever makes an HTTP call — no respx mock registered at all, so any
    # attempt to actually call out would fail loudly rather than silently
    # matching an unintended default.
    with _flow() as flow, pytest.raises(PlatformLoginError, match="expired"):
        flow.poll_for_token(_device_auth(expires_in=0))


@respx.mock
def test_refresh_success():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 300}
        )
    )
    with _flow() as flow:
        token_set = flow.refresh("rt-1")

    assert token_set.access_token == "at-2"
    assert token_set.refresh_token == "rt-2"


@respx.mock
def test_refresh_failure_raises():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with _flow() as flow, pytest.raises(PlatformLoginError, match="run `platform login` again"):
        flow.refresh("rt-1")
