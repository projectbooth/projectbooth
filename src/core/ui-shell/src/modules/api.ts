// Talks to gateway's GET /modules (src/core/gateway/app/modules.py,
// ui-shell-plan.md item 4 — already live-verified end to end from a browser
// console during item 3's own verification). Deliberately plain fetch(), no
// data-fetching library: one GET endpoint, refetched only on workspace
// change, no mutations/pagination/complex caching — the same "hand-rolled
// for a narrow, well-understood mechanism" choice this repo has already
// made twice (gateway's own raw httpx, ui-shell's hand-rolled PKCE over an
// OIDC library).
import { gatewayBaseUrl } from './config'

/** The wire shape, verbatim — gateway's JSON uses snake_case field names. */
interface ModuleDto {
  module_id: string
  display_name: string
  icon: string
  nav_path: string | null
  status: string
  // ui-shell-plan.md item 8 (feature/module-proxy, 2026-09-10) — whether
  // GET /modules/{id}/proxy-token (./proxyToken.ts) has anywhere to forward
  // to. Not the raw proxied URL itself — gateway never hands the browser its
  // module's cluster-internal Service DNS name, only this flag.
  has_own_ui: boolean
}

/** The shape the rest of ui-shell actually works with — camelCase, mirroring
 * the existing TokenResponse -> TokenSet mapping convention in
 * ../auth/callback.ts. */
export interface Module {
  moduleId: string
  displayName: string
  icon: string
  navPath: string | null
  status: string
  hasOwnUi: boolean
}

function fromDto(dto: ModuleDto): Module {
  return {
    moduleId: dto.module_id,
    displayName: dto.display_name,
    icon: dto.icon,
    navPath: dto.nav_path,
    status: dto.status,
    hasOwnUi: dto.has_own_ui,
  }
}

/** Mirrors ../auth/errors.ts's AuthError, plus the HTTP status — useModules.ts
 * branches its error UI on this (401 vs. 403 vs. 503 read very differently
 * to a user). `detail` is gateway's own message body, already human-readable
 * for the cases that matter (e.g. derive_headers()'s 403 message names the
 * workspace and tells the user what to do about it) — rendered verbatim
 * rather than re-authored client-side. */
export class ModulesError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(`GET /modules failed: ${status} ${detail}`)
    this.name = 'ModulesError'
    this.status = status
    this.detail = detail
  }
}

export async function fetchModules(workspace: string, accessToken: string, signal?: AbortSignal): Promise<Module[]> {
  const response = await fetch(`${gatewayBaseUrl()}/modules`, {
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
      // Non-JSON error body (shouldn't happen against a real gateway response,
      // but don't let a parse failure mask the original HTTP status) — fall
      // back to statusText, already assigned above.
    }
    throw new ModulesError(response.status, detail)
  }

  const body = (await response.json()) as { modules: ModuleDto[] }
  return body.modules.map(fromDto)
}
