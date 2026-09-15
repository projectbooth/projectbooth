// React wrapper around mutationState.ts's pure state machine plus
// ./mutations.ts's network calls — same split as
// ../workspace/WorkspaceContext.tsx wrapping workspaces.ts. Owns per-module
// install/uninstall state for the whole Add-ons page (one map, not one hook
// instance per row) so Addons.tsx can render each row from a single source
// of truth rather than each row polling independently.
import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth/useAuth'
import type { AddonEntry } from './api'
import type { AddonsStatus } from './useAddons'
import { hasAnyQueued, resolveQueuedMutations, timeOutStaleMutations, type MutationEntry, type MutationsById } from './mutationState'
import { forceCleanupModule, installModule, uninstallModule, type AddonMutationAction } from './mutations'

/** How often to re-poll the catalog while anything is queued. Short enough
 * to feel responsive for a workflow that usually finishes in under a
 * minute (see ui-shell-plan.md item 7's live-verification writeup — the
 * "Module lifecycle" workflow itself ran in ~33s), long enough not to hammer
 * gateway/Argo CD from an idle browser tab. */
const POLL_INTERVAL_MS = 5_000

/** How long to keep polling before giving up and showing a "still
 * processing" note instead of a spinner that never resolves. Generous on
 * purpose: besides the workflow's own run time, Argo CD's own reconciliation
 * (and, for uninstall, the modules-root prune gap tracked in
 * docs/known-issues.md) can take a while — this is a UX nicety, not a
 * guarantee, so it errs long rather than flipping to "stuck" too eagerly. */
const POLL_TIMEOUT_MS = 120_000

export interface UseAddonMutationsResult {
  stateFor: (moduleId: string) => MutationEntry | undefined
  install: (moduleId: string) => void
  uninstall: (moduleId: string) => void
  /** The "Force cleanup" workaround (gateway's POST /modules/{id}/force-cleanup)
   * made callable — see mutations.ts's forceCleanupModule docstring and
   * docs/known-issues.md's modules-root prune-gap entry. A successful call
   * folds back into the same {phase: 'queued', action: 'uninstall'} shape a
   * fresh uninstall would produce, so the polling/resolution/timeout logic
   * above handles the rest unmodified — Addons.tsx is the only thing that
   * needs to know this exists as a distinct action. */
  forceCleanup: (moduleId: string) => void
}

/** `entries`/`catalogStatus` should be `useAddons()`'s own return values for
 * the same workspace — this hook doesn't fetch the catalog itself, it only
 * triggers `refetch` while polling and reads whatever `entries` that produces
 * next render. */
export function useAddonMutations(
  workspace: string | null,
  entries: AddonEntry[],
  catalogStatus: AddonsStatus,
  refetch: () => void,
  notInstalledStatus: string,
): UseAddonMutationsResult {
  const { tokens } = useAuth()
  const [rawStates, setRawStates] = useState<MutationsById>(new Map())

  // Derived at render, not via an effect reacting to `entries` — the same
  // "computed at render time, never set synchronously inside an effect body"
  // discipline ./useAddons.ts's own docstring already establishes for its
  // status derivation. A transient fetch error (entries briefly empty) must
  // never look like every queued uninstall just finished, so this only ever
  // resolves against `entries` when catalogStatus === 'success' — anything
  // else (idle/loading/error) leaves last-known state exactly as it was.
  const states = catalogStatus === 'success' ? resolveQueuedMutations(rawStates, entries, notInstalledStatus) : rawStates

  // While anything is queued: poll the catalog and sweep for timeouts on the
  // same interval. Stops itself the moment nothing is queued anymore (every
  // row either resolved — reflected in `states` above the next time `entries`
  // updates — or timed out).
  useEffect(() => {
    if (!hasAnyQueued(states)) return
    const id = setInterval(() => {
      refetch()
      setRawStates((prev) => timeOutStaleMutations(prev, Date.now(), POLL_TIMEOUT_MS))
    }, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [states, refetch])

  // `call` is the network function to invoke — decoupled from `action` (which
  // only labels the resulting MutationEntry/error) so forceCleanup can share
  // this exact submitting→queued/error flow while hitting a different
  // endpoint under the same 'uninstall' action label.
  const run = useCallback(
    (moduleId: string, action: AddonMutationAction, call: (moduleId: string, workspace: string, accessToken: string) => Promise<void>) => {
      if (!tokens || !workspace) return
      setRawStates((prev) => new Map(prev).set(moduleId, { phase: 'submitting', action }))
      call(moduleId, workspace, tokens.accessToken)
        .then(() => {
          setRawStates((prev) => new Map(prev).set(moduleId, { phase: 'queued', action, queuedAt: Date.now() }))
        })
        .catch((err: unknown) => {
          setRawStates((prev) => new Map(prev).set(moduleId, { phase: 'error', action, error: err instanceof Error ? err : new Error(String(err)) }))
        })
    },
    [tokens, workspace],
  )

  return {
    stateFor: (moduleId) => states.get(moduleId),
    install: (moduleId) => run(moduleId, 'install', installModule),
    uninstall: (moduleId) => run(moduleId, 'uninstall', uninstallModule),
    forceCleanup: (moduleId) => run(moduleId, 'uninstall', forceCleanupModule),
  }
}
