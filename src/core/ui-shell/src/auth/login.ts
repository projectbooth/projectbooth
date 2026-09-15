// Starts the authorization-code+PKCE redirect. See callback.ts for the
// other half (handling the return trip) — the two files share the
// sessionStorage keys below, deliberately kept private to this pair rather
// than exposed as part of tokens.ts's public TokenSet storage (the
// verifier/state are single-use flow state, not session state).

import { generateCodeChallenge, generateCodeVerifier, generateState } from './pkce'
import { authorizeEndpoint, keycloakClientId, redirectUri } from './config'

const VERIFIER_KEY = 'platform-ui-shell:pkce-verifier'
const STATE_KEY = 'platform-ui-shell:pkce-state'

export function readAndClearPkceFlowState(): { verifier: string | null; state: string | null } {
  const verifier = sessionStorage.getItem(VERIFIER_KEY)
  const state = sessionStorage.getItem(STATE_KEY)
  sessionStorage.removeItem(VERIFIER_KEY)
  sessionStorage.removeItem(STATE_KEY)
  return { verifier, state }
}

/** Generates a fresh verifier/challenge/state, stores the verifier+state, and navigates to Keycloak's authorize endpoint. Never returns (full-page navigation) unless something throws before the redirect. */
export async function startLogin(): Promise<void> {
  const verifier = generateCodeVerifier()
  const challenge = await generateCodeChallenge(verifier)
  const state = generateState()

  sessionStorage.setItem(VERIFIER_KEY, verifier)
  sessionStorage.setItem(STATE_KEY, state)

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: keycloakClientId(),
    redirect_uri: redirectUri(),
    scope: 'openid profile email',
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  })

  window.location.assign(`${authorizeEndpoint()}?${params.toString()}`)
}
