# ui-shell + gateway module registry + Add-ons page: a properly-scoped build plan

## Context

`module-lifecycle-plan.md`'s item 7 — "gateway's module registry / `PlatformModule` registrations /
the Add-ons page API, and `ui-shell` itself" — has been deferred twice already: once when that doc
was written (item 6 vs. 7 split), once again when platform-module-deps stayed scoped to item 6 only.
The user picked item 7 up next. Research this pass (docs/ui-shell-plan branch, three parallel
Explore passes over ARCHITECTURE.md, this repo's existing service conventions, and gateway's current
proxy) found the same shape of problem module-lifecycle-plan.md itself was written to solve: item 7
isn't one branch's worth of work, it's several genuinely separate subsystems bundled under one
bullet point, each needing its own real design decision before any of it can be built. This doc
exists so that work is tracked as a real, scoped plan, the same reason `module-lifecycle-plan.md`
itself exists (see that doc's own Context) — it intentionally doesn't re-explain design decisions
ARCHITECTURE.md already made, and focuses on what's genuinely still open.

## What already exists

- `src/core/ui-shell/README.md` — five lines: "the one front door: unified nav... catalog
  browser... pipeline & run status... deep-links into each module's own UI... workspace switcher.
  ARCHITECTURE.md §2. Not built yet." No code, no framework choice, no build tooling anywhere.
- `src/core/gateway/app/argocd.py` (item 6, platform-module-deps branch) — `list_module_applications()`
  lists installed module `Application`s + their Argo CD health via the Kubernetes API. This is the
  live half of what the Add-ons page needs (see item 6 below) — already built, already reusable.
- `src/core/gateway/app/modules.py` — the one existing precedent for "a new gateway router:" auth via
  `require_auth`, error handling via a typed exception → HTTP status, respx-testable design. Any new
  gateway endpoint this doc's items need should follow this same shape.
- `module.yaml`'s `proxyTo` field (`ModuleManifest.proxyTo`, `platform_cli/manifest.py`) — validated
  at load time (required string) but confirmed via repo-wide grep to be read by **nothing else**:
  `render_application_manifest()` never writes it into the deployed `Application`, no test
  references it. 100% inert schema today, not a working mechanism.
- Nothing else. No `PlatformModule` CRD/registration mechanism exists (the string appears exactly
  once in ARCHITECTURE.md, §3, undefined beyond that one mention). No static module index. No
  frontend tooling anywhere in this repo — every Dockerfile today (`gateway`, `catalog-service`) is
  single-stage Python/uvicorn.

## Dependency-ordered build list

Each item names what's actually still undecided, not just unbuilt — several of these were never
specified anywhere, unlike module-lifecycle-plan.md's items, which mostly had an ARCHITECTURE.md
section to point at.

1. **Pick a frontend stack; scaffold `ui-shell` as a real-but-empty app, deployed end-to-end.**
   ARCHITECTURE.md names no framework anywhere (grepped for React/Vue/Svelte/Vite — nothing). The
   one concrete breadcrumb in the whole repo is `catalog-service/.env.example`'s
   `CORS_ORIGINS=http://localhost:5173` — Vite's default dev port — with the comment "for local dev
   against ui-shell... gateway proxies same-origin, no CORS needed there in-cluster." **Recommended:
   React + TypeScript + Vite** — the least-surprising choice given that's the only signal anywhere,
   rather than a fresh framework evaluation with zero constraints. This step also needs: a new
   multi-stage `Dockerfile` (Node build stage → nginx/static-serve runtime stage — the first
   multi-stage build anywhere in this repo, nothing to copy verbatim, only the surrounding
   conventions: non-root user, build-context scoping comment, `EXPOSE`);
   `src/core/argocd/manifests/ui-shell.yaml` (Deployment+Service+Certificate+Ingress, mirroring
   `gateway.yaml`'s structure exactly, host `app.platform.local` — confirmed free, only
   `keycloak.platform.local`/`gateway.platform.local` are claimed anywhere in this repo today); a new
   self-referencing `apps/core/ui-shell.yaml` Application pointer (hardcoded `repoURL`/
   `targetRevision: dev`, same convention every other self-referencing app uses — see
   `argocd/README.md`'s own section on this); and a new `ci.yml` path-filter + test + build-and-push
   job set, mirroring gateway's exactly. No backend calls, no auth, no nav data — just prove
   git push → CI build → Argo deploy → reachable over Ingress with a static placeholder page.

   **✅ Built, 2026-09-04 (feature/ui-shell-scaffold branch)** — see `src/core/ui-shell/README.md`
   for the full writeup. React+TS+Vite scaffold, multi-stage Docker image, `ui-shell.yaml`/
   `apps/core/ui-shell.yaml` at sync wave 4, `ci.yml`'s `test-ui-shell`/`build-and-push-ui-shell`
   pair. Still just a static placeholder page — items 2-8 below remain fully open.
2. **Resolve the same-origin question.** The original design already assumes gateway proxies
   ui-shell in-cluster so the browser never needs CORS (`.env.example`'s own comment) — but gateway's
   proxy today (`app/proxy.py`) forwards to catalog-service only, one fixed `httpx.AsyncClient` built
   once at startup, nothing path-parameterized. Real decision, not a mechanical extension: does
   gateway grow a second, permanent proxy target for ui-shell's static assets (mirroring how
   `catalog_client` is built in `main.py`'s lifespan), or does ui-shell get its own Ingress host and
   gateway grows CORS support instead (something gateway has zero of today — grepped for
   cors/CORS/Access-Control, zero matches)? This blocks item 5 — nav can't call gateway from the
   browser until one of these is chosen.

   **✅ Resolved, 2026-09-08 (feature/gateway-cors-ui-shell branch): CORS, not same-origin proxy.**
   `proxy.py` turned out to be hardcoded to exactly one backend with no dispatch layer and auth
   applied uniformly to every request — folding ui-shell in would have meant real restructuring, plus
   an explicit keep-or-remove call on ui-shell's already-live Ingress/Certificate. Gateway now mirrors
   catalog-service's own proven `CORSMiddleware` pattern instead (`app/config.py`'s `cors_origins`,
   `app/main.py`'s `configure_cors()`) — see `src/core/gateway/README.md` for the full writeup. Also
   settles the auth-model question for item 3 ahead of time: identity stays bearer-token-in-
   `Authorization`-header, never a cookie, so there's no cross-origin-cookie complication to solve
   there either.
3. **Browser OAuth2/PKCE login.** `platform-cli`'s device flow (`platform_sdk/keycloak_login.py`) is
   CLI-shaped — poll a device code, no browser redirect involved. ui-shell needs a structurally
   different flow: authorization code + PKCE, a redirect URI, session/token storage in the browser.
   `bootstrap/keycloak-bootstrap-login-client.sh`'s own header already explains why
   `platform-cli`/`platform-cli-login` are deliberately two separate Keycloak clients, never merged
   — a third, browser-shaped client continues that same separation rather than overloading either
   existing one. Nothing in `platform_sdk` is reusable here.

   **✅ Built, 2026-09-08 (feature/ui-shell-oauth branch)** — a third Keycloak client,
   `platform-ui-shell` (public, `standardFlowEnabled`, `pkce.code.challenge.method: S256` enforced),
   created by `bootstrap/keycloak-bootstrap-ui-shell-client.sh` (one-time per cluster, same category
   as the other two client scripts). Client-side: hand-rolled authorization-code+PKCE in
   `src/core/ui-shell/src/auth/` — no OIDC library, no router (confirmed unnecessary: `nginx.conf`'s
   existing SPA fallback already serves `/auth/callback`) — tokens in `sessionStorage`, silent
   refresh scheduled before expiry. **Confirmed gateway needs zero code changes**: `app/auth.py`'s
   `verify_token()` has no client-specific check, so any token from this new client is accepted
   exactly like a `platform-cli-login` token, given the same `groups` protocol mapper. See
   `src/core/ui-shell/README.md`'s own "Auth" section for the full writeup. Item 5 (real nav) is now
   unblocked — both items 2 and 3 it depended on are resolved.

   **Confirmed live, 2026-09-08:** real browser round trip against `homelab-dev` end to end — "Log
   in" redirected to Keycloak, a real login completed, landed back on `/auth/callback` then `/` with
   `code`/`state` stripped, showed the logged-in username. The resulting access token was then handed
   directly to gateway (`fetch` from the browser console, `Authorization: Bearer <token>` +
   `X-Workspace: personal`) and got back a real `200 {"modules": []}` — proof gateway's full
   `verify_token()` → `derive_headers()` chain accepts a `platform-ui-shell` token exactly like one
   from `platform-cli-login`, not just that ui-shell can decode its own JWT locally. "Log out"
   confirmed working too. One real bug found and fixed along the way — see
   `docs/known-issues.md`'s entry on `post.logout.redirect.uris` needing Keycloak's `##`
   multi-value-attribute separator, not a space (the bootstrap script 400'd on first run without it).
4. **Gateway module registry v1 — installed modules only.** A new endpoint (extending
   `app/modules.py`, reusing `app/argocd.py`'s `list_module_applications()`) that lists installed
   modules WITH their `displayName`/`icon`/`navPath` — which requires propagating those fields from
   `module.yaml` into the deployed `Application` (e.g. as annotations, in
   `render_application_manifest()` — the same place `proxyTo` needs the same treatment for item 8).
   Deliberately installed-modules-only, no static index needed yet — same "the caller already has
   what it needs locally" realization item 6 had for dependency-checking; nav only needs to know
   what's *actually running*, not the full catalog.

   **✅ Built, 2026-09-08 (feature/gateway-module-registry branch)** — `render_application_manifest()`
   (`platform_cli/manifest.py`) now writes `displayName`/`icon`/`navPath` onto the generated
   `Application` as `platform.io/display-name`/`platform.io/icon`/`platform.io/nav-path`
   annotations (values go through `json.dumps()`, not raw interpolation, so free-form operator text
   can't corrupt the YAML). `app/argocd.py`'s new `list_module_summaries()` reads them back out
   (falling back to id/"puzzle"/None for any Application that predates this branch); gateway's new
   `GET /modules` (`app/modules.py`) exposes that, reusing `require_auth` exactly like
   `check-requirements` — the precedent that endpoint's own docstring already established for
   non-workspace-scoped module data. `proxyTo` still isn't propagated — that stays item 8's own
   future pass over `render_application_manifest()`. See `src/core/gateway/README.md` for the full
   writeup. ui-shell itself still calls nothing (item 5, still blocked on item 3).
5. **ui-shell's real nav**, calling item 4's endpoint once items 2 and 3 are resolved.

   **✅ Built, 2026-09-09 (feature/ui-shell-nav branch)** — `react-router@^8.3.1`, the first routing
   library in this repo (declarative mode: no loaders/actions anywhere here, and the route tree needs
   to be conditionally absent while unauthenticated). `App.tsx` splits into `/auth/callback`
   (`auth/AuthCallbackRoute.tsx`) and `Gate`, which shows `shell/LoginScreen.tsx` or `shell/Shell.tsx`
   by `useAuth().status`; `Shell` owns its own nested routes (`/` module list, `modules/:moduleId`
   detail) so nothing under it mounts, or calls gateway, for an unauthenticated visitor. Fixed a real
   bug along the way: `auth/callback.ts` used to call `window.history.replaceState()` directly to
   clean up the URL after login, which fires no `popstate` event and would leave a mounted router's
   internal location stuck on `/auth/callback` — moved to `AuthCallbackRoute.tsx`'s `useNavigate()`
   instead. A new `src/workspace/` module parses the ID token's `groups` claim client-side (there is
   no "list my workspaces" endpoint anywhere in the platform) into a real workspace switcher, mirroring
   gateway's own `derive_headers()` role logic exactly — display-only, same precedent `tokens.ts`
   already set for `preferred_username`, never an authorization decision. A new `src/modules/` module
   calls `GET /modules` (item 4) via a hand-rolled hook (no data-fetching library) and renders a real
   module list and a client-side detail page per module (keyed by `module_id`, not raw `nav_path` —
   unvalidated server-side and can be `null`), with an honest note that deep-linking into a module's
   own UI is item 8, not built yet. See `src/core/ui-shell/README.md`'s own "Real nav" section for the
   full writeup.
6. **Add-ons page — the static release-time module index.** ARCHITECTURE.md §3's own description:
   "`platform-gateway` reads a static module index built from every `modules/*/module.yaml` at
   release time (so it can list modules that aren't installed yet)... overlays it with... live
   registrations... shows each module's state... by reading Argo CD's `Application` status." The
   live-status half is item 6/platform-module-deps' `argocd.py`, already built. The release-time
   static-index half — a new CI/build step scanning `src/modules/*/module.yaml` into a JSON
   artifact something can serve — has no code anywhere. Bigger and separable from item 4's live-only
   registry.

   **✅ Built (backend only), 2026-09-09 (feature/gateway-module-catalog branch)** — deliberately
   the same item-4/item-5 split: this branch is the endpoint, the Add-ons *page* itself in
   `ui-shell` is a future item. `platform module build-index <out-path>` (new,
   `platform_cli/module_index.py`'s pure `build_static_module_index()`) scans every
   `src/modules/*/module.yaml` except `_template`, validated through the same `load_module_manifest`
   `install`/`scaffold` already use. It runs as a new step in `build-and-push-gateway` (`ci.yml`),
   before the Docker build, writing straight into `src/core/gateway/app/module_catalog.json` —
   inside gateway's existing `COPY app ./app`, no Dockerfile change needed, and gateway's own build
   context stays scoped to `src/core/gateway` rather than growing a cross-package one. `src/modules/**`
   joined gateway's CI path filter as part of this — it watched nothing before, so a module-only
   change used to trigger no rebuild at all. Gateway's new `GET /modules/catalog`
   (`app/module_index.py`'s `load_static_module_index()` + the same `list_module_applications()`
   `check-requirements` already uses) lists every module in the static index, installed or not,
   overlaid with live status — display fields always come from the static file, never a live
   Application's annotations, so a long-installed module's stale un-reinstalled annotations can't
   leak through. A missing/malformed static file degrades to an empty catalog, never a crash or a
   503 — a different failure mode than `ArgoCDUnavailableError`, since nothing else behind gateway
   reads this file.

   **Confirmed live, 2026-09-09**, against `homelab-dev` after a real `dev`-branch CI run and
   `rollout restart`: the generated `module_catalog.json` landed inside the running pod exactly
   where `config.py` expects it; `GET /modules/catalog` returned `hello-module` with its real
   `display_name`/`icon`/`nav_path`/`requires`/`optional` from the static file and `status:
   "Healthy"` sourced from live Argo CD (not the static file's default) — proof the overlay is a
   real merge of the two, not one path silently winning; missing/wrong-workspace auth returned
   401/403 exactly like `GET /modules` and `GET /modules/check-requirements`.
7. **Install/Remove buttons on the Add-ons page.** A real, unanswered trust-boundary question, not
   a UI question: does gateway get git push credentials to actually commit
   `modules-enabled/*.yaml` the way `platform-cli` does from the operator's own local git checkout
   today? Nothing in this repo has ever given a *service* write access to its own source repo.

   **Scoped, 2026-09-09** (two decisions with the repo owner, feature/ui-shell-addons branch): (a)
   this branch builds only the read-only Add-ons *page* — the item-4/5 split repeated once more,
   item 6 shipped the endpoint with no consumer, this ships the consumer with no mutation; (b) when
   the mutation mechanism itself is built (a separate future branch), gateway triggers a GitHub
   Actions `workflow_dispatch` rather than holding a direct git/PAT credential — reusing the same
   ephemeral, auto-scoped `GITHUB_TOKEN` pattern `ci.yml` already uses for GHCR pushes. Decision (b)
   is recorded here but not implemented; nothing in this branch triggers a workflow or touches git.

   **✅ Built (read-only page), 2026-09-09 (feature/ui-shell-addons branch)** — new `src/addons/`
   directory in `ui-shell` (its own top-level concern, alongside `auth/`, `modules/`, `shell/`,
   `workspace/`), consuming item 6's `GET /modules/catalog` through a hand-rolled hook (`useAddons.ts`,
   a structural copy of `modules/useModules.ts`). `Addons.tsx` lists every catalog entry — installed
   or not — with its icon, status badge, and `requires` list as plain text (no satisfaction
   computation here; `check-requirements` still owns that), a page-level "not built yet" notice, and a
   disabled `Install` button with a tooltip. An already-installed entry's name links to the existing
   `/modules/:moduleId` detail page instead of showing the button. `Shell.tsx` gained a `/addons`
   route and a small nav (`NavLink`, first use in this repo) to reach it. See
   `src/core/ui-shell/README.md`'s own "Add-ons page" section for the full writeup.

   **Confirmed live, 2026-09-09**, against `homelab-dev` after merge to `dev` and a `rollout restart`:
   the "Add-ons" nav link appears and routes to `/addons`; `hello-module` renders with its correct
   icon/name/`requires` ("No dependencies") and its real live status ("Healthy"); the page-level notice
   and the disabled, tooltipped Install button both render as designed; since `hello-module` is
   installed, its name links through to the existing `/modules/hello-module` detail page, confirmed
   still rendering correctly (no regression from the new route/nav); loading `/addons` directly from
   the URL bar (not just client-side navigation) rendered correctly — the same `nginx.conf` SPA
   fallback regression check item 5 established, now passing for a second route.

   **✅ Mutation mechanism built (backend only), 2026-09-10 (feature/gateway-module-lifecycle-dispatch
   branch)** — the trust-boundary decision recorded above, actually built: `POST
   /modules/{module_id}/install` and `.../uninstall` (`app/modules.py`) don't touch git themselves.
   Each calls new `app/github_dispatch.py`'s `trigger_module_workflow()`, which asks the GitHub API to
   start a `workflow_dispatch` run of new `.github/workflows/module-lifecycle.yml` — that workflow
   (unmodified `platform module install`/`uninstall` underneath) does the real commit/push, using
   GitHub Actions' own ephemeral, auto-scoped `GITHUB_TOKEN` (`contents: write`, scoped to that one
   job only), exactly the pattern decided on. Both endpoints are fire-and-forget: a `202` means the
   workflow was told to start, nothing here waits for or reports back on how it went.

   New `app/auth.py` `require_role()` is this service's first check of `derived.role` for anything
   beyond plain membership — both endpoints require at least **editor** (a scoping decision with the
   repo owner, alongside "backend only" and the GitHub-dispatch mechanism itself). `install` reuses
   `check-requirements`'s own comparison for a `409` on unsatisfied `requires`, and deliberately does
   **not** block reinstalling an already-`Healthy` module (`manifest.py`'s own docstring already
   documents that as safe). Gateway's credential is a fine-grained GitHub PAT scoped to **Actions:
   read/write only** on this repo (never Contents — the actual push never uses it), stored as this
   repo's first real **SealedSecret** (`argocd/manifests/gateway.yaml`; the controller has been
   deployed since day one but never actually used until now) via new
   `bootstrap/seal-gateway-github-token.sh`.

   Two real git-plumbing wrinkles found during research, both fixed entirely in
   `module-lifecycle.yml` with no change to `platform_cli/repo.py`'s core logic: (1)
   `actions/checkout@v4` sets `origin` to the HTTPS form, not the SSH form every self-referencing
   Application (and Argo CD's own configured credential) expects — fixed with a new optional
   `--repo-url` override on `platform module install` (`platform_cli/module.py`), which the workflow
   passes explicitly; every existing local/CLI call is unaffected since it still defaults to
   `discover_repo_url()`. (2) `actions/checkout@v4` leaves a detached HEAD even with an explicit
   `ref:` — fixed with `git checkout -B dev` right after checkout, so the unmodified `commit_and_push`
   has a real upstream to push to. See `src/core/gateway/README.md`'s own "Install/uninstall dispatch"
   and "GitHub PAT for the module-lifecycle dispatch" sections for the full writeup.

   **Confirmed live, 2026-09-10**, against `homelab-dev` and the real GitHub repo, end to end:
   `POST .../hello-module/install` with an editor-role token returned `202`; the "Module lifecycle"
   workflow fired and succeeded (`33s`, single green step); the resulting commit
   (`gateway-module-lifecycle-bot`) landed with `repoURL: git@github.com:DougallPercival/
   opendataplatform.git` — the correct SSH form, proving wrinkle (1) is actually fixed, not just
   reasoned about — and Argo CD reconciled `hello-module` straight to `Synced`/`Healthy`, never stuck
   `Unknown`, proving wrinkle (2) is fixed too (a detached-HEAD push failure would have left the file
   unchanged and Argo CD with nothing new to reconcile). `POST .../uninstall` also returned `202`, its
   own workflow run succeeded, and the commit correctly deleted `modules-enabled/hello-module.yaml`
   and pushed. A viewer-role token got `403` from both endpoints with the exact designed message,
   without ever reaching GitHub; `nonexistent-module` got `404` from `install`. The `409`
   unsatisfied-`requires` case was not exercised live (no second catalog module declaring
   `hello-module` as a dependency exists to test against) — left to the passing test suite
   (`test_modules.py`), same as any other case this branch didn't have real fixtures for.

   Three real things found and fixed/worked around along the way, worth recording since they'll bite
   again on a fresh setup otherwise: `bootstrap/seal-gateway-github-token.sh` originally called
   `kubeseal --fetch-cert` under plain `sudo`, which has no k3s-specific kubeconfig fallback the way
   `sudo kubectl` does — fixed by passing `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` explicitly (override:
   `KUBESEAL_KUBECONFIG`). `module-lifecycle.yml` (a `workflow_dispatch`-only workflow, no
   `push`/`pull_request` trigger) never appeared in GitHub's Actions UI until it existed on the repo's
   **default branch** — this repo's default was `main`, but every self-referencing Application and
   this whole branch's own workflow only ever targets `dev`; fixed operationally by switching the
   repo's default branch to `dev` itself, on the reasoning that `main` wasn't actually part of this
   repo's real workflow anywhere else. And see `docs/known-issues.md`'s new entry,
   "`modules-root` doesn't reliably auto-prune a module's `Application` object on uninstall" — a
   real, pre-existing gap (predates this branch, in `apps/core/modules-root.yaml` from
   platform-module-lifecycle) this was the first live exercise of `platform module uninstall` to
   actually hit; worked around per-instance with a direct `kubectl delete application`, left open as
   real follow-up work since it affects both doors into uninstall equally, not something this
   branch's own scope covers fixing.

   **Wiring the Add-ons page's Install/Remove buttons, `feature/ui-shell-addons-mutation`, built and
   confirmed live 2026-09-10** — the last piece of item 7, closing the item-6→7(read-only)→7(mutation)
   split. `ui-shell/src/addons/mutations.ts` calls the two endpoints above directly; a pure/React split
   (`mutationState.ts` + `useAddonMutations.ts`, same pattern `workspace/workspaces.ts`/
   `WorkspaceContext.tsx` already established) turns a `202`'s fire-and-forget "queued" into a short
   poll against the already-fetched catalog (5s interval, 2-minute window) that resolves itself the
   moment the module's real status flips, or falls back to a "still processing" note if it doesn't.
   Role-gating mirrors gateway's `require_role(derived, "editor")` client-side (a UX nicety only,
   re-checked server-side regardless); Remove goes through an inline confirm step noting removal can
   take a few minutes, rather than blocking this branch on fixing the `modules-root` prune gap first —
   this session's own scoping decision. See `ui-shell/README.md`'s "What's built" and "What can only be
   confirmed live" sections for the full writeup.

   **Confirmed live, 2026-09-10**, against `homelab-dev`, clicking through the real page (not curl):
   Install on `hello-module` showed the disabled "Installing…" state, a "queued" note, and — this
   needed one extra fix along the way, see below — resolved on its own to `Healthy` with a real Remove
   button once checked back on. Remove showed the confirm/cancel step with the "can take a few minutes"
   note, fired successfully (workflow succeeded, commit landed), and hit the `modules-root` prune gap
   exactly as expected: the row never resolved on its own, and after 2 minutes correctly fell back to
   "Still processing — refresh in a bit to check, or try again below" rather than staying stuck — the
   manual `kubectl -n argocd delete application hello-module` workaround then resolved it immediately,
   confirming this branch's timeout fallback and the existing workaround both hold under a second real
   exercise (see the updated `docs/known-issues.md` entry).

   One real deploy-pipeline issue found and fixed along the way, not a code bug: right after merge, the
   Add-ons page kept showing the *old* read-only UI even though Argo CD reported `ui-shell` `Synced`/
   `Healthy` on the merge commit. Root cause — `ui-shell`'s image tag is the mutable `:dev`, not a
   per-commit digest, so Argo CD's sync status only reflects whether the Deployment's *spec* (which
   always just says `:dev`) matches git, not whether the running pod has actually pulled today's build;
   the pod was 12 hours old and had simply never been asked to re-pull. `imagePullPolicy: Always` means
   it would have, given the chance — `sudo kubectl -n ui-shell rollout restart deployment ui-shell`
   forced it and the digest changed as expected. Worth remembering for every future `ui-shell`/gateway
   branch: merging to `dev` and confirming Argo CD is `Synced` is not sufficient proof the new build is
   actually running — a changed image digest after a rollout (or a restart) is the real check.

   **Considered and deferred, not built:** a "Force cleanup" action (a new gateway endpoint deleting
   the orphaned `Application` via Argo CD's own API, surfaced as a UI button only once a Remove has
   timed out) to make the `modules-root` workaround a click instead of a terminal command. Decided
   against scope-creeping this branch; documented as a deferred idea, including the one design decision
   already made for whenever it's picked up (Argo CD's REST API over a direct Kubernetes RBAC grant,
   and why it must never fire automatically on click — see `docs/known-issues.md`'s updated entry for
   the full reasoning, including the race condition with the workflow's own git push that rules out
   firing it immediately).
8. **Reverse-proxying into a module's own UI** ("deep-links into each module's own UI," §2 — the
   only phrase touching this anywhere, undefined beyond that). Needed `proxyTo` actually propagated
   into the deployed `Application` (previously inert, see "What already exists" above) plus a new
   gateway route, and left proxy vs. iframe vs. plain external link unresolved — ARCHITECTURE.md
   never said which.

   **Scoped, 2026-09-10** (three decisions with the repo owner): (a) auth — gateway mints a
   short-lived, module-scoped proxy token, accepted as a `?token=` query param on the new proxy
   route, because a plain `<iframe src>` navigation structurally cannot send a custom
   `Authorization` header, and this system has deliberately never used cookies for identity; (b)
   presentation — an embedded iframe on the existing module detail page, not a new-tab link; (c)
   scope — the full slice in one branch (schema propagation, gateway route, token minting, `ui-shell`
   UI), proven end to end against `hello-module`.

   **✅ Built, 2026-09-10 (feature/module-proxy branch)** — `platform_cli/manifest.py`'s
   `render_application_manifest()` now writes `proxyTo` as the `platform.io/proxy-to` annotation, the
   same pattern `displayName`/`icon`/`navPath` (item 4) already established. New
   `app/module_proxy.py` (gateway): `GET /modules/{id}/proxy-token` mints a stateless, 5-minute
   HS256 JWT (`module_id`, `workspace`, `user`, `role`, `purpose: "module-proxy"` claims, all taken
   from that request's own verified auth, never re-derived) — HS256 rather than a second RS256
   keypair or a server-side token store, since nothing else ever verifies this token type and
   gateway has no persistence layer that would survive a pod restart. `{GET,POST,PUT,PATCH,DELETE}
   /modules/{id}/proxy[/{path}]` then decodes that token from `?token=`, resolves the module's
   backend URL fresh on every request (never cached, so an uninstall mid-session 404s the very next
   request), and streams the request through — forwarding `X-Workspace`/`X-User`/`X-Role` from the
   **token's** claims, with any caller-supplied versions of those headers stripped first, and
   stripping `X-Frame-Options`/CSP `frame-ancestors` from the module's response so gateway's own
   origin framing it doesn't get silently blocked. `ModuleSummary.has_own_ui` (`GET /modules`) is a
   boolean, not the raw proxy URL, which stays server-side-only. `ui-shell`'s `ModuleDetail.tsx`
   mints a token once per page view (`useModuleProxyToken.ts`) and renders
   `<iframe src=".../proxy/?token=...">` when `hasOwnUi` is true; a module installed before this
   branch shows a "reinstall to pick this up" notice instead. Full writeups: `src/core/gateway/
   README.md`'s "Reverse-proxying into a module's own UI" section and `src/core/ui-shell/README.md`'s
   matching entry.

   **Follow-up-request propagation, fixed 2026-09-11** (a later branch, not this one): the `?token=`
   query param alone never propagated to a module's own follow-up requests — a relative
   `<script src>`/`fetch()` a module's page issues resolves against the current document URL and drops
   the query string entirely. `app/module_proxy.py`'s `_set_proxy_cookie` now also sets the token as a
   cookie scoped to `Path=/modules/{module_id}/proxy` on every successful proxied response, so any
   follow-up request under that path — including a JS-issued `fetch()`, not just markup-declared
   resources — carries it automatically. Real, known trade-off: a third-party cookie from the browser's
   point of view (the iframe's origin differs from `ui-shell`'s top-level page origin), which some
   browsers block or partition by default — where that happens, it falls back to the original gap, not
   worse. **Confirmed live, 2026-09-11**, against `homelab-dev`, with `curl` standing in for a module's
   own follow-up request (no real second module exists yet to prove it against a real browser): minted
   a token, hit `.../proxy/?token=...`, and got back a `200` with `set-cookie:
   mp_token_hello-module=...; HttpOnly; Max-Age=182; Path=/modules/hello-module/proxy; SameSite=none;
   Secure` — then a second request to `.../proxy/` with **no `?token=` at all**, only that cookie, also
   came back `200`. That's the exact contract a module's own `fetch()` needs; the server-side mechanism
   is proven, a real browser/real-second-module test is the remaining gap. See `src/core/gateway/
   README.md`'s matching section and `docs/known-issues.md`.

   **Confirmed live, 2026-09-10**, against `homelab-dev` and the real GitHub repo, end to end: `curl`
   against `GET /modules/hello-module/proxy-token` with a real editor-role token returned a JWT whose
   decoded claims matched exactly (`module_id`, `workspace: "personal"`, `user`, `role: "editor"`,
   `purpose: "module-proxy"`, `exp - iat = 300`); `curl` against `.../proxy/?token=...` streamed back
   the real stock nginx page with a `200` and no `X-Frame-Options` header. Before `hello-module` was
   reinstalled to pick up the new `proxy-to` annotation, the mint endpoint correctly `404`'d with the
   documented "isn't installed, or doesn't have a proxied UI yet" message — a live confirmation of the
   exact transient-annotation-gap behavior items 4/6 already established, not a bug. In the browser,
   `hello-module`'s detail page initially still showed the *old* "isn't built yet" static notice even
   after merge — the same `ui-shell` mutable-`:dev`-tag stale-pod gotcha item 7's own close-out
   documented, hit for a second time; `sudo kubectl -n ui-shell rollout restart deployment/ui-shell`
   plus a hard-refresh fixed it, and the iframe then rendered the real nginx page inline. This is now
   the second time in two consecutive branches this exact gotcha has bitten — worth treating "merged
   and `Synced`" as never sufficient proof a `ui-shell`/gateway branch is actually live; always force
   a rollout restart and check the image digest changed.

   One real, unrelated bug found and fixed along the way: this branch's second `SealedSecret`
   document in `argocd/manifests/gateway.yaml` (`gateway-module-proxy-secret`, alongside item 7's
   `gateway-github-token`) broke `bootstrap/seal-gateway-github-token.sh`'s original boundary-detection
   logic, which found "the last `SealedSecret` document in the file" positionally — a shortcut that
   only ever worked while there was exactly one. Left as-is, a future GitHub PAT rotation would have
   silently overwritten the *wrong* document. Fixed with a new shared `bootstrap/lib/common.sh`
   function, `replace_sealed_secret_document()`, that finds each document by its own `metadata.name`
   instead — used by both the existing script and this branch's new
   `bootstrap/seal-gateway-module-proxy-secret.sh` (which generates the secret itself via
   `openssl rand -hex 32` rather than prompting, since it's an internal signing secret with no
   external credential to go create first). While fixing this, also found the original
   boundary-detection regex never matched at all against this file's real on-disk (CRLF-terminated)
   line endings — a second, latent bug independent of the two-document issue — so the fix is
   CRLF-tolerant too. Verified by simulating a rotation of each secret independently against a copy
   of the real `gateway.yaml` before ever running either script against the live cluster.

   This closes out every item on this doc's build list — see `src/core/gateway/README.md`'s and
   `src/core/ui-shell/README.md`'s own "What's NOT built yet" sections, both now pointing past
   `ui-shell-plan.md` entirely to ARCHITECTURE.md's broader, longer-term gaps instead.

## Recommended first slice

**Item 1 only.** Scaffold + deploy pipeline, no auth, no data, no dynamic content — proves the
entirely-new infrastructure (frontend build tooling, a multi-stage Docker image, a fourth Ingress
host, a fourth CI job) works end-to-end before any of items 2-8's real design decisions get built on
top of it. Deliberately not committing to a build order beyond "item 1 first," unlike
`module-lifecycle-plan.md`'s own "items 1-5 as one slice" — that slice was one tightly-coupled
mechanism end-to-end (install → reconcile → uninstall), where building item 3 without item 2 already
in place made no sense. Items 2-8 here are more independent: several separate subsystems that happen
to feed the same eventual page, not one pipeline. Each deserves its own scoping pass when it's
picked up, the same way this doc itself is that scoping pass for item 7 as a whole.

## Open questions this doc deliberately doesn't resolve

- **Same-origin proxying vs. CORS** (item 2) — which one gateway actually implements.
- **Git push credentials for gateway** (item 7) — **decided 2026-09-09, built and confirmed live
  2026-09-10**: gateway triggers a GitHub Actions `workflow_dispatch` rather than holding a direct
  git/PAT credential, per the writeup under item 7 above. `platform-cli` running as the operator
  locally, and now gateway's dispatch (via the workflow's own ephemeral `GITHUB_TOKEN`), are the only
  two things that ever commit to this repo — still never a credential gateway itself holds.
- **Proxy vs. iframe vs. external link** for deep-links into a module's own UI (item 8) —
  **decided and built 2026-09-10**: an embedded iframe, authenticated via a short-lived,
  module-scoped proxy token gateway mints and accepts as a `?token=` query param (a plain
  `<iframe src>` navigation can't send a custom header). See the writeup under item 8 above.
- **What `PlatformModule` registrations actually are** — ARCHITECTURE.md's one undefined mention
  (§3). Items 4-5 above sidestep needing an answer (they read Argo CD `Application` state directly,
  the same move item 6 made for dependency-checking) — but if a future need reintroduces this
  concept literally, it still has no schema or mechanism anywhere to build from.
