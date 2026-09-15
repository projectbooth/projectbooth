// Pure state-transition logic for in-flight install/uninstall requests on the
// Add-ons page — split out from useAddonMutations.ts the same way
// workspace/workspaces.ts's pure parsing logic is split from
// WorkspaceContext.tsx's React wrapper (see that file's own docstring for the
// precedent), so this state machine gets real unit tests too, not just the
// membership-parsing one.
import type { AddonEntry } from './api'
import type { AddonMutationAction, AddonMutationError } from './mutations'

export type MutationPhase = 'submitting' | 'queued' | 'timed-out' | 'error'

export interface MutationEntry {
  phase: MutationPhase
  action: AddonMutationAction
  /** Set only once phase reaches 'queued' — when the polling/timeout window
   * started. */
  queuedAt?: number
  /** Set only once phase reaches 'error'. */
  error?: AddonMutationError | Error
}

export type MutationsById = ReadonlyMap<string, MutationEntry>

/** gateway's own `_NOT_INSTALLED_STATUS` value — see ./Addons.tsx's own copy
 * of this same literal for why it's copied rather than imported from a
 * shared module. Passed in here (not hardcoded) so this file has no
 * dependency on Addons.tsx and stays trivially testable. */
export function isInstalled(status: string, notInstalledStatus: string): boolean {
  return status !== notInstalledStatus
}

/** Sweeps every 'queued' entry against the latest fetched catalog and
 * resolves (removes) any whose observed installed/not-installed state now
 * matches what that mutation was waiting for. Only ever meant to be called
 * with a *successful* catalog fetch's `entries` — a transient fetch error
 * emptying the list must never look like "every queued uninstall just
 * resolved," so the caller (useAddonMutations) gates this on useAddons()'s
 * own `status === 'success'`, not something this pure function can enforce
 * itself. Returns the same `states` reference when nothing changed, so
 * callers can skip a re-render/effect re-run off an identical reference. */
export function resolveQueuedMutations(states: MutationsById, entries: AddonEntry[], notInstalledStatus: string): MutationsById {
  let changed = false
  const next = new Map(states)
  for (const [moduleId, entry] of states) {
    if (entry.phase !== 'queued') continue
    const catalogEntry = entries.find((e) => e.moduleId === moduleId)
    // A module missing from the catalog entirely (shouldn't happen at
    // runtime — the static index doesn't shrink) reads as "not installed,"
    // the same fallback app/module_index.py's own load side already uses
    // for a degraded/empty catalog.
    const installed = catalogEntry ? isInstalled(catalogEntry.status, notInstalledStatus) : false
    const resolved = entry.action === 'install' ? installed : !installed
    if (resolved) {
      next.delete(moduleId)
      changed = true
    }
  }
  return changed ? next : states
}

/** Flips any 'queued' entry older than `timeoutMs` to 'timed-out' — a
 * deliberately soft signal, not a failure: the workflow may still be
 * running, or (see docs/known-issues.md's modules-root entry) Argo CD may
 * just be slow to actually prune an uninstalled module. The row's button
 * re-enables once timed-out rather than staying disabled forever waiting on
 * a resolution that might not land inside *this* browser tab's polling
 * window at all. Same "same reference back when nothing changed" convention
 * as resolveQueuedMutations. */
export function timeOutStaleMutations(states: MutationsById, now: number, timeoutMs: number): MutationsById {
  let changed = false
  const next = new Map(states)
  for (const [moduleId, entry] of states) {
    if (entry.phase !== 'queued') continue
    if (entry.queuedAt !== undefined && now - entry.queuedAt > timeoutMs) {
      next.set(moduleId, { ...entry, phase: 'timed-out' })
      changed = true
    }
  }
  return changed ? next : states
}

export function hasAnyQueued(states: MutationsById): boolean {
  for (const entry of states.values()) {
    if (entry.phase === 'queued') return true
  }
  return false
}
