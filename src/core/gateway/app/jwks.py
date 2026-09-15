"""JWKS fetch + in-memory TTL cache. `verify_token()` (auth.py) needs the
public key matching a token's `kid` to check its signature; refetching the
whole JWKS document from Keycloak on every single proxied request would
mean every API call through gateway also costs a round trip to Keycloak.

Refresh-on-unknown-kid: `get_key()` forces a refetch whenever it's asked for
a `kid` it doesn't currently have cached, even if the TTL hasn't expired
yet — this is what lets gateway pick up a newly-rotated Keycloak signing key
within one request rather than waiting up to `jwks_cache_seconds` for the
cache to expire on its own.

An `asyncio.Lock` (not just an `if` check) guards the actual refetch because
gateway serves requests concurrently — without it, a burst of requests that
all land on the same expired/unknown-kid cache would each kick off their own
JWKS fetch simultaneously instead of one request doing it and the rest
reusing the result.
"""
from __future__ import annotations

import asyncio
import time

import httpx


class JWKSCache:
    def __init__(self, *, client: httpx.AsyncClient, jwks_path: str, ttl_seconds: int) -> None:
        # `client` is expected to already be scoped to Keycloak's base_url
        # (see main.py's lifespan) — this class only ever GETs `jwks_path`
        # against it, never anything else.
        self._client = client
        self._jwks_path = jwks_path
        self._ttl_seconds = ttl_seconds
        self._keys_by_kid: dict[str, dict] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> dict | None:
        now = time.monotonic()
        is_stale = (now - self._fetched_at) >= self._ttl_seconds
        if kid not in self._keys_by_kid or is_stale:
            await self._refresh(kid)
        return self._keys_by_kid.get(kid)

    async def _refresh(self, kid: str) -> None:
        async with self._lock:
            # Re-check inside the lock, against the SAME condition that
            # made the caller want a refresh in the first place — not just
            # "is the cache non-empty and non-stale," which would wrongly
            # skip a real fetch when a concurrent caller's earlier refresh
            # satisfied ITS OWN reason for refreshing (staleness) but not
            # this caller's (a still-missing kid, e.g. one that rotated in
            # after that earlier fetch already ran). A concurrent call for
            # the SAME kid, though, correctly finds it now present and skips
            # — that's the coalescing this lock exists for.
            now = time.monotonic()
            is_stale = (now - self._fetched_at) >= self._ttl_seconds
            if kid in self._keys_by_kid and not is_stale:
                return
            response = await self._client.get(self._jwks_path)
            response.raise_for_status()
            body = response.json()
            self._keys_by_kid = {key["kid"]: key for key in body.get("keys", []) if "kid" in key}
            self._fetched_at = time.monotonic()
