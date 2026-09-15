// The React context object itself, split out from AuthContext.tsx (which
// exports only the AuthProvider component) and useAuth.ts (which exports
// only the hook) — oxlint's react/only-export-components wants a file that
// exports a component to export nothing else, Fast Refresh's own
// requirement, not just a style preference.
import { createContext } from 'react'
import type { TokenSet } from './tokens'

export type Status = 'loading' | 'authenticated' | 'unauthenticated' | 'error'

export interface AuthContextValue {
  status: Status
  tokens: TokenSet | null
  message: string | null
  login: () => void
  logout: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)
