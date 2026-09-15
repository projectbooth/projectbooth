// RFC 7636 (Authorization Code + PKCE) primitives — the only cryptographic
// pieces this login flow needs. Hand-rolled deliberately, not a library: see
// this package's README "Auth" section for why (small, well-understood
// mechanism; avoids stacking an unverified OIDC-library-compatibility risk
// on top of this repo's React 19 / TypeScript ~6.0.2 / Vite 8 versions on
// top of the one Keycloak-version surprise this repo already hit once,
// bootstrap/keycloak-bootstrap-login-client.sh's device-grant-field
// fallback).
//
// Pure functions only — no fetch, no storage, no window. login.ts/
// callback.ts own wiring these into the actual redirect/exchange.

const VERIFIER_BYTE_LENGTH = 32 // -> 43-character base64url string, well within RFC 7636's 43-128 char requirement
const STATE_BYTE_LENGTH = 16

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** A cryptographically random `code_verifier`, RFC 7636 §4.1. */
export function generateCodeVerifier(): string {
  const bytes = new Uint8Array(VERIFIER_BYTE_LENGTH)
  crypto.getRandomValues(bytes)
  return base64UrlEncode(bytes)
}

/** SHA-256 `code_challenge` for a given verifier, RFC 7636 §4.2 (S256 method only — plain is never used here). */
export async function generateCodeChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return base64UrlEncode(new Uint8Array(digest))
}

/** A random `state` value for CSRF protection on the authorization redirect — separate from PKCE's own verifier/challenge pair. */
export function generateState(): string {
  const bytes = new Uint8Array(STATE_BYTE_LENGTH)
  crypto.getRandomValues(bytes)
  return base64UrlEncode(bytes)
}
