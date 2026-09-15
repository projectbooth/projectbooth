// Talks to gateway's GET /modules/catalog (src/core/gateway/app/modules.py,
// ui-shell-plan.md item 6 — already live-verified end to end against
// homelab-dev). Structurally a copy of ../modules/api.ts's GET /modules
// wrapper, not a shared helper: same "hand-rolled for a narrow,
// well-understood mechanism" choice that file's own comment already makes,
// repeated here for a second, differently-shaped endpoint rather than
// generalized — this repo hasn't needed to share fetch plumbing between two
// endpoints yet.
import { gatewayBaseUrl } from '../modules/config'

/** The wire shape, verbatim — gateway's JSON uses snake_case field names. */
interface ModuleCatalogEntryDto {
  module_id: string
  display_name: string
  icon: string
  nav_path: string | null
  requires: string[]
  optional: boolean
  status: string
}

/** The shape the rest of ui-shell actually works with — camelCase, mirroring
 * ../modules/api.ts's ModuleDto -> Module mapping. */
export interface AddonEntry {
  moduleId: string
  displayName: string
  icon: string
  navPath: string | null
  requires: string[]
  optional: boolean
  status: string
}

function fromDto(dto: ModuleCatalogEntryDto): AddonEntry {
  return {
    moduleId: dto.module_id,
    displayName: dto.display_name,
    icon: dto.icon,
    navPath: dto.nav_path,
    requires: dto.requires,
    optional: dto.optional,
    status: dto.status,
  }
}

/** Mirrors ../modules/api.ts's ModulesError exactly — same status + detail
 * shape, same "gateway's own detail message is already human-readable, don't
 * re-author it client-side" reasoning. */
export class AddonsError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(`GET /modules/catalog failed: ${status} ${detail}`)
    this.name = 'AddonsError'
    this.status = status
    this.detail = detail
  }
}

export async function fetchAddons(workspace: string, accessToken: string, signal?: AbortSignal): Promise<AddonEntry[]> {
  const response = await fetch(`${gatewayBaseUrl()}/modules/catalog`, {
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
    throw new AddonsError(response.status, detail)
  }

  const body = (await response.json()) as { modules: ModuleCatalogEntryDto[] }
  return body.modules.map(fromDto)
}
