// Reads the three VITE_KEYCLOAK_* build-time values (see ../vite-env.d.ts
// for their type declarations, .env.production/.env.example for where the
// real values live) and derives every URL this flow needs. Deliberately the
// only module that touches import.meta.env directly — everything else in
// auth/ takes URLs/config as arguments or imports from here.

function requireEnv(key: 'VITE_KEYCLOAK_URL' | 'VITE_KEYCLOAK_REALM' | 'VITE_KEYCLOAK_CLIENT_ID'): string {
  const value = import.meta.env[key]
  if (!value) {
    throw new Error(
      `${key} is not set — check .env.production (deployed build) or .env (local dev, copied from .env.example).`,
    )
  }
  return value
}

export function keycloakClientId(): string {
  return requireEnv('VITE_KEYCLOAK_CLIENT_ID')
}

function realmUrl(): string {
  return `${requireEnv('VITE_KEYCLOAK_URL')}/realms/${requireEnv('VITE_KEYCLOAK_REALM')}`
}

export function authorizeEndpoint(): string {
  return `${realmUrl()}/protocol/openid-connect/auth`
}

export function tokenEndpoint(): string {
  return `${realmUrl()}/protocol/openid-connect/token`
}

export function endSessionEndpoint(): string {
  return `${realmUrl()}/protocol/openid-connect/logout`
}

// Computed from window.location.origin at runtime, NOT a baked env var —
// the same build works correctly whether it's running as the deployed image
// (https://app.platform.local) or via `npm run dev` (http://localhost:5173),
// since bootstrap/keycloak-bootstrap-ui-shell-client.sh registers both
// origins' /auth/callback path as valid redirect URIs on the same client.
export function redirectUri(): string {
  return `${window.location.origin}/auth/callback`
}

export function postLogoutRedirectUri(): string {
  return `${window.location.origin}/`
}
