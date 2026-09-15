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
def test_proxy_strips_x_forwarded_headers(sign_proxy_token, mounted_sa):
    # 2026-09-15, live Trino verification: ingress-nginx sets X-Forwarded-For/-Proto/-Host on the
    # request gateway receives; without stripping them here they were being copied onto the brand-new
    # httpx request this route builds (not a continuation of the inbound one) — harmless against
    # hello-module here, but Trino's real coordinator (Airlift/Jetty) 406s any request carrying an
    # X-Forwarded-* header it isn't explicitly configured to trust, which is exactly what live
    # verification hit. Regression test for that fix, not hello-module-specific.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    module_route = respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="ok"))

    with TestClient(app) as client:
        response = client.get(
            f"/modules/hello-module/proxy?token={token}",
            headers={
                "X-Forwarded-For": "203.0.113.1",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "app.platform.local",
                "X-Forwarded-Port": "443",
                "Forwarded": "for=203.0.113.1;proto=https",
            },
        )

    assert response.status_code == 200
    sent = module_route.calls.last.request
    for header in (
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "forwarded",
    ):
        assert header not in sent.headers


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
def test_proxy_rewrites_a_same_host_redirect_back_under_the_proxy_path(sign_proxy_token, mounted_sa):
    # 2026-09-15, live Trino verification: Trino's coordinator 303s `GET /` to `/ui/`, built as the
    # FULL absolute URL it thinks is its own address (http://<its-own-service-dns>/ui/, matching
    # MODULE_BASE's shape here) — confirmed live via DevTools' Network tab, not guessed. Passed
    # through unmodified, the browser tries to navigate the iframe straight to that address, which a
    # real browser can never resolve (it's a cluster-internal-only hostname) — the iframe just goes
    # blank, no visible error anywhere. Regression test for _rewrite_redirect_location, exercised
    # through the real route rather than calling the helper directly.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(
        return_value=httpx.Response(303, headers={"location": f"{MODULE_BASE}/ui/"})
    )

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/modules/hello-module/proxy/ui/"


@respx.mock
def test_proxy_rewrites_a_path_absolute_redirect_too(sign_proxy_token, mounted_sa):
    # A module returning a bare path-absolute Location (no scheme/host) is a different but similarly
    # broken case if left alone: the browser would resolve it against gateway's own origin
    # (https://gateway.platform.local/ui/), a path gateway has no route for at all.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(303, headers={"location": "/ui/"}))

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/modules/hello-module/proxy/ui/"


@respx.mock
def test_proxy_leaves_a_genuinely_external_redirect_untouched(sign_proxy_token, mounted_sa):
    # A module redirecting somewhere that ISN'T itself (an OAuth provider, say) must pass through
    # as-is — rewriting it would send the browser to a broken gateway-local path instead of the real
    # external destination the module actually intended.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/login").mock(
        return_value=httpx.Response(
            303, headers={"location": "https://accounts.example.com/o/oauth2/auth"}
        )
    )

    with TestClient(app) as client:
        response = client.get(
            f"/modules/hello-module/proxy/login?token={token}", follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"] == "https://accounts.example.com/o/oauth2/auth"


class _CloseTrackingStream(httpx.AsyncByteStream):
    """Simulates a REAL network stream in one specific way respx's default mocked responses never
    do: it depends on the underlying connection staying open across multiple reads. respx normally
    hands back a response whose body is already fully materialized in memory, so closing the client
    that "owns" it has no effect on reading it afterward — which is exactly why the real bug below
    could ship unnoticed through this whole test suite for as long as it did. This stream instead
    raises `httpx.ReadError` if iterated after the client has already been closed, making "was
    aclose() called too early" an actual observable test failure instead of something only a real
    socket (or Trino, live) would ever catch.
    """

    def __init__(self, chunks: list[bytes], closed_flag: dict) -> None:
        self._chunks = chunks
        self._closed_flag = closed_flag

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._closed_flag["closed"]:
                raise httpx.ReadError("simulated: read attempted after the client was already closed")
            yield chunk

    async def aclose(self) -> None:
        pass


@respx.mock
def test_proxy_streams_the_full_body_even_when_it_requires_multiple_reads(
    sign_proxy_token, mounted_sa, monkeypatch
):
    # 2026-09-15, live Trino verification: the ORIGINAL shape of this function was
    # `async with httpx.AsyncClient(base_url=target, ...) as client:` with `return response` inside
    # that block. Returning from inside an `async with` runs `__aexit__` — closing the client, and
    # the connection to the module, IMMEDIATELY, before the StreamingResponse's body has actually
    # been read (that only happens later, when Starlette's ASGI layer drives stream_body() after
    # this function has already returned). hello-module's tiny static page always happened to arrive
    # in whatever single chunk httpx buffers before send() returns, so this went unnoticed — Trino's
    # real, gzip-compressed login.html needed a genuine follow-up read, hit the already-closed
    # connection, and failed with httpx.ReadError (confirmed live from gateway's own pod logs),
    # which the browser surfaced as net::ERR_HTTP2_PROTOCOL_ERROR (headers/200 already sent, then
    # the stream died). Regression test: a multi-chunk body whose second/third chunk would only
    # succeed if the client is STILL OPEN at that point.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])

    closed_flag = {"closed": False}
    real_aclose = httpx.AsyncClient.aclose
    real_aexit = httpx.AsyncClient.__aexit__

    def _matches_module_client(self) -> bool:
        # Scoped to the module's OWN client (matched by base_url) — mounted_sa/_mock_k8s's Kubernetes
        # client and the app's JWKS client each open and close their own short-lived AsyncClients too,
        # entirely legitimately, and would otherwise trip this flag before module_proxy's own client
        # ever gets involved.
        return str(self.base_url).rstrip("/") == MODULE_BASE

    async def _tracking_aclose(self):
        if _matches_module_client(self):
            closed_flag["closed"] = True
        await real_aclose(self)

    async def _tracking_aexit(self, *args):
        # `async with httpx.AsyncClient(...) as client:` closes via __aexit__ calling
        # `self._transport.__aexit__(...)` directly — NOT via the public aclose() method above. Both
        # have to be tracked: the ORIGINAL buggy shape (`async with ... return response`) only ever
        # goes through this path, never through aclose() at all, so a hook on aclose() alone would
        # never observe it closing early — silently making this test pass regardless of whether the
        # real bug was present, which is worse than not having the test.
        if _matches_module_client(self):
            closed_flag["closed"] = True
        await real_aexit(self, *args)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", _tracking_aclose)
    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", _tracking_aexit)

    stream = _CloseTrackingStream([b"chunk-one ", b"chunk-two ", b"chunk-three"], closed_flag)
    respx.get(f"{MODULE_BASE}/big.html").mock(
        return_value=httpx.Response(200, stream=stream, headers={"content-type": "text/html"})
    )

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy/big.html?token={token}")

    assert response.status_code == 200
    assert response.text == "chunk-one chunk-two chunk-three"


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
