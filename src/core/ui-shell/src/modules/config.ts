// Kept separate from ../auth/config.ts on purpose — that file's own
// docstring scopes it to "the three VITE_KEYCLOAK_* build-time values"
// specifically, and gateway is an unrelated concern (a different Ingress
// host, no OIDC endpoints of its own). Same requireEnv() shape, not shared,
// same "one concern per file" split this codebase already uses throughout
// auth/.

function requireEnv(key: 'VITE_GATEWAY_URL'): string {
  const value = import.meta.env[key]
  if (!value) {
    throw new Error(
      `${key} is not set — check .env.production (deployed build) or .env (local dev, copied from .env.example).`,
    )
  }
  return value
}

export function gatewayBaseUrl(): string {
  return requireEnv('VITE_GATEWAY_URL')
}
