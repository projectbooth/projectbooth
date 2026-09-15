"""`GET /modules/check-requirements` — module-lifecycle-plan.md item 6
(platform-module-deps branch, 2026-09-03). ARCHITECTURE.md §3: "the
dependency check lives once, at the API layer both doors call through" —
this is that one place. `platform module install` (platform-cli) is the
first caller; a future Add-ons page (item 7, still deferred) would call the
exact same endpoint with a module's own `requires` list, not a second
implementation of the satisfied/not-satisfied comparison.

Requires the same auth proxy.py's catch-all enforces (a verified token,
member of the workspace named by X-Workspace) — dependency information is
scoped the same way everything else behind gateway is, even though
argocd.py's own query isn't workspace-scoped itself (Argo CD Applications
aren't workspace-scoped resources); this endpoint doesn't leak anything an
authenticated platform-cli user couldn't already infer by attempting the
install and reading Argo CD's own error, it just answers faster and without
a wasted failed install.

`GET /modules` — ui-shell-plan.md item 4 (feature/gateway-module-registry
branch, 2026-09-08): lists installed modules with the displayName/icon/
navPath ui-shell's future nav needs (item 5, still deferred — blocked on
item 3, browser OAuth). Deliberately installed-only, no static release-time
catalog (that's item 6, bigger and separable) — same "the caller already has
what it needs locally" move this file's check-requirements already made.
Reuses `require_auth` exactly like check-requirements above, following that
same precedent rather than inventing a workspace-optional auth variant: a
verified user's own membership check gates this the same way it gates
everything else behind gateway, even though module Applications themselves
aren't workspace-scoped resources.

`GET /modules/catalog` — ui-shell-plan.md item 6 (feature/gateway-module-catalog branch,
2026-09-09): the Add-ons page's static release-time catalog `GET /modules` above deliberately left
out — every module under `src/modules/`, installed or not, overlaid with live status. Reads
`app/module_index.py`'s `load_static_module_index()` (the JSON `platform module build-index`
generates at gateway's own build time — see that module's docstring) for display metadata
(`display_name`/`icon`/`nav_path`/`requires`/`optional`), then `list_module_applications()` — the
same narrow name->health dict `check-requirements` already uses, not `list_module_summaries()` —
for live status only, defaulting to `_NOT_INSTALLED_STATUS` exactly like `check-requirements`
already does. Display metadata deliberately always comes from the static file, never from a live
Application's `platform.io/*` annotations: ARCHITECTURE.md §3 scopes the overlay to *state* only
("overlays it with... live registrations... shows each module's state"), and the static file
(regenerated every gateway release) can't go stale relative to `module.yaml` the way a long-installed
module's un-reinstalled Application annotations can. `requires`/`optional` are included even though
nothing here computes satisfaction from them — a future Install button (item 7) needs exactly this
to show a disabled state with why, and `check-requirements` above already owns that computation
("the dependency check lives once, at the API layer both doors call through" — this endpoint only
ever passes the raw list through).

`POST /modules/{module_id}/install` and `POST /modules/{module_id}/uninstall` — ui-shell-plan.md
item 7's mutation mechanism (feature/gateway-module-lifecycle-dispatch, 2026-09-10, backend), wired up
to the Add-ons page's Install/Remove buttons by feature/ui-shell-addons-mutation the same day. Both
require `require_role(derived, "editor")` on top of `require_auth`'s usual membership check —
gateway's first endpoints to gate on role rather than membership alone. On success, both dispatch
`app/github_dispatch.py`'s `trigger_module_workflow()` and return 202 immediately — fire-and-forget;
nothing here waits for or reports the triggered workflow's outcome, `GET /modules/catalog` above is
how a caller would eventually observe the resulting status change once Argo CD reconciles. `install`
reuses this file's own `_check_requires`-equivalent comparison (`list_module_applications()` +
`_SATISFIED_STATUS`/`_NOT_INSTALLED_STATUS`, exactly `check-requirements`'s own logic — a third
caller of the one place this comparison lives, not a second implementation of it) and 404s for a
`module_id` absent from the static catalog; deliberately does NOT block re-installing an
already-installed module (`platform_cli/manifest.py`'s own docstring: reinstalling is safe, "it
overwrites this file in place"). `uninstall` 404s for a `module_id` with no live Application — the
same "isn't installed" case `platform module uninstall` itself already refuses.

`GET /modules`'s `has_own_ui` field (ui-shell-plan.md item 8, feature/module-proxy, 2026-09-10) tells
ui-shell whether to attempt `GET /modules/{id}/proxy-token` — see `app/module_proxy.py` for the actual
token-minting endpoint and the `GET|POST|PUT|PATCH|DELETE /modules/{id}/proxy[/{path}]` reverse-proxy
route it authorizes, both deliberately a separate module from this one (registry/lifecycle concerns)
and from `app/proxy.py` (one fixed, startup-known backend; module_proxy.py resolves a different
backend per request).

`POST /modules/{module_id}/force-cleanup` (feature/force-cleanup, 2026-09-10) — `docs/known-issues.md`'s
deferred "Force cleanup" idea, now built: the manual `kubectl -n argocd delete application <id>`
workaround for the `modules-root` prune gap, made clickable. Same `require_role(derived, "editor")`
bar as install/uninstall, but synchronous (200, not 202) — no workflow to dispatch, `app/argocd.py`'s
`delete_module_application()` IS the action. See that function's own docstring for why this reuses
the gateway ServiceAccount's existing Kubernetes RBAC (broadened to allow `delete`) rather than a
second, Argo-CD-native credential.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import JSONResponse

from app.argocd import (
    ArgoCDUnavailableError,
    delete_module_application,
    list_module_applications,
    list_module_summaries,
)
from app.auth import AuthError, require_auth, require_role
from app.github_dispatch import GitHubDispatchError, trigger_module_workflow
from app.jwks import JWKSCache
from app.module_index import load_static_module_index

router = APIRouter()

# A module is usable by something that depends on it only once Argo CD
# reports it Healthy — Progressing/Degraded/Missing/Unknown are all "not
# satisfied," not just "absent." See this branch's plan, decision 2: a
# dependency that isn't actually up yet isn't a dependency that's met.
_SATISFIED_STATUS = "Healthy"
_NOT_INSTALLED_STATUS = "not installed"

# Matches ModuleManifest.id's own pattern (platform_cli/manifest.py) — defense in depth on a path
# param that flows into a workflow_dispatch input and eventually a CLI positional arg (subprocess
# argv, never shell-interpolated, so this isn't closing an injection hole so much as failing fast
# with a clear 422 instead of a confusing downstream 404/workflow failure for an obviously-wrong id).
_MODULE_ID_PATTERN = r"^[a-z0-9-]+$"


@router.get("/modules/check-requirements")
async def check_requirements(
    request: Request,
    # Repeated query params (?requires=a&requires=b), per this branch's plan
    # — matches how a module's own `requires: [...]` list (already a plain
    # list) gets forwarded by platform-sdk's check_module_requirements
    # without needing to invent a delimiter/encoding for a single string.
    requires: list[str] = Query(default=[]),
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    jwks: JWKSCache = request.app.state.jwks
    try:
        await require_auth(authorization, x_workspace, jwks)
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        installed = await list_module_applications()
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    results = []
    for module_id in requires:
        status = installed.get(module_id, _NOT_INSTALLED_STATUS)
        results.append(
            {"module_id": module_id, "satisfied": status == _SATISFIED_STATUS, "status": status}
        )
    return {"results": results}


@router.get("/modules")
async def list_modules(
    request: Request,
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    jwks: JWKSCache = request.app.state.jwks
    try:
        await require_auth(authorization, x_workspace, jwks)
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        modules = await list_module_summaries()
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # Every installed module regardless of status (Healthy/Progressing/
    # Degraded/Unknown), not filtered to healthy-only — how to render a
    # degraded module (grey it out, badge it, hide it) is ui-shell's call
    # once it builds real nav (item 5), not something this endpoint should
    # pre-decide by withholding data.
    return {
        "modules": [
            {
                "module_id": m.module_id,
                "display_name": m.display_name,
                "icon": m.icon,
                "nav_path": m.nav_path,
                "status": m.status,
                # ui-shell-plan.md item 8 (feature/module-proxy, 2026-09-10) — whether
                # GET /modules/{id}/proxy-token (app/module_proxy.py) has anywhere to forward to.
                "has_own_ui": m.has_own_ui,
            }
            for m in modules
        ]
    }


@router.get("/modules/catalog")
async def list_module_catalog(
    request: Request,
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    jwks: JWKSCache = request.app.state.jwks
    try:
        await require_auth(authorization, x_workspace, jwks)
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        installed = await list_module_applications()
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # A missing/malformed static index degrades to [] (see module_index.py's own docstring) rather
    # than a second error path here — an empty catalog on its own is never a 503, only Argo CD
    # unreachability above is.
    static_modules = load_static_module_index()
    return {
        "modules": [
            {
                "module_id": m["id"],
                "display_name": m["displayName"],
                "icon": m["icon"],
                "nav_path": m["navPath"],
                "requires": m["requires"],
                "optional": m["optional"],
                "status": installed.get(m["id"], _NOT_INSTALLED_STATUS),
            }
            for m in static_modules
        ]
    }


@router.post("/modules/{module_id}/install")
async def install_module(
    request: Request,
    module_id: str = Path(..., pattern=_MODULE_ID_PATTERN),
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    jwks: JWKSCache = request.app.state.jwks
    try:
        derived = await require_auth(authorization, x_workspace, jwks)
        require_role(derived, "editor")
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    static_modules = load_static_module_index()
    entry = next((m for m in static_modules if m["id"] == module_id), None)
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"{module_id!r} isn't a known module (not found in the static catalog)."},
        )

    try:
        installed = await list_module_applications()
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # Same comparison check-requirements (above) already owns — a third caller reusing it, not a
    # second implementation. Deliberately does NOT block re-installing an already-installed module;
    # see this function's docstring in this module's top-level docstring for why.
    unsatisfied = [
        {"module_id": req_id, "satisfied": False, "status": installed.get(req_id, _NOT_INSTALLED_STATUS)}
        for req_id in entry["requires"]
        if installed.get(req_id, _NOT_INSTALLED_STATUS) != _SATISFIED_STATUS
    ]
    if unsatisfied:
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"{module_id!r} declares requires that aren't installed and healthy yet.",
                "unsatisfied": unsatisfied,
            },
        )

    try:
        await trigger_module_workflow(module_id, "install")
    except GitHubDispatchError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return JSONResponse(
        status_code=202,
        content={
            "module_id": module_id,
            "action": "install",
            "status": "queued",
            "detail": "Argo CD will pick this up once the triggered workflow finishes and pushes.",
        },
    )


@router.post("/modules/{module_id}/uninstall")
async def uninstall_module(
    request: Request,
    module_id: str = Path(..., pattern=_MODULE_ID_PATTERN),
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    jwks: JWKSCache = request.app.state.jwks
    try:
        derived = await require_auth(authorization, x_workspace, jwks)
        require_role(derived, "editor")
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        installed = await list_module_applications()
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if module_id not in installed:
        return JSONResponse(
            status_code=404, content={"detail": f"{module_id!r} isn't installed — nothing to uninstall."}
        )

    try:
        await trigger_module_workflow(module_id, "uninstall")
    except GitHubDispatchError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return JSONResponse(
        status_code=202,
        content={
            "module_id": module_id,
            "action": "uninstall",
            "status": "queued",
            "detail": "Argo CD will prune this module once the triggered workflow finishes and pushes.",
        },
    )


@router.post("/modules/{module_id}/force-cleanup")
async def force_cleanup_module(
    request: Request,
    module_id: str = Path(..., pattern=_MODULE_ID_PATTERN),
    authorization: str | None = Header(default=None),
    x_workspace: str | None = Header(default=None),
):
    """`docs/known-issues.md`'s "Force cleanup" — a deliberate, manually-clicked alternative to
    `sudo kubectl -n argocd delete application <module-id>` for the `modules-root` prune gap: an
    uninstall's own workflow already succeeded (the git commit landed, `modules-enabled/*.yaml` is
    gone) but Argo CD's automated prune skipped the orphaned `Application` anyway. Same
    `require_role(derived, "editor")` bar as install/uninstall — this deletes a live cluster object,
    not a read.

    Deliberately synchronous, unlike install/uninstall's GitHub Actions dispatch: there's no workflow
    to trigger here, `app/argocd.py`'s `delete_module_application()` IS the whole action, so this
    returns 200 (not 202) once that Kubernetes API call itself completes — though the underlying
    Deployment/Service teardown Argo CD's own finalizer performs can still take a few more moments
    after that, same as it would after a manual `kubectl delete`.

    404 for a `module_id` with no live Application uses the exact same check `uninstall_module` above
    already makes — if there's no orphaned Application, there's nothing to force-clean.
    """
    jwks: JWKSCache = request.app.state.jwks
    try:
        derived = await require_auth(authorization, x_workspace, jwks)
        require_role(derived, "editor")
    except AuthError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    try:
        installed = await list_module_applications()
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if module_id not in installed:
        return JSONResponse(
            status_code=404,
            content={"detail": f"{module_id!r} has no live Application object — nothing to force-clean."},
        )

    try:
        await delete_module_application(module_id)
    except ArgoCDUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return JSONResponse(
        status_code=200,
        content={
            "module_id": module_id,
            "action": "force-cleanup",
            "status": "deleted",
            "detail": (
                "The Application object has been deleted. Argo CD's own finalizer may take a few "
                "more moments to finish tearing down its underlying resources."
            ),
        },
    )
