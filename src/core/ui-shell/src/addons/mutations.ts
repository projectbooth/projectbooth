// Talks to gateway's POST /modules/{id}/install, .../uninstall, and
// .../force-cleanup (src/core/gateway/app/modules.py, ui-shell-plan.md item
// 7's mutation mechanism — feature/gateway-module-lifecycle-dispatch, already
// live-verified end to end against homelab-dev — plus feature/force-cleanup's
// third endpoint). Structurally a sibling of ./api.ts's GET /modules/catalog
// wrapper, not a shared helper — same "hand-rolled per endpoint" choice that
// file's own comment already makes, now applied to POSTs instead of a GET.
//
// install/uninstall are fire-and-forget: a 202 means gateway told GitHub to
// start the module-lifecycle workflow, nothing more. Neither call's success
// means the module is actually installed/removed yet — see useAddonMutations
// for how the rest of the page turns that into something a user can watch.
// force-cleanup is different — it's a synchronous 200, the delete already
// happened by the time the response comes back (see app/modules.py's
// force-cleanup docstring for why) — but the caller (useAddonMutations)
// still folds a successful call back into the same 'queued' mutation-state
// shape as a fresh uninstall, so the existing polling/resolution machinery
// picks up from there unmodified.
import { gatewayBaseUrl } from '../modules/config'

export type AddonMutationAction = 'install' | 'uninstall'

/** Mirrors ./api.ts's AddonsError, plus one field: gateway's 409 (unsatisfied
 * `requires`) carries a structured `unsatisfied` list alongside `detail` —
 * see app/modules.py's install_module docstring for the exact response
 * shape. Every other error status (401/403/404/422/503) only ever has
 * `detail`, so this stays optional rather than every AddonMutationError
 * needing one. */
export class AddonMutationError extends Error {
  status: number
  detail: string
  unsatisfied?: string[]

  constructor(action: AddonMutationAction, status: number, detail: string, unsatisfied?: string[]) {
    super(`${action} failed: ${status} ${detail}`)
    this.name = 'AddonMutationError'
    this.status = status
    this.detail = detail
    this.unsatisfied = unsatisfied
  }
}

// `action` labels the error (and picks which mutation family a caller should
// treat this as); `path` is the actual URL segment. These coincide for
// install/uninstall but not for force-cleanup, which hits a third endpoint
// while still being an 'uninstall'-family error if it fails.
async function postMutation(action: AddonMutationAction, path: string, moduleId: string, workspace: string, accessToken: string, signal?: AbortSignal): Promise<void> {
  const response = await fetch(`${gatewayBaseUrl()}/modules/${encodeURIComponent(moduleId)}/${path}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'X-Workspace': workspace,
    },
    signal,
  })

  if (!response.ok) {
    let detail = response.statusText
    let unsatisfied: string[] | undefined
    try {
      const body = (await response.json()) as { detail?: string; unsatisfied?: string[] }
      if (body.detail) detail = body.detail
      if (Array.isArray(body.unsatisfied)) unsatisfied = body.unsatisfied
    } catch {
      // Non-JSON error body (shouldn't happen against a real gateway
      // response, but don't let a parse failure mask the original HTTP
      // status) — fall back to statusText, already assigned above.
    }
    throw new AddonMutationError(action, response.status, detail, unsatisfied)
  }

  // The success body (202's {module_id, action, status: "queued", detail},
  // or force-cleanup's 200 {module_id, action, status: "deleted", detail})
  // has nothing the caller needs — the point of calling this is "did the
  // request succeed," not the echoed body. useAddonMutations tracks
  // mutation state itself.
}

export function installModule(moduleId: string, workspace: string, accessToken: string, signal?: AbortSignal): Promise<void> {
  return postMutation('install', 'install', moduleId, workspace, accessToken, signal)
}

export function uninstallModule(moduleId: string, workspace: string, accessToken: string, signal?: AbortSignal): Promise<void> {
  return postMutation('uninstall', 'uninstall', moduleId, workspace, accessToken, signal)
}

/** The "Force cleanup" workaround, made callable — see app/modules.py's
 * force-cleanup docstring and docs/known-issues.md's modules-root prune-gap
 * entry for why this exists. Labeled as an 'uninstall'-family error on
 * failure: it's conceptually finishing an uninstall that got stuck, not a
 * fourth kind of mutation. */
export function forceCleanupModule(moduleId: string, workspace: string, accessToken: string, signal?: AbortSignal): Promise<void> {
  return postMutation('uninstall', 'force-cleanup', moduleId, workspace, accessToken, signal)
}
