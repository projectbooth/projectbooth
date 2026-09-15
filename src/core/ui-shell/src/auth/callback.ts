// Handles the return trip from Keycloak's authorize redirect: /auth/callback
// with ?code=...&state=... (or ?error=...) in the URL. See login.ts for the
// outbound half and its sessionStorage keys, which this reads and clears.

import { readAndClearPkceFlowState } from './login'
import { redirectUri, tokenEndpoint, keycloakClientId } from './config'
import { decodeJwtPayload, saveTokens, type TokenSet } from './tokens'
import { AuthError } from './errors'

interface TokenResponse {
  access_token: string
  refresh_token: string
  id_token: string
  expires_in: number
}

/** Exchanges the authorization code in the current URL for a token set and saves it. Single-use — see AuthContext.tsx's module-level guard against React 19 StrictMode's dev-only double effect invocation, which would otherwise call this twice for one real login. Purely the token exchange — no navigation/history side effect here (that used to be window.history.replaceState() at the end of this function; item 5, ui-shell-plan.md, moved it to AuthCallbackRoute.tsx's useNavigate() call instead, since a raw history mutation fires no popstate event and a mounted client router would never see it — its internal location would go stale at '/auth/callback' even though the visible URL bar had changed). */
export async function handleCallback(): Promise<TokenSet> {
  const url = new URL(window.location.href)
  const error = url.searchParams.get('error')
  const code = url.searchParams.get('code')
  const returnedState = url.searchParams.get('state')

  const { verifier, state: expectedState } = readAndClearPkceFlowState()

  if (error) {
    throw new AuthError(`Keycloak returned an error: ${error} — ${url.searchParams.get('error_description') ?? 'no description'}`)
  }
  if (!code) {
    throw new AuthError('No authorization code in the callback URL.')
  }
  if (!verifier || !expectedState) {
    throw new AuthError('No PKCE flow state found in sessionStorage — this callback was not reached via startLogin(), or the flow state already expired/was consumed.')
  }
  if (returnedState !== expectedState) {
    throw new AuthError('The returned state does not match the one this browser generated — possible CSRF, aborting.')
  }

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: keycloakClientId(),
    code,
    redirect_uri: redirectUri(),
    code_verifier: verifier,
  })

  const response = await fetch(tokenEndpoint(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!response.ok) {
    throw new AuthError(`Token exchange failed: ${response.status} ${await response.text()}`)
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
