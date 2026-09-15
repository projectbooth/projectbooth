// Split from AuthContext.tsx so that file exports only the AuthProvider
// component (oxlint's react/only-export-components — Fast Refresh only
// works when a file exports just components; sharing a hook from the same
// file defeats that for the whole context module).
import { useContext } from 'react'
import { AuthContext, type AuthContextValue } from './context'

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth() called outside an <AuthProvider>.')
  }
  return context
}
