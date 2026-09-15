// Talks to gateway's GET /modules/{id}/proxy-token (src/core/gateway/app/module_proxy.py,
// ui-shell-plan.md item 8). Sibling to ./api.ts's GET /modules wrapper and ../addons/mutations.ts's
// POST wrapper — same "hand-rolled fetch() per endpoint" choice both of those already make, now for
// a third, equally narrow gateway call.
//
// A plain <iframe src> navigation (ModuleDetail.tsx) can't send a custom Authorization header — this
// is the one authenticated fetch() ModuleDetail makes to get a short-lived, module-scoped token it
// then embeds as ?token= in that iframe's src instead. See module_proxy.py's own module docstring for
// the full "why a token, not a cookie" reasoning; ui-shell doesn't need to re-derive any of it, only
// to call this endpoint once per page view and hand the result to the iframe.
import { gatewayBaseUrl } from './config'

export interface ModuleProxyToken {
  token: string
  expiresIn: number
}

/** Mirrors ./api.ts's ModulesError / ../addons/mutations.ts's AddonMutationError — same
 * (status, detail) shape every gateway-error class in this codebase already uses. */
export class ModuleProxyTokenError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(`GET .../proxy-token failed: ${status} ${detail}`)
    this.name = 'ModuleProxyTokenError'
    this.status = status
    this.detail = detail
  }
}

export async function fetchModuleProxyToken(
  moduleId: string,
  workspace: string,
  accessToken: string,
  signal?: AbortSignal,
): Promise<ModuleProxyToken> {
  const response = await fetch(`${gatewayBaseUrl()}/modules/${encodeURIComponent(moduleId)}/proxy-token`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'X-Workspace': workspace,
    },
    signal,
  })

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // Non-JSON error body (shouldn't happen against a real gateway response) — fall back to
      // statusText, already assigned above. Same defensive pattern every other error path here uses.
    }
    throw new ModuleProxyTokenError(response.status, detail)
  }

  const body = (await response.json()) as { token: string; expires_in: number }
  return { token: body.token, expiresIn: body.expires_in }
}
