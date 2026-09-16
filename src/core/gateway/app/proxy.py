"""The catch-all proxy route — after auth.py's verify_token()/
derive_headers() turn the caller's Authorization header + X-Workspace hint
into trustworthy X-Workspace/X-User/X-Role headers, this forwards the
request to catalog-service and streams its response straight back.

Registered LAST in main.py, after /healthz — Starlette matches routes in
registration order, and a catch-all `/{path:path}` would shadow /healthz
(and anything else) if it were added first. See main.py's own comment at
the include_router() call for this same point made where it matters.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from app.auth import AuthError, derive_headers, verify_token
from app.jwks import JWKSCache
from app.modules import _MODULE_ID_PATTERN

router = APIRouter()

# 2026-09-16 (Trino live-verification): a module's own frontend can issue follow-up XHR/fetch calls
# to hardcoded ABSOLUTE paths instead of something relative to wherever it's actually mounted — Trino's
# bundled Web UI does exactly this, calling `fetch("/ui/api/cluster")` rather than
# `fetch("ui/api/cluster")` or anything derived from its own current location. That request never
# reaches module_proxy.py's `/modules/{module_id}/proxy/...` route at all; it lands HERE, in this
# catch-all, indistinguishable at a glance from a genuine catalog-service API call. verify_token()
# below then rejects it (no Authorization header — a browser has no reason to attach a Keycloak
# bearer token to a same-origin fetch() the module's own bundle issued), which looks like an auth
# failure but has nothing to do with auth: the module's UI never even reached its own backend.
# Confirmed live 2026-09-16: Trino's dashboard "flickered" forever, polling
# `/ui/api/cluster`/`stats`/`query` every few seconds, each one landing here and 401ing, while the
# correctly-proxied `/modules/trino/proxy/ui/...` requests (the page itself, its JS/CSS bundles) all
# succeeded — see docs/known-issues.md for the full write-up.
#
# Fixed generically, not Trino-specifically — ARCHITECTURE.md's Phase 5/6 modules (Spark, Dask,
# Superset, MLflow) will hit this same class of bug on whatever chart they ship. A stray request like
# this still carries a Referer naming the page it actually came from —
# `https://gateway.platform.local/modules/{module_id}/proxy/ui/` — so a request with NO Authorization
# header and a Referer pointing under some module's proxy path is far more likely to be exactly this
# case than a genuine unauthenticated catalog-service call. Redirecting it (307, so the original
# method and body survive the round trip) back into module_proxy.py's own route lets that route's
# existing cookie-based auth handle it correctly, no change needed in any module's own code. A caller
# that DID send an Authorization header is making a real, deliberate catalog-service call and always
# goes through the normal path below untouched, even with a stale Referer left over from a previous
# module-proxy page.
_MODULE_ID_CHARS = _MODULE_ID_PATTERN.strip("^$")
_REFERER_MODULE_PROXY_RE = re.compile(rf"^https?://[^/]+/modules/(?P<module_id>{_MODULE_ID_CHARS})/proxy/")


def _module_proxy_redirect_target(
    path: str, authorization: str | None, referer: str | None, query_string: str
) -> str | None:
    """Pure function — see the module-level comment above for the full reasoning. Returns the
    path-absolute redirect target (resolved against gateway's own origin, no scheme/host needed) or
    None when this request should be handled normally by `proxy()` below."""
    if authorization:
        return None
    if not referer:
        return None
    match = _REFERER_MODULE_PROXY_RE.match(referer)
    if not match:
        return None
    module_id = match.group("module_id")
    query = f"?{query_string}" if query_string else ""
    return f"/modules/{module_id}/proxy/{path}{query}"


# Hop-by-hop headers per RFC 7230 §6.1 — meaningful only for the single
# connection they were sent on, never something to blindly copy from an
# inbound request to the outbound one or from the backend's response back to
# the original caller. `content-length`/`host` are included too: httpx sets
# its own correct content-length for the outbound request body, and `host`
# has to be catalog-service's own, not whatever the original caller sent —
# forwarding either verbatim risks a mismatched/rejected request.
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

# Never forwarded from the inbound request under ANY circumstance, even
# though they're not hop-by-hop headers — stripped separately from the set
# above so it stays obvious *why* each is excluded. authorization/x-workspace
# are read and consumed by auth.py above, not blindly passed through;
# x-user/x-role are the actual point of this whole branch — nothing a caller
# sends for either may ever reach catalog-service, only what derive_headers()
# computed from the verified token.
_CLIENT_AUTH_HEADERS = {"authorization", "x-workspace", "x-user", "x-role"}


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    redirect_target = _module_proxy_redirect_target(
        path, request.headers.get("authorization"), request.headers.get("referer"), request.url.query
    )
    if redirect_target is not None:
        return RedirectResponse(url=redirect_target, status_code=307)

    catalog_client: httpx.AsyncClient = request.app.state.catalog_client
    jwks: JWKSCache = request.app.state.jwks

    try:
        claims = await verify_token(request.headers.get("authorization"), jwks)
        derived = derive_headers(claims, request.headers.get("x-workspace"))
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    body = await request.body()
    outbound_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() not in _CLIENT_AUTH_HEADERS
    }
    # The ONLY place X-Workspace/X-User/X-Role get set on the outbound
    # request — always all three, always gateway-derived. See
    # catalog-service/app/deps.py's DEFAULT_ROLE comment for why silently
    # omitting X-Role would be a real privilege-escalation path, not a
    # hypothetical one.
    outbound_headers.update(derived.as_headers())

    upstream_request = catalog_client.build_request(
        request.method,
        f"/{path}",
        params=request.query_params,
        content=body,
        headers=outbound_headers,
    )
    try:
        upstream_response = await catalog_client.send(upstream_request, stream=True)
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"detail": "catalog-service did not respond in time."})
    except httpx.ConnectError:
        return JSONResponse(status_code=502, content={"detail": "catalog-service is unreachable."})

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }

    async def stream_body() -> AsyncIterator[bytes]:
        # Streamed, not buffered-then-returned — a large dataset listing or
        # export shouldn't have to sit fully in gateway's memory before the
        # first byte reaches the caller. aclose() in `finally` releases the
        # upstream connection back to catalog_client's pool whether the
        # stream finished normally or the caller disconnected partway
        # through.
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
