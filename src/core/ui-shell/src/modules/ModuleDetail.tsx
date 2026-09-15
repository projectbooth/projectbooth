import { Link, useParams } from 'react-router'
import { useWorkspace } from '../workspace/useWorkspace'
import styles from './ModuleDetail.module.css'
import { ModuleIcon } from './icons'
import { useModules } from './useModules'
import { gatewayBaseUrl } from './config'
import { useModuleProxyToken } from './useModuleProxyToken'

/** The "modules/:moduleId" route inside shell/Shell.tsx. Built entirely from
 * the already-fetched GET /modules list (no second gateway call for the
 * metadata fields below — gateway has no per-module detail route). Routed by
 * module_id, not raw nav_path — module_id is guaranteed present/unique/URL-
 * safe (^[a-z0-9-]+$ in ModuleManifest), nav_path is completely unvalidated
 * server-side and can be null for any module installed before item 4's
 * annotations existed. nav_path is still shown below as a labeled field,
 * just not used to build this route.
 *
 * ui-shell-plan.md item 8 (feature/module-proxy, 2026-09-10): the module's
 * own UI, when it has one (`module.hasOwnUi`), renders as an embedded
 * iframe below the metadata block — see ./useModuleProxyToken.ts for how the
 * short-lived proxy token it needs gets minted, and app/module_proxy.py's
 * own module docstring for the full "why a token in the iframe's src, not a
 * header or a cookie" reasoning this component doesn't need to re-derive. */
export function ModuleDetail() {
  const { moduleId } = useParams<{ moduleId: string }>()
  const { selected } = useWorkspace()
  const { status, modules } = useModules(selected)

  if (status === 'idle' || status === 'loading') {
    return <p className={styles.message}>Loading...</p>
  }

  const module = modules.find((m) => m.moduleId === moduleId)

  if (!module) {
    return (
      <div className={styles.message}>
        <p>
          "{moduleId}" isn't installed in the "{selected}" workspace (or the link is stale).
        </p>
        <Link to="/">Back to modules</Link>
      </div>
    )
  }

  return (
    <div className={styles.page}>
      <div className={styles.detail}>
        <Link to="/" className={styles.back}>
          &larr; Back to modules
        </Link>
        <div className={styles.header}>
          <ModuleIcon icon={module.icon} />
          <h2>{module.displayName}</h2>
        </div>
        <dl className={styles.fields}>
          <dt>Status</dt>
          <dd>{module.status}</dd>
          <dt>Module ID</dt>
          <dd>{module.moduleId}</dd>
          <dt>Icon</dt>
          <dd>{module.icon}</dd>
          <dt>nav_path</dt>
          <dd>{module.navPath ?? <em>not set</em>}</dd>
        </dl>
        {!module.hasOwnUi && (
          <p className={styles.notice}>
            {module.displayName} doesn't have a proxied UI yet — it was installed before this
            platform supported item 8's reverse-proxying. Reinstalling it (`platform module install
            {' '}
            {module.moduleId}`) picks this up.
          </p>
        )}
      </div>
      {module.hasOwnUi && <ModuleFrame moduleId={module.moduleId} displayName={module.displayName} workspace={selected} />}
    </div>
  )
}

function ModuleFrame({
  moduleId,
  displayName,
  workspace,
}: {
  moduleId: string
  displayName: string
  workspace: string | null
}) {
  const { status, token, error, refetch } = useModuleProxyToken(moduleId, workspace)

  if (status === 'idle' || status === 'loading') {
    return <p className={styles.message}>Loading {displayName}'s UI...</p>
  }

  if (status === 'error') {
    return (
      <div className={styles.message}>
        <p>{describeProxyTokenError(error)}</p>
        <button onClick={refetch}>Retry</button>
      </div>
    )
  }

  const src = `${gatewayBaseUrl()}/modules/${encodeURIComponent(moduleId)}/proxy/?token=${encodeURIComponent(token ?? '')}`
  return <iframe src={src} title={`${displayName}'s own UI`} className={styles.frame} />
}

function describeProxyTokenError(error: Error | null): string {
  // Gateway's own detail message is already human-readable (mirrors every other
  // *Error class's .detail usage in this codebase, e.g. AddonsListError in ../addons/Addons.tsx) —
  // rendered verbatim, not re-authored client-side.
  const detail = (error as { detail?: string } | null)?.detail
  return detail ?? error?.message ?? "Couldn't load this module's UI."
}
