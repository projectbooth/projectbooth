"""Integration tests for GET /modules/{id}/proxy-token and GET|POST|.../modules/{id}/proxy[/{path}],
exercised through FastAPI's TestClient — same shape as test_proxy.py/test_modules.py: the real ASGI
app, real lifespan, respx intercepting the Keycloak JWKS endpoint, the Kubernetes API (`mounted_sa`
points at), and here also the module's own backend (`MODULE_BASE`). No live cluster needed.

module_proxy_secret/sign_proxy_token live in conftest.py, shared with any future test that needs to
hand-craft a module-proxy token directly rather than going through the mint endpoint (expired, wrong
module_id, wrong purpose — cases the mint endpoint itself could never produce, so they have to be
hand-signed to exercise _decode_proxy_token()'s own rejection paths).
"""
from __future__ import annotations

import httpx
import jwt
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

KEYCLOAK_BASE = "https://keycloak.test"
JWKS_PATH = "/realms/platform/protocol/openid-connect/certs"
MODULE_BASE = "http://module.test"


@pytest.fixture(autouse=True)
def _point_settings_at_test_backends(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_internal_url", KEYCLOAK_BASE)
    monkeypatch.setattr(settings, "keycloak_public_url", KEYCLOAK_BASE)


@pytest.fixture
def auth_header(sign_token):
    return {"Authorization": f"Bearer {sign_token()}"}


def _mock_jwks(jwk_dict):
    return respx.get(f"{KEYCLOAK_BASE}{JWKS_PATH}").mock(
        return_value=httpx.Response(200, json={"keys": [jwk_dict]})
    )


def _k8s_url() -> str:
    return (
        f"{settings.k8s_api_url}/apis/argoproj.io/v1alpha1/namespaces/"
        f"{settings.argocd_namespace}/applications"
    )


def _mock_k8s(items: list[dict]):
    return respx.get(_k8s_url()).mock(return_value=httpx.Response(200, json={"items": items}))


def _application(name: str, proxy_to: str | None = MODULE_BASE) -> dict:
    annotations = {"platform.io/proxy-to": proxy_to} if proxy_to is not None else {}
    return {
        "metadata": {"name": name, "annotations": annotations},
        "status": {"health": {"status": "Healthy"}},
    }


# --- GET /modules/{id}/proxy-token -------------------------------------------------------------


@respx.mock
def test_mint_returns_401_for_missing_authorization(mounted_sa):
    with TestClient(app) as client:
        response = client.get("/modules/hello-module/proxy-token", headers={"X-Workspace": "personal"})
    assert response.status_code == 401


@respx.mock
def test_mint_returns_403_for_no_matching_workspace_membership(jwk_dict, sign_token, mounted_sa):
    _mock_jwks(jwk_dict)
    token = sign_token({"groups": ["/workspaces/other-workspace/viewer"]})
    with TestClient(app) as client:
        response = client.get(
            "/modules/hello-module/proxy-token",
            headers={"Authorization": f"Bearer {token}", "X-Workspace": "personal"},
        )
    assert response.status_code == 403


@respx.mock
def test_mint_returns_404_when_not_installed(jwk_dict, auth_header, mounted_sa):
    _mock_jwks(jwk_dict)
    _mock_k8s([])
    with TestClient(app) as client:
        response = client.get(
            "/modules/hello-module/proxy-token", headers={**auth_header, "X-Workspace": "personal"}
        )
    assert response.status_code == 404


@respx.mock
def test_mint_returns_404_when_installed_but_no_proxy_to_annotation(jwk_dict, auth_header, mounted_sa):
    # A module installed before feature/module-proxy merged, not yet reinstalled.
    _mock_jwks(jwk_dict)
    _mock_k8s([_application("hello-module", proxy_to=None)])
    with TestClient(app) as client:
        response = client.get(
            "/modules/hello-module/proxy-token", headers={**auth_header, "X-Workspace": "personal"}
        )
    assert response.status_code == 404


@respx.mock
def test_mint_returns_503_when_secret_is_not_configured(jwk_dict, auth_header, mounted_sa):
    # module_proxy_secret fixture deliberately NOT requested here — settings.module_proxy_token_secret
    # stays at its blank default, exercising the real "not configured yet" path.
    _mock_jwks(jwk_dict)
    _mock_k8s([_application("hello-module")])
    with TestClient(app) as client:
        response = client.get(
            "/modules/hello-module/proxy-token", headers={**auth_header, "X-Workspace": "personal"}
        )
    assert response.status_code == 503


@respx.mock
def test_mint_returns_a_token_carrying_the_verified_identity(
    jwk_dict, auth_header, mounted_sa, module_proxy_secret
):
    _mock_jwks(jwk_dict)
    _mock_k8s([_application("hello-module")])
    with TestClient(app) as client:
        response = client.get(
            "/modules/hello-module/proxy-token", headers={**auth_header, "X-Workspace": "personal"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["module_id"] == "hello-module"
    assert body["expires_in"] == settings.module_proxy_token_ttl_seconds

    claims = jwt.decode(body["token"], key=module_proxy_secret, algorithms=["HS256"])
    assert claims["module_id"] == "hello-module"
    assert claims["workspace"] == "personal"
    assert claims["user"] == "alice"  # sign_token's default preferred_username
    assert claims["role"] == "editor"  # sign_token's default groups claim
    assert claims["purpose"] == "module-proxy"
    assert claims["exp"] - claims["iat"] == settings.module_proxy_token_ttl_seconds


# --- GET|POST|.../modules/{id}/proxy[/{path}] ---------------------------------------------------


@respx.mock
def test_proxy_returns_401_for_missing_token(mounted_sa):
    with TestClient(app) as client:
        response = client.get("/modules/hello-module/proxy/")
    assert response.status_code == 401


@respx.mock
def test_proxy_returns_401_for_an_expired_token(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module", expires_in=-10)
    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy/?token={token}")
    assert response.status_code == 401


@respx.mock
def test_proxy_returns_403_for_a_token_minted_for_a_different_module(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("other-module")
    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy/?token={token}")
    assert response.status_code == 403


@respx.mock
def test_proxy_returns_403_for_a_token_with_the_wrong_purpose(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module", purpose="something-else")
    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy/?token={token}")
    assert response.status_code == 403


@respx.mock
def test_proxy_returns_404_when_module_was_uninstalled_since_the_token_was_minted(
    sign_proxy_token, mounted_sa
):
    token = sign_proxy_token("hello-module")
    _mock_k8s([])  # no longer installed
    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy/?token={token}")
    assert response.status_code == 404


@respx.mock
def test_proxy_streams_the_module_response_through(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/index.html").mock(
        return_value=httpx.Response(200, text="<html>hello</html>", headers={"content-type": "text/html"})
    )

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy/index.html?token={token}")

    assert response.status_code == 200
    assert response.text == "<html>hello</html>"
    assert response.headers["content-type"] == "text/html"


@respx.mock
def test_proxy_root_path_reaches_the_module_root(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="root"))

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 200
    assert response.text == "root"


@respx.mock
def test_proxy_forwards_headers_derived_from_the_token_not_the_request(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module", workspace="personal", user="alice", role="editor")
    _mock_k8s([_application("hello-module")])
    module_route = respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="ok"))

    with TestClient(app) as client:
        response = client.get(
            f"/modules/hello-module/proxy?token={token}",
            headers={"X-Workspace": "mallory-workspace", "X-User": "mallory", "X-Role": "owner"},
        )

    assert response.status_code == 200
    sent = module_route.calls.last.request
    assert sent.headers["x-workspace"] == "personal"
    assert sent.headers["x-user"] == "alice"
    assert sent.headers["x-role"] == "editor"


@respx.mock
def test_proxy_strips_the_token_query_param_but_forwards_others(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    module_route = respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="ok"))

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}&tab=settings")

    assert response.status_code == 200
    sent = module_route.calls.last.request
    assert "token" not in sent.url.params
    assert sent.url.params["tab"] == "settings"


@respx.mock
def test_proxy_sets_a_path_scoped_cookie_on_success(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="root"))

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"mp_token_hello-module={token}")
    # Case-insensitive substring checks rather than a full Set-Cookie parse — this is asserting the
    # attributes _set_proxy_cookie actually asked for are present, not re-deriving http.cookies'
    # own exact capitalization convention.
    lowered = set_cookie.lower()
    assert "path=/modules/hello-module/proxy" in lowered
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=none" in lowered


@respx.mock
def test_proxy_accepts_the_token_from_the_cookie_with_no_query_param(sign_proxy_token, mounted_sa):
    # Simulates the case this cookie exists for: a relative <script src>/fetch() a module's own page
    # issues after the initial iframe load, which carries no ?token= at all.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/app.js").mock(
        return_value=httpx.Response(
            200, text="console.log(1)", headers={"content-type": "application/javascript"}
        )
    )

    with TestClient(app) as client:
        # Set on the client's own cookie jar rather than passed per-request — the latter is deprecated
        # on recent httpx/starlette (ambiguous persistence semantics), and this test wants exactly the
        # jar behavior anyway: a real browser's cookie jar is what actually carries this cookie on a
        # module's own follow-up request in production.
        client.cookies.set("mp_token_hello-module", token)
        response = client.get("/modules/hello-module/proxy/app.js")

    assert response.status_code == 200
    assert response.text == "console.log(1)"


@respx.mock
def test_proxy_never_forwards_its_own_cookie_to_the_module(sign_proxy_token, mounted_sa):
    # The Cookie header is gateway's own internal auth artifact for this one route — it must never
    # reach the module's backend, the same "never client-declared" discipline that already governs
    # Authorization/X-Workspace/X-User/X-Role above.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    module_route = respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="ok"))

    with TestClient(app) as client:
        client.cookies.set("mp_token_hello-module", token)
        client.cookies.set("unrelated", "should-not-leak-either")
        response = client.get("/modules/hello-module/proxy")

    assert response.status_code == 200
    sent = module_route.calls.last.request
    assert "cookie" not in sent.headers


@respx.mock
def test_proxy_strips_x_frame_options_and_frame_ancestors(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(
        return_value=httpx.Response(
            200,
            text="ok",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "default-src 'self'; frame-ancestors 'self'",
            },
        )
    )

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 200
    assert "x-frame-options" not in response.headers
    assert "frame-ancestors" not in response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in response.headers["content-security-policy"]


@respx.mock
def test_proxy_returns_502_when_module_is_unreachable(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(side_effect=httpx.ConnectError("connection refused"))

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 502


@respx.mock
def test_proxy_returns_504_when_module_times_out(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(side_effect=httpx.TimeoutException("timed out"))

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 504


@respx.mock
def test_proxy_streams_a_post_body_through(sign_proxy_token, mounted_sa):
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    module_route = respx.post(f"{MODULE_BASE}/submit").mock(return_value=httpx.Response(201, text="created"))

    with TestClient(app) as client:
        response = client.post(f"/modules/hello-module/proxy/submit?token={token}", json={"a": 1})

    assert response.status_code == 201
    # httpx's own json= encoding uses compact separators (no space after ":") — this is a passthrough
    # check (module_proxy.py forwards request.body() raw, unmodified), not a claim about JSON style.
    assert module_route.calls.last.request.content == b'{"a":1}'
