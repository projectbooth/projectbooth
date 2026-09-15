import { describe, expect, it } from 'vitest'
import { generateCodeChallenge, generateCodeVerifier, generateState } from './pkce'

describe('generateCodeVerifier', () => {
  it('produces a base64url string of a length RFC 7636 allows (43-128 chars)', () => {
    const verifier = generateCodeVerifier()
    expect(verifier.length).toBeGreaterThanOrEqual(43)
    expect(verifier.length).toBeLessThanOrEqual(128)
    expect(verifier).toMatch(/^[A-Za-z0-9_-]+$/)
  })

  it('is different every call', () => {
    expect(generateCodeVerifier()).not.toBe(generateCodeVerifier())
  })
})

describe('generateCodeChallenge', () => {
  // RFC 7636 Appendix B's own worked example — a stronger check than
  // self-consistency alone, since it proves this implementation matches the
  // spec's actual SHA-256+base64url algorithm, not just "produces something
  // stable."
  it('matches RFC 7636 Appendix B', async () => {
    const verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
    const challenge = await generateCodeChallenge(verifier)
    expect(challenge).toBe('E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')
  })

  it('is deterministic for the same verifier', async () => {
    const verifier = generateCodeVerifier()
    const a = await generateCodeChallenge(verifier)
    const b = await generateCodeChallenge(verifier)
    expect(a).toBe(b)
  })
})

describe('generateState', () => {
  it('produces a base64url string', () => {
    expect(generateState()).toMatch(/^[A-Za-z0-9_-]+$/)
  })

  it('is different every call', () => {
    expect(generateState()).not.toBe(generateState())
  })
})
