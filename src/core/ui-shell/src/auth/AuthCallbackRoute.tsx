// Rendered at /auth/callback (see ../App.tsx). AuthContext.tsx's own mount
// effect (isCallbackPath()) is what actually calls handleCallback() and does
// the code exchange — this component's only job is the router-side half of
// returning to a normal URL once that settles: navigate('/', {replace:
// true}) via react-router's own useNavigate(), replacing the raw
// window.history.replaceState() callback.ts used to call directly. That
// direct call fired no popstate event, so a mounted client router would
// never see the URL change — its internal location would stay stuck on
// '/auth/callback' even after the visible URL bar read '/'. Routing through
// useNavigate() instead means the router's own state and the URL bar always
// agree.
import { useEffect } from 'react'
import { useNavigate } from 'react-router'
import { useAuth } from './useAuth'

export default function AuthCallbackRoute() {
  const { status, message } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    // 'loading' means AuthContext's effect hasn't resolved handleCallback()
    // yet (or, for React 19 StrictMode's dev-only double-invoke, is a no-op
    // second pass) — nothing to navigate away from until status settles into
    // 'authenticated' or 'error'.
    if (status === 'loading') return
    navigate('/', { replace: true })
  }, [status, navigate])

  if (status === 'error') return <p>login error: {message}</p>
  return <p>signing you in...</p>
}
