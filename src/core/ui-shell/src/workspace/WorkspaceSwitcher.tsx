import styles from './WorkspaceSwitcher.module.css'
import { useWorkspace } from './useWorkspace'

/** Renders nothing when there are zero memberships — the caller (shell/Shell.tsx)
 * / ModuleList already show their own "no workspace" message; a dropdown with
 * nothing to select would just be confusing chrome. */
export function WorkspaceSwitcher() {
  const { workspaces, selected, select } = useWorkspace()

  if (workspaces.length === 0) return null

  return (
    <label className={styles.switcher}>
      <span className={styles.label}>Workspace</span>
      <select
        className={styles.select}
        value={selected ?? ''}
        onChange={(e) => select(e.target.value)}
      >
        {workspaces.map((w) => (
          <option key={w.name} value={w.name}>
            {w.name} ({w.role})
          </option>
        ))}
      </select>
    </label>
  )
}
