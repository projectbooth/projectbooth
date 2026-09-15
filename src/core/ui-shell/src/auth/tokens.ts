// Token storage and the pure helpers around it. sessionStorage, not
// localStorage: see this package's README "Auth" section for the trade-off
// (clears on tab close rather than lingering indefinitely; the PKCE
// code_verifier/state already need sessionStorage to survive the redirect
// to Keycloak and back, so reusing it for the tokens themselves avoids a
// second storage mechanism).
//
// IMPORTANT: decodeJwtPayload()/isExpired() below are for DISPLAY and
// REFRESH-TIMING ONLY. ui-shell has no way to verify a JWT's signature —
// that stays platform-gateway's job (app/auth.py's verify_token()). Never
// use a decoded claim here to make an authorization decision; only gateway
// ever does that.

const STORAGE_KEY = 'platform-ui-shell:tokens'

export interface TokenSet {
  accessToken: string
  refreshToken: string
  idToken: string
  /** Epoch milliseconds — computed at save time from the token response's `expires_in`. */
  expiresAt: number
  preferredUsername: string
}

export function saveTokens(tokens: TokenSet): void {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
}

export function loadTokens(): TokenSet | null {
  const raw = sessionStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as TokenSet
  } catch {
    // Corrupt/foreign value under our own key — treat as "not logged in"
    // rather than throwing and breaking the whole app on load.
    return null
  }
}

export function clearTokens(): void {
  sessionStorage.removeItem(STORAGE_KEY)
}

/** Decodes a JWT's payload without verifying its signature — display/refresh-timing only, see module docstring above. */
export function decodeJwtPayload(token: string): Record<string, unknown> {
  const parts = token.split('.')
  if (parts.length !== 3) {
    throw new Error('Not a JWT — expected three dot-separated parts.')
  }
  const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=')
  return JSON.parse(atob(padded)) as Record<string, unknown>
}

export function isExpired(tokens: TokenSet, { now = Date.now(), skewSeconds = 30 } = {}): boolean {
  return now >= tokens.expiresAt - skewSeconds * 1000
}
