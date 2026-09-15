// ui-shell-plan.md item 5: the top-level route split. /auth/callback must
// render regardless of auth status — it's what drives status out of
// 'loading' in the first place (see auth/AuthCallbackRoute.tsx) — so it gets
// its own top-level Route rather than living inside Gate's authenticated-only
// tree. Everything else goes through Gate, which decides between the login
// screen and the real authenticated shell based on useAuth().status; Shell
// (shell/Shell.tsx) owns its own nested Routes for the authenticated app, so
// nothing under it (workspace switcher, module list/detail, any future gateway
// call) ever mounts for an unauthenticated visitor.
import { Route, Routes } from 'react-router'
import AuthCallbackRoute from './auth/AuthCallbackRoute'
import { useAuth } from './auth/useAuth'
import { LoginScreen } from './shell/LoginScreen'
import { Shell } from './shell/Shell'

function App() {
  return (
    <Routes>
      <Route path="/auth/callback" element={<AuthCallbackRoute />} />
      <Route path="/*" element={<Gate />} />
    </Routes>
  )
}

function Gate() {
  const { status, message } = useAuth()

  if (status === 'loading') return <p>loading...</p>
  if (status === 'authenticated') return <Shell />
  return <LoginScreen message={status === 'error' ? message : null} />
}

export default App
