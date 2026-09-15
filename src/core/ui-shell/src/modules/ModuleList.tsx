import { Link } from 'react-router'
import { useAuth } from '../auth/useAuth'
import { useWorkspace } from '../workspace/useWorkspace'
import styles from './ModuleList.module.css'
import { ModuleIcon } from './icons'
import { ModulesError } from './api'
import { useModules } from './useModules'

/** The "/" route content inside shell/Shell.tsx — the nav/list view built
 * from gateway's GET /modules (item 4), scoped to the currently-selected
 * workspace (workspace/WorkspaceContext.tsx). */
export function ModuleList() {
  const { logout } = useAuth()
  const { workspaces, selected } = useWorkspace()
  const { status, modules, error, refetch } = useModules(selected)

  if (workspaces.length === 0) {
    return <p className={styles.message}>You don't belong to any workspace yet.</p>
  }

  if (status === 'idle' || status === 'loading') {
    return <p className={styles.message}>Loading modules...</p>
  }

  if (status === 'error') {
    return <ModuleListError error={error} onRetry={refetch} onLogout={logout} />
  }

  if (modules.length === 0) {
    return <p className={styles.message}>No modules installed in this workspace yet.</p>
  }

  return (
    <ul className={styles.list}>
      {modules.map((module) => (
        <li key={module.moduleId}>
          <Link to={`/modules/${module.moduleId}`} className={styles.item}>
            <ModuleIcon icon={module.icon} />
            <span className={styles.name}>{module.displayName}</span>
            <span className={styles.status} data-status={module.status}>
              {module.status}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  )
}

function ModuleListError({
  error,
  onRetry,
  onLogout,
}: {
  error: ModulesError | Error | null
  onRetry: () => void
  onLogout: () => void
}) {
  if (error instanceof ModulesError) {
    if (error.status === 401) {
      return (
        <div className={styles.message}>
          <p>Your session has expired.</p>
          <button onClick={onLogout}>Log in again</button>
        </div>
      )
    }
    if (error.status === 403) {
      // Gateway's own detail message is already human-readable and names the
      // workspace + what to do about it (see app/auth.py's derive_headers()) —
      // rendered verbatim rather than re-authored client-side.
      return <p className={styles.message}>{error.detail}</p>
    }
    if (error.status === 503) {
      return (
        <div className={styles.message}>
          <p>The module registry is temporarily unavailable. {error.detail}</p>
          <button onClick={onRetry}>Retry</button>
        </div>
      )
    }
    return (
      <div className={styles.message}>
        <p>{error.detail}</p>
        <button onClick={onRetry}>Retry</button>
      </div>
    )
  }
  return (
    <div className={styles.message}>
      <p>{error?.message ?? 'Something went wrong loading modules.'}</p>
      <button onClick={onRetry}>Retry</button>
    </div>
  )
}
