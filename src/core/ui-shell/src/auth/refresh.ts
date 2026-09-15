// Silent token refresh — no redirect, no user interaction. A plain OIDC
// refresh_token grant against the same token endpoint callback.ts uses.

import { tokenEndpoint, keycloakClientId } from './config'
import { decodeJwtPayload, saveTokens, type TokenSet } from './tokens'
import { AuthError } from './errors'

interface TokenResponse {
  access_token: string
  refresh_token: string
  id_token: string
  expires_in: number
}

const REFRESH_MARGIN_MS = 60_000 // refresh ~60s before expiry, not exactly at it

export async function refreshTokens(refreshToken: string): Promise<TokenSet> {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    client_id: keycloakClientId(),
    refresh_token: refreshToken,
  })

  const response = await fetch(tokenEndpoint(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!response.ok) {
    throw new AuthError(`Token refresh failed: ${response.status} ${await response.text()}`)
  }
  const tokenResponse = (await response.json()) as TokenResponse

  const idClaims = decodeJwtPayload(tokenResponse.id_token)
  const preferredUsername =
    (idClaims.preferred_username as string | undefined) ?? (idClaims.sub as string | undefined) ?? 'unknown'

  const tokens: TokenSet = {
    accessToken: tokenResponse.access_token,
    refreshToken: tokenResponse.refresh_token,
    idToken: tokenResponse.id_token,
    expiresAt: Date.now() + tokenResponse.expires_in * 1000,
    preferredUsername,
  }
  saveTokens(tokens)
  return tokens
}

/** Schedules a silent refresh ~60s before `tokens.expiresAt`, re-scheduling itself on success. Returns the timeout id (pass to clearTimeout to cancel, e.g. on logout or unmount). */
export function scheduleRefresh(
  tokens: TokenSet,
  onRefreshed: (tokens: TokenSet) => void,
  onFailed: (error: unknown) => void,
): ReturnType<typeof setTimeout> {
  const delay = Math.max(0, tokens.expiresAt - Date.now() - REFRESH_MARGIN_MS)
  return setTimeout(() => {
    refreshTokens(tokens.refreshToken)
      .then((refreshed) => {
        onRefreshed(refreshed)
        scheduleRefresh(refreshed, onRefreshed, onFailed)
      })
      .catch(onFailed)
  }, delay)
}
