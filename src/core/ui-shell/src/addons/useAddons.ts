// Structural copy of ../modules/useModules.ts for the catalog endpoint — see
// that file's own docstring for the full reasoning behind the derived-at-
// render state machine and the AbortController-per-fetch guard; repeated
// here rather than shared, same "one hook per endpoint" choice api.ts makes.
import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth/useAuth'
import { fetchAddons, AddonsError, type AddonEntry } from './api'

export type AddonsStatus = 'idle' | 'loading' | 'success' | 'error'

export interface UseAddonsResult {
  status: AddonsStatus
  entries: AddonEntry[]
  error: AddonsError | Error | null
  refetch: () => void
}

type FetchOutcome =
  | { key: string; kind: 'success'; entries: AddonEntry[] }
  | { key: string; kind: 'error'; error: AddonsError | Error }

/** Fetches gateway's module catalog for the given workspace, refetching
 * whenever `workspace` (or the access token, or a manual `refetch()`)
 * changes. Same idle/loading/success/error derivation as useModules.ts —
 * `status` is computed at render time by comparing the current fetch key
 * against the key of the last *resolved* outcome, never set synchronously
 * inside the effect body. */
export function useAddons(workspace: string | null): UseAddonsResult {
  const { tokens } = useAuth()
  const [retryTick, setRetryTick] = useState(0)
  const [outcome, setOutcome] = useState<FetchOutcome | null>(null)

  const key = workspace === null || !tokens ? null : `${workspace}:${tokens.accessToken}:${retryTick}`

  useEffect(() => {
    if (key === null || workspace === null || !tokens) return

    const controller = new AbortController()
    fetchAddons(workspace, tokens.accessToken, controller.signal)
      .then((entries) => {
        if (controller.signal.aborted) return
        setOutcome({ key, kind: 'success', entries })
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setOutcome({ key, kind: 'error', error: err instanceof Error ? err : new Error(String(err)) })
      })

    return () => controller.abort()
  }, [key, workspace, tokens])

  const refetch = useCallback(() => setRetryTick((t) => t + 1), [])

  if (key === null) {
    return { status: 'idle', entries: [], error: null, refetch }
  }
  if (outcome === null || outcome.key !== key) {
    return { status: 'loading', entries: [], error: null, refetch }
  }
  if (outcome.kind === 'success') {
    return { status: 'success', entries: outcome.entries, error: null, refetch }
  }
  return { status: 'error', entries: [], error: outcome.error, refetch }
}
