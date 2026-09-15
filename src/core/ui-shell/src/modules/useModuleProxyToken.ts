import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth/useAuth'
import { fetchModuleProxyToken, ModuleProxyTokenError } from './proxyToken'

export type ModuleProxyTokenStatus = 'idle' | 'loading' | 'success' | 'error'

export interface UseModuleProxyTokenResult {
  status: ModuleProxyTokenStatus
  token: string | null
  error: ModuleProxyTokenError | Error | null
  refetch: () => void
}

type FetchOutcome =
  | { key: string; kind: 'success'; token: string }
  | { key: string; kind: 'error'; error: ModuleProxyTokenError | Error }

/** Mints a proxy token for `moduleId` in `workspace`, structurally identical to ./useModules.ts's own
 * derived-at-render idle/loading/success/error pattern — same reasoning for why (see that file's own
 * comment: status is derived by comparing the current fetch key against the key of the last *resolved*
 * fetch, never a separate status flipped synchronously inside the effect body).
 *
 * Deliberately mints ONCE per (moduleId, workspace, accessToken) — no polling, no auto-refresh. This
 * branch's plan, §1: 5 minutes (settings.module_proxy_token_ttl_seconds server-side) is long enough to
 * actually look at a module's detail page; a `refetch()` (e.g. a user-facing retry button after an
 * error) mints a fresh one on demand instead. */
export function useModuleProxyToken(moduleId: string | null, workspace: string | null): UseModuleProxyTokenResult {
  const { tokens } = useAuth()
  const [retryTick, setRetryTick] = useState(0)
  const [outcome, setOutcome] = useState<FetchOutcome | null>(null)

  const key =
    moduleId === null || workspace === null || !tokens
      ? null
      : `${moduleId}:${workspace}:${tokens.accessToken}:${retryTick}`

  useEffect(() => {
    if (key === null || moduleId === null || workspace === null || !tokens) return

    const controller = new AbortController()
    fetchModuleProxyToken(moduleId, workspace, tokens.accessToken, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setOutcome({ key, kind: 'success', token: result.token })
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setOutcome({ key, kind: 'error', error: err instanceof Error ? err : new Error(String(err)) })
      })

    return () => controller.abort()
  }, [key, moduleId, workspace, tokens])

  const refetch = useCallback(() => setRetryTick((t) => t + 1), [])

  if (key === null) {
    return { status: 'idle', token: null, error: null, refetch }
  }
  if (outcome === null || outcome.key !== key) {
    return { status: 'loading', token: null, error: null, refetch }
  }
  if (outcome.kind === 'success') {
    return { status: 'success', token: outcome.token, error: null, refetch }
  }
  return { status: 'error', token: null, error: outcome.error, refetch }
}
