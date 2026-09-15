// Pure logic, no React — parses workspace membership out of the ID token's
// 'groups' claim (decoded client-side, DISPLAY ONLY, same precedent
// ../auth/tokens.ts already established for preferred_username: this never
// makes an authorization decision, that stays gateway's job via the
// required X-Workspace header on every request — see
// src/core/gateway/app/auth.py's derive_headers(), which this file's parsing
// logic deliberately mirrors).
//
// There is no "list my workspaces" endpoint anywhere in the platform — the
// groups claim (entries shaped /workspaces/<name>/<role>, Keycloak's
// oidc-group-membership-mapper with full.path=true) is the only source, so
// this is what drives the workspace switcher.

const STORAGE_KEY = 'platform-ui-shell:workspace'

/** Exactly the role set gateway's derive_headers() checks (app/auth.py's
 * _ROLE_PRIORITY) — anything else under a /workspaces/<name>/ prefix is not
 * a real membership as far as this platform is concerned. */
const ROLE_PRIORITY = ['owner', 'editor', 'viewer'] as const
type Role = (typeof ROLE_PRIORITY)[number]

export interface WorkspaceMembership {
  name: string
  role: Role
}

function isRole(value: string): value is Role {
  return (ROLE_PRIORITY as readonly string[]).includes(value)
}

/** Parses /workspaces/<name>/<role> entries out of a decoded token's `groups`
 * claim. Mirrors gateway's derive_headers() exactly: only `owner`/`editor`/
 * `viewer` count as a real membership, extra path segments are ignored, and
 * a workspace appearing under more than one role (possible if someone's
 * group memberships were edited by hand) is deduped to a single entry using
 * the same owner > editor > viewer priority gateway uses to pick a role.
 * `groups` is `unknown` because it comes straight out of a JWT payload
 * decoded via decodeJwtPayload()'s Record<string, unknown> — absent, not an
 * array, or malformed entries all resolve to "no memberships found" rather
 * than throwing. */
export function parseWorkspaceMemberships(groups: unknown): WorkspaceMembership[] {
  if (!Array.isArray(groups)) return []

  const bestRoleByName = new Map<string, Role>()
  for (const entry of groups) {
    if (typeof entry !== 'string') continue
    const parts = entry.split('/')
    // '/workspaces/<name>/<role>' splits (on '/') to ['', 'workspaces', name, role] — exactly 4 parts.
    if (parts.length !== 4 || parts[0] !== '' || parts[1] !== 'workspaces') continue
    const name = parts[2]
    const role = parts[3]
    if (!name || !isRole(role)) continue

    const existing = bestRoleByName.get(name)
    if (!existing || ROLE_PRIORITY.indexOf(role) < ROLE_PRIORITY.indexOf(existing)) {
      bestRoleByName.set(name, role)
    }
  }

  return [...bestRoleByName.entries()].map(([name, role]) => ({ name, role }))
}

export function loadSelectedWorkspace(): string | null {
  return sessionStorage.getItem(STORAGE_KEY)
}

export function saveSelectedWorkspace(name: string): void {
  sessionStorage.setItem(STORAGE_KEY, name)
}
