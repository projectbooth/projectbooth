"""ui-shell-plan.md item 8 (feature/module-proxy, 2026-09-10) — reverse-proxying into a module's own
UI, embedded as an iframe on ui-shell's module detail page. Two routes, both nested under one reserved
`proxy` path segment so a module's own arbitrary UI paths can never collide with a gateway-reserved one
(a bare `/modules/{id}/{path:path}` catch-all, the shape ui-shell-plan.md's own backlog text naively
suggested, risks exactly that — a module whose own page happens to route to e.g. `/install` would
otherwise shadow `app/modules.py`'s real endpoint):

- `GET /modules/{module_id}/proxy-token` — mint a short-lived, module-scoped proxy token.
- `GET|POST|PUT|PATCH|DELETE /modules/{module_id}/proxy[/{path:path}]` — the actual reverse proxy,
  authorized by that token.

Why a token at all, not just require_auth() the way every other route here does: a plain browser
navigation (an `<iframe src="...">`, which is how ui-shell renders a module's UI — see this branch's
plan, decision 2) cannot send a custom `Authorization` header, and this system has deliberately never
used cookies (see main.py's CORS comment: "identity stays bearer-token-in-Authorization-header...
never a cookie"). So ui-shell fetches a token from the mint endpoint above with a normal authenticated
fetch() first, then builds the iframe's `src` with that token as a `?token=` query param — the proxy
route below accepts THAT instead of an Authorization header, and only for this one narrow purpose.

The token itself is a stateless HS256 JWT, signed with `settings.module_proxy_token_secret` — not a
second RS256 keypair (nothing outside gateway ever verifies this token type; publishing a JWKS for one
gateway-internal, seconds-lived token would be new infrastructure for no real benefit) and not a
server-side token store (gateway has no persistence layer, and an in-memory map wouldn't survive a pod
restart or a future `replicas: >1`, unlike the JWKS cache which is deliberately per-replica-safe
already). It carries the SAME workspace/user/role the mint endpoint's own require_auth() already
verified — the proxy route below never re-verifies a Keycloak token itself (it structurally can't,
there's no Authorization header on a plain navigation), it only trusts what the mint endpoint verified
and signed a moment earlier. `purpose: "module-proxy"` is defense in depth: verify_token() (app/auth.py)
hardcodes `algorithms=["RS256"]`, so an HS256 token is already structurally rejected everywhere else in
gateway without this check — `purpose` is a second, explicit layer on the one place that DOES accept
HS256, so this token type can never be confused with, or accidentally accepted as, anything else.

Follow-up requests (2026-09-11, feature/module-proxy-cookie branch): the query-string token alone never
propagated to a module's own follow-up requests — any relative `<script src>`/`fetch()` the module's
returned page issued resolved against the current document URL and dropped the query string entirely.
Fixed not by rewriting response HTML (fragile — a regex/parser pass over arbitrary module-controlled
markup, and it still couldn't fix a JS-issued `fetch()` building its own URL at runtime) but by ALSO
setting the same token as a cookie, scoped to `Path=/modules/{module_id}/proxy`, on every successful
proxied response (`_set_proxy_cookie` below). A relative request from the module's own page always
stays under that exact path — the browser resolves it against the current document URL, which IS that
path — so it carries the cookie automatically, no HTML rewriting and no change needed in the module's
own code at all. This is a deliberate, narrow exception to main.py's "identity stays bearer-token-in-
Authorization-header, never a cookie" rule (see configure_cors()'s own docstring there): that rule is
about gateway's primary identity mechanism, not this one gateway-internal, 5-minute, module-and-path-
scoped credential, which already deviated from it (the query param) for the same iframe-navigation
reason before this fix existed.

Real, known trade-off, not a bug: because the iframe's document origin (gateway's) differs from the
top-level page's origin (ui-shell's), this cookie is a "third-party" cookie from the browser's own
storage-partitioning point of view, regardless of `SameSite=None`. Safari (ITP) and Firefox (Total
Cookie Protection) block or partition third-party cookies by default today; Chrome is moving the same
direction. Where that happens, the cookie simply never gets attached — the module's follow-up requests
fail exactly as they did before this fix (401, no `?token=`), not worse. `SameSite=None; Secure` is set
so the cookie is at least accepted by browsers that do allow it; `Secure` requires the real HTTPS
ingress this cluster already runs (cert-manager, `docs/known-issues.md`'s self-signed CA). Only provably
correct end-to-end against `hello-module` (a single self-contained static page) — a module with a real
multi-file frontend or its own backend calls is the real test this hasn't had yet; see
`docs/known-issues.md` for the live-verification writeup once one exists.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
import jwt
from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.argocd import ArgoCDUnavailableError, get_module_proxy_target
from app.auth import AuthError, DerivedHeaders, require_auth
from app.config import settings
from app.jwks import JWKSCache
from app.modules import _MODULE_ID_PATTERN
from app.proxy import _HOP_BY_HOP_HEADERS

router = APIRouter()

_TOKEN_PURPOSE = "module-proxy"


class ModuleProxyTokenError(Exception):
    """`settings.module_proxy_token_secret` isn't configured — same "missing setting -> a clear,
    distinct error, not a crash or a silently-insecure default" shape `app/github_dispatch.py`'s
    `GitHubDispatchError` already establishes for `github_token`. The mint endpoint turns this into a
    503, same as that one."""


def _mint_proxy_token(module_id: str, derived: DerivedHeaders) -> str:
    if not settings.module_proxy_token_secret:
        raise ModuleProxyTokenError(
            "GATEWAY_MODULE_PROXY_TOKEN_SECRET isn't configured — see "
            "bootstrap/seal-gateway-module-proxy-secret.sh."
        )
    now = int(time.time())
    payload = {
        "module_id": module_id,
        "workspace": derived.workspace,
        "user": derived.user,
        "role": derived.role,
        "purpose": _TOKEN_PURPOSE,
        "iat": now,
        "exp": now + settings.module_proxy_token_ttl_seconds,
    }
    return jwt.encode(payload, settings.module_proxy_token_secret, algorithm="HS256")


def _decode_proxy_token(token: str, module_id: str) -> tuple[DerivedHeaders, int]:
    """Raises AuthError exactly like verify_token()/derive_headers() do, so the two route handlers
    below can convert it to a JSONResponse the same way every other route in this package does.
    Deliberately refuses outright (401) if the secret isn't configured, the same defense-in-depth
    reasoning as _mint_proxy_token above refusing to sign with an empty key: an unconfigured-secret
    environment should never accept ANY token, not even one that happens to verify against an empty
    string.

    Returns the token's own `exp` claim alongside DerivedHeaders — not because callers care about
    identity's shape any differently, but because _set_proxy_cookie (below) needs it to size the
    follow-up-request cookie's Max-Age accurately, and re-decoding the token a second time just to read
    one claim already proven valid here would be wasted work for no benefit."""
    if not settings.module_proxy_token_secret:
        raise AuthError(401, "Module proxy tokens aren't configured on this gateway.")
    try:
        claims = jwt.decode(
            token,
            key=settings.module_proxy_token_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "module_id", "purpose", "workspace", "user", "role"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(401, "Proxy token has expired — reload the module's page for a new one.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(401, f"Invalid proxy token: {exc}") from exc

    if claims.get("purpose") != _TOKEN_PURPOSE:
        raise AuthError(403, "Not a module-proxy token.")
    if claims.get("module_id") != module_id:
        raise AuthError(403, f"This token was minted for a different module than {module_id!r}.")

    derived = DerivedHeaders(workspace=claims["workspace"], user=claims["user"], role=claims["role"])
    return derived, int(claims["exp"])


def _proxy_cookie_name(module_id: str) -> str:
    # module_id is already constrained to _MODULE_ID_PATTERN (^[a-z0-9-]+$) by FastAPI's own Path(...,
    # pattern=...) validation on every route that accepts one — safe to interpolate directly into a
    # cookie name with no further escaping.
    return f"mp_token_{module_id}"


def _set_proxy_cookie(response: StreamingResponse, module_id: str, token: str, exp: int) -> None:
    """Lets a module's own follow-up requests — a relative <script src>/<link href>, or a same-path
    fetch()/XHR its own bundle issues — carry the SAME proxy token the initial <iframe src> navigation
    used, with no change needed in the module's own code: a browser resolves a relative URL against the
    CURRENT document URL, which is always under .../modules/{module_id}/proxy/... for anything this
    route ever serves, so `Path=`-scoping the cookie to that exact prefix is enough to guarantee it's
    attached. See this file's own module docstring for the full "why a cookie, why it's a deliberate
    exception, and its real third-party-cookie-blocking trade-off" reasoning.

    Set fresh on EVERY successful proxied response (not just the first, query-param-authenticated one)
    so Max-Age keeps tracking the token's real remaining lifetime as time passes. This never extends the
    token's actual lifetime — _decode_proxy_token still enforces the JWT's own `exp` claim on every
    single request regardless of what the cookie's Max-Age says — it only keeps the browser's expiry
    bookkeeping accurate rather than fixed at whatever it was on the very first request.
    """
    response.set_cookie(
        key=_proxy_cookie_name(module_id),
        value=token,
        max_age=max(0, exp - int(time.time())),
        path=f"/modules/{module_id}/proxy",
        httponly=True,
        secure=True,
        samesite="none",
    )


def _sanitize_frame_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strips `X-Frame-Options` entirely and strips any `frame-ancestors` directive out of a forwarded
    `Content-Security-Policy` — distinct from proxy.py's `_HOP_BY_HOP_HEADERS`, since catalog-service
    responses were never embedded in an iframe and this concern didn't previously exist anywhere in
    this codebase. `hello-module`'s stock nginx sets neither today, so this branch's own live
    verification can't prove it's needed — but gateway's origin (the frame, `gateway.platform.local`)
    differs from ui-shell's (the top-level page, `app.platform.local`), and any future module on a
    hardened base image plausibly sets one or both; without this, that would silently blank the iframe
    with no error surfaced anywhere, not a loud failure someone would think to look here for.
    """
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower == "x-frame-options":
            continue
        if lower == "content-security-policy":
            directives = [d.strip() for d in value.split(";") if d.strip()]
            directives = [d for d in directives if not d.lower().startswith("frame-ancestors")]
            if not directives:
                continue
            value = "; ".join(directives)
        sanitized[key] = value
    return sanitized


@router.get("/modules/{module_id}/proxy-token")
async def mint_module_proxy_token(
    request: Request,
    module_id: str = Path(..., pattern=_MODULE_ID_PATTERN),
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    """Same auth as GET /modules — require_auth() only, no require_role() gate. This is read-only
    viewing of a module a viewer-role user can already see is installed via GET /modules; it grants no
    new capability beyond "look at what you can already see is running," a fundamentally different
    trust boundary than install/uninstall's real cluster-mutating GitHub dispatch (the reason those two
    specifically require "editor"). GET, not POST: minting has no persisted side effect on gateway's
    own state, the same shape as GET /modules/check-requirements above.
    """
    jwks: JWKSCache = request.app.state.jwks
    try:
        derived = await require_auth(authorization, x_workspace, jwks)
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        target = await get_module_proxy_target(module_id)
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if target is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"{module_id!r} isn't installed, or doesn't have a proxied UI yet — "
                "reinstalling it picks up ui-shell-plan.md item 8's proxy-to annotation."
            },
        )

    try:
        token = _mint_proxy_token(module_id, derived)
    except ModuleProxyTokenError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return {"module_id": module_id, "token": token, "expires_in": settings.module_proxy_token_ttl_seconds}


async def _proxy_module_request(module_id: str, path: str, request: Request):
    # ?token= (the initial <iframe src> navigation, which can't send a header at all) takes priority
    # over the cookie (a follow-up request the module's own page issued) when, implausibly, both are
    # present — a query param is always the caller's most explicit, freshest statement of intent.
    token = request.query_params.get("token") or request.cookies.get(_proxy_cookie_name(module_id))
    if not token:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Missing ?token= and no module-proxy cookie — fetch one from "
                "GET /modules/{id}/proxy-token first."
            },
        )

    try:
        derived, exp = _decode_proxy_token(token, module_id)
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        target = await get_module_proxy_target(module_id)
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if target is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"{module_id!r} isn't installed (or was uninstalled since this token was minted)."
            },
        )

    body = await request.body()
    # Same _HOP_BY_HOP_HEADERS exclusion proxy.py's own catch-all uses, PLUS the same
    # _CLIENT_AUTH_HEADERS-style stripping that file's own docstring explains is the whole point —
    # X-Workspace/X-User/X-Role below always come from the TOKEN's claims, never a request header, and
    # that's only actually true if any caller-supplied value is stripped first: a plain <iframe src>
    # navigation never sends these, but a dict already holding a lowercase "x-workspace" key plus a
    # later `.update()` adding a differently-cased "X-Workspace" key doesn't overwrite it in a plain
    # Python dict — it creates a SECOND header, and httpx sends both, comma-joined, to the module. Same
    # "never client-declared" discipline proxy.py's own comment on this exact point already documents.
    #
    # "cookie" wasn't a concern for proxy.py's own version of this same exclusion set — this codebase
    # never set a cookie before _set_proxy_cookie above. Now that a follow-up request from the module's
    # own page carries our `mp_token_{module_id}` cookie (that's the whole point of it), it has to be
    # stripped here for the same reason `token` is stripped from outbound_params below: it's gateway's
    # own internal auth artifact for THIS route, never something the module's own backend should ever
    # see forwarded to it.
    _client_supplied_auth_headers = {"authorization", "x-workspace", "x-user", "x-role", "cookie"}
    outbound_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() not in _client_supplied_auth_headers
    }
    outbound_headers.update(derived.as_headers())

    # `token` is gateway's own auth mechanism for this route, not something the module itself should
    # ever see; every other query param passes through untouched.
    outbound_params = {k: v for k, v in request.query_params.multi_items() if k != "token"}

    async with httpx.AsyncClient(base_url=target, timeout=settings.upstream_timeout_seconds) as client:
        outbound_request = client.build_request(
            request.method, f"/{path}", params=outbound_params, content=body, headers=outbound_headers
        )
        try:
            upstream_response = await client.send(outbound_request, stream=True)
        except httpx.TimeoutException:
            return JSONResponse(
                status_code=504, content={"detail": f"{module_id!r}'s own UI did not respond in time."}
            )
        except httpx.ConnectError:
            return JSONResponse(
                status_code=502, content={"detail": f"{module_id!r}'s own UI is unreachable."}
            )

        response_headers = _sanitize_frame_headers(
            {
                key: value
                for key, value in upstream_response.headers.items()
                if key.lower() not in _HOP_BY_HOP_HEADERS
            }
        )

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

        response = StreamingResponse(
            stream_body(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_response.headers.get("content-type"),
        )
        _set_proxy_cookie(response, module_id, token, exp)
        return response


# Two routes, one handler — Starlette's {path:path} converter only matches when the URL has the
# trailing "/" present (`/modules/hello-module/proxy` alone would NOT match a route declared only as
# `.../proxy/{path:path}`), so the bare `.../proxy` route is registered too, to correctly reach the
# module's own root. `path` defaults to "" for that bare route (FastAPI treats a parameter absent from
# a given route's own path template as an optional query param for THAT route — harmless here, nothing
# legitimately sends `?path=`).
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@router.api_route("/modules/{module_id}/proxy", methods=_METHODS)
@router.api_route("/modules/{module_id}/proxy/{path:path}", methods=_METHODS)
async def proxy_module(module_id: str, request: Request, path: str = ""):
    return await _proxy_module_request(module_id, path, request)
