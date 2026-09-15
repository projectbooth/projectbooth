// The React context object itself, split out from WorkspaceContext.tsx
// (exports only the WorkspaceProvider component) and useWorkspace.ts (exports
// only the hook) — same oxlint react/only-export-components split
// ../auth/context.ts already established.
import { createContext } from 'react'
import type { WorkspaceMembership } from './workspaces'

export interface WorkspaceContextValue {
  /** Every workspace the current user belongs to, per the ID token's groups
   * claim. Empty array is a real, distinct state — "logged in, no workspace
   * memberships yet" — not an error. */
  workspaces: WorkspaceMembership[]
  /** The X-Workspace value to send with gateway calls. Null iff `workspaces`
   * is empty — there is nothing to select. */
  selected: string | null
  select: (name: string) => void
}

export const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)
