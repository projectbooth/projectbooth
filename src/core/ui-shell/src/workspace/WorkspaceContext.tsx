// Only ever mounted inside shell/Shell.tsx — i.e. only when useAuth().status
// === 'authenticated', so tokens is guaranteed non-null here. Decodes the ID
// token's groups claim via the *existing* decodeJwtPayload from
// ../auth/tokens.ts (same display-only precedent it already established for
// preferred_username — no new decoding mechanism) and parses it with
// workspaces.ts's parseWorkspaceMemberships(), which mirrors gateway's own
// derive_headers() logic.
import { useMemo, useState, type ReactNode } from 'react'
import { useAuth } from '../auth/useAuth'
import { decodeJwtPayload } from '../auth/tokens'
import { WorkspaceContext, type WorkspaceContextValue } from './context'
import { loadSelectedWorkspace, parseWorkspaceMemberships, saveSelectedWorkspace } from './workspaces'

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { tokens } = useAuth()

  const workspaces = useMemo(() => {
    if (!tokens) return []
    const claims = decodeJwtPayload(tokens.idToken)
    return parseWorkspaceMemberships(claims.groups)
    // tokens.idToken is stable for the lifetime of one session (a refresh
    // rotates it, which is exactly when memberships should be re-derived).
  }, [tokens])

  // Lazy initializer, not an effect: picking the initial selection from
  // sessionStorage + the already-computed `workspaces` list is synchronous,
  // no external system to wait on — same reasoning AuthContext.tsx's own
  // lazy useState initializers already use for the equivalent problem.
  const [selected, setSelected] = useState<string | null>(() => {
    const persisted = loadSelectedWorkspace()
    if (persisted && workspaces.some((w) => w.name === persisted)) return persisted
    return workspaces[0]?.name ?? null
  })

  const select = (name: string) => {
    setSelected(name)
    saveSelectedWorkspace(name)
  }

  const value: WorkspaceContextValue = { workspaces, selected, select }

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}
