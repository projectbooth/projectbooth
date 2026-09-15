import { describe, expect, it } from 'vitest'
import type { AddonEntry } from './api'
import { hasAnyQueued, isInstalled, resolveQueuedMutations, timeOutStaleMutations, type MutationsById } from './mutationState'

const NOT_INSTALLED = 'not installed'

function entry(overrides: Partial<AddonEntry> & { moduleId: string; status: string }): AddonEntry {
  return {
    displayName: overrides.moduleId,
    icon: 'puzzle',
    navPath: null,
    requires: [],
    optional: false,
    ...overrides,
  }
}

describe('isInstalled', () => {
  it('treats the not-installed sentinel as not installed', () => {
    expect(isInstalled(NOT_INSTALLED, NOT_INSTALLED)).toBe(false)
  })

  it('treats any other status as installed', () => {
    expect(isInstalled('Healthy', NOT_INSTALLED)).toBe(true)
    expect(isInstalled('Degraded', NOT_INSTALLED)).toBe(true)
  })
})

describe('resolveQueuedMutations', () => {
  it('resolves a queued install once the catalog shows the module installed', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'install', queuedAt: 0 }]])
    const entries = [entry({ moduleId: 'hello-module', status: 'Healthy' })]

    const next = resolveQueuedMutations(states, entries, NOT_INSTALLED)

    expect(next.has('hello-module')).toBe(false)
  })

  it('leaves a queued install alone while the catalog still shows it not installed', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'install', queuedAt: 0 }]])
    const entries = [entry({ moduleId: 'hello-module', status: NOT_INSTALLED })]

    const next = resolveQueuedMutations(states, entries, NOT_INSTALLED)

    expect(next.get('hello-module')).toEqual({ phase: 'queued', action: 'install', queuedAt: 0 })
  })

  it('resolves a queued uninstall once the catalog shows the module not installed', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'uninstall', queuedAt: 0 }]])
    const entries = [entry({ moduleId: 'hello-module', status: NOT_INSTALLED })]

    const next = resolveQueuedMutations(states, entries, NOT_INSTALLED)

    expect(next.has('hello-module')).toBe(false)
  })

  it('resolves a queued uninstall when the module disappears from the catalog entirely', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'uninstall', queuedAt: 0 }]])

    const next = resolveQueuedMutations(states, [], NOT_INSTALLED)

    expect(next.has('hello-module')).toBe(false)
  })

  it('leaves a queued uninstall alone while the catalog still shows it installed', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'uninstall', queuedAt: 0 }]])
    const entries = [entry({ moduleId: 'hello-module', status: 'Healthy' })]

    const next = resolveQueuedMutations(states, entries, NOT_INSTALLED)

    expect(next.has('hello-module')).toBe(true)
  })

  it('never touches an entry that is not currently queued', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'submitting', action: 'install' }]])
    const entries = [entry({ moduleId: 'hello-module', status: 'Healthy' })]

    const next = resolveQueuedMutations(states, entries, NOT_INSTALLED)

    expect(next.get('hello-module')).toEqual({ phase: 'submitting', action: 'install' })
  })

  it('returns the same reference when nothing resolved', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'install', queuedAt: 0 }]])
    const entries = [entry({ moduleId: 'hello-module', status: NOT_INSTALLED })]

    expect(resolveQueuedMutations(states, entries, NOT_INSTALLED)).toBe(states)
  })
})

describe('timeOutStaleMutations', () => {
  it('flips a queued entry older than the timeout to timed-out', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'install', queuedAt: 0 }]])

    const next = timeOutStaleMutations(states, 10_000, 5_000)

    expect(next.get('hello-module')).toEqual({ phase: 'timed-out', action: 'install', queuedAt: 0 })
  })

  it('leaves a queued entry inside the window alone', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'install', queuedAt: 8_000 }]])

    const next = timeOutStaleMutations(states, 10_000, 5_000)

    expect(next.get('hello-module')).toEqual({ phase: 'queued', action: 'install', queuedAt: 8_000 })
  })

  it('never touches a non-queued entry', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'error', action: 'install' }]])

    const next = timeOutStaleMutations(states, 10_000, 5_000)

    expect(next.get('hello-module')).toEqual({ phase: 'error', action: 'install' })
  })

  it('returns the same reference when nothing timed out', () => {
    const states: MutationsById = new Map([['hello-module', { phase: 'queued', action: 'install', queuedAt: 8_000 }]])

    expect(timeOutStaleMutations(states, 10_000, 5_000)).toBe(states)
  })
})

describe('hasAnyQueued', () => {
  it('is false for an empty map', () => {
    expect(hasAnyQueued(new Map())).toBe(false)
  })

  it('is false when nothing is in the queued phase', () => {
    const states: MutationsById = new Map([
      ['a', { phase: 'submitting', action: 'install' }],
      ['b', { phase: 'error', action: 'uninstall' }],
      ['c', { phase: 'timed-out', action: 'install', queuedAt: 0 }],
    ])
    expect(hasAnyQueued(states)).toBe(false)
  })

  it('is true when at least one entry is queued', () => {
    const states: MutationsById = new Map([
      ['a', { phase: 'submitting', action: 'install' }],
      ['b', { phase: 'queued', action: 'uninstall', queuedAt: 0 }],
    ])
    expect(hasAnyQueued(states)).toBe(true)
  })
})
