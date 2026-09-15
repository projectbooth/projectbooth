"""platform-gateway's FastAPI app. Run locally with:

    uvicorn app.main:app --reload

against a reachable Keycloak + catalog-service (port-forwards for local
testing against a real cluster; see this package's README). Verifies every
caller's Keycloak JWT and proxies to catalog-service with gateway-derived
X-Workspace/X-User/X-Role headers — see app/auth.py and app/proxy.py for
the actual logic; this module just wires the two httpx clients they share
into app.state during startup and shuts them down cleanly on exit. Also
conditionally adds CORS support (configure_cors(), below) for ui-shell —
see that function's own docstring, ui-shell-plan.md item 2.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, settings
from app.jwks import JWKSCache
from app.module_proxy import router as module_proxy_router
from app.modules import router as modules_router
from app.proxy import router as proxy_router


def _keycloak_tls_verify() -> str | bool:
    # A real Deployment always has this file mounted (gateway.yaml's volume,
    # sourced from platform-ca-secret — Reflector mirrors it into this
    # namespace, see argocd/manifests/cluster-issuer.yaml's secretTemplate
    # annotations). Falling back to the system default trust store (True)
    # rather than erroring when the file isn't there is what lets this
    # module import and the lifespan below construct cleanly in local
    # dev/tests, where nothing mounts it — real TLS verification of a real
    # in-cluster Keycloak connection only matters in-cluster, where the file
    # is always present.
    if Path(settings.keycloak_ca_path).is_file():
        return settings.keycloak_ca_path
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    catalog_client = httpx.AsyncClient(
        base_url=settings.catalog_service_url, timeout=settings.upstream_timeout_seconds
    )
    keycloak_client = httpx.AsyncClient(
        base_url=settings.keycloak_internal_url,
        timeout=settings.upstream_timeout_seconds,
        verify=_keycloak_tls_verify(),
    )
    app.state.catalog_client = catalog_client
    app.state.jwks = JWKSCache(
        client=keycloak_client, jwks_path=settings.jwks_path, ttl_seconds=settings.jwks_cache_seconds
    )
    try:
        yield
    finally:
        await catalog_client.aclose()
        await keycloak_client.aclose()


app = FastAPI(
    title="platform-gateway",
    description="Verifies Keycloak JWTs, proxies to catalog-service with derived, trustworthy "
    "X-Workspace/X-User/X-Role headers. ARCHITECTURE.md §2 (layer 3), §3, §10.",
    version="0.1.0",
    lifespan=lifespan,
)


def configure_cors(app: FastAPI, settings: Settings) -> None:
    """Add CORSMiddleware iff GATEWAY_CORS_ORIGINS is set — a plain function
    taking app/settings as arguments, not just an inline `if` block at
    import time, specifically so tests can exercise the "configured" case
    directly (settings is a module-level singleton and app is built once at
    import, so an inline conditional can't be flipped on for one test
    without fragile import-order tricks — see tests/test_cors.py). ui-shell-
    plan.md item 2: ui-shell keeps its own Ingress host (app.platform.local)
    rather than gateway growing a second proxy target, so this is real
    cross-origin traffic in production, not just a local-dev convenience —
    see config.py's own comment on cors_origins.

    allow_methods/allow_headers of "*" mirrors catalog-service's own choice
    (app/main.py) — gateway's actual auth (verify_token/derive_headers)
    still governs what's accepted regardless of what CORS allows at
    preflight, so this doesn't weaken anything real. allow_credentials
    deliberately left at its default (False): identity stays bearer-token-
    in-Authorization-header (platform_sdk's existing pattern), never a
    cookie, so there's nothing cross-origin-cookie-shaped to allow.

    One narrow, deliberate exception (2026-09-11, app/module_proxy.py): the module-proxy route sets a
    short-lived, path-scoped cookie so a module's own iframe-embedded page can carry its proxy token on
    relative follow-up requests. That cookie is never read by gateway's own CORS-governed cross-origin
    surface here — it's set and read entirely within requests to gateway's own `/modules/{id}/proxy/...`
    path, made either as a plain browser navigation (the iframe itself) or as a same-origin request the
    module's OWN page issues to that same path, neither of which this CORSMiddleware config touches.
    allow_credentials stays False; this doesn't need it to be true. See module_proxy.py's own module
    docstring for the full reasoning.
    """
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
        )


configure_cors(app, settings)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# modules_router (app/modules.py, platform-module-deps branch) has to be
# registered before proxy_router for the same reason /healthz is above it —
# proxy_router's `/{path:path}` catch-all would otherwise swallow
# /modules/check-requirements too. proxy_router stays LAST, after
# everything else, for that same reason. See proxy.py's own module
# docstring for the same point.
#
# module_proxy_router (app/module_proxy.py, ui-shell-plan.md item 8) sits between the two: its own
# routes are scoped under the literal /modules/{id}/proxy[/{path}] segment, so registering it before
# modules_router or after wouldn't actually matter for THOSE two routers not colliding with each other
# — but it still has to come before proxy_router for the same catch-all reason as modules_router.
app.include_router(modules_router)
app.include_router(module_proxy_router)
app.include_router(proxy_router)
