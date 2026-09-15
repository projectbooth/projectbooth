import { Navigate, NavLink, Route, Routes } from 'react-router'
import { Addons } from '../addons/Addons'
import { useAuth } from '../auth/useAuth'
import { ModuleDetail } from '../modules/ModuleDetail'
import { ModuleList } from '../modules/ModuleList'
import { WorkspaceProvider } from '../workspace/WorkspaceContext'
import { WorkspaceSwitcher } from '../workspace/WorkspaceSwitcher'
import styles from './Shell.module.css'

/** The authenticated app frame — only ever rendered when useAuth().status ===
 * 'authenticated' (see ../App.tsx's Gate). Owns its own nested <Routes>: a
 * supported "descendant Routes" pattern, matched against whatever's left of
 * the URL after App.tsx's outer <Route path="/*"> already matched — no
 * <Outlet> needed since Shell is rendered directly as that route's element.
 * WorkspaceProvider mounts here and nowhere else, so an unauthenticated
 * visitor's tree never fires a /modules or /modules/catalog call.
 *
 * `nav` (item 7, ui-shell-plan.md) uses react-router's NavLink rather than
 * Link — the first use of it in this repo — purely for the free
 * `aria-current`/active-class it gives over plain Link, no other behavior
 * difference. */
export function Shell() {
  const { tokens, logout } = useAuth()

  return (
    <WorkspaceProvider>
      <div className={styles.shell}>
        <header className={styles.header}>
          <h1 className={styles.title}>ui-shell</h1>
          <nav className={styles.nav}>
            <NavLink to="/" end className={({ isActive }) => (isActive ? styles.navLinkActive : styles.navLink)}>
              Modules
            </NavLink>
            <NavLink to="/addons" className={({ isActive }) => (isActive ? styles.navLinkActive : styles.navLink)}>
              Add-ons
            </NavLink>
          </nav>
          <div className={styles.headerControls}>
            <WorkspaceSwitcher />
            <span className={styles.username}>{tokens?.preferredUsername}</span>
            <button onClick={logout}>Log out</button>
          </div>
        </header>
        <main className={styles.content}>
          <Routes>
            <Route path="/" element={<ModuleList />} />
            <Route path="modules/:moduleId" element={<ModuleDetail />} />
            <Route path="addons" element={<Addons />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </WorkspaceProvider>
  )
}
