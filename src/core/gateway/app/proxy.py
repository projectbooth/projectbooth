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

from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import AuthError, derive_headers, verify_token
from app.jwks import JWKSCache

router = APIRouter()

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
