import { beforeEach, describe, expect, it } from 'vitest'
import { loadSelectedWorkspace, parseWorkspaceMemberships, saveSelectedWorkspace } from './workspaces'

// Same hand-stubbed Map-backed sessionStorage as ../auth/tokens.test.ts —
// not shared as a utility, matching this repo's existing choice to keep each
// test file self-contained rather than add a shared test-helpers module for
// one tiny stub.
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

describe('parseWorkspaceMemberships', () => {
  it('parses normal /workspaces/<name>/<role> entries', () => {
    const groups = ['/workspaces/personal/owner', '/workspaces/acme/editor']
    expect(parseWorkspaceMemberships(groups)).toEqual([
      { name: 'personal', role: 'owner' },
      { name: 'acme', role: 'editor' },
    ])
  })

  it('returns [] when groups is absent (undefined)', () => {
    expect(parseWorkspaceMemberships(undefined)).toEqual([])
  })

  it('returns [] when groups is an empty array', () => {
    expect(parseWorkspaceMemberships([])).toEqual([])
  })

  it('returns [] rather than throwing when groups is not an array at all', () => {
    expect(parseWorkspaceMemberships('not-an-array')).toEqual([])
    expect(parseWorkspaceMemberships(null)).toEqual([])
    expect(parseWorkspaceMemberships(42)).toEqual([])
    expect(parseWorkspaceMemberships({ foo: 'bar' })).toEqual([])
  })

  it('ignores non-workspace group entries mixed in', () => {
    const groups = ['/some-other-group/whatever', '/workspaces/personal/owner']
    expect(parseWorkspaceMemberships(groups)).toEqual([{ name: 'personal', role: 'owner' }])
  })

  it('ignores an invalid role segment — only owner/editor/viewer count, matching gateway', () => {
    const groups = ['/workspaces/acme/admin']
    expect(parseWorkspaceMemberships(groups)).toEqual([])
  })

  it('ignores malformed entries with extra path segments', () => {
    const groups = ['/workspaces/acme/owner/extra', '/workspaces/personal/owner']
    expect(parseWorkspaceMemberships(groups)).toEqual([{ name: 'personal', role: 'owner' }])
  })

  it('ignores non-string entries without throwing', () => {
    const groups = [42, null, '/workspaces/personal/owner']
    expect(parseWorkspaceMemberships(groups)).toEqual([{ name: 'personal', role: 'owner' }])
  })

  it('dedupes a workspace appearing under multiple roles, highest priority wins (owner > editor > viewer)', () => {
    const groups = ['/workspaces/acme/viewer', '/workspaces/acme/owner', '/workspaces/acme/editor']
    expect(parseWorkspaceMemberships(groups)).toEqual([{ name: 'acme', role: 'owner' }])
  })

  it('dedupe order does not depend on input order', () => {
    const groups = ['/workspaces/acme/editor', '/workspaces/acme/viewer']
    expect(parseWorkspaceMemberships(groups)).toEqual([{ name: 'acme', role: 'editor' }])
  })
})

describe('loadSelectedWorkspace / saveSelectedWorkspace', () => {
  it('round-trips a selection through sessionStorage', () => {
    expect(loadSelectedWorkspace()).toBeNull()
    saveSelectedWorkspace('personal')
    expect(loadSelectedWorkspace()).toBe('personal')
  })

  it('a later save overwrites the earlier selection', () => {
    saveSelectedWorkspace('personal')
    saveSelectedWorkspace('acme')
    expect(loadSelectedWorkspace()).toBe('acme')
  })
})
