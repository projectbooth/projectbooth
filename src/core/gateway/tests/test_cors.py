"""Tests for app/main.py's configure_cors() — ui-shell-plan.md item 2.

Deliberately doesn't test this via `app.main`'s real module-level `app`
singleton for the "configured" cases — `settings` is a module-level object
and `app` is built once at import time, so there's no way to flip
`cors_origin_list` on for one test without either monkeypatching an
environment variable before `app.main` is ever imported (fragile,
order-dependent across the rest of this test suite) or, the approach taken
here, calling `configure_cors()` directly against a throwaway `FastAPI()` +
`Settings(...)` pair — the same "test the reusable piece directly" move
`test_auth.py` already makes for `verify_token()`/`derive_headers()`.

The one case that DOES use the real `app` (test below,
`test_real_app_has_no_cors_by_default`) is intentional: it's a regression
guard on gateway's actual default-off deployed/test state, not a test of
`configure_cors()` itself.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, configure_cors


def test_configure_cors_adds_nothing_when_unset():
    throwaway = FastAPI()
    configure_cors(throwaway, Settings(cors_origins=""))
    assert not any(m.cls is CORSMiddleware for m in throwaway.user_middleware)


def test_configure_cors_adds_middleware_when_set():
    throwaway = FastAPI()
    configure_cors(throwaway, Settings(cors_origins="https://app.platform.local"))
    cors_entries = [m for m in throwaway.user_middleware if m.cls is CORSMiddleware]
    assert len(cors_entries) == 1
    assert cors_entries[0].kwargs["allow_origins"] == ["https://app.platform.local"]


def test_preflight_allows_the_configured_origin():
    throwaway = FastAPI()
    configure_cors(throwaway, Settings(cors_origins="https://app.platform.local"))

    @throwaway.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(throwaway) as client:
        response = client.options(
            "/ping",
            headers={
                "Origin": "https://app.platform.local",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") == "https://app.platform.local"


def test_preflight_omits_the_header_for_a_disallowed_origin():
    # Starlette's CORSMiddleware permits by omission, not by rejecting the
    # request outright — it's the BROWSER that enforces the block on seeing
    # no matching Access-Control-Allow-Origin header, not the server. This
    # test documents that shape rather than asserting a 403 that never
    # happens.
    throwaway = FastAPI()
    configure_cors(throwaway, Settings(cors_origins="https://app.platform.local"))

    @throwaway.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(throwaway) as client:
        response = client.options(
            "/ping",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in response.headers


def test_real_app_has_no_cors_by_default():
    # Regression guard on gateway's actual deployed/test state: CI never
    # sets GATEWAY_CORS_ORIGINS, so the real app.main app should carry no
    # CORSMiddleware and /healthz should carry no CORS header at all.
    assert not any(m.cls is CORSMiddleware for m in app.user_middleware)
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"Origin": "https://app.platform.local"})
    assert "access-control-allow-origin" not in response.headers
