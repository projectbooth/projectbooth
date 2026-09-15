"""Unit tests for app/argocd.py — respx-mocked Kubernetes API, no real
cluster. Proves list_module_applications()'s parsing/error-handling; the
actual RBAC granting real access can only be confirmed live (this branch's
plan, "What can only be confirmed live").
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app import argocd
from app.config import settings

K8S_URL = (
    f"{settings.k8s_api_url}/apis/argoproj.io/v1alpha1/namespaces/{settings.argocd_namespace}/applications"
)


def _application(name: str, health_status: str | None) -> dict:
    status = {"health": {"status": health_status}} if health_status is not None else {}
    return {"metadata": {"name": name, "labels": {"platform.io/tier": "module"}}, "status": status}


# mounted_sa fixture lives in conftest.py — shared with test_modules.py,
# which needs the same "list_module_applications() can actually run"
# starting point behind its endpoint test client.


@respx.mock
async def test_returns_health_status_per_module(mounted_sa):
    respx.get(K8S_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    _application("hello-module", "Healthy"),
                    _application("other-module", "Progressing"),
                ]
            },
        )
    )

    result = await argocd.list_module_applications()

    assert result == {"hello-module": "Healthy", "other-module": "Progressing"}


@respx.mock
async def test_missing_module_is_simply_absent_from_the_result(mounted_sa):
    respx.get(K8S_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    result = await argocd.list_module_applications()

    assert result == {}
    assert "hello-module" not in result


@respx.mock
async def test_application_with_no_health_status_yet_reads_as_unknown(mounted_sa):
    respx.get(K8S_URL).mock(
        return_value=httpx.Response(200, json={"items": [_application("fresh-module", None)]})
    )

    result = await argocd.list_module_applications()

    assert result == {"fresh-module": "Unknown"}


@respx.mock
async def test_request_is_scoped_to_the_module_tier_label_and_configured_namespace(mounted_sa):
    route = respx.get(K8S_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    await argocd.list_module_applications()

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.params["labelSelector"] == "platform.io/tier=module"
    assert request.headers["authorization"] == "Bearer fake-sa-token"


@respx.mock
async def test_non_200_response_raises_argocd_unavailable(mounted_sa):
    # A 403 here is exactly what a wrong/missing Role or RoleBinding would
    # produce live — see this branch's plan, "What can only be confirmed
    # live," and argocd.py's own ArgoCDUnavailableError docstring.
    respx.get(K8S_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="403"):
        await argocd.list_module_applications()


@respx.mock
async def test_connection_failure_raises_argocd_unavailable(mounted_sa):
    respx.get(K8S_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="Couldn't reach"):
        await argocd.list_module_applications()


@respx.mock
async def test_malformed_response_body_raises_argocd_unavailable(mounted_sa):
    respx.get(K8S_URL).mock(return_value=httpx.Response(200, json={"not-items": []}))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="Unexpected response shape"):
        await argocd.list_module_applications()


async def test_missing_token_file_raises_argocd_unavailable_without_any_http_call(monkeypatch, tmp_path):
    # Deliberately no @respx.mock here: if this made an HTTP call at all
    # (rather than failing fast on the missing file), respx would raise for
    # the unmocked request, which would also fail the test, but the message
    # this asserts on is the real evidence it never got that far.
    monkeypatch.setattr(settings, "k8s_sa_token_path", str(tmp_path / "no-such-token"))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="ServiceAccount token"):
        await argocd.list_module_applications()


async def test_missing_ca_file_raises_argocd_unavailable_without_any_http_call(monkeypatch, tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("fake-sa-token")
    monkeypatch.setattr(settings, "k8s_sa_token_path", str(token_path))
    monkeypatch.setattr(settings, "k8s_sa_ca_path", str(tmp_path / "no-such-ca"))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="CA bundle"):
        await argocd.list_module_applications()


# --- get_module_proxy_target() — ui-shell-plan.md item 8, feature/module-proxy, 2026-09-10 ---


@respx.mock
async def test_get_module_proxy_target_returns_the_proxy_to_annotation(mounted_sa):
    respx.get(K8S_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {
                            "name": "hello-module",
                            "annotations": {"platform.io/proxy-to": "http://hello-module.hello-module.svc:80"},
                        },
                        "status": {"health": {"status": "Healthy"}},
                    }
                ]
            },
        )
    )

    result = await argocd.get_module_proxy_target("hello-module")

    assert result == "http://hello-module.hello-module.svc:80"


@respx.mock
async def test_get_module_proxy_target_is_none_when_installed_but_no_annotation(mounted_sa):
    # A module installed before feature/module-proxy merged, not yet reinstalled.
    respx.get(K8S_URL).mock(
        return_value=httpx.Response(200, json={"items": [_application("hello-module", "Healthy")]})
    )

    result = await argocd.get_module_proxy_target("hello-module")

    assert result is None


@respx.mock
async def test_get_module_proxy_target_is_none_when_not_installed(mounted_sa):
    respx.get(K8S_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    result = await argocd.get_module_proxy_target("hello-module")

    assert result is None


@respx.mock
async def test_list_module_summaries_has_own_ui_reflects_proxy_to_annotation(mounted_sa):
    respx.get(K8S_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {
                            "name": "hello-module",
                            "annotations": {
                                "platform.io/display-name": "Hello Module",
                                "platform.io/proxy-to": "http://hello-module.hello-module.svc:80",
                            },
                        },
                        "status": {"health": {"status": "Healthy"}},
                    },
                    {
                        "metadata": {"name": "old-module", "annotations": {}},
                        "status": {"health": {"status": "Healthy"}},
                    },
                ]
            },
        )
    )

    summaries = {s.module_id: s.has_own_ui for s in await argocd.list_module_summaries()}

    assert summaries == {"hello-module": True, "old-module": False}


# --- delete_module_application() — Force cleanup, feature/force-cleanup, 2026-09-10 ---

APPLICATION_URL = f"{K8S_URL}/hello-module"


@respx.mock
async def test_delete_module_application_sends_a_plain_delete(mounted_sa):
    route = respx.delete(APPLICATION_URL).mock(return_value=httpx.Response(200))

    await argocd.delete_module_application("hello-module")

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer fake-sa-token"
    # No cascade/propagation query params — see the function's own docstring
    # for why this deliberately mirrors a bare `kubectl delete`.
    assert len(request.url.params) == 0


@respx.mock
async def test_delete_module_application_accepts_202(mounted_sa):
    # The Kubernetes API can return 202 for a delete still processing behind
    # a finalizer — this is success too, not something the caller should see
    # as an error.
    respx.delete(APPLICATION_URL).mock(return_value=httpx.Response(202))

    await argocd.delete_module_application("hello-module")


@respx.mock
async def test_delete_module_application_treats_404_as_success(mounted_sa):
    # Already gone (a race, or a genuine re-click) — the end state
    # force-cleanup was asked to produce already holds, so this must not
    # raise.
    respx.delete(APPLICATION_URL).mock(return_value=httpx.Response(404, text="not found"))

    await argocd.delete_module_application("hello-module")


@respx.mock
async def test_delete_module_application_raises_on_other_non_2xx(mounted_sa):
    respx.delete(APPLICATION_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="403"):
        await argocd.delete_module_application("hello-module")


@respx.mock
async def test_delete_module_application_connection_failure_raises_argocd_unavailable(mounted_sa):
    respx.delete(APPLICATION_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(argocd.ArgoCDUnavailableError, match="Couldn't reach"):
        await argocd.delete_module_application("hello-module")
