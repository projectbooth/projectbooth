/// <reference types="vite/client" />

// Custom build-time env vars — see .env.production/.env.example for real
// values, src/auth/config.ts for where these are read. vite/client's own
// ambient ImportMetaEnv (tsconfig.app.json's "types": ["vite/client"])
// covers MODE/BASE_URL/PROD/DEV/SSR but not project-specific VITE_* keys,
// which need this augmentation to be typed rather than falling through as
// `any`.
interface ImportMetaEnv {
  readonly VITE_KEYCLOAK_URL: string
  readonly VITE_KEYCLOAK_REALM: string
  readonly VITE_KEYCLOAK_CLIENT_ID: string
  readonly VITE_GATEWAY_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
