# auth

Keycloak realm + workspace-group config — the `personal` workspace gets seeded here at Phase 0.
ARCHITECTURE.md §2, §4. The Keycloak *instance itself* is provisioned via
`src/core/argocd/apps/core/keycloak-operator.yaml` + `keycloak-instance.yaml`; what lives in this
folder is the realm layered on top once that instance is healthy, applied by its own Argo CD
Application (`src/core/argocd/apps/core/keycloak-realm.yaml`, wave 3).

`realm-platform.yaml` — a `KeycloakRealmImport` resource (the Keycloak Operator's own CRD; no
extra tooling) that creates a `platform` realm and seeds the workspace-group model: every
workspace is a Keycloak group path, `/workspaces/<name>/<role>`, where `<role>` is one of
`owner`/`editor`/`viewer` (ARCHITECTURE.md §4's three roles) — the group path encodes both which
workspace and which role in one membership, so inviting someone is one action
(`platform workspace invite alice --role editor`), not a separate group-add plus role-assignment.
Each role-group also carries the matching realm role, so a consumer can check either the `groups`
or the `realm_access.roles` JWT claim. Only one workspace is seeded so far — `personal` — matching
"what it buys a solo user: nothing, on purpose" from ARCHITECTURE.md §4.

Deliberately NOT here yet: any seeded user, and any OIDC client. Both need an actual
login-flow consumer to be meaningful — `platform-gateway` and `ui-shell`
(`src/core/gateway/`, `src/core/ui-shell/`) are still just scaffolded READMEs, not running
services, so a client definition now would be guessing at redirect URIs nobody can use yet, and a
git-committed user credential serves no purpose with nothing to log into. See the comments in
`realm-platform.yaml` for how to validate the realm import in the meantime (Keycloak's own admin
console, master realm, switch to `platform`) and the one real limitation worth knowing before
editing it later: `KeycloakRealmImport` only ever creates a realm — re-syncing this resource after
`platform` already exists does NOT update it.
