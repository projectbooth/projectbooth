import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './auth/AuthContext.tsx'

// BrowserRouter wraps AuthProvider (not the other way around) so
// AuthCallbackRoute (App.tsx's /auth/callback route) can call react-router's
// own useNavigate() to return to '/' once the code exchange settles — see
// auth/callback.ts's docstring for why it no longer calls
// window.history.replaceState() itself (item 5, ui-shell-plan.md): a raw
// history mutation fires no popstate event, so a mounted router would never
// see it and its internal location would go stale.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
)
