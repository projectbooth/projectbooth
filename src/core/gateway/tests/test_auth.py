"""Unit tests for verify_token()/derive_headers() — real signed JWTs (see
conftest.py's throwaway RSA keypair) verified against a real JWKSCache
pointed at a respx-mocked JWKS endpoint. This is what proves signature/
expiry/issuer checking actually works, not just that the code calls a
library function correctly.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import AuthError, DerivedHeaders, derive_headers, require_role, verify_token
from app.jwks import JWKSCache

BASE_URL = "https://keycloak.test"
JWKS_PATH = "/realms/platform/protocol/openid-connect/certs"


@pytest.fixture
def jwks(jwk_dict):
    with respx.mock:
        respx.get(f"{BASE_URL}{JWKS_PATH}").mock(return_value=httpx.Response(200, json={"keys": [jwk_dict]}))
        client = httpx.AsyncClient(base_url=BASE_URL)
        yield JWKSCache(client=client, jwks_path=JWKS_PATH, ttl_seconds=300)


# ---- verify_token ---------------------------------------------------------


async def test_verify_token_accepts_a_validly_signed_token(sign_token, jwks):
    claims = await verify_token(f"Bearer {sign_token()}", jwks)
    assert claims["sub"] == "user-1"
    assert claims["preferred_username"] == "alice"


async def test_verify_token_rejects_missing_authorization_header(jwks):
    with pytest.raises(AuthError) as exc_info:
        await verify_token(None, jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_non_bearer_scheme(jwks):
    with pytest.raises(AuthError) as exc_info:
        await verify_token("Basic dXNlcjpwYXNz", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_empty_bearer_token(jwks):
    with pytest.raises(AuthError) as exc_info:
        await verify_token("Bearer ", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_malformed_token(jwks):
    with pytest.raises(AuthError) as exc_info:
        await verify_token("Bearer not-a-real-jwt", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_expired_token(sign_token, jwks):
    token = sign_token(expires_in=-10)
    with pytest.raises(AuthError, match="expired") as exc_info:
        await verify_token(f"Bearer {token}", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_wrong_issuer(sign_token, jwks):
    token = sign_token({"iss": "https://not-keycloak.example/realms/platform"})
    with pytest.raises(AuthError) as exc_info:
        await verify_token(f"Bearer {token}", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_unknown_kid(sign_token, jwks):
    token = sign_token(kid="some-other-key-id")
    with pytest.raises(AuthError, match="unrecognized") as exc_info:
        await verify_token(f"Bearer {token}", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_rejects_missing_kid(sign_token, jwks):
    token = sign_token(kid=None)
    with pytest.raises(AuthError, match="kid") as exc_info:
        await verify_token(f"Bearer {token}", jwks)
    assert exc_info.value.status_code == 401


async def test_verify_token_accepts_a_token_carrying_an_aud_claim(sign_token, jwks):
    # Regression test for a real bug (found live, 2026-09-02, not caught by
    # any test before this): a token with an `aud` claim — which every real
    # Keycloak-issued token has — used to fail with InvalidAudienceError
    # because verify_token() never told PyJWT what audience to expect. Every
    # other test in this file already exercises this path implicitly now
    # that conftest.py's sign_token defaults to including `aud`, but this
    # one pins it explicitly so the specific failure mode has a named test,
    # not just incidental coverage.
    claims = await verify_token(f"Bearer {sign_token({'aud': 'some-client-id'})}", jwks)
    assert claims["aud"] == "some-client-id"


async def test_verify_token_rejects_signature_from_a_different_key(sign_token, jwks):
    # Same kid as the real keypair, but actually SIGNED with a different
    # key — this is what proves signature verification is genuinely
    # checked, not just that a kid happens to match something in the JWKS.
    impostor_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = sign_token(key=impostor_key)
    with pytest.raises(AuthError) as exc_info:
        await verify_token(f"Bearer {token}", jwks)
    assert exc_info.value.status_code == 401


# ---- derive_headers ---------------------------------------------------


def test_derive_headers_picks_the_matching_workspace_role():
    claims = {"preferred_username": "alice", "groups": ["/workspaces/personal/editor"]}
    derived = derive_headers(claims, "personal")
    assert derived.workspace == "personal"
    assert derived.user == "alice"
    assert derived.role == "editor"


def test_derive_headers_falls_back_to_sub_when_no_preferred_username():
    claims = {"sub": "user-1", "groups": ["/workspaces/personal/viewer"]}
    derived = derive_headers(claims, "personal")
    assert derived.user == "user-1"


def test_derive_headers_ignores_group_memberships_in_other_workspaces():
    claims = {"preferred_username": "alice", "groups": ["/workspaces/other-workspace/owner"]}
    with pytest.raises(AuthError) as exc_info:
        derive_headers(claims, "personal")
    assert exc_info.value.status_code == 403


def test_derive_headers_requires_a_workspace_hint():
    claims = {"preferred_username": "alice", "groups": ["/workspaces/personal/editor"]}
    with pytest.raises(AuthError) as exc_info:
        derive_headers(claims, None)
    assert exc_info.value.status_code == 400


def test_derive_headers_requires_some_group_membership():
    claims = {"preferred_username": "alice", "groups": []}
    with pytest.raises(AuthError) as exc_info:
        derive_headers(claims, "personal")
    assert exc_info.value.status_code == 403


def test_derive_headers_picks_highest_privilege_when_multiple_roles_match():
    # Not something `platform workspace invite` itself would ever produce,
    # but nothing stops a human admin from adding someone to more than one
    # role-group under the same workspace by hand — see _ROLE_PRIORITY's
    # own comment in auth.py for why "owner" has to win here, not whichever
    # one the token's groups claim happens to list first.
    claims = {
        "preferred_username": "alice",
        "groups": ["/workspaces/personal/viewer", "/workspaces/personal/owner"],
    }
    derived = derive_headers(claims, "personal")
    assert derived.role == "owner"


def test_derive_headers_as_headers_shape():
    claims = {"preferred_username": "alice", "groups": ["/workspaces/personal/editor"]}
    derived = derive_headers(claims, "personal")
    assert derived.as_headers() == {"X-Workspace": "personal", "X-User": "alice", "X-Role": "editor"}


# ---- require_role -----------------------------------------------------
# ui-shell-plan.md item 7's mutation mechanism (feature/gateway-module-lifecycle-dispatch,
# 2026-09-10) — the first caller of _ROLE_PRIORITY's ordering for anything beyond plain membership.


def _derived(role: str) -> DerivedHeaders:
    return DerivedHeaders(workspace="personal", user="alice", role=role)


def test_require_role_allows_owner_against_editor_minimum():
    require_role(_derived("owner"), "editor")  # doesn't raise


def test_require_role_allows_editor_against_editor_minimum():
    require_role(_derived("editor"), "editor")  # doesn't raise


def test_require_role_rejects_viewer_against_editor_minimum():
    with pytest.raises(AuthError) as exc_info:
        require_role(_derived("viewer"), "editor")
    assert exc_info.value.status_code == 403
    assert "editor" in exc_info.value.detail
    assert "viewer" in exc_info.value.detail


def test_require_role_rejects_editor_against_owner_minimum():
    with pytest.raises(AuthError) as exc_info:
        require_role(_derived("editor"), "owner")
    assert exc_info.value.status_code == 403


def test_require_role_allows_owner_against_owner_minimum():
    require_role(_derived("owner"), "owner")  # doesn't raise
