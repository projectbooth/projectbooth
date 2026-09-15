/** Anything that goes wrong in the auth/ package surfaces as this — AuthContext catches it and
 * moves to the 'error' status rather than letting a rejected promise crash the app. */
export class AuthError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AuthError'
  }
}
