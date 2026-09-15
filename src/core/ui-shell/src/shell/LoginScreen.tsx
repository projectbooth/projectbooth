import { useAuth } from '../auth/useAuth'

/** The unauthenticated/error screen — same content App.tsx rendered directly
 * before item 5 split it into its own component. Rendered by ../App.tsx's
 * Gate whenever useAuth().status isn't 'authenticated' (and isn't the
 * transient 'loading' state, which Gate handles itself). */
export function LoginScreen({ message }: { message: string | null }) {
  const { login } = useAuth()

  return (
    <main>
      <h1>ui-shell</h1>
      {message ? (
        <p>login error: {message}</p>
      ) : (
        <p>not logged in.</p>
      )}
      <button onClick={login}>Log in</button>
    </main>
  )
}
