"""Triggers the `module-lifecycle.yml` GitHub Actions workflow — ui-shell-plan.md item 7's mutation
mechanism (feature/gateway-module-lifecycle-dispatch branch, 2026-09-10). This is the ONE place
gateway talks to GitHub's own API, and deliberately the only credential-bearing half of the whole
mechanism: gateway holds a fine-grained Personal Access Token scoped to Actions: read/write on this
one repo (`settings.github_token`, sourced from a SealedSecret — see argocd/manifests/gateway.yaml)
and does nothing more privileged than ask GitHub to *start* a workflow run. The actual git commit and
push happen inside that run, authenticated with GitHub Actions' own ephemeral, auto-scoped
`GITHUB_TOKEN` — gateway's PAT never touches this repo's contents, only its Actions API. See
docs/architecture/ui-shell-plan.md item 7's own writeup for the full trust-boundary reasoning.

Raw `httpx`, not a GitHub SDK — same "already talks to every other backend through httpx" precedent
app/argocd.py's own docstring gives for its raw Kubernetes API calls: one POST endpoint doesn't
justify a whole client library dependency.
"""
from __future__ import annotations

import httpx

from app.config import settings


class GitHubDispatchError(Exception):
    """Raised for anything that stops this from actually starting the workflow: the PAT isn't
    configured (a real, distinct startup gap — see settings.github_token's own default), a
    connection failure, or a non-204 response (GitHub's workflow_dispatch endpoint answers 204 No
    Content on success; anything else — 401 (bad/expired PAT), 404 (wrong repo/workflow file name),
    422 (bad ref/inputs) — is a real failure, not a partial success). app/modules.py turns this into
    a 503, the same "a real, distinct exception -> a clear 503" shape ArgoCDUnavailableError already
    uses for the Kubernetes API's own failure modes."""


async def trigger_module_workflow(module_id: str, action: str) -> None:
    """Dispatches `settings.github_workflow_file` against `settings.github_dispatch_ref` (always
    `dev` — the same branch every self-referencing Application and every generated module
    Application already targets), passing `module_id`/`action` as the workflow's own `inputs:` —
    see .github/workflows/module-lifecycle.yml, which does nothing more than run `platform module
    install/uninstall <module_id>` with those exact values. Fire-and-forget: a 204 here only means
    GitHub accepted the request to start a run, not that the run has finished, or even started yet
    — app/modules.py's callers reflect that in their own response (a 202, "queued", not "done").
    """
    if not settings.github_token:
        raise GitHubDispatchError(
            "GATEWAY_GITHUB_TOKEN isn't configured — the SealedSecret this reads from either "
            "hasn't been created yet, or gateway hasn't been rolled out since it was (see "
            "bootstrap/seal-gateway-github-token.sh and gateway/README.md)."
        )

    url = (
        f"{settings.github_api_url}/repos/{settings.github_repo}/actions/workflows/"
        f"{settings.github_workflow_file}/dispatches"
    )
    body = {"ref": settings.github_dispatch_ref, "inputs": {"module_id": module_id, "action": action}}
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubDispatchError(f"Couldn't reach the GitHub API at {url!r}: {exc}") from exc

    if response.status_code != 204:
        raise GitHubDispatchError(
            f"GitHub returned {response.status_code} dispatching {settings.github_workflow_file!r} "
            f"(module_id={module_id!r}, action={action!r}): {response.text[:500]}"
        )
