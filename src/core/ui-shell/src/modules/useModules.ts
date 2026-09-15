import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth/useAuth'
import { fetchModules, ModulesError, type Module } from './api'

export type ModulesStatus = 'idle' | 'loading' | 'success' | 'error'

export interface UseModulesResult {
  status: ModulesStatus
  modules: Module[]
  error: ModulesError | Error | null
  refetch: () => void
}

type FetchOutcome =
  | { key: string; kind: 'success'; modules: Module[] }
  | { key: string; kind: 'error'; error: ModulesError | Error }

/** Fetches gateway's module list for the given workspace, refetching whenever
 * `workspace` (or the access token, e.g. after a silent refresh, or a manual
 * `refetch()`) changes. `workspace === null` is the "no workspace
 * membership" case — status stays 'idle' and nothing is fetched, distinct
 * from a real error. Uses an AbortController so a slow response for a
 * workspace the user has already switched away from can never overwrite
 * state for the newly-selected one.
 *
 * `status` is derived entirely at render time by comparing the current fetch
 * "key" (workspace + access token + retry count) against the key of the last
 * *resolved* fetch (`outcome`, set only inside the effect's async .then()/
 * .catch() callbacks) — deliberately not a separate `status` state flipped
 * synchronously inside the effect body itself, which is what
 * ../auth/AuthContext.tsx's own initial-state effect ran into and fixed the
 * same way (derive during render, only set state from an actually-async
 * callback). */
export function useModules(workspace: string | null): UseModulesResult {
  const { tokens } = useAuth()
  const [retryTick, setRetryTick] = useState(0)
  const [outcome, setOutcome] = useState<FetchOutcome | null>(null)

  const key = workspace === null || !tokens ? null : `${workspace}:${tokens.accessToken}:${retryTick}`

  useEffect(() => {
    if (key === null || workspace === null || !tokens) return

    const controller = new AbortController()
    fetchModules(workspace, tokens.accessToken, controller.signal)
      .then((modules) => {
        if (controller.signal.aborted) return
        setOutcome({ key, kind: 'success', modules })
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setOutcome({ key, kind: 'error', error: err instanceof Error ? err : new Error(String(err)) })
      })

    return () => controller.abort()
  }, [key, workspace, tokens])

  const refetch = useCallback(() => setRetryTick((t) => t + 1), [])

  if (key === null) {
    return { status: 'idle', modules: [], error: null, refetch }
  }
  if (outcome === null || outcome.key !== key) {
    // Either the very first fetch for this key hasn't resolved yet, or `key`
    // just changed (new workspace/token/retry) and the previous outcome is
    // now stale — both are "in flight" from the caller's point of view.
    return { status: 'loading', modules: [], error: null, refetch }
  }
  if (outcome.kind === 'success') {
    return { status: 'success', modules: outcome.modules, error: null, refetch }
  }
  return { status: 'error', modules: [], error: outcome.error, refetch }
}
