// Split from WorkspaceContext.tsx so that file exports only the
// WorkspaceProvider component — same Fast Refresh / oxlint
// react/only-export-components reasoning as ../auth/useAuth.ts.
import { useContext } from 'react'
import { WorkspaceContext, type WorkspaceContextValue } from './context'

export function useWorkspace(): WorkspaceContextValue {
  const context = useContext(WorkspaceContext)
  if (!context) {
    throw new Error('useWorkspace() called outside a <WorkspaceProvider>.')
  }
  return context
}
