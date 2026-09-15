"""Unit tests for JWKSCache — respx-mocked Keycloak JWKS endpoint, no real
network. Proves the cache-hit vs. refresh-on-unknown-kid/stale-TTL behavior
jwks.py's own module docstring describes.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.jwks import JWKSCache

BASE_URL = "https://keycloak.test"
JWKS_PATH = "/realms/platform/protocol/openid-connect/certs"


def _jwks_body(*kids: str) -> dict:
    # 'n'/'e' values here are meaningless placeholders — JWKSCache only
    # ever caches/returns these dicts verbatim, it never tries to construct
    # a real key from them (that's auth.py's job, tested against real keys
    # in test_auth.py).
    return {"keys": [{"kid": kid, "kty": "RSA", "n": "placeholder", "e": "AQAB"} for kid in kids]}


@pytest.fixture
def client():
    return httpx.AsyncClient(base_url=BASE_URL)


@respx.mock
async def test_get_key_fetches_once_and_serves_both_keys_from_cache(client):
    route = respx.get(f"{BASE_URL}{JWKS_PATH}").mock(
        return_value=httpx.Response(200, json=_jwks_body("k1", "k2"))
    )
    cache = JWKSCache(client=client, jwks_path=JWKS_PATH, ttl_seconds=300)

    key1 = await cache.get_key("k1")
    key2 = await cache.get_key("k2")

    assert key1["kid"] == "k1"
    assert key2["kid"] == "k2"
    assert route.call_count == 1  # one fetch covered both keys


@respx.mock
async def test_unknown_kid_forces_a_refetch_even_within_the_ttl(client):
    route = respx.get(f"{BASE_URL}{JWKS_PATH}").mock(
        side_effect=[
            httpx.Response(200, json=_jwks_body("k1")),
            httpx.Response(200, json=_jwks_body("k1", "k2")),  # k2 "rotated in" between fetches
        ]
    )
    cache = JWKSCache(client=client, jwks_path=JWKS_PATH, ttl_seconds=300)

    assert (await cache.get_key("k1"))["kid"] == "k1"
    assert route.call_count == 1

    key2 = await cache.get_key("k2")
    assert key2["kid"] == "k2"
    assert route.call_count == 2  # forced a second fetch despite a fresh (non-stale) cache


@respx.mock
async def test_get_key_returns_none_for_a_kid_that_never_shows_up(client):
    respx.get(f"{BASE_URL}{JWKS_PATH}").mock(return_value=httpx.Response(200, json=_jwks_body("k1")))
    cache = JWKSCache(client=client, jwks_path=JWKS_PATH, ttl_seconds=300)

    assert await cache.get_key("ghost-kid") is None


@respx.mock
async def test_stale_ttl_forces_a_refetch_even_for_an_already_known_kid(client):
    route = respx.get(f"{BASE_URL}{JWKS_PATH}").mock(return_value=httpx.Response(200, json=_jwks_body("k1")))
    # ttl_seconds=0 -> stale essentially immediately after the first fetch.
    cache = JWKSCache(client=client, jwks_path=JWKS_PATH, ttl_seconds=0)

    await cache.get_key("k1")
    await cache.get_key("k1")

    assert route.call_count == 2


@respx.mock
async def test_concurrent_callers_for_the_same_unknown_kid_only_fetch_once(client):
    import asyncio

    route = respx.get(f"{BASE_URL}{JWKS_PATH}").mock(return_value=httpx.Response(200, json=_jwks_body("k1")))
    cache = JWKSCache(client=client, jwks_path=JWKS_PATH, ttl_seconds=300)

    results = await asyncio.gather(*(cache.get_key("k1") for _ in range(5)))

    assert all(result["kid"] == "k1" for result in results)
    assert route.call_count == 1  # the asyncio.Lock in _refresh() coalesced all five
