"""Integration tests for the catch-all proxy route, exercised through
FastAPI's TestClient — so main.py's real `lifespan` runs and the real
`httpx.AsyncClient`s app/proxy.py and app/jwks.py use get constructed
exactly the way they would in production, with respx intercepting both the
Keycloak JWKS endpoint and catalog-service's base URL. No live cluster
needed. See app/main.py's module docstring / this package's README for why
this is the level these tests operate at (real ASGI app, mocked HTTP
transport) rather than calling proxy() directly.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

KEYCLOAK_BASE = "https://keycloak.test"
CATALOG_BASE = "http://catalog.test"
JWKS_PATH = "/realms/platform/protocol/openid-connect/certs"


@pytest.fixture(autouse=True)
def _point_settings_at_test_backends(monkeypatch):
    # settings is a module-level singleton (app/config.py) that main.py's
    # lifespan reads fresh at app-startup time — monkeypatch.setattr
    # reverts these automatically after each test, so nothing here leaks
    # into test_auth.py/test_jwks.py even though they share the same
    # settings object within one pytest session.
    monkeypatch.setattr(settings, "keycloak_internal_url", KEYCLOAK_BASE)
    # so expected_issuer matches what sign_token puts in `iss`
    monkeypatch.setattr(settings, "keycloak_public_url", KEYCLOAK_BASE)
    monkeypatch.setattr(settings, "catalog_service_url", CATALOG_BASE)


@pytest.fixture
def auth_header(sign_token):
    return {"Authorization": f"Bearer {sign_token()}"}


def _mock_jwks(jwk_dict):
    return respx.get(f"{KEYCLOAK_BASE}{JWKS_PATH}").mock(
        return_value=httpx.Response(200, json={"keys": [jwk_dict]})
    )


@respx.mock
def test_proxy_forwards_gateway_derived_headers_and_ignores_client_supplied_ones(jwk_dict, auth_header):
    _mock_jwks(jwk_dict)
    catalog_route = respx.get(f"{CATALOG_BASE}/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "workspace_id": "x",
                "workspace_name": "personal",
                "user_id": "alice",
                "role": "editor",
            },
        )
    )

    with TestClient(app) as client:
        response = client.get(
            "/me",
            headers={
                **auth_header,
                "X-Workspace": "personal",
                "X-User": "mallory",  # must never reach catalog-service
                "X-Role": "owner",  # same
            },
        )

    assert response.status_code == 200
    sent = catalog_route.calls.last.request
    assert sent.headers["x-workspace"] == "personal"
    assert sent.headers["x-user"] == "alice"  # from the verified TOKEN, not the "mallory" header above
    assert sent.headers["x-role"] == "editor"  # derived from the groups claim, not the "owner" header above


@respx.mock
def test_proxy_returns_401_for_missing_authorization(jwk_dict):
    _mock_jwks(jwk_dict)
    with TestClient(app) as client:
        response = client.get("/me", headers={"X-Workspace": "personal"})
    assert response.status_code == 401


@respx.mock
def test_proxy_returns_403_for_no_matching_workspace_membership(jwk_dict, sign_token):
    _mock_jwks(jwk_dict)
    token = sign_token({"groups": ["/workspaces/other-workspace/viewer"]})
    with TestClient(app) as client:
        response = client.get("/me", headers={"Authorization": f"Bearer {token}", "X-Workspace": "personal"})
    assert response.status_code == 403


@respx.mock
def test_proxy_returns_400_for_missing_workspace_hint(jwk_dict, auth_header):
    _mock_jwks(jwk_dict)
    with TestClient(app) as client:
        response = client.get("/me", headers=auth_header)
    assert response.status_code == 400


@respx.mock
def test_proxy_streams_a_post_body_and_status_code_through(jwk_dict, auth_header):
    _mock_jwks(jwk_dict)
    respx.post(f"{CATALOG_BASE}/datasets").mock(
        return_value=httpx.Response(201, json={"id": "d1", "name": "reddit-sentiment"})
    )

    with TestClient(app) as client:
        response = client.post(
            "/datasets", json={"name": "reddit-sentiment"}, headers={**auth_header, "X-Workspace": "personal"}
        )

    assert response.status_code == 201
    assert response.json() == {"id": "d1", "name": "reddit-sentiment"}


@respx.mock
def test_proxy_returns_502_when_catalog_service_is_unreachable(jwk_dict, auth_header):
    _mock_jwks(jwk_dict)
    respx.get(f"{CATALOG_BASE}/me").mock(side_effect=httpx.ConnectError("connection refused"))

    with TestClient(app) as client:
        response = client.get("/me", headers={**auth_header, "X-Workspace": "personal"})

    assert response.status_code == 502


@respx.mock
def test_proxy_returns_504_when_catalog_service_times_out(jwk_dict, auth_header):
    _mock_jwks(jwk_dict)
    respx.get(f"{CATALOG_BASE}/me").mock(side_effect=httpx.TimeoutException("timed out"))

    with TestClient(app) as client:
        response = client.get("/me", headers={**auth_header, "X-Workspace": "personal"})

    assert response.status_code == 504


def test_healthz_requires_no_auth_and_does_not_touch_catalog_service():
    # No respx mocks registered at all — if this route accidentally fell
    # through to the proxy's catch-all, any real HTTP attempt would error
    # loudly rather than silently succeeding.
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# 2026-09-16 (Trino live-verification): regression tests for _module_proxy_redirect_target, exercised
# through the real route the same way every other proxy.py behavior in this file is — see that
# function's own module-level comment in proxy.py for the full story (Trino's bundled Web UI calls
# fetch("/ui/api/cluster") etc., which escapes module_proxy.py's own route entirely and lands here).


@respx.mock
def test_proxy_redirects_a_referer_scoped_unauthenticated_request_into_the_module_proxy_route(jwk_dict):
    _mock_jwks(jwk_dict)  # registered but must never be called — no Authorization header on this request
    with TestClient(app) as client:
        response = client.get(
            "/ui/api/cluster",
            headers={"Referer": "https://gateway.platform.local/modules/trino/proxy/ui/"},
            follow_redirects=False,
        )
    assert response.status_code == 307
    assert response.headers["location"] == "/modules/trino/proxy/ui/api/cluster"


@respx.mock
def test_proxy_redirect_preserves_the_query_string(jwk_dict):
    _mock_jwks(jwk_dict)
    with TestClient(app) as client:
        response = client.get(
            "/ui/api/query?queryId=abc123",
            headers={"Referer": "https://gateway.platform.local/modules/trino/proxy/ui/"},
            follow_redirects=False,
        )
    assert response.status_code == 307
    assert response.headers["location"] == "/modules/trino/proxy/ui/api/query?queryId=abc123"


@respx.mock
def test_proxy_does_not_redirect_when_authorization_header_is_present(jwk_dict, auth_header):
    # A real, deliberate catalog-service call that just happens to carry a stale Referer from a
    # previously-viewed module-proxy page must go through the normal path untouched, not get
    # redirected into a module's own UI.
    _mock_jwks(jwk_dict)
    catalog_route = respx.get(f"{CATALOG_BASE}/ui/api/cluster").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with TestClient(app) as client:
        response = client.get(
            "/ui/api/cluster",
            headers={
                **auth_header,
                "X-Workspace": "personal",
                "Referer": "https://gateway.platform.local/modules/trino/proxy/ui/",
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert catalog_route.called


@respx.mock
def test_proxy_does_not_redirect_without_a_referer_header(jwk_dict):
    # No Authorization AND no Referer at all — nothing here points at a module-proxy origin, so this
    # must fall through to the ordinary "missing Authorization" 401, not a redirect to nowhere.
    _mock_jwks(jwk_dict)
    with TestClient(app) as client:
        response = client.get("/ui/api/cluster", follow_redirects=False)
    assert response.status_code == 401


@respx.mock
def test_proxy_does_not_redirect_for_a_referer_outside_any_module_proxy_path(jwk_dict):
    _mock_jwks(jwk_dict)
    with TestClient(app) as client:
        response = client.get(
            "/ui/api/cluster",
            headers={"Referer": "https://gateway.platform.local/some/other/page"},
            follow_redirects=False,
        )
    assert response.status_code == 401
