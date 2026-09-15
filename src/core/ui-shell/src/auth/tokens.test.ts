import { beforeEach, describe, expect, it } from 'vitest'
import { clearTokens, decodeJwtPayload, isExpired, loadTokens, saveTokens, type TokenSet } from './tokens'

// A tiny hand-stubbed Map-backed sessionStorage — not real jsdom (see this
// package's README "Auth" section for why: crypto.subtle needs Vitest's
// default 'node' environment, and jsdom would also drag in
// @testing-library/react for component tests this branch deliberately
// doesn't add). Just enough of the Storage interface for tokens.ts's own
// getItem/setItem/removeItem calls.
function installFakeSessionStorage() {
  const store = new Map<string, string>()
  ;(globalThis as unknown as { sessionStorage: Storage }).sessionStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    get length() {
      return store.size
    },
  } as Storage
}

beforeEach(() => {
  installFakeSessionStorage()
})

// Deliberately not Buffer.from(...).toString('base64url') — that needs
// Node's @types/node global, which tsconfig.app.json's scoped `types`
// field (["vite/client"] only) doesn't include, and this test should mirror
// the same browser-safe primitives pkce.ts itself uses rather than lean on
// a Node-only API.
function base64UrlEncodeJson(obj: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(obj))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

const SAMPLE_TOKENS: TokenSet = {
  accessToken: 'access-token-value',
  refreshToken: 'refresh-token-value',
  idToken: 'id-token-value',
  expiresAt: 1_800_000_000_000,
  preferredUsername: 'alice',
}

describe('saveTokens / loadTokens / clearTokens', () => {
  it('round-trips a token set through sessionStorage', () => {
    expect(loadTokens()).toBeNull()
    saveTokens(SAMPLE_TOKENS)
    expect(loadTokens()).toEqual(SAMPLE_TOKENS)
    clearTokens()
    expect(loadTokens()).toBeNull()
  })

  it('treats a corrupt stored value as "not logged in" rather than throwing', () => {
    sessionStorage.setItem('platform-ui-shell:tokens', 'not valid json{')
    expect(loadTokens()).toBeNull()
  })
})

describe('decodeJwtPayload', () => {
  it('decodes the payload of a well-formed (unsigned) JWT-shaped string', () => {
    const header = base64UrlEncodeJson({ alg: 'none', typ: 'JWT' })
    const payload = base64UrlEncodeJson({ preferred_username: 'alice', sub: 'abc-123' })
    const token = `${header}.${payload}.`
    expect(decodeJwtPayload(token)).toEqual({ preferred_username: 'alice', sub: 'abc-123' })
  })

  it('throws on something that is not three dot-separated parts', () => {
    expect(() => decodeJwtPayload('not-a-jwt')).toThrow()
  })
})

describe('isExpired', () => {
  it('is false well before expiresAt', () => {
    const tokens = { ...SAMPLE_TOKENS, expiresAt: 1_000_000 }
    expect(isExpired(tokens, { now: 500_000 })).toBe(false)
  })

  it('is true after expiresAt', () => {
    const tokens = { ...SAMPLE_TOKENS, expiresAt: 1_000_000 }
    expect(isExpired(tokens, { now: 1_500_000 })).toBe(true)
  })

  it('applies the skew margin — true even slightly before the literal expiresAt', () => {
    const tokens = { ...SAMPLE_TOKENS, expiresAt: 1_000_000 }
    expect(isExpired(tokens, { now: 999_990, skewSeconds: 30 })).toBe(true)
  })
})
