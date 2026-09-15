"""Settings, read from environment / .env — same pydantic-settings pattern
catalog-service/app/config.py and platform_sdk/config.py both use. See
.env.example for the in-cluster values gateway.yaml's Deployment actually
sets; the defaults below match those, so a plain `docker run` against a
real cluster's Keycloak/catalog-service (e.g. via port-forwards for local
testing) needs little to no overriding.

Two Keycloak URLs, deliberately different strings pointing at the same
Keycloak — this is the single most important thing to get right in this
file, so it's explained here rather than left for someone to work out from
a confusing bug report:

- `keycloak_internal_url` — where gateway itself CONNECTS to fetch the JWKS
  (jwks.py). Keycloak's real in-cluster Service DNS name, reached directly
  — no port-forward, no hostname-resolution patch. platform_sdk's CLI-side
  Keycloak tooling needs that trick (see
  platform_sdk/_keycloak_connection.py's docstring) because Keycloak's
  hostname provider strictly enforces `keycloak.platform.local` for
  everything it GENERATES; that's a separate concern from what Host header
  a request arrives with, and pure JWKS/JSON API calls never hit it. What
  DOES need fixing for this URL to work is TLS: `keycloak-tls`'s Certificate
  needed this DNS name added as a SAN (see
  argocd/manifests/keycloak-instance.yaml, 2026-09-02) since a cert only
  listing the public hostname would otherwise fail hostname verification
  for a direct in-cluster connection.
- `keycloak_public_url` — what Keycloak actually PUTS in every token's
  `iss` claim. Governed by `spec.hostname.hostname`
  (keycloak.platform.local) — that setting affects every URL Keycloak
  itself *generates*, issuer strings included, regardless of which
  hostname/IP a client actually connected through. verify_token() (auth.py)
  checks a token's `iss` against THIS value, never against
  `keycloak_internal_url` — using the wrong one here would make every
  single token fail verification with a confusing issuer-mismatch error,
  not a connection error, which is exactly the kind of thing worth a long
  comment to prevent.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", env_file_encoding="utf-8")

    keycloak_realm: str = "platform"
    keycloak_internal_url: str = "https://platform-service.keycloak.svc.cluster.local:8443"
    keycloak_public_url: str = "https://keycloak.platform.local"
    # Mounted from platform-ca-secret (cert-manager's platform-ca
    # ClusterIssuer) — Reflector mirrors that Secret into this service's
    # namespace (see argocd/manifests/cluster-issuer.yaml's secretTemplate
    # annotations, 2026-09-02) and gateway.yaml's Deployment mounts it here.
    # Falls back to the system default trust store if this path doesn't
    # exist (see main.py's _keycloak_tls_verify()) — true in local dev/tests,
    # never true in a real Deployment.
    keycloak_ca_path: str = "/etc/gateway/keycloak-ca/ca.crt"

    # catalog-service's real in-cluster Service DNS name + port — see
    # argocd/manifests/catalog-service.yaml's Service (name/namespace both
    # "catalog-service", port 8000).
    catalog_service_url: str = "http://catalog-service.catalog-service.svc.cluster.local:8000"

    # How long a fetched JWKS document is trusted before get_key() forces a
    # refetch even for a `kid` it already has cached — bounds how long a
    # revoked/rotated Keycloak signing key could theoretically still verify
    # a token here (it can't actually forge a NEW token without Keycloak's
    # private key, but this bounds how long an already-issued token signed
    # with a since-rotated key stays accepted). 5 minutes is generous
    # relative to how rarely Keycloak actually rotates realm keys.
    jwks_cache_seconds: int = 300

    upstream_timeout_seconds: float = 10.0

    # platform-module-deps branch (2026-09-03) — app/argocd.py's in-cluster
    # Kubernetes API client, for GET /modules/check-requirements
    # (app/modules.py). Standard mounted-ServiceAccount paths/URL; a real
    # Deployment always has these (see gateway.yaml's new `gateway`
    # ServiceAccount), same "file exists -> real value, else dev-friendly
    # gap" story as keycloak_ca_path above — see argocd.py's own docstring
    # for what happens when they're missing (a clear 503, not a crash).
    k8s_api_url: str = "https://kubernetes.default.svc"
    k8s_sa_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    k8s_sa_ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    # Namespace argocd/manifests/*.yaml install every Application into
    # (root/apps/core/modules-root/each modules-enabled/*.yaml) — see
    # argocd/README.md.
    argocd_namespace: str = "argocd"

    # feature/gateway-cors-ui-shell (2026-09-08), ui-shell-plan.md item 2 —
    # same shape as catalog-service/app/config.py's own cors_origins, but
    # NOT theoretical here the way catalog-service's copy is (that one's
    # unused in-cluster; a NetworkPolicy means a browser never reaches
    # catalog-service directly). ui-shell really does call gateway
    # cross-origin, so this needs a real value in gateway.yaml's Deployment
    # env, not just a documented-but-empty local-dev knob — see main.py's
    # configure_cors() for how this gets used.
    cors_origins: str = ""

    # ui-shell-plan.md item 6 (feature/gateway-module-catalog, 2026-09-09) — the static
    # release-time module index GET /modules/catalog reads (app/module_index.py). Deliberately
    # RELATIVE, unlike the k8s_sa_*/keycloak_ca_path settings above which are absolute paths to
    # files mounted from Secrets at known cluster locations: this file instead ships INSIDE
    # gateway's own image, generated by build-and-push-gateway's CI step
    # (`platform module build-index src/core/gateway/app/module_catalog.json`, run from the repo
    # root) at exactly this path relative to the gateway package. The same relative suffix resolves
    # correctly against either real cwd this process ever runs with: the Dockerfile's `WORKDIR
    # /srv` (`COPY app ./app` puts it at /srv/app/module_catalog.json) in a real Deployment, or
    # this package's own directory (README.md's local-dev `uvicorn app.main:app` invocation) when
    # running locally.
    static_module_index_path: str = "app/module_catalog.json"

    # ui-shell-plan.md item 7's mutation mechanism (feature/gateway-module-lifecycle-dispatch,
    # 2026-09-10) — app/github_dispatch.py's trigger_module_workflow(). github_token is
    # deliberately blank by default, same "missing -> a clear, distinct error, not a crash" shape
    # k8s_sa_token_path/keycloak_ca_path already use: in a real Deployment it comes from a
    # SealedSecret-backed Secret via secretKeyRef (argocd/manifests/gateway.yaml,
    # GATEWAY_GITHUB_TOKEN), decrypted in-cluster — the plaintext never touches git, and local
    # dev/tests never set it, which is exactly when github_dispatch.py should refuse up front
    # rather than attempt a request with an empty Authorization header.
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    # Real casing (not GHCR's lowercased form — see ci.yml's IMAGE_NAME comment for why that one's
    # different): the GitHub REST API's own repo path segment, matching every repoURL: elsewhere.
    github_repo: str = "DougallPercival/opendataplatform"
    github_workflow_file: str = "module-lifecycle.yml"
    # Always `dev` — the one branch every self-referencing Application and every module
    # `platform module install` generates already targets (see argocd/README.md's "Self-referencing
    # apps" section). Not derived from anything at request time; there's no notion of "install
    # against test/main" anywhere else in this repo either.
    github_dispatch_ref: str = "dev"

    # ui-shell-plan.md item 8 (feature/module-proxy, 2026-09-10) — app/module_proxy.py's short-lived,
    # module-scoped proxy tokens (GET /modules/{id}/proxy-token, verified by the GET|POST|PUT|PATCH|
    # DELETE /modules/{id}/proxy[/{path}] route). A stateless HS256 JWT signed with THIS secret, not a
    # second RS256 keypair (nothing outside gateway ever verifies this token type — publishing a JWKS
    # for one gateway-internal, seconds-lived token would be new infrastructure for no real benefit)
    # and not a server-side token store (gateway has no persistence layer, and an in-memory map
    # wouldn't survive a pod restart or a future `replicas: >1`). Deliberately blank by default, same
    # "missing -> a clear, distinct error, not a crash" shape github_token above already uses: minting
    # should refuse up front rather than sign with an empty key. In a real Deployment this comes from
    # a SealedSecret-backed Secret via secretKeyRef (argocd/manifests/gateway.yaml,
    # GATEWAY_MODULE_PROXY_TOKEN_SECRET) — see bootstrap/seal-gateway-module-proxy-secret.sh, which
    # GENERATES this value (openssl rand -hex 32) rather than prompting for one the way
    # seal-gateway-github-token.sh does: this isn't an external credential like a GitHub PAT, nothing
    # outside gateway itself ever needs to know it.
    module_proxy_token_secret: str = ""
    # 5 minutes: long enough to actually look at a module's detail page, short enough not to leave a
    # long-lived credential sitting in a URL (browser history, gateway access logs, and any Referer
    # header the module's own content generates are all more exposed for a query-string token than an
    # Authorization header — a real, accepted trade-off of embedding it in the iframe's src, not
    # something this setting can fix). Minted once per ModuleDetail page view, never refreshed.
    module_proxy_token_ttl_seconds: int = 300

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(" ") if o.strip()]

    @property
    def jwks_path(self) -> str:
        return f"/realms/{self.keycloak_realm}/protocol/openid-connect/certs"

    @property
    def expected_issuer(self) -> str:
        return f"{self.keycloak_public_url}/realms/{self.keycloak_realm}"


settings = Settings()
