"""Unit tests for PlatformClient — mocked at the HTTP transport level via
respx, not run against a live gateway. Deliberate: this package's job is
"does PlatformClient send the right request and parse the right response,"
not "does gateway/catalog-service work" (those get their own test suites).
Keeping this SDK's tests transport-mocked means its CI job never needs a
live cluster the way a real end-to-end run does.

Credentials are mocked too (`platform_sdk.client.load_credentials`/
`save_credentials` monkeypatched, never a real ~/.config/platform/
credentials.json) — same reasoning, one layer up: this suite's job isn't
"does credentials.py read/write a file correctly" (that's
test_credentials.py's job) or "does the device flow work" (test_keycloak_
login.py's), just "does PlatformClient use whatever TokenSet it's handed
correctly."
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from platform_sdk import NotAuthenticatedError, PlatformAPIError, PlatformClient, TokenSet, Visibility

BASE_URL = "http://gateway.test"


def _token_set(**overrides) -> TokenSet:
    kwargs = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),  # comfortably not near-expiry
        "preferred_username": "alice",
    }
    kwargs.update(overrides)
    return TokenSet(**kwargs)


@pytest.fixture
def valid_credentials(monkeypatch):
    """Most tests don't care about the refresh path — this fixture gives
    them a token that's nowhere near expiry, so _ensure_token()'s refresh
    branch never fires and save_credentials is never called."""
    token_set = _token_set()
    monkeypatch.setattr("platform_sdk.client.load_credentials", lambda: token_set)
    save_calls: list[TokenSet] = []
    monkeypatch.setattr("platform_sdk.client.save_credentials", save_calls.append)
    return token_set, save_calls


@pytest.fixture
def client(valid_credentials):
    c = PlatformClient(gateway_url=BASE_URL, workspace="personal")
    yield c
    c.close()


def _workspace_body(name: str = "personal") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "display_name": name.title(),
        "created_at": datetime.now(UTC).isoformat(),
    }


def _dataset_body(name: str = "reddit-sentiment", **overrides) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "name": name,
        "visibility": "private",
        "description": None,
        "location_uri": None,
        "created_by": "alice",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    body.update(overrides)
    return body


def _function_body(name: str = "clean_text", **overrides) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "name": name,
        "visibility": "private",
        "description": None,
        "current_version": 0,
        "module_path": None,
        "created_by": "alice",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    body.update(overrides)
    return body


def _function_version_body(function_id: str, version: int = 1, **overrides) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "function_id": function_id,
        "version": version,
        "signature": "def clean_text(s: str) -> str",
        "docstring": None,
        "module_path": "pipelines.reddit.clean_text",
        "published_by": "alice",
        "published_at": datetime.now(UTC).isoformat(),
    }
    body.update(overrides)
    return body


@respx.mock
def test_headers_send_bearer_token_and_workspace_hint_only(client):
    route = respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(
            200, json={"workspace_id": str(uuid.uuid4()), "workspace_name": "personal",
                       "user_id": "alice", "role": "owner"}
        )
    )
    client.me()
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer at-1"
    assert sent.headers["x-workspace"] == "personal"
    # Never sent anymore — gateway derives identity/role from the verified
    # token itself, not from anything the client declares. See client.py's
    # module docstring for why this is the actual point of this branch.
    assert "x-user" not in sent.headers
    assert "x-role" not in sent.headers


def test_no_credentials_file_raises_not_authenticated_error(monkeypatch):
    monkeypatch.setattr("platform_sdk.client.load_credentials", lambda: None)
    c = PlatformClient(gateway_url=BASE_URL, workspace="personal")
    with pytest.raises(NotAuthenticatedError, match="platform login"):
        c.me()
    c.close()


def test_expired_credentials_with_no_refresh_token_raises_not_authenticated_error(monkeypatch):
    token_set = _token_set(refresh_token=None, expires_at=datetime.now(UTC) - timedelta(seconds=5))
    monkeypatch.setattr("platform_sdk.client.load_credentials", lambda: token_set)
    c = PlatformClient(gateway_url=BASE_URL, workspace="personal")
    with pytest.raises(NotAuthenticatedError, match="platform login"):
        c.me()
    c.close()


@respx.mock
def test_near_expiry_token_is_silently_refreshed_and_saved(monkeypatch):
    near_expiry = _token_set(access_token="stale-at", expires_at=datetime.now(UTC) + timedelta(seconds=5))
    monkeypatch.setattr("platform_sdk.client.load_credentials", lambda: near_expiry)
    save_calls: list[TokenSet] = []
    monkeypatch.setattr("platform_sdk.client.save_credentials", save_calls.append)

    refreshed = _token_set(access_token="fresh-at")

    class _FakeFlow:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def refresh(self, refresh_token):
            assert refresh_token == "rt-1"
            return refreshed

    monkeypatch.setattr("platform_sdk.client.KeycloakLoginFlow", lambda **kwargs: _FakeFlow())

    route = respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(
            200, json={"workspace_id": str(uuid.uuid4()), "workspace_name": "personal",
                       "user_id": "alice", "role": "owner"}
        )
    )

    c = PlatformClient(gateway_url=BASE_URL, workspace="personal")
    c.me()
    c.close()

    assert route.calls.last.request.headers["authorization"] == "Bearer fresh-at"
    assert save_calls == [refreshed]


@respx.mock
def test_token_refresh_happens_at_most_once_per_client_instance(monkeypatch):
    # _ensure_token() caches after its first call — a second request from
    # the same PlatformClient instance must reuse the cached TokenSet, not
    # refresh (or even re-read credentials.json) again.
    near_expiry = _token_set(access_token="stale-at", expires_at=datetime.now(UTC) + timedelta(seconds=5))
    load_calls = {"count": 0}

    def _load():
        load_calls["count"] += 1
        return near_expiry

    monkeypatch.setattr("platform_sdk.client.load_credentials", _load)
    monkeypatch.setattr("platform_sdk.client.save_credentials", lambda _ts: None)

    refresh_calls = {"count": 0}
    refreshed = _token_set(access_token="fresh-at")

    class _FakeFlow:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def refresh(self, refresh_token):
            refresh_calls["count"] += 1
            return refreshed

    monkeypatch.setattr("platform_sdk.client.KeycloakLoginFlow", lambda **kwargs: _FakeFlow())

    respx.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(
            200, json={"workspace_id": str(uuid.uuid4()), "workspace_name": "personal",
                       "user_id": "alice", "role": "owner"}
        )
    )
    respx.get(f"{BASE_URL}/workspaces").mock(return_value=httpx.Response(200, json=[]))

    c = PlatformClient(gateway_url=BASE_URL, workspace="personal")
    c.me()
    c.list_workspaces()
    c.close()

    assert load_calls["count"] == 1
    assert refresh_calls["count"] == 1


@respx.mock
def test_list_and_create_workspace(client):
    respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(200, json=[_workspace_body("personal")])
    )
    workspaces = client.list_workspaces()
    assert len(workspaces) == 1
    assert workspaces[0].name == "personal"

    respx.post(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(201, json=_workspace_body("another"))
    )
    created = client.create_workspace("another", "Another")
    assert created.name == "another"


@respx.mock
def test_dataset_crud_round_trip(client):
    dataset_id = str(uuid.uuid4())

    respx.post(f"{BASE_URL}/datasets").mock(
        return_value=httpx.Response(201, json=_dataset_body(id=dataset_id))
    )
    created = client.create_dataset("reddit-sentiment", visibility=Visibility.PRIVATE)
    assert str(created.id) == dataset_id
    assert created.visibility is Visibility.PRIVATE

    respx.get(f"{BASE_URL}/datasets/{dataset_id}").mock(
        return_value=httpx.Response(200, json=_dataset_body(id=dataset_id))
    )
    fetched = client.get_dataset(dataset_id)
    assert str(fetched.id) == dataset_id

    respx.patch(f"{BASE_URL}/datasets/{dataset_id}").mock(
        return_value=httpx.Response(200, json=_dataset_body(id=dataset_id, description="updated"))
    )
    updated = client.update_dataset(dataset_id, description="updated")
    assert updated.description == "updated"

    respx.delete(f"{BASE_URL}/datasets/{dataset_id}").mock(return_value=httpx.Response(204))
    client.delete_dataset(dataset_id)  # no exception = success


@respx.mock
def test_function_crud_round_trip(client):
    function_id = str(uuid.uuid4())

    respx.post(f"{BASE_URL}/functions").mock(
        return_value=httpx.Response(201, json=_function_body(id=function_id))
    )
    created = client.create_function("clean_text", visibility=Visibility.PRIVATE)
    assert str(created.id) == function_id
    assert created.current_version == 0
    assert created.module_path is None

    respx.get(f"{BASE_URL}/functions").mock(
        return_value=httpx.Response(200, json=[_function_body(id=function_id)])
    )
    functions = client.list_functions()
    assert len(functions) == 1
    assert str(functions[0].id) == function_id

    respx.get(f"{BASE_URL}/functions/{function_id}").mock(
        return_value=httpx.Response(200, json=_function_body(id=function_id))
    )
    fetched = client.get_function(function_id)
    assert str(fetched.id) == function_id

    respx.patch(f"{BASE_URL}/functions/{function_id}").mock(
        return_value=httpx.Response(200, json=_function_body(id=function_id, description="updated"))
    )
    updated = client.update_function(function_id, description="updated")
    assert updated.description == "updated"

    respx.delete(f"{BASE_URL}/functions/{function_id}").mock(return_value=httpx.Response(204))
    client.delete_function(function_id)  # no exception = success


@respx.mock
def test_function_publish_bumps_version(client):
    function_id = str(uuid.uuid4())

    respx.post(f"{BASE_URL}/functions/{function_id}/publish").mock(
        return_value=httpx.Response(201, json=_function_version_body(function_id, version=1))
    )
    version = client.publish_function(
        function_id,
        signature="def clean_text(s: str) -> str",
        module_path="pipelines.reddit.clean_text",
    )
    assert version.version == 1
    assert version.module_path == "pipelines.reddit.clean_text"
    # published_by wasn't passed — confirm the request body left it out
    # entirely rather than sending an explicit null (see client.py's own
    # comment on why: catalog-service's `body.published_by or
    # principal.user_id` fallback only needs the key to be absent/None).
    sent_body = respx.calls.last.request.content
    assert b"published_by" not in sent_body

    respx.get(f"{BASE_URL}/functions/{function_id}/versions").mock(
        return_value=httpx.Response(200, json=[_function_version_body(function_id, version=1)])
    )
    versions = client.list_function_versions(function_id)
    assert len(versions) == 1
    assert versions[0].version == 1


@respx.mock
def test_function_promote_sets_visibility_public(client):
    function_id = str(uuid.uuid4())

    respx.post(f"{BASE_URL}/functions/{function_id}/promote").mock(
        return_value=httpx.Response(200, json=_function_body(id=function_id, visibility="public"))
    )
    promoted = client.promote_function(function_id)
    assert promoted.visibility is Visibility.PUBLIC
    # No body at all — the endpoint is unconditional (see client.py's comment).
    assert respx.calls.last.request.content == b""


@respx.mock
def test_404_surfaces_as_platform_api_error_with_status_and_detail(client):
    missing_id = str(uuid.uuid4())
    respx.get(f"{BASE_URL}/datasets/{missing_id}").mock(
        return_value=httpx.Response(404, json={"detail": "Dataset not found."})
    )
    with pytest.raises(PlatformAPIError) as exc_info:
        client.get_dataset(missing_id)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Dataset not found."


@respx.mock
def test_403_from_gateway_role_check_surfaces_correctly(client):
    respx.post(f"{BASE_URL}/datasets").mock(
        return_value=httpx.Response(403, json={"detail": "Viewers cannot create entries."})
    )
    with pytest.raises(PlatformAPIError) as exc_info:
        client.create_dataset("nope")
    assert exc_info.value.status_code == 403


def test_client_is_a_context_manager():
    with PlatformClient(gateway_url=BASE_URL) as c:
        assert c is not None
    # __exit__ closed the underlying httpx.Client — a second close() is a
    # harmless no-op (httpx's own contract), just confirming this doesn't
    # raise rather than asserting anything deeper about httpx's internals.
    c.close()


# ---- CA pinning (2026-09-02, platform-ingress branch) ---------------------
#
# Regression tests for a real bug found live the same day: gateway_url's
# default moved from a plain-HTTP port-forward to a real https:// Ingress
# hostname, and PlatformClient never pinned a CA for it — every request
# failed with CERTIFICATE_VERIFY_FAILED against platform-ca's self-signed
# cert. See client.py's own module docstring for the full story — including
# a SECOND bug found immediately after the first fix: extracting the CA
# eagerly in __init__ broke `platform login` itself, since
# platform_cli/main.py's callback constructs a PlatformClient for every
# command whether or not it ends up sending a request. These tests exercise
# _ensure_http() directly (the lazy build), not just construction, since
# construction alone no longer touches kubectl either way.


def test_https_gateway_url_pins_the_platform_ca_cert_on_first_use(monkeypatch):
    calls: list[str] = []

    def _fake_extract(kubectl_cmd: str) -> str:
        calls.append(kubectl_cmd)
        return "/tmp/fake-ca.crt"

    monkeypatch.setattr("platform_sdk.client.extract_platform_ca_cert", _fake_extract)
    monkeypatch.setattr("platform_sdk.client.cleanup_ca_cert", lambda _path: None)

    # A fake httpx.Client stand-in rather than the real thing: the real one
    # would actually try to load "/tmp/fake-ca.crt" as a cert file and fail
    # with FileNotFoundError — this test only needs to confirm PlatformClient
    # *asked* for that path to be trusted, not that httpx can load it.
    captured: dict[str, object] = {}

    class _FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr("platform_sdk.client.httpx.Client", _FakeHttpxClient)

    c = PlatformClient(gateway_url="https://gateway.platform.local")
    assert calls == []  # not yet — construction alone must not touch kubectl

    c._ensure_http()
    assert calls == ["sudo /usr/local/bin/kubectl"]  # config.py's own default
    assert captured["verify"] == "/tmp/fake-ca.crt"

    c._ensure_http()  # a second call must reuse the same client, not re-extract
    assert calls == ["sudo /usr/local/bin/kubectl"]

    c.close()


def test_constructing_a_platform_client_never_touches_kubectl(monkeypatch):
    # Regression test for the second bug: platform_cli/main.py's Typer
    # callback constructs a PlatformClient for EVERY command, including
    # `login`/`workspace invite`, which never send it a request at all — so
    # __init__ itself must never call extract_platform_ca_cert, regardless
    # of gateway_url's scheme, or `platform login` breaks whenever kubectl
    # isn't reachable (exactly backwards: login is the one command that
    # shouldn't need gateway access yet).
    def _fail_if_called(kubectl_cmd: str) -> str:
        raise AssertionError("extract_platform_ca_cert() must not be called from __init__")

    monkeypatch.setattr("platform_sdk.client.extract_platform_ca_cert", _fail_if_called)

    c = PlatformClient(gateway_url="https://gateway.platform.local")
    c.close()  # never having built a client, cleanup_ca_cert(None) — no-op


def test_http_gateway_url_never_touches_kubectl_even_on_first_use(monkeypatch):
    # The test suite's own BASE_URL ("http://gateway.test") already exercises
    # this implicitly in every other test in this file — this one pins it
    # explicitly, and proves it by making extract_platform_ca_cert raise if
    # it's ever called, rather than just not asserting on it.
    def _fail_if_called(kubectl_cmd: str) -> str:
        raise AssertionError("extract_platform_ca_cert() should never be called for a plain-HTTP gateway_url")

    monkeypatch.setattr("platform_sdk.client.extract_platform_ca_cert", _fail_if_called)

    c = PlatformClient(gateway_url=BASE_URL)
    c._ensure_http()  # the actual lazy build — still must not touch kubectl
    c.close()


@respx.mock
def test_check_module_requirements(client):
    # platform-module-deps branch (2026-09-03) — mirrors gateway's own
    # GET /modules/check-requirements response shape exactly (see
    # ModuleRequirementStatus's docstring).
    route = respx.get(f"{BASE_URL}/modules/check-requirements").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"module_id": "hello-module", "satisfied": True, "status": "Healthy"},
                    {"module_id": "other-module", "satisfied": False, "status": "not installed"},
                ]
            },
        )
    )

    results = client.check_module_requirements(["hello-module", "other-module"])

    assert len(results) == 2
    assert results[0].module_id == "hello-module"
    assert results[0].satisfied is True
    assert results[1].satisfied is False
    assert results[1].status == "not installed"
    # sent as repeated ?requires=... query params, not a single delimited string
    sent_params = route.calls.last.request.url.params.get_list("requires")
    assert sent_params == ["hello-module", "other-module"]


@respx.mock
def test_check_module_requirements_with_no_requirements_returns_empty_list(client):
    respx.get(f"{BASE_URL}/modules/check-requirements").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    assert client.check_module_requirements([]) == []
