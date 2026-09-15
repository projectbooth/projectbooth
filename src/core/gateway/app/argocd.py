"""In-cluster Kubernetes API client for reading (and, since 2026-09-10, deleting) Argo CD
`Application` objects — platform-module-deps branch (module-lifecycle-plan.md item 6, 2026-09-03).

`app/modules.py`'s `/modules/check-requirements` endpoint needs to know which
modules are currently installed-and-healthy. Every module Application
`platform module install` writes (platform_cli/manifest.py's
render_application_manifest, platform-module-lifecycle branch) carries
`metadata.name: <module id>` and `metadata.labels: {platform.io/tier:
module}` — so "is module X up" is answerable as a single Kubernetes API list
call against Argo CD's own CRD, no separate module registry needed.

2026-09-08 (feature/gateway-module-registry branch, ui-shell-plan.md item 4):
`list_module_summaries()` below reuses that same list call (factored out as
`_fetch_module_application_items()`) but also reads each Application's
`platform.io/display-name`/`platform.io/icon`/`platform.io/nav-path`
annotations (manifest.py's `render_application_manifest()` is what writes
them) — this is the data gateway's `GET /modules` needs to hand ui-shell's
future nav. `list_module_applications()` itself is untouched: it's
`check-requirements`' existing narrower contract (name -> health only), no
reason to widen it just because a second caller now wants more.

Deliberately raw `httpx` rather than the official `kubernetes` package: this
package already talks to every other backend (catalog-service, Keycloak)
through httpx.AsyncClient, and pulling in a heavyweight, sync-first client
library for one read-only, one-shot GET would be a real departure from that
pattern (see this branch's plan, decision 3) — `asyncio.to_thread`-wrapping
a sync library call is more moving parts than this needs.

Auth: the bearer token is read fresh from the mounted ServiceAccount token
file on EVERY call, not cached — it's a cheap local file read (no network),
it's always current (kubelet rotates the projected token well before expiry;
re-reading the file picks that up for free), and caching it would just be
unnecessary complexity for no real benefit here. TLS is verified against the
in-cluster CA bundle the same ServiceAccount projection mounts.

Same "file exists -> real path, else dev-friendly fallback" shape
main.py's `_keycloak_tls_verify()` already uses: outside a real Deployment
(local dev, unit tests) these files aren't mounted, so `list_module_applications`
raises `ArgoCDUnavailableError` up front instead of crashing on a
FileNotFoundError deep inside an httpx call — `app/modules.py` turns that into
a 503, a legible "can't check requirements right now" rather than a 500.

2026-09-10 (feature/force-cleanup): `delete_module_application()` below is this module's first
WRITE — `docs/known-issues.md`'s "modules-root doesn't reliably auto-prune a module's Application
object on uninstall" gap, made clickable instead of requiring a terminal. Reuses the exact same
ServiceAccount credential every read above already holds (the gateway `Role` broadened to also
allow `delete`, not a second credential) — see that function's own docstring for the full "why not
Argo CD's own REST API instead" reasoning.
"""
from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import settings

# Every module Application platform_cli/manifest.py's render_application_manifest
# writes carries this label — see that module's docstring and this branch's
# plan, decision 2. Anything else living in the argocd namespace (root,
# modules-root, core service Applications) does NOT carry it, so this
# selector is what keeps this query scoped to modules only.
_MODULE_TIER_LABEL = "platform.io/tier=module"


class ArgoCDUnavailableError(Exception):
    """Raised for anything that stops this from getting a real answer from
    the Kubernetes API: the ServiceAccount token/CA aren't mounted (not
    running in-cluster, or RBAC/volume misconfigured), a connection failure,
    a non-2xx response (a 403 here means the Role/RoleBinding this branch
    adds is wrong or missing — see argocd/README.md), or a malformed body.

    Deliberately its own exception, not silently treated as "no modules
    installed": that would make every dependency look unsatisfied for a
    plumbing reason having nothing to do with whether the dependency is
    actually there, which is a much worse failure mode than a clear error.
    """


def _read_text(path: str, *, what: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise ArgoCDUnavailableError(
            f"{what} not found at {path!r} — not running in-cluster, or the gateway "
            "ServiceAccount isn't mounted (see argocd/manifests/gateway.yaml)."
        )
    return p.read_text().strip()


def _service_account_credentials() -> tuple[str, ssl.SSLContext]:
    """The (bearer token, TLS context) pair every call in this module needs —
    factored out of `_fetch_module_application_items()` (2026-09-03) once
    `delete_module_application()` (2026-09-10, feature/force-cleanup) needed
    the exact same pair for a second HTTP verb against the same API. Token is
    still read fresh on every call, never cached — see this module's own
    docstring for why.
    """
    token = _read_text(settings.k8s_sa_token_path, what="ServiceAccount token")
    ca_path = settings.k8s_sa_ca_path
    if not Path(ca_path).is_file():
        raise ArgoCDUnavailableError(
            f"ServiceAccount CA bundle not found at {ca_path!r} — not running in-cluster, or the "
            "gateway ServiceAccount isn't mounted (see argocd/manifests/gateway.yaml)."
        )
    # httpx>=0.28 deprecated passing `verify=<path-string>` directly (it still
    # works, just warns) in favor of building the SSLContext explicitly —
    # doing that here rather than leaving the warning in every test run.
    return token, ssl.create_default_context(cafile=ca_path)


def _applications_url(module_id: str | None = None) -> str:
    """The Kubernetes API URL for either the Application collection (list) or
    one named Application (get/delete) in `settings.argocd_namespace`."""
    base = (
        f"{settings.k8s_api_url}/apis/argoproj.io/v1alpha1/namespaces/"
        f"{settings.argocd_namespace}/applications"
    )
    return base if module_id is None else f"{base}/{module_id}"


async def _fetch_module_application_items() -> list[dict]:
    """The shared Kubernetes API call both `list_module_applications()` and
    `list_module_summaries()` build on: list every Application labeled
    `platform.io/tier=module` in `settings.argocd_namespace`, return the raw
    `items` list from the response body. Auth/TLS/error-handling live here
    exactly once — see this module's own docstring for why the token is read
    fresh every call rather than cached.
    """
    token, ssl_context = _service_account_credentials()
    url = _applications_url()
    try:
        async with httpx.AsyncClient(verify=ssl_context, timeout=settings.upstream_timeout_seconds) as client:
            response = await client.get(
                url,
                params={"labelSelector": _MODULE_TIER_LABEL},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise ArgoCDUnavailableError(f"Couldn't reach the Kubernetes API at {url!r}: {exc}") from exc

    if response.status_code != 200:
        raise ArgoCDUnavailableError(
            f"Kubernetes API returned {response.status_code} listing Applications "
            f"(labelSelector={_MODULE_TIER_LABEL!r}, namespace={settings.argocd_namespace!r}): "
            f"{response.text[:500]}"
        )

    try:
        body = response.json()
        return body["items"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ArgoCDUnavailableError(f"Unexpected response shape from the Kubernetes API: {exc}") from exc


async def list_module_applications() -> dict[str, str]:
    """Returns `{module_id: health_status}` for every Argo CD Application
    labeled `platform.io/tier=module` in `settings.argocd_namespace` — one
    entry per module that has ever been `platform module install`ed,
    regardless of its current health (a module can be `Progressing` or
    `Degraded`, not just `Healthy`; app/modules.py decides what counts as
    "satisfied", this function just reports what's really there). A module
    that was never installed simply has no key in the returned dict —
    callers check with `.get(module_id)`, not a KeyError.
    """
    items = await _fetch_module_application_items()
    result: dict[str, str] = {}
    for item in items:
        name = item.get("metadata", {}).get("name")
        if not name:
            continue
        # Absent entirely (e.g. Argo hasn't run a health check yet, right
        # after the Application was created) reads as "Unknown" — a real,
        # distinct-from-Healthy status app/modules.py reports as unsatisfied,
        # rather than this function guessing or defaulting to "Healthy".
        health_status = item.get("status", {}).get("health", {}).get("status") or "Unknown"
        result[name] = health_status
    return result


@dataclass
class ModuleSummary:
    """What `GET /modules` (app/modules.py, ui-shell-plan.md item 4) hands
    back for one installed module — display_name/icon/nav_path come from the
    `platform.io/*` annotations `platform_cli/manifest.py`'s
    `render_application_manifest()` writes onto the generated Application.

    2026-09-10 (feature/module-proxy, ui-shell-plan.md item 8): `has_own_ui` added — whether this
    Application carries a `platform.io/proxy-to` annotation at all. A bool, not the raw URL: ui-shell
    only ever needs to know whether to attempt fetching a proxy token (app/module_proxy.py), never the
    module's cluster-internal Service DNS name itself — that stays server-side only, resolved fresh
    per proxied request by `get_module_proxy_target()` below, never handed to a browser.
    """

    module_id: str
    display_name: str
    icon: str
    nav_path: str | None
    status: str
    has_own_ui: bool


async def list_module_summaries() -> list[ModuleSummary]:
    """Like `list_module_applications()`, but reads each Application's
    `platform.io/display-name`/`platform.io/icon`/`platform.io/nav-path`
    annotations too. An Application rendered before this branch (any module
    installed before `feature/gateway-module-registry` merged, still not
    reinstalled) carries none of these annotations — falls back to
    `module_id`/`"puzzle"`/`None` rather than erroring, the same "missing
    reads as a real, distinct default" discipline `list_module_applications()`
    already applies to health status. Same story for `has_own_ui` (item 8):
    since `ModuleManifest.proxyTo` is a REQUIRED module.yaml field, every module installed after
    feature/module-proxy merged always has a `proxy-to` annotation — `has_own_ui` only ever reads
    `False` for the same "installed before this branch, not yet reinstalled" transient state, not a
    module that permanently lacks a UI.
    """
    items = await _fetch_module_application_items()
    summaries: list[ModuleSummary] = []
    for item in items:
        name = item.get("metadata", {}).get("name")
        if not name:
            continue
        annotations = item.get("metadata", {}).get("annotations") or {}
        health_status = item.get("status", {}).get("health", {}).get("status") or "Unknown"
        summaries.append(
            ModuleSummary(
                module_id=name,
                display_name=annotations.get("platform.io/display-name", name),
                icon=annotations.get("platform.io/icon", "puzzle"),
                nav_path=annotations.get("platform.io/nav-path"),
                status=health_status,
                has_own_ui="platform.io/proxy-to" in annotations,
            )
        )
    return summaries


async def get_module_proxy_target(module_id: str) -> str | None:
    """The cluster-internal Service URL (module.yaml's `proxyTo`, propagated as this Application's
    `platform.io/proxy-to` annotation by `platform_cli/manifest.py`) that
    `app/module_proxy.py`'s reverse-proxy route forwards a module's own UI traffic to. `None` covers
    both "not installed at all" and "installed but no proxy-to annotation yet" (a module installed
    before feature/module-proxy merged, not yet reinstalled) — the proxy route 404s either way,
    matching `install_module`'s existing "not a known/ready module" convention; it doesn't need to
    tell those two cases apart, only whether there's somewhere to forward to right now.

    Deliberately re-resolved fresh on EVERY proxied request, never cached or baked into a minted
    proxy token — same "never cache, always read fresh" discipline `_fetch_module_application_items()`
    already applies to the ServiceAccount token itself. This means a module uninstalled mid-session
    correctly 404s the very next proxied request instead of continuing to forward into a namespace
    that's being torn down.
    """
    items = await _fetch_module_application_items()
    for item in items:
        if item.get("metadata", {}).get("name") == module_id:
            annotations = item.get("metadata", {}).get("annotations") or {}
            return annotations.get("platform.io/proxy-to")
    return None


async def delete_module_application(module_id: str) -> None:
    """`docs/known-issues.md`'s "Force cleanup" — the manual `sudo kubectl -n argocd delete
    application <module-id>` workaround for the `modules-root` prune gap, made callable from
    `app/modules.py`'s `POST /modules/{module_id}/force-cleanup` instead of requiring a terminal.

    A plain Kubernetes API DELETE against this Application, nothing more: no special cascade/
    propagation options, because plain `kubectl delete` doesn't pass any either, and the whole point
    is reproducing that exact, already-proven-safe operation. The Application already carries the
    `resources-finalizer.argocd.argoproj.io` finalizer Argo CD's own controller put there on
    creation — that finalizer is what actually cascades the delete to the underlying Deployment/
    Service (etc.) before letting the Application object itself disappear, entirely independent of
    which client (this function, or a human's `kubectl`) issued the DELETE that started it.

    2026-09-10 (feature/force-cleanup): deliberately NOT a call to Argo CD's own REST API
    (`argocd-server`) with a separate Argo CD-native credential — broadening the gateway
    ServiceAccount's existing (until now read-only) Role to also allow `delete` on
    `applications.argoproj.io` (see `argocd/manifests/gateway.yaml`'s RBAC comment) reuses the exact
    credential `_fetch_module_application_items()` above already holds, needs no new SealedSecret/
    bootstrap script/settings, and produces the identical outcome (same finalizer, same cascade) —
    "one fewer credential shape in the system" that this feature's own design note in
    `docs/known-issues.md` called for, just via the Kubernetes API rather than Argo CD's HTTP one.

    Does not check the Application exists first — `app/modules.py`'s caller already does that (the
    same `list_module_applications()` check `uninstall_module` uses) to produce a proper 404 with a
    clear message. If the Application is somehow already gone by the time this actually runs (a race
    with something else deleting it, or a genuine re-click), the Kubernetes API's own 404 here is
    treated as success — the end state ("no orphaned Application") is exactly what force-cleanup was
    asked to produce, so there's nothing to surface as an error.
    """
    token, ssl_context = _service_account_credentials()
    url = _applications_url(module_id)
    try:
        async with httpx.AsyncClient(verify=ssl_context, timeout=settings.upstream_timeout_seconds) as client:
            response = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ArgoCDUnavailableError(f"Couldn't reach the Kubernetes API at {url!r}: {exc}") from exc

    if response.status_code == 404:
        return

    if response.status_code not in (200, 202):
        raise ArgoCDUnavailableError(
            f"Kubernetes API returned {response.status_code} deleting Application {module_id!r} "
            f"(namespace={settings.argocd_namespace!r}): {response.text[:500]}"
        )
