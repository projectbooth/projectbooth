# ui-shell

ARCHITECTURE.md §2's "one front door: unified nav... catalog browser... pipeline & run status...
deep-links into each module's own UI... workspace switcher." `docs/architecture/ui-shell-plan.md`
scoped that into 8 separately-decidable pieces; items 1, 3, and 5 are built so far, see below.

## What's built (2026-09-04, feature/ui-shell-scaffold branch)

A real React + TypeScript + Vite scaffold (`npm create vite@latest -- --template react-ts`, not
hand-typed — see `package.json` for the actual generated dependency versions), deployed through the
same GitOps pipeline every other core service uses: a multi-stage `Dockerfile` (Node 22 build stage
→ `nginxinc/nginx-unprivileged` runtime stage — this repo's first multi-stage image; gateway and
catalog-service are both single-stage Python/uvicorn), `manifests/ui-shell.yaml` +
`apps/core/ui-shell.yaml` (mirroring `gateway.yaml`/`apps/core/gateway.yaml`'s structure, sync wave
4 alongside gateway), and a `ci.yml` job pair (`test-ui-shell`/`build-and-push-ui-shell`, mirroring
gateway's). Reachable at `app.platform.local` once deployed — confirmed free when this was scoped,
only `keycloak.platform.local`/`gateway.platform.local` were claimed.

The page itself is deliberately a static placeholder (`src/App.tsx`) — no state, no data fetching,
no auth, nothing calling any backend. The point of this branch is proving the infrastructure (a
frontend build toolchain that didn't exist anywhere in this repo before now, this repo's first
multi-stage Docker image, a fourth Ingress host, a fourth CI job pair) works end-to-end before any
real design decisions get built on top of it — see `ui-shell-plan.md`'s "Recommended first slice."

## What's built (2026-09-08, feature/ui-shell-oauth branch) — Auth

Item 3: real browser login, authorization code + PKCE (RFC 7636), against a new Keycloak client —
`platform-ui-shell`, public, `standardFlowEnabled`, `pkce.code.challenge.method: S256` enforced —
created by `bootstrap/keycloak-bootstrap-ui-shell-client.sh` (see that script's own header for the
full "why a third client" reasoning; it's a separate client from both `platform-cli` and
`platform-cli-login`, same "narrowly-scoped clients cost nothing extra" logic those two already
follow). **One-time step, per cluster, not wired into `bootstrap/install.sh`:** run that script once
(same category as the other two Keycloak-client bootstrap scripts) before login will work — see the
script's own header and its printed output for what it does and confirms.

Hand-rolled, not a library (`oidc-client-ts`/`react-oidc-context`) — `src/auth/` is ~250 lines of
Web Crypto (`crypto.subtle.digest`) and plain `fetch()`, no new runtime dependency beyond `vitest`
for testing it. No router either: `nginx.conf`'s existing SPA fallback already serves
`/auth/callback` to `index.html`, so a single `window.location.pathname` check inside
`AuthProvider`'s mount effect is the entire "routing" this needs. See `src/auth/pkce.ts`,
`config.ts`, `tokens.ts`, `login.ts`, `callback.ts`, `refresh.ts`, `logout.ts`, and
`AuthContext.tsx`/`context.ts`/`useAuth.ts` for the actual implementation — each file's own
docstring covers its piece; `tokens.ts`'s is worth reading first, it states the one rule everything
else follows (ui-shell decodes its own token for display/refresh-timing only, never for an
authorization decision — that stays gateway's job, `app/auth.py`'s `verify_token()`).

Tokens live in `sessionStorage` (not `localStorage`, never a cookie — item 2's already-resolved
bearer-token-in-`Authorization`-header decision carries forward unchanged, so ui-shell calls gateway
with a plain `fetch(url, {headers: {Authorization: 'Bearer ...'}})`, no `credentials: 'include'`).
Silent refresh is scheduled ~60s before each access token's expiry. `App.tsx` still isn't real nav
(that's item 5, a separate future branch, now unblocked by this one) — just a login button when
logged out, the username + a logout button when logged in, enough to prove the flow works end to
end.

**Confirmed no gateway changes needed at all** — `app/auth.py`'s `verify_token()` checks signature/
issuer/`exp`/`sub` with `verify_aud` deliberately off and no client-specific check anywhere, so a
token from this new client is accepted exactly like a `platform-cli-login` token, as long as it
carries the `groups` claim (the bootstrap script adds the same protocol mapper the other two
clients have).

## What's built (2026-09-09, feature/ui-shell-nav branch) — Real nav

Item 5: ui-shell stops being a login button and becomes an actual app. `react-router@^8.3.1`
(declarative mode — `BrowserRouter`/`Routes`/`Route`, not the data router; there are no
loaders/actions anywhere here, and the route tree needs to be conditionally absent while
unauthenticated, which fits an ordinary `if` far better) is the first routing library in this repo.
`App.tsx` now splits into `/auth/callback` (`auth/AuthCallbackRoute.tsx`) and everything else
(`Gate`, which shows `shell/LoginScreen.tsx` or `shell/Shell.tsx` depending on `useAuth().status`).
`Shell` owns its own nested `<Routes>` — `/` (the module list) and `modules/:moduleId` (a detail
page) — so nothing under it (the workspace switcher, the module list, any gateway call) ever mounts
for an unauthenticated visitor.

**A real bug fixed along the way**: `auth/callback.ts` used to call
`window.history.replaceState({}, '', '/')` directly to strip `?code&state` from the URL after a
login. That call fires no `popstate` event, so a mounted client router would never see it — the URL
bar would read `/` while the router's own internal location stayed stuck on `/auth/callback`. Fixed
by removing that call and adding the router-side cleanup to `auth/AuthCallbackRoute.tsx` instead,
via `useNavigate()`. `AuthContext.tsx`'s own `isCallbackPath()`/`callbackHandled` guard needed no
changes — it only ever reads `window.location.pathname` once, never touches history.

**Workspace switcher** (`src/workspace/`): there is no "list my workspaces" endpoint anywhere in the
platform, and gateway requires an `X-Workspace` header on every call — the only source is the ID
token's own `groups` claim (`/workspaces/<name>/<role>` entries), decoded client-side via the
*existing* `decodeJwtPayload` from `auth/tokens.ts` (same display-only precedent it already
established for `preferred_username` — this never makes an authorization decision, that stays
gateway's job). `workspaces.ts`'s `parseWorkspaceMemberships()` mirrors gateway's own
`derive_headers()` exactly (`owner`/`editor`/`viewer` only, same dedupe priority). Selection persists
in `sessionStorage`.

**Module list and detail** (`src/modules/`): `GET /modules` (item 4) via plain `fetch()` in a
hand-rolled hook (`useModules.ts`) — no data-fetching library, one GET endpoint refetched on
workspace change, same "hand-rolled for a narrow mechanism" choice this repo has made twice already
(gateway's raw `httpx`, ui-shell's own PKCE). Clicking a module routes to `/modules/<module_id>`
(keyed by `module_id`, not raw `nav_path` — `nav_path` is completely unvalidated server-side and can
be `null`) and shows real live data from the already-fetched list, with an honest note that opening
the module's own UI is item 8, not built yet — not a fake iframe or dead link. (Item 8 landed
2026-09-10, `feature/module-proxy` — see that section further down; this note is accurate for this
branch's own point in time, not the repo's current state.) Icons are a small
hand-drawn local SVG map (`modules/icons.tsx`) with a fallback glyph, not an icon-library dependency
— `icon` is a completely free-form string with no enforced value set anywhere in the platform.

Styling moved to CSS Modules (`Component.module.css` next to each `Component.tsx`) — `index.css` now
holds only true globals. No UI framework introduced.

## What's built (2026-09-09, feature/ui-shell-addons branch) — Add-ons page

Item 7 of `ui-shell-plan.md`, scoped to a **read-only page only** — confirmed with the repo owner
this session. Item 7 as titled ("Install/Remove buttons") presupposed a page that didn't exist yet,
and its real mechanism hinges on an unresolved trust-boundary question (does gateway get git-write
credentials?). That question is now decided but not built: when the mutation mechanism lands (a
future branch), gateway will trigger a GitHub Actions `workflow_dispatch` rather than holding a
direct git/PAT credential, reusing the same ephemeral, auto-scoped `GITHUB_TOKEN` pattern `ci.yml`
already uses for GHCR pushes. Nothing in this branch calls it — no mutation fires from this page at
all.

New `src/addons/` directory (its own top-level concern, alongside `auth/`, `modules/`, `shell/`,
`workspace/`), consuming item 6's `GET /modules/catalog` via a hand-rolled `fetch()` hook
(`useAddons.ts`) that's a structural copy of `modules/useModules.ts` — same derived-at-render
idle/loading/success/error state machine, same per-fetch `AbortController`. `Addons.tsx` lists every
module in the catalog (installed or not), reusing `modules/icons.tsx`'s `ModuleIcon` and a copy of
`ModuleList.module.css`'s status-badge palette. Each row shows its `requires` list as plain text (no
dependency-satisfaction computation here — `check-requirements` still owns that) and a disabled
`Install` button with a `title` tooltip; a page-level notice states plainly that installing/removing
isn't built yet. A module already installed links its name to the existing
`/modules/:moduleId` detail page instead of showing the disabled button — `ModuleDetail.tsx` needed no
changes, it already looks up by `moduleId` against its own list independent of how the user navigated
there.

`shell/Shell.tsx` gained a `/addons` route and a small nav (`NavLink` — the first use of it in this
repo, purely for the free active-link styling over plain `Link`) to switch between "Modules" and
"Add-ons".

## What's built (2026-09-10, feature/ui-shell-addons-mutation branch) — real Install/Remove

The rest of item 7: the Add-ons page's Install/Remove buttons actually call gateway's
`POST /modules/{id}/install` and `.../uninstall` (`feature/gateway-module-lifecycle-dispatch`, same
day) instead of sitting disabled. New `src/addons/mutations.ts` (hand-rolled `fetch()` wrapper, same
"one file per concern" precedent `api.ts`/`useAddons.ts` already set) and `mutationState.ts` — a
**pure** state machine (`MutationPhase = 'submitting' | 'queued' | 'timed-out' | 'error'`, split out
specifically so it gets real unit tests, the same `WorkspaceContext.tsx`/`workspaces.ts` split this
repo already established) — plus `useAddonMutations.ts`, the React wrapper owning one
`Map<moduleId, MutationEntry>` for the whole page rather than one hook instance per row, so every row
renders from a single source of truth.

Both endpoints are fire-and-forget (a `202` just means "a workflow was told to start"), so a
successful call moves a row to `'queued'` and a 5-second poll (`useAddons`' own `refetch`) sweeps the
catalog for the row's expected end state, up to a 2-minute window before giving up and flipping to
`'timed-out'` — generous on purpose, since besides the triggered GitHub Actions workflow's own run
time, Argo CD's reconciliation (and, for uninstall, the `modules-root` prune gap `docs/known-issues.md`
documents) can genuinely take a while. `Addons.tsx`'s `AddonRow` gained a `confirmingRemove` step
before an uninstall actually fires (`"Removing can take a few minutes to fully complete"`), and every
button/note in `AddonRowActions`/`AddonRowNote` now reads live `MutationEntry` state instead of the
static "isn't built yet" copy the read-only branch above shipped.

## What's built (2026-09-10, feature/module-proxy branch) — a module's own UI, embedded

Item 8 of `ui-shell-plan.md` — the last item on that doc's build list. `ModuleDetail.tsx` renders a
module's own UI as an embedded `<iframe>` when `module.hasOwnUi` is true (`GET /modules`'s new
`has_own_ui` field — see `src/core/gateway/README.md`'s matching section for the backend half), or a
"reinstall to pick this up" notice when it's `false` (a module installed before this branch, same
transient-annotation-gap shape items 4/6 already established — not a module permanently lacking a UI).

New `useModuleProxyToken.ts` mints a proxy token once per page view via a plain authenticated
`fetch()` to gateway's new `GET /modules/{id}/proxy-token` (`proxyToken.ts`, structurally identical to
`mutations.ts`'s hand-rolled-per-endpoint convention), then `ModuleFrame` builds
`<iframe src=".../modules/{id}/proxy/?token=...">` — a query-param token rather than a header, since a
plain `<iframe src>` navigation structurally cannot send a custom `Authorization` header and this
system has never used cookies for identity. Loading/error states follow the same pattern every other
hook in this repo already uses (`describeProxyTokenError` renders gateway's own `detail` message
verbatim, mirroring `AddonsListError`).

**Follow-up-request propagation, fixed on the backend, confirmed live 2026-09-11 — nothing changed
here:** the query-string token alone never propagated to a module's own follow-up requests. Gateway
now also sets the same token as a `Path=/modules/{id}/proxy`-scoped cookie on every successful proxied
response, so this page's own `<iframe src=".../proxy/?token=...">` needs no change at all — the fix is
entirely server-side, and was proven live against `homelab-dev` with `curl` standing in for a module's
own follow-up request (mint, capture the `Set-Cookie`, then a second request with no `?token=` at all
came back `200` on the cookie alone). Still not proven inside a real browser against a real module with
its own frontend assets or backend calls — `hello-module`'s self-contained stock nginx page issues no
follow-up requests to prove that against yet — see `src/core/gateway/README.md`'s matching section for
the full mechanism and its real third-party-cookie trade-off.

**Confirmed live, 2026-09-10:** the iframe rendered `hello-module`'s real page inline against
`homelab-dev`, after hitting (and fixing) the same mutable-`:dev`-tag stale-pod gotcha documented in
`docs/known-issues.md` for a second time in as many branches.

## What's built (2026-09-10, feature/force-cleanup branch) — Force cleanup

`docs/known-issues.md`'s `modules-root` prune-gap workaround (`sudo kubectl -n argocd delete
application <id>`), made click-able. `Addons.tsx`'s `AddonRowActions` now recognizes one specific
state — a mutation whose `phase` is `'timed-out'` and whose `action` is `'uninstall'` — and swaps
the row's normal Remove button for a distinct **Force cleanup** button instead of falling through to
the generic Remove-with-confirm flow (re-clicking Remove would just re-dispatch the same workflow
gateway already ran; this is a prune gap, not a failed workflow). `AddonRowNote` gets matching copy
for that same state pointing at the new button.

Wired through the same layers item 7's mutation mechanism already established: `mutations.ts` gained
`forceCleanupModule()` (posts to gateway's new `POST /modules/{id}/force-cleanup`, labeled as an
`'uninstall'`-family `AddonMutationError` on failure — conceptually finishing a stuck uninstall, not
a fourth kind of mutation); `useAddonMutations.ts` gained a `forceCleanup` callback alongside
`install`/`uninstall`, reusing the exact same `submitting → queued/error` flow `run()` already
implements (refactored to take the network call as a parameter so both share it). A successful call
folds back into the same `{phase: 'queued', action: 'uninstall'}` shape a fresh uninstall produces,
so `mutationState.ts`'s existing polling/resolution/timeout machinery — untouched by this branch —
picks up from there and resolves the row once the deletion is actually reflected in the catalog.

Deliberately never fires on its own: only a user's click, never the timeout itself, avoids a real
race with the original uninstall workflow's own still-in-flight git push — see
`src/core/gateway/README.md`'s Force cleanup section for the full reasoning (same one place gateway's
own docstrings point to).

## What's NOT built yet

**`ui-shell-plan.md`'s entire dependency-ordered build list is done** (this section corrected
2026-09-11 — it had gone stale, still describing item 7's real Install/Remove action and item 8's
module-proxy as future work well after both shipped and were live-verified; see the sections above).

What's still genuinely open: the module-proxy's follow-up-request cookie mechanism is **confirmed live
via `curl`, 2026-09-11** (see that section above), but still not proven inside a real browser against a
real module with its own frontend assets or backend calls — `hello-module` alone can't close that last
gap. The recurring mutable-`:dev`-
image-tag stale-pod gotcha that bit this page's own branches three separate times this session is now
**fixed and confirmed live, 2026-09-11** (`ci.yml`'s git-sha annotation bump — see `argocd/README.md`'s
matching section and `docs/known-issues.md`'s entry for the live-verification writeup: `ui-shell`'s
deployed annotation and a fresh, zero-restart pod rollout both confirmed on `homelab-dev` with no
manual `rollout restart`). Broader gaps against ARCHITECTURE.md's longer-term vision aren't scoped in
this doc — track those in `ARCHITECTURE.md`/`docs/known-issues.md` as they come up.

## Running it locally

```bash
npm install
cp .env.example .env   # see that file's own comment for why local dev uses the same real values
npm run dev
```

Vite's dev server listens on `:5173` by default — the same port `catalog-service/.env.example`'s
`CORS_ORIGINS` breadcrumb already referenced before any of this existed, and one of the two origins
`bootstrap/keycloak-bootstrap-ui-shell-client.sh` registers as a valid redirect URI. Real browser
login against `homelab-dev`'s Keycloak needs the same `/etc/hosts` entry the CLI's `platform login`
already requires (see `docs/known-issues.md`'s "Reaching Keycloak by raw IP" entry) — there's no
port-forward equivalent for an interactive browser redirect.

To build and run the actual container image (what CI builds and what Argo CD deploys):

```bash
docker build -t ui-shell:local .
docker run --rm -p 8080:8080 ui-shell:local
# http://localhost:8080
```

## Running its tests

```bash
npm run lint   # oxlint — the Vite react-ts template's default, not ESLint
npm run build  # tsc -b && vite build
npm test       # vitest run
```

Vitest arrived `feature/ui-shell-oauth` (item 3) — the trigger the earlier version of this README
named: real pure logic worth exercising (PKCE math, expiry math, JWT-payload decoding), not a static
placeholder page anymore. Scoped narrowly, matching gateway's own "pure logic direct, live for the
rest" discipline: `src/auth/pkce.test.ts` and `tokens.test.ts` cover the PKCE/token-storage math
directly (including RFC 7636 Appendix B's own worked example, not just self-consistency) —
deliberately *not* testing the actual `fetch()` calls, redirects, or component rendering, since
those need a real Keycloak or a rendered DOM to mean anything. Runs in Vitest's default `node`
environment, not `jsdom` — `crypto.subtle` is a Node 22 global but a known `jsdom` gap, and skipping
it also avoids pulling in `@testing-library/react` for component tests.

`feature/ui-shell-nav` (item 5) added `src/workspace/workspaces.test.ts` on the same discipline —
`parseWorkspaceMemberships()`'s parsing/dedupe logic and the `sessionStorage` round-trip are real
pure logic, covered directly with hand-built fixtures, no mocking framework. Deliberately *not*
tested: `useModules`, `WorkspaceContext.tsx`, `Shell.tsx`, `ModuleList.tsx`, `ModuleDetail.tsx`,
`WorkspaceSwitcher.tsx` — all either need a rendered DOM (the same `jsdom` gap above) or are thin
composition with no pure logic of their own once `workspaces.ts` is factored out.

`feature/ui-shell-addons` (item 7) adds `src/addons/` on the same, unchanged discipline: `useAddons.ts`
and `Addons.tsx` are structural copies of already-untested `useModules.ts`/`ModuleList.tsx`, and
`addons/api.ts`'s `fromDto` is a direct field mapping, same as `modules/api.ts`'s own untested
`fromDto` — no new test files.

## What can only be confirmed live

Same category as every other containerized-service branch in this repo: the image doesn't exist
until this branch merges to `dev` (`ci.yml`'s `on.push.branches: [dev, test, main]` only builds and
pushes on those three branches, never on a feature branch) — so whether `app.platform.local` is
actually reachable and the page renders can only be checked on `homelab-dev` after merge.

Specific to this branch: run `bootstrap/keycloak-bootstrap-ui-shell-client.sh` once against the real
cluster (new client, one-time step), then a real browser login end to end — "Log in" redirects to
Keycloak, a real login completes, lands back on `/auth/callback` then `/` with `code`/`state`
stripped from the URL, shows the logged-in username; "Log out" clears the session and returns to
logged-out. `post.logout.redirect.uris` needed a real fix, not just confirmation — see
`docs/known-issues.md`'s entry on it: a multi-valued Keycloak client attribute has to be joined with
`##`, not a space, or client creation 400s outright. Fixed in the script; worth re-checking the
actual logout redirect behaves once it's exercised live for the first time.

Specific to `feature/ui-shell-nav` (item 5): after a real login, confirm the URL bar reads `/` with
no leftover `?code&state` (the direct regression check for the `history.replaceState` fix above);
confirm the workspace switcher shows the real group-derived workspace(s) for the logged-in user (or
the "no workspace" empty state, if they have none); confirm switching workspaces fires a fresh
`GET /modules` with the new `X-Workspace` header and the list updates; click a module, confirm the
detail page and the item-8 note; confirm browser back/forward and a hard refresh on
`/modules/<id>` all work (the last one is the real test of `nginx.conf`'s `try_files` SPA fallback
against a route besides `/`, for the first time).

Specific to `feature/ui-shell-addons` (item 7, read-only page): after a real login, confirm the new
"Add-ons" nav link appears and navigates to `/addons`; confirm every module in `src/modules/` appears
with correct display name/icon/`requires`, and its correct status (real Argo CD health if installed,
the "not installed" sentinel otherwise); confirm the page-level "not built yet" notice is visible and
the Install button is disabled with a hover tooltip; if a module is installed, confirm its row links
to the existing `/modules/<id>` detail page with no regression; confirm switching workspaces refetches
the catalog with the new `X-Workspace` header; confirm browser back/forward and a hard refresh on
`/addons` work (same SPA-fallback regression check as `/modules/<id>` above, now for a second route).
