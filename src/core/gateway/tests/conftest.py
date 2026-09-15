"""Shared test fixtures — a throwaway RSA keypair, generated once per test
SESSION (not per test — nothing here needs a fresh key per test, and
generating a 2048-bit RSA key is by a wide margin the slowest part of any
test that touches it), used to sign real JWTs. Every auth-related test in
this suite (test_auth.py directly, test_proxy.py through the full ASGI app)
verifies against tokens actually signed this way rather than mocking
`jwt.decode`/`jwt.encode` — that would only prove this code calls a
library function, not that signature/expiry/issuer checking actually works.
"""
from __future__ import annotations

import datetime
import json
import time

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.x509.oid import NameOID

from app.config import settings

KID = "test-kid-1"


@pytest.fixture(scope="session")
def rsa_keypair() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwk_dict(rsa_keypair: RSAPrivateKey) -> dict:
    """The public half of rsa_keypair, as a JWK dict — what a respx-mocked
    Keycloak JWKS endpoint returns in every test that needs one."""
    algorithm = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    jwk = json.loads(algorithm.to_jwk(rsa_keypair.public_key()))
    jwk["kid"] = KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


@pytest.fixture
def sign_token(rsa_keypair: RSAPrivateKey):
    """Returns a function, not a fixed token — every test needs a slightly
    different claim set (expired, wrong issuer, no matching workspace
    group, ...), so this hands back something each test calls with its own
    overrides rather than guessing one default shape that fits everyone.

    `key=` lets a test sign with a DIFFERENT private key than rsa_keypair
    (same `kid`, wrong actual key) — see test_auth.py's
    test_verify_token_rejects_signature_from_a_different_key for why that
    matters: it's what proves signature verification is actually checked,
    not just that a `kid` happens to match something in the JWKS.
    """

    def _sign(claims: dict | None = None, *, kid: str | None = KID, expires_in: int = 300, key=None) -> str:
        payload = {
            "sub": "user-1",
            "iss": settings.expected_issuer,
            "exp": int(time.time()) + expires_in,
            "preferred_username": "alice",
            "groups": ["/workspaces/personal/editor"],
            # A real Keycloak token always carries this (its own default is
            # "account" unless a client's scopes/mappers say otherwise) —
            # included here by default so every test signs a realistic
            # token shape, not one gateway happens to accept only because
            # it's missing a claim real tokens actually have. Found live,
            # 2026-09-02: this fixture's tokens never carried `aud` before,
            # so `verify_token`'s missing `verify_aud: False` (app/auth.py)
            # went untested until a real login hit it — see that file's own
            # comment on the fix.
            "aud": "account",
        }
        if claims:
            payload.update(claims)
        headers = {"kid": kid} if kid is not None else {}
        return jwt.encode(payload, key or rsa_keypair, algorithm="RS256", headers=headers)

    return _sign


@pytest.fixture
def module_proxy_secret(monkeypatch):
    """Points settings.module_proxy_token_secret at a fixed test value — shared by test_module_proxy.py
    (both the mint endpoint and the proxy route itself) and by sign_proxy_token below, which needs the
    SAME secret to hand-craft tokens app/module_proxy.py's _decode_proxy_token() will actually accept."""
    # >=32 bytes — PyJWT's own InsecureKeyLengthWarning threshold for HS256 (RFC 7518 §3.2); the real
    # secret (bootstrap/seal-gateway-module-proxy-secret.sh's `openssl rand -hex 32`, used as the raw
    # 64-character hex string, not decoded) is comfortably above this too.
    secret = "test-module-proxy-secret-0123456789"
    monkeypatch.setattr(settings, "module_proxy_token_secret", secret)
    return secret


@pytest.fixture
def sign_proxy_token(module_proxy_secret):
    """Like sign_token above, but for app/module_proxy.py's HS256 module-proxy tokens — hands back a
    function so each test can build a token with its own overrides (expired, wrong module_id, wrong
    purpose, missing a required claim) rather than one fixed default shape. Requires
    module_proxy_secret so a test using this fixture doesn't also need to request that one by name."""

    def _sign(module_id: str = "hello-module", *, expires_in: int = 300, **overrides) -> str:
        payload = {
            "module_id": module_id,
            "workspace": "personal",
            "user": "alice",
            "role": "editor",
            "purpose": "module-proxy",
            "iat": int(time.time()),
            "exp": int(time.time()) + expires_in,
        }
        payload.update(overrides)
        return jwt.encode(payload, module_proxy_secret, algorithm="HS256")

    return _sign


@pytest.fixture(scope="session")
def self_signed_ca_pem() -> bytes:
    """A real, syntactically-valid self-signed CA certificate in PEM form —
    test_argocd.py's httpx.AsyncClient(verify=<path>) needs a file
    ssl.create_default_context() can actually parse (it builds the
    SSLContext eagerly in the client constructor, before respx's mocked
    transport ever gets involved, so a placeholder string like "fake-ca"
    fails with an SSL error before any request is made). The private key is
    thrown away immediately — nothing in this suite ever needs to sign
    anything with it, only to hand httpx a loadable cert file."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def mounted_sa(tmp_path, monkeypatch, self_signed_ca_pem):
    """Points settings at fake ServiceAccount token/CA files so
    app/argocd.py's list_module_applications() gets past its own "not
    running in-cluster" check — shared by test_argocd.py (direct unit tests)
    and test_modules.py (the /modules/check-requirements endpoint tests,
    which call list_module_applications() indirectly through the real ASGI
    app). The CA file has to be a real, parseable PEM cert (see
    self_signed_ca_pem above) — httpx builds its SSLContext from it eagerly,
    before respx's mocked transport is ever reached."""
    token_path = tmp_path / "token"
    token_path.write_text("fake-sa-token\n")
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(self_signed_ca_pem)
    monkeypatch.setattr(settings, "k8s_sa_token_path", str(token_path))
    monkeypatch.setattr(settings, "k8s_sa_ca_path", str(ca_path))
    return token_path, ca_path
