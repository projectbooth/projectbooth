"""CLI-level tests — CliRunner drives `platform ...` exactly as a real
shell invocation would, but platform_cli.main.PlatformClient is
monkeypatched to a fake with no network/HTTP involved at all. That's a
deliberate second layer below platform-sdk's own respx-mocked tests: those
prove PlatformClient talks HTTP correctly, these prove the CLI wires
flags/output/errors around *a* client correctly — a fake stands in fine,
since PlatformClient's own behavior isn't what's under test here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from platform_sdk import (
    Dataset,
    DeviceAuthorization,
    Function,
    FunctionVersion,
    InviteResult,
    KeycloakAdminError,
    NotAuthenticatedError,
    PlatformAPIError,
    PlatformLoginError,
    Principal,
    Role,
    TokenSet,
    Visibility,
    Workspace,
)
from typer.testing import CliRunner

import platform_cli.login as login_module
import platform_cli.main as main_module
import platform_cli.workspace as workspace_module
from platform_cli.main import app

runner = CliRunner()


class FakeClient:
    """Stands in for PlatformClient. Records calls (for assertions) and
    returns canned/queued responses — no network, no real catalog-service."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, tuple, dict]] = []
        self._responses: dict[str, object] = {}

    def queue(self, method: str, value) -> None:
        self._responses[method] = value

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        result = self._responses.get(method)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass

    def me(self):
        return self._record("me")

    def list_workspaces(self):
        return self._record("list_workspaces") or []

    def create_workspace(self, name, display_name):
        return self._record("create_workspace", name, display_name)

    def get_workspace(self, workspace_id):
        return self._record("get_workspace", workspace_id)

    def list_datasets(self):
        return self._record("list_datasets") or []

    def create_dataset(self, name, *, visibility=Visibility.PRIVATE, description=None, location_uri=None):
        return self._record(
            "create_dataset", name, visibility=visibility, description=description, location_uri=location_uri
        )

    def get_dataset(self, dataset_id):
        return self._record("get_dataset", dataset_id)

    def update_dataset(self, dataset_id, **fields):
        return self._record("update_dataset", dataset_id, **fields)

    def delete_dataset(self, dataset_id):
        return self._record("delete_dataset", dataset_id)

    def list_functions(self):
        return self._record("list_functions") or []

    def create_function(self, name, *, visibility=Visibility.PRIVATE, description=None, module_path=None):
        return self._record(
            "create_function", name, visibility=visibility, description=description, module_path=module_path
        )

    def get_function(self, function_id):
        return self._record("get_function", function_id)

    def update_function(self, function_id, **fields):
        return self._record("update_function", function_id, **fields)

    def delete_function(self, function_id):
        return self._record("delete_function", function_id)

    def list_function_versions(self, function_id):
        return self._record("list_function_versions", function_id) or []

    def publish_function(self, function_id, *, signature, module_path, docstring=None, published_by=None):
        return self._record(
            "publish_function",
            function_id,
            signature=signature,
            module_path=module_path,
            docstring=docstring,
            published_by=published_by,
        )

    def promote_function(self, function_id):
        return self._record("promote_function", function_id)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(main_module, "PlatformClient", lambda **kwargs: client)
    return client


class FakeKeycloakAdminClient:
    """Stands in for KeycloakAdminClient — no real port-forward/kubectl/HTTP
    involved. `invite` is the only command that uses this, and it's the one
    command in this file NOT covered by platform-sdk's own respx-mocked
    tests for KeycloakAdminClient (that suite proves the Admin API calls are
    right; this one proves the CLI wires flags/output/errors around *a*
    client correctly — same split as FakeClient/PlatformClient above)."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.invite_calls: list[tuple] = []
        self._invite_response: InviteResult | Exception | None = None

    def queue_invite(self, value) -> None:
        self._invite_response = value

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def invite(self, username, *, workspace, role):
        self.invite_calls.append((username, workspace, role))
        if isinstance(self._invite_response, Exception):
            raise self._invite_response
        return self._invite_response


@pytest.fixture
def fake_keycloak(monkeypatch):
    client = FakeKeycloakAdminClient()
    monkeypatch.setattr(workspace_module, "KeycloakAdminClient", lambda **kwargs: client)
    return client


def _workspace(name="personal") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, display_name=name.title(), created_at=datetime.now(UTC))


def _dataset(name="reddit-sentiment", **overrides) -> Dataset:
    fields = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name=name,
        visibility=Visibility.PRIVATE,
        description=None,
        location_uri=None,
        created_by="alice",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return Dataset(**fields)


def _function(name="clean_text", **overrides) -> Function:
    fields = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name=name,
        visibility=Visibility.PRIVATE,
        description=None,
        current_version=0,
        module_path=None,
        created_by="alice",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return Function(**fields)


def _function_version(function_id, version=1, **overrides) -> FunctionVersion:
    fields = dict(
        id=uuid.uuid4(),
        function_id=function_id,
        version=version,
        signature="def clean_text(s: str) -> str",
        docstring=None,
        module_path="pipelines.reddit.clean_text",
        published_by="alice",
        published_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return FunctionVersion(**fields)


def test_me_prints_principal(fake_client):
    principal = Principal(
        workspace_id=uuid.uuid4(), workspace_name="personal", user_id="alice", role="owner"
    )
    fake_client.queue("me", principal)
    result = runner.invoke(app, ["me"])
    assert result.exit_code == 0, result.output
    assert "personal" in result.output
    assert "alice" in result.output
    assert "owner" in result.output


def test_workspace_list_empty_says_so(fake_client):
    result = runner.invoke(app, ["workspace", "list"])
    assert result.exit_code == 0
    assert "No workspaces" in result.output


def test_workspace_create_defaults_display_name_to_title_case(fake_client):
    fake_client.queue("create_workspace", _workspace("nfl-betting"))
    result = runner.invoke(app, ["workspace", "create", "nfl-betting"])
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert method == "create_workspace"
    assert args == ("nfl-betting", "Nfl-Betting")  # str.title() — good enough for this pass, see README


def test_dataset_create_passes_visibility_case_insensitively(fake_client):
    fake_client.queue("create_dataset", _dataset("public-thing", visibility=Visibility.PUBLIC))
    result = runner.invoke(app, ["dataset", "create", "public-thing", "--visibility", "PUBLIC"])
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert kwargs["visibility"] == Visibility.PUBLIC


def test_dataset_update_with_no_flags_exits_nonzero_without_calling_client(fake_client):
    result = runner.invoke(app, ["dataset", "update", str(uuid.uuid4())])
    assert result.exit_code == 1
    assert fake_client.calls == []  # never even reached the client


def test_dataset_update_only_sends_the_flags_actually_passed(fake_client):
    dataset_id = str(uuid.uuid4())
    fake_client.queue("update_dataset", _dataset(description="new desc"))
    result = runner.invoke(app, ["dataset", "update", dataset_id, "--description", "new desc"])
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert args == (dataset_id,)
    assert kwargs == {"description": "new desc"}  # visibility/location_uri NOT present, not None-valued


def test_function_create_passes_visibility_case_insensitively(fake_client):
    fake_client.queue("create_function", _function("public-fn", visibility=Visibility.PUBLIC))
    result = runner.invoke(app, ["function", "create", "public-fn", "--visibility", "PUBLIC"])
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert kwargs["visibility"] == Visibility.PUBLIC


def test_function_update_with_no_flags_exits_nonzero_without_calling_client(fake_client):
    result = runner.invoke(app, ["function", "update", str(uuid.uuid4())])
    assert result.exit_code == 1
    assert fake_client.calls == []


def test_function_update_only_sends_the_flags_actually_passed(fake_client):
    function_id = str(uuid.uuid4())
    fake_client.queue("update_function", _function(description="new desc"))
    result = runner.invoke(app, ["function", "update", function_id, "--description", "new desc"])
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert args == (function_id,)
    assert kwargs == {"description": "new desc"}  # visibility NOT present, not None-valued


def test_function_publish_requires_signature_and_module_path(fake_client):
    # Typer's own "missing option" enforcement, not this repo's code — just
    # confirming these two really are required, matching FunctionPublish's
    # schema (app/schemas.py), not accidentally optional.
    result = runner.invoke(app, ["function", "publish", str(uuid.uuid4())])
    assert result.exit_code != 0
    assert fake_client.calls == []


def test_function_publish_sends_flags_and_prints_version(fake_client):
    function_id = str(uuid.uuid4())
    fake_client.queue("publish_function", _function_version(function_id, version=1))
    result = runner.invoke(
        app,
        [
            "function", "publish", function_id,
            "--signature", "def clean_text(s: str) -> str",
            "--module-path", "pipelines.reddit.clean_text",
        ],
    )
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert args == (function_id,)
    assert kwargs["signature"] == "def clean_text(s: str) -> str"
    assert kwargs["module_path"] == "pipelines.reddit.clean_text"
    assert kwargs["published_by"] is None  # not passed, not invented
    assert "v1" in result.output


def test_function_promote_takes_no_flags_and_prints_new_visibility(fake_client):
    function_id = str(uuid.uuid4())
    fake_client.queue("promote_function", _function(id=function_id, visibility=Visibility.PUBLIC))
    result = runner.invoke(app, ["function", "promote", function_id])
    assert result.exit_code == 0, result.output
    method, args, kwargs = fake_client.calls[-1]
    assert args == (function_id,)
    assert kwargs == {}  # no flags to pass through — the endpoint takes no body
    assert "public" in result.output


def test_function_versions_lists_newest_first_as_given_by_the_client(fake_client):
    function_id = str(uuid.uuid4())
    fake_client.queue(
        "list_function_versions",
        [_function_version(function_id, version=2), _function_version(function_id, version=1)],
    )
    result = runner.invoke(app, ["function", "versions", function_id])
    assert result.exit_code == 0, result.output
    assert "v2" in result.output
    assert "v1" in result.output


def test_function_versions_empty_says_so(fake_client):
    result = runner.invoke(app, ["function", "versions", str(uuid.uuid4())])
    assert result.exit_code == 0
    assert "No published versions" in result.output


def test_api_error_prints_to_stderr_and_exits_1(fake_client):
    error = PlatformAPIError(404, "Dataset not found.", method="GET", url="/datasets/x")
    fake_client.queue("get_dataset", error)
    result = runner.invoke(app, ["dataset", "get", "some-id"])
    assert result.exit_code == 1
    assert "404" in result.output
    assert "Dataset not found." in result.output


def test_workspace_invite_defaults_role_to_viewer_and_workspace_to_setting(fake_keycloak, monkeypatch):
    # Confirms the "personal" fallback rather than an env leak from another test.
    monkeypatch.delenv("PLATFORM_WORKSPACE", raising=False)
    fake_keycloak.queue_invite(
        InviteResult(
            username="alice", workspace="personal", role=Role.VIEWER,
            group_path="/workspaces/personal/viewer", group_created=False,
        )
    )
    result = runner.invoke(app, ["workspace", "invite", "alice"])
    assert result.exit_code == 0, result.output
    username, workspace, role = fake_keycloak.invite_calls[-1]
    assert (username, workspace, role) == ("alice", "personal", Role.VIEWER)
    assert "Reused" in result.output
    assert "/workspaces/personal/viewer" in result.output


def test_workspace_invite_passes_workspace_and_role_flags(fake_keycloak):
    fake_keycloak.queue_invite(
        InviteResult(
            username="bob", workspace="nfl-betting", role=Role.EDITOR,
            group_path="/workspaces/nfl-betting/editor", group_created=True,
        )
    )
    result = runner.invoke(
        app, ["workspace", "invite", "bob", "--workspace", "nfl-betting", "--role", "editor"]
    )
    assert result.exit_code == 0, result.output
    assert fake_keycloak.invite_calls[-1] == ("bob", "nfl-betting", Role.EDITOR)
    assert "Created" in result.output


def test_workspace_invite_keycloak_error_prints_and_exits_1(fake_keycloak):
    fake_keycloak.queue_invite(KeycloakAdminError("No Keycloak user named 'ghost' in realm 'platform'."))
    result = runner.invoke(app, ["workspace", "invite", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output


# ---- auth: gateway_url/workspace flag passthrough, removed --user/--role,
# NotAuthenticatedError surfacing, `platform login` -----------------------


def test_gateway_url_and_workspace_flags_passed_to_platform_client(monkeypatch):
    captured: dict = {}

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

        def me(self):
            return Principal(workspace_id=uuid.uuid4(), workspace_name="x", user_id="y", role="owner")

    monkeypatch.setattr(main_module, "PlatformClient", _Client)
    result = runner.invoke(app, ["--gateway-url", "http://gateway.test", "--workspace", "nfl-betting", "me"])
    assert result.exit_code == 0, result.output
    assert captured == {"gateway_url": "http://gateway.test", "workspace": "nfl-betting"}


@pytest.mark.parametrize("removed_flag", ["--user", "--role", "--catalog-url"])
def test_removed_flags_are_rejected_not_silently_ignored(fake_client, removed_flag):
    # --user/--role/--catalog-url no longer exist (see main.py's module
    # docstring) — confirms they fail loudly (Typer's "no such option")
    # rather than being silently accepted and doing nothing.
    result = runner.invoke(app, [removed_flag, "whatever", "me"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_not_authenticated_error_prints_fix_and_exits_1(fake_client):
    fake_client.queue("me", NotAuthenticatedError("Not logged in — run `platform login` first."))
    result = runner.invoke(app, ["me"])
    assert result.exit_code == 1
    assert "platform login" in result.output


class FakeLoginFlow:
    """Stands in for KeycloakLoginFlow — no real port-forward/kubectl/HTTP.
    Same fake-at-the-boundary split as FakeClient/FakeKeycloakAdminClient
    above: platform-sdk's own respx-mocked test_keycloak_login.py proves the
    device-flow HTTP calls are right; this proves `platform login` wires
    its output/error-handling around *a* flow correctly."""

    def __init__(self, device_auth: DeviceAuthorization, *, token_set=None, poll_exception=None):
        self._device_auth = device_auth
        self._token_set = token_set
        self._poll_exception = poll_exception

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def start_device_authorization(self) -> DeviceAuthorization:
        return self._device_auth

    def poll_for_token(self, device_auth: DeviceAuthorization):
        if self._poll_exception is not None:
            raise self._poll_exception
        return self._token_set


def _device_auth(verification_uri_complete: str | None = "https://keycloak.test/device?user_code=ABCD-EFGH"):
    return DeviceAuthorization(
        device_code="dc-1",
        user_code="ABCD-EFGH",
        verification_uri="https://keycloak.test/device",
        verification_uri_complete=verification_uri_complete,
        expires_in=600,
        interval=5,
    )


def test_login_success_prints_verification_url_and_saves_credentials(monkeypatch):
    token_set = TokenSet(
        access_token="at-1", refresh_token="rt-1", expires_at=datetime.now(UTC), preferred_username="alice"
    )
    fake_flow = FakeLoginFlow(_device_auth(), token_set=token_set)
    monkeypatch.setattr(login_module, "KeycloakLoginFlow", lambda **kwargs: fake_flow)
    saved: list[TokenSet] = []
    monkeypatch.setattr(login_module, "save_credentials", saved.append)

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0, result.output
    assert "https://keycloak.test/device?user_code=ABCD-EFGH" in result.output
    assert "Logged in as alice" in result.output
    assert saved == [token_set]


def test_login_falls_back_to_uri_plus_code_when_complete_form_absent(monkeypatch):
    token_set = TokenSet(
        access_token="at-1", refresh_token="rt-1", expires_at=datetime.now(UTC), preferred_username=None
    )
    fake_flow = FakeLoginFlow(_device_auth(verification_uri_complete=None), token_set=token_set)
    monkeypatch.setattr(login_module, "KeycloakLoginFlow", lambda **kwargs: fake_flow)
    monkeypatch.setattr(login_module, "save_credentials", lambda _ts: None)

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0, result.output
    assert "https://keycloak.test/device" in result.output
    assert "ABCD-EFGH" in result.output
    assert "Logged in as you" in result.output  # no preferred_username -> generic fallback


def test_login_failure_prints_and_exits_1(monkeypatch):
    fake_flow = FakeLoginFlow(
        _device_auth(), poll_exception=PlatformLoginError("Login was denied in the browser.")
    )
    monkeypatch.setattr(login_module, "KeycloakLoginFlow", lambda **kwargs: fake_flow)

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 1
    assert "denied" in result.output
