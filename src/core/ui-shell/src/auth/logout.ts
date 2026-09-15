// RP-initiated logout (OIDC end_session_endpoint). See
// bootstrap/keycloak-bootstrap-ui-shell-client.sh's header comment on
// post.logout.redirect.uris for the one genuinely-uncertain-until-tested
// detail this depends on.

import { clearTokens } from './tokens'
import { endSessionEndpoint, keycloakClientId, postLogoutRedirectUri } from './config'

/** Clears local session state and navigates to Keycloak's end_session_endpoint. Never returns (full-page navigation) unless something throws first. */
export function startLogout(idToken: string): void {
  clearTokens()

  const params = new URLSearchParams({
    client_id: keycloakClientId(),
    id_token_hint: idToken,
    post_logout_redirect_uri: postLogoutRedirectUri(),
  })

  window.location.assign(`${endSessionEndpoint()}?${params.toString()}`)
}
