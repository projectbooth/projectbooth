"""Unit tests for KeycloakAdminClient — same transport-mocked-via-respx
approach test_client.py uses for PlatformClient, and for the same reason:
this suite's job is "does KeycloakAdminClient send the right Admin REST API
calls and parse the right responses," not "does a live Keycloak/kubectl
port-forward work" (that can only be confirmed against a real cluster — see
bootstrap/keycloak-bootstrap-cli-client.sh's own header comment for why its
Admin API assumptions were deliberately verified against Keycloak's docs
rather than guessed, and PR/README notes for the live run that confirmed
it).

Every test here constructs KeycloakAdminClient with `_client=` already set
to a real httpx.Client pointed at a fake base_url, bypassing
`extract_platform_ca_cert()` entirely (that function shells out to kubectl —
not unit-testable without a live cluster, same as the bootstrap script
itself; see test_keycloak_connection.py for its own direct coverage,
mocking subprocess.run). That's the class's private escape hatch for tests,
not a documented public constructor argument.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from platform_sdk import InviteResult, KeycloakAdminClient, KeycloakAdminError, Role

BASE_URL = "https://keycloak.test"
REALM = "platform"


def _client(**overrides) -> KeycloakAdminClient:
    kwargs = {
        "host": "keycloak.test",
        "realm": REALM,
        "client_id": "platform-cli",
        "client_secret": "s3cr3t",
        "_client": httpx.Client(base_url=BASE_URL),
    }
    kwargs.update(overrides)
    return KeycloakAdminClient(**kwargs)


def _mock_token():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "fake-token"})
    )


def _mock_user(username: str, user_id: str = "user-1"):
    respx.get(f"{BASE_URL}/admin/realms/{REALM}/users", params={"username": username, "exact": "true"}).mock(
        return_value=httpx.Response(200, json=[{"id": user_id, "username": username}])
    )


def test_missing_client_secret_raises_immediately():
    with pytest.raises(KeycloakAdminError, match="PLATFORM_KEYCLOAK_CLIENT_SECRET"):
        KeycloakAdminClient(client_secret=None, host="keycloak.test", _client=httpx.Client(base_url=BASE_URL))


@respx.mock
def test_invite_reuses_an_existing_group_without_creating_anything():
    _mock_token()
    _mock_user("alice")
    respx.get(f"{BASE_URL}/admin/realms/{REALM}/group-by-path/workspaces/personal/viewer").mock(
        return_value=httpx.Response(200, json={"id": "group-1"})
    )
    join_route = respx.put(
        f"{BASE_URL}/admin/realms/{REALM}/users/user-1/groups/group-1"
    ).mock(return_value=httpx.Response(204))

    with _client() as admin:
        result = admin.invite("alice", workspace="personal", role=Role.VIEWER)

    assert join_route.called
    assert result == InviteResult(
        username="alice", workspace="personal", role=Role.VIEWER,
        group_path="/workspaces/personal/viewer", group_created=False,
    )


@respx.mock
def test_invite_self_heals_a_missing_workspace_and_role_group():
    _mock_token()
    _mock_user("bob", user_id="user-2")

    # Call counts below match KeycloakAdminClient's actual sequence exactly
    # (each unique URL gets ONE respx registration, with a side_effect list
    # long enough for every time that URL is actually hit — registering the
    # same route twice doesn't override the first, so this can't just
    # "correct" an earlier mock partway through):
    #   1. _ensure_role_group's own group-by-path(role_path)      -> 404
    #   2. group-by-path("workspaces")                            -> 200 (always seeded)
    #   3. _get_or_create_group(workspace path)'s first lookup    -> 404
    #      ...POST children creates it...
    #   4. _get_or_create_group(workspace path)'s re-lookup       -> 200
    #   5. _get_or_create_group(role path)'s first lookup         -> 404
    #      ...POST children creates it...
    #   6. _get_or_create_group(role path)'s re-lookup            -> 200
    respx.get(f"{BASE_URL}/admin/realms/{REALM}/group-by-path/workspaces").mock(
        return_value=httpx.Response(200, json={"id": "workspaces-group"})
    )
    respx.get(f"{BASE_URL}/admin/realms/{REALM}/group-by-path/workspaces/nfl-betting").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json={"id": "workspace-group"}),
        ]
    )
    respx.get(f"{BASE_URL}/admin/realms/{REALM}/group-by-path/workspaces/nfl-betting/editor").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(404),
            httpx.Response(200, json={"id": "role-group"}),
        ]
    )
    create_workspace_group = respx.post(
        f"{BASE_URL}/admin/realms/{REALM}/groups/workspaces-group/children"
    ).mock(return_value=httpx.Response(201, json={"id": "workspace-group"}))
    create_role_group = respx.post(
        f"{BASE_URL}/admin/realms/{REALM}/groups/workspace-group/children"
    ).mock(return_value=httpx.Response(201, json={"id": "role-group"}))

    respx.get(f"{BASE_URL}/admin/realms/{REALM}/roles/editor").mock(
        return_value=httpx.Response(200, json={"id": "role-repr-id", "name": "editor"})
    )
    map_role = respx.post(f"{BASE_URL}/admin/realms/{REALM}/groups/role-group/role-mappings/realm").mock(
        return_value=httpx.Response(204)
    )
    join_route = respx.put(f"{BASE_URL}/admin/realms/{REALM}/users/user-2/groups/role-group").mock(
        return_value=httpx.Response(204)
    )

    with _client() as admin:
        result = admin.invite("bob", workspace="nfl-betting", role=Role.EDITOR)

    assert create_workspace_group.called
    assert create_role_group.called
    assert map_role.called
    assert join_route.called
    assert result.group_created is True
    assert result.group_path == "/workspaces/nfl-betting/editor"


@respx.mock
def test_invite_unknown_username_raises_a_clear_error_not_a_bare_404():
    _mock_token()
    respx.get(f"{BASE_URL}/admin/realms/{REALM}/users", params={"username": "ghost", "exact": "true"}).mock(
        return_value=httpx.Response(200, json=[])
    )

    with _client() as admin, pytest.raises(KeycloakAdminError, match="registrationAllowed"):
        admin.invite("ghost", workspace="personal", role=Role.VIEWER)


@respx.mock
def test_bad_token_response_raises_keycloak_admin_error():
    respx.post(f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(KeycloakAdminError):
        with _client():
            pass
