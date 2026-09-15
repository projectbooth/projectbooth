import { useEffect, useState, type ReactNode } from 'react'
import { handleCallback } from './callback'
import { AuthContext, type AuthContextValue, type Status } from './context'
import { startLogin } from './login'
import { startLogout } from './logout'
import { scheduleRefresh } from './refresh'
import { isExpired, loadTokens, type TokenSet } from './tokens'

const isCallbackPath = () => window.location.pathname === '/auth/callback'

// Module-level, not React state: React 19's StrictMode double-invokes
// effects in dev, and an authorization code is single-use. Without this
// guard, the second (dev-only) invocation of the callback-handling effect
// below would either double-spend the code (Keycloak returns invalid_grant
// on the second exchange) or find the PKCE verifier/state already cleared
// by the first invocation and throw a spurious CSRF-looking error — a race,
// not a correctness issue with handleCallback() itself. This resets on a
// real page load (module re-evaluates), not on StrictMode's remount, which
// is exactly the distinction needed here.
let callbackHandled = false

export function AuthProvider({ children }: { children: ReactNode }) {
  // Lazy initializers, not an effect: for the common (non-callback) case,
  // sessionStorage is synchronously readable, so there's no "external
  // system" to wait on and no reason to render a 'loading' flash before
  // deriving the real initial state. Only the callback path is genuinely
  // async (a network token exchange) and needs an effect below.
  const [status, setStatus] = useState<Status>(() => {
    if (isCallbackPath()) return 'loading'
    const stored = loadTokens()
    return stored && !isExpired(stored) ? 'authenticated' : 'unauthenticated'
  })
  const [tokens, setTokens] = useState<TokenSet | null>(() => {
    if (isCallbackPath()) return null
    const stored = loadTokens()
    return stored && !isExpired(stored) ? stored : null
  })
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!isCallbackPath() || callbackHandled) return
    callbackHandled = true
    handleCallback()
      .then((result) => {
        setTokens(result)
        setStatus('authenticated')
      })
      .catch((error: unknown) => {
        setMessage(error instanceof Error ? error.message : String(error))
        setStatus('error')
      })
  }, [])

  useEffect(() => {
    if (status !== 'authenticated' || !tokens) return
    const timeoutId = scheduleRefresh(
      tokens,
      (refreshed) => setTokens(refreshed),
      (error: unknown) => {
        setMessage(error instanceof Error ? error.message : String(error))
        setStatus('unauthenticated')
        setTokens(null)
      },
    )
    return () => clearTimeout(timeoutId)
  }, [status, tokens])

  const value: AuthContextValue = {
    status,
    tokens,
    message,
    login: () => {
      void startLogin()
    },
    logout: () => {
      if (tokens) startLogout(tokens.idToken)
    },
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
