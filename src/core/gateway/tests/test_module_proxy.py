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
def test_proxy_never_forwards_its_own_cookie_to_the_module_but_does_forward_others(
    sign_proxy_token, mounted_sa
):
    # mp_token_{module_id} is gateway's own internal auth artifact for this one route — it must
    # never reach the module's backend, the same "never client-declared" discipline that already
    # governs Authorization/X-Workspace/X-User/X-Role above. 2026-09-15, live Trino verification:
    # this test USED to also assert the whole Cookie header vanished entirely (blanket-stripping
    # everything, "unrelated" included) — that was itself the bug behind Trino's login redirect
    # loop: Trino's own real session cookie, set on a successful POST /ui/login, got discarded the
    # same way "unrelated" did here, so the very next request looked unauthenticated and bounced
    # back to the login page. A cookie the MODULE itself set legitimately needs to round-trip; only
    # gateway's own token doesn't. "unrelated" stands in for exactly that case now.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    module_route = respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="ok"))

    with TestClient(app) as client:
        client.cookies.set("mp_token_hello-module", token)
        client.cookies.set("unrelated", "should-still-be-forwarded")
        response = client.get("/modules/hello-module/proxy")

    assert response.status_code == 200
    sent = module_route.calls.last.request
    assert "mp_token_hello-module" not in sent.headers["cookie"]
    assert sent.headers["cookie"] == "unrelated=should-still-be-forwarded"


@respx.mock
def test_proxy_omits_cookie_header_entirely_when_nothing_remains_after_stripping(
    sign_proxy_token, mounted_sa
):
    # The common case in practice: the browser's only cookie under this path IS our own token (the
    # module hasn't set one of its own, or this is before it does). Omitting the header entirely
    # here (rather than sending an empty "Cookie:") is _strip_proxy_cookie_from_header's own
    # explicit contract — worth its own assertion, not just inferred from the test above.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    module_route = respx.get(f"{MODULE_BASE}/").mock(return_value=httpx.Response(200, text="ok"))

    with TestClient(app) as client:
        client.cookies.set("mp_token_hello-module", token)
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


@respx.mock
def test_proxy_rewrites_an_explicit_cookie_path_back_under_the_proxy_path(sign_proxy_token, mounted_sa):
    # 2026-09-15, live Trino verification, login-redirect-loop root cause #2 (found AFTER the
    # cookie-forwarding fix above was confirmed deployed, yet the login still looped): Trino's login
    # response sets `Trino-UI-Token=...;Version=1;Path=/ui;HttpOnly` (confirmed live via DevTools'
    # Response Headers) — Trino's own idea of its root, exactly like the Location-header bug above but
    # in Set-Cookie's Path= attribute instead. Per RFC 6265 §5.1.4 the browser matches Path literally
    # against the request path with no proxy-awareness — a request to
    # /modules/hello-module/proxy/ui/... does not start with /ui, so the browser would never reattach
    # this cookie to any follow-up request, silently breaking the session with no error anywhere.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.post(f"{MODULE_BASE}/ui/login").mock(
        return_value=httpx.Response(
            303,
            headers={
                "location": "/ui/",
                "set-cookie": "Trino-UI-Token=abc123;Version=1;Path=/ui;HttpOnly",
            },
        )
    )

    with TestClient(app) as client:
        response = client.post(
            f"/modules/hello-module/proxy/ui/login?token={token}", follow_redirects=False
        )

    assert response.status_code == 303
    set_cookie_values = response.headers.get_list("set-cookie")
    trino_cookie = next(v for v in set_cookie_values if v.startswith("Trino-UI-Token="))
    lowered = trino_cookie.lower()
    assert "path=/modules/hello-module/proxy/ui" in lowered
    assert not lowered.rstrip(";").endswith("path=/ui")


@respx.mock
def test_proxy_leaves_a_cookie_with_no_explicit_path_untouched(sign_proxy_token, mounted_sa):
    # No Path= attribute at all means the browser computes its own default-path from the ACTUAL
    # request URL it used (the proxy's own /modules/hello-module/proxy/... path) — already correctly
    # scoped with zero rewriting needed. Rewriting here anyway would narrow a scope the module never
    # asked for.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(
        return_value=httpx.Response(200, text="ok", headers={"set-cookie": "session=xyz;HttpOnly"})
    )

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 200
    set_cookie_values = response.headers.get_list("set-cookie")
    session_cookie = next(v for v in set_cookie_values if v.startswith("session="))
    assert session_cookie == "session=xyz;HttpOnly"


@respx.mock
def test_proxy_forwards_multiple_set_cookie_headers_from_one_response(sign_proxy_token, mounted_sa):
    # Building response_headers as a plain {key: value} dict from upstream_response.headers.items()
    # would silently keep only the LAST Set-Cookie when a module's response sets more than one in the
    # same response (a real, RFC 6265 §3-legal shape — a session cookie and a CSRF cookie together,
    # say) — no error anywhere, the earlier one(s) would just never reach the browser. Regression test
    # for handling set-cookie via multi_items()/response.headers.append() instead of a dict merge.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.get(f"{MODULE_BASE}/").mock(
        return_value=httpx.Response(
            200,
            text="ok",
            headers=[
                ("set-cookie", "first=1;Path=/ui"),
                ("set-cookie", "second=2;HttpOnly"),
            ],
        )
    )

    with TestClient(app) as client:
        response = client.get(f"/modules/hello-module/proxy?token={token}")

    assert response.status_code == 200
    set_cookie_values = response.headers.get_list("set-cookie")
    assert any(v.startswith("first=1") for v in set_cookie_values)
    assert any(v.startswith("second=2") for v in set_cookie_values)
    # Own module-proxy cookie (_set_proxy_cookie) must still be set too — three total.
    assert any(v.startswith("mp_token_hello-module=") for v in set_cookie_values)
    assert len(set_cookie_values) == 3


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


@respx.mock
def test_a_module_set_session_cookie_survives_round_trip_across_two_requests(sign_proxy_token, mounted_sa):
    # End-to-end mirror of the exact live failure this branch fixes: Trino's coordinator sets its own
    # session cookie on a successful POST /ui/login, then rejects the VERY NEXT request (GET /ui/) as
    # unauthenticated because gateway was discarding that cookie before forwarding it back — bouncing
    # the browser straight back to the login page, indistinguishable from a genuinely rejected login
    # without checking gateway's own logs. Two real requests through the real ASGI app (not a direct
    # call to the helper), a real cookie jar (TestClient's own, same as a browser's), proving the
    # SECOND request actually carries the module's session cookie forward — not just that the helper
    # function computes the right string in isolation.
    token = sign_proxy_token("hello-module")
    _mock_k8s([_application("hello-module")])
    respx.post(f"{MODULE_BASE}/login").mock(
        return_value=httpx.Response(
            200, text="logged in", headers={"set-cookie": "module-session=real-session-abc123; Path=/"}
        )
    )
    dashboard_route = respx.get(f"{MODULE_BASE}/dashboard").mock(
        return_value=httpx.Response(200, text="welcome back")
    )

    with TestClient(app) as client:
        login_response = client.post(f"/modules/hello-module/proxy/login?token={token}")
        assert login_response.status_code == 200
        assert "module-session=real-session-abc123" in login_response.headers["set-cookie"]

        # A real browser would now automatically be carrying BOTH cookies (our own mp_token_ one,
        # set on this same response, and the module's module-session just received) on its next
        # request. Set them explicitly here rather than relying on TestClient's cookie jar to honor
        # `_set_proxy_cookie`'s `Secure` flag over its plain (non-HTTPS) test transport — the same
        # reason this file's OTHER cookie tests already set cookies by hand instead of letting a
        # Set-Cookie response header propagate automatically.
        client.cookies.set("mp_token_hello-module", token)
        client.cookies.set("module-session", "real-session-abc123")
        dashboard_response = client.get("/modules/hello-module/proxy/dashboard")

    assert dashboard_response.status_code == 200
    sent = dashboard_route.calls.last.request
    assert "module-session=real-session-abc123" in sent.headers["cookie"]
    assert "mp_token_hello-module" not in sent.headers["cookie"]
