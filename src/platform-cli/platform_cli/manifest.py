"""`module.yaml` schema, validation, and Argo CD `Application`-manifest generation —
docs/architecture/module-lifecycle-plan.md's items 2 and 3 (platform-module-lifecycle branch,
2026-09-03). Two things happen here, deliberately kept in one file since they're two sides of the
same "turn a module.yaml into a running module" conversion `platform module install` performs:

1. `ModuleManifest`/`Placement`/`Toleration` — the same lightweight descriptor ARCHITECTURE.md §3
   already shows (`modules/notebook-jupyterhub/module.yaml`), plus §7's `placement` block, plus one
   additive field this branch adds: `namespace` (defaults to `id`) — something has to decide the
   generated Application's `destination.namespace`, and every existing core service already gets
   its own namespace named after itself, so adding it to the schema keeps module.yaml the one
   source of truth rather than inventing a second convention elsewhere.

   `model_config = ConfigDict(extra="forbid")` resolves the plan doc's "module.yaml validation
   failure mode" open question: a typo'd field name fails loudly at `install`/`scaffold` time with
   a specific field-level message (via `ManifestError`, caught by `errors.py`'s
   `handle_module_errors`), not silently ignored the way a plain dict-based YAML load would.

2. `render_application_manifest()` — turns a validated `ModuleManifest` into the literal YAML text
   `platform module install` writes to `src/modules-enabled/<id>.yaml`. Deliberately a hand-written
   string template, not `yaml.safe_dump(...)`: every other Application manifest in this repo
   (apps/core/*.yaml) carries a substantial header comment explaining what it is and why it's
   shaped the way it is — `yaml.safe_dump` can't produce that, and a generated file that looks
   nothing like every hand-authored one next to it would be its own small inconsistency. The
   generated Application is a complete, standalone manifest (not a lightweight pointer) — it's what
   `modules-root` (src/core/argocd/apps/core/modules-root.yaml) picks up directly, the same way
   root-app.yaml turns files in apps/core/ into core services (see that file's own header comment
   for the three-level app-of-apps structure this relies on).

   2026-09-08 (feature/gateway-module-registry branch, ui-shell-plan.md item 4): the generated
   `Application`'s metadata also carries `displayName`/`icon`/`navPath` as `platform.io/*`
   annotations now — gateway's `GET /modules` (app/argocd.py's `list_module_summaries()`) reads
   them back out to build ui-shell's future nav. Values go through `json.dumps()` rather than raw
   f-string interpolation: these are free-form operator text (`displayName` especially), and a
   colon or quote in one would otherwise corrupt the generated YAML — a JSON string is also a valid
   YAML double-quoted flow scalar, so this needs no new dependency.

   2026-09-10 (feature/module-proxy branch, ui-shell-plan.md item 8): `proxyTo` now propagates too,
   as `platform.io/proxy-to` — the last of module.yaml's display/deploy metadata to make this trip.
   Gateway's new `GET /modules/{id}/proxy[/{path}]` route (app/module_proxy.py) reads this annotation
   back out (via a new `argocd.get_module_proxy_target()`, same "fresh Kubernetes API read, never
   cached" pattern `list_module_applications()`/`list_module_summaries()` already use) to know which
   in-cluster Service to forward a module's own UI traffic to. Same `json.dumps()` convention as the
   other three annotations, even though `proxyTo` is a plain internal URL unlikely to need escaping —
   consistency with its neighbors is worth more than the one saved line.

   2026-09-14 (feature/module-external-chart branch, ARCHITECTURE.md §11 Phase 3 kickoff): a module's
   chart no longer has to live in this repo. `ModuleManifest.externalChart` (new, optional) lets
   `module.yaml` point straight at a chart's own upstream Helm repo instead — the same shape
   `apps/optional/storage-seaweedfs/seaweedfs.yaml` already uses by hand (`repoURL`/`chart`/
   `targetRevision`), now reachable through the module system for the first time. `None` (the
   default) is every module written before this branch, completely unchanged: `hello-module` and
   `_template` both still resolve to a local `src/charts/<id>/` chart the way they always have.
   Picked over vendoring the upstream chart as a Helm dependency (a real alternative, discussed and
   rejected with the repo owner) because Phase 5/6 already know they'll need to wrap several more
   third-party charts (Spark operator, Dask, Superset, MLflow) — worth building and fully testing
   this once now rather than a narrower fix per module, and it avoids committing binary `.tgz`
   vendored charts to git. `ModuleManifest.values` (also new) is the other half of the same change:
   an external-chart module has no local `values.yaml` layer of its own to carry its real
   configuration the way a local chart's does, so `module.yaml` needs somewhere to put it directly.
   Defaults to `{}` for every existing module, so nothing already written changes shape. See
   `src/modules/trino/module.yaml` for the first real module built this way, and
   `docs/architecture/module-lifecycle-plan.md`'s matching entry for the full design writeup.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ManifestError(Exception):
    """A module.yaml failed to load or validate. Always carries a message that's already
    readable on its own — callers (module.py) don't need to inspect a wrapped Pydantic
    ValidationError themselves."""


class Toleration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    operator: str = "Equal"
    value: str | None = None
    effect: str


class Placement(BaseModel):
    """ARCHITECTURE.md §7: "a module's module.yaml carries an optional placement hint that the
    chart wrapper turns into the actual nodeAffinity/tolerations block" — this is that hint,
    unchanged from what §7 shows. (Renders a PREFERRED affinity, not a hard nodeSelector — see
    §12's "Single-node placement fallback" decision; that change lives entirely in the chart's
    _helpers.tpl, this schema is unaffected.)"""

    model_config = ConfigDict(extra="forbid")

    role: str
    tolerations: list[Toleration] = Field(default_factory=list)


class ExternalChart(BaseModel):
    """A module's chart isn't in this repo at all — it lives in the chart's own upstream Helm repo
    (this module's own docstring, 2026-09-14). Mirrors exactly what
    `apps/optional/storage-seaweedfs/seaweedfs.yaml` already writes by hand into an `Application`'s
    `spec.source`; this is just that same three-field shape, now reachable from `module.yaml`."""

    model_config = ConfigDict(extra="forbid")

    repoURL: str
    chart: str
    version: str


class ModuleManifest(BaseModel):
    """The `module.yaml` schema. Field set matches ARCHITECTURE.md §3's
    `modules/notebook-jupyterhub/module.yaml` example exactly, plus `namespace` (see this module's
    docstring), `placement` (§7), and `externalChart`/`values` (2026-09-14, see this module's
    docstring). `extra="forbid"` means an unknown field in module.yaml is a validation error, not a
    silently-ignored typo."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")  # used as k8s namespace + PVC label value
    displayName: str
    icon: str = "puzzle"
    navPath: str
    proxyTo: str
    healthCheck: str = "/healthz"
    # Other module IDs only — things that can appear in modules-enabled/,
    # i.e. something `platform module install` itself generated an
    # Application for. NOT core services (auth/Keycloak, catalog-service,
    # gateway, ...): those are always present (bootstrap/install.sh's Phase
    # 0), never installed this way, so no Application will ever exist for
    # them — module-lifecycle-plan.md item 6's dependency check
    # (platform_cli/module.py's `install`, gateway's
    # GET /modules/check-requirements) takes this literally and would
    # permanently fail for a `requires` entry that isn't a real module id.
    # See src/modules/_template/module.yaml's own comment on this same
    # point, and this branch's plan, decision 5.
    requires: list[str] = Field(default_factory=list)
    optional: bool = True
    namespace: str | None = None
    placement: Placement | None = None
    # 2026-09-14 (feature/module-external-chart) — None (the default) is every module written
    # before this branch: its chart lives at src/charts/<id>/ in this repo, exactly as before.
    # Set, it points render_application_manifest() at this chart's own upstream Helm repo instead —
    # see this module's own docstring and ExternalChart's.
    externalChart: ExternalChart | None = None
    # Free-form extra Helm values, merged alongside `placement` into the generated Application's
    # `spec.source.helm.values` block. Every module gets this for free, not just external-chart
    # ones — but a LOCAL chart's real configuration normally lives in its own
    # src/charts/<id>/values.yaml, so this stays {} for hello-module/_template and every module
    # like them. An EXTERNAL-chart module has no such file of its own, so this is where its real
    # configuration (e.g. Trino's `catalogs:`) has to live instead.
    values: dict = Field(default_factory=dict)

    @property
    def resolved_namespace(self) -> str:
        return self.namespace or self.id


def load_module_manifest(path: Path) -> ModuleManifest:
    """Load and validate `path` (a module.yaml). Raises ManifestError — never a raw
    yaml.YAMLError or pydantic.ValidationError — so callers can just print str(exc)."""
    if not path.is_file():
        raise ManifestError(f"no module.yaml found at {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{path} must be a YAML mapping at the top level, got {type(raw).__name__}")
    try:
        return ModuleManifest.model_validate(raw)
    except ValidationError as exc:
        # exc's own str() is already a readable, per-field message (pydantic v2's default
        # formatting) — no need to re-derive one field at a time here.
        raise ManifestError(f"{path} failed validation:\n{exc}") from exc


def _values_block(manifest: ModuleManifest) -> str:
    """The `spec.source.helm.values` YAML text: `placement` (decision 3, module-lifecycle-plan.md —
    empty when the module declares none, so the chart's _helpers.tpl renders no affinity/tolerations
    at all) merged with any free-form `manifest.values` (2026-09-14, this module's own docstring)
    into one document. `sort_keys=False` preserves each dict's own field order (e.g. a Toleration's
    key/operator/value/effect) rather than alphabetizing it — matches this function's pre-2026-09-14
    hand-rolled output, which existing tests already assert against."""
    placement: dict = {}
    if manifest.placement is not None:
        placement = {"role": manifest.placement.role}
        placement["tolerations"] = [
            {
                key: value
                for key, value in (
                    ("key", t.key),
                    ("operator", t.operator),
                    ("value", t.value),
                    ("effect", t.effect),
                )
                if value is not None
            }
            for t in manifest.placement.tolerations
        ]
    combined = {"placement": placement, **manifest.values}
    return yaml.safe_dump(combined, sort_keys=False)


def render_application_manifest(
    manifest: ModuleManifest, *, repo_url: str, chart_path: str | None
) -> str:
    """Renders the complete Argo CD `Application` YAML `platform module install` writes to
    `src/modules-enabled/<id>.yaml`. `repo_url` comes from `repo.discover_repo_url()` (git remote
    get-url origin) — the one place this improves on the hand-authored "self-referencing apps"
    convention (argocd/README.md), which hardcodes repoURL with a "forkers: edit this" comment;
    generated content doesn't need that caveat, it can just ask git. `targetRevision` still
    hardcodes `dev` for a LOCAL chart, matching every other self-referencing Application in this repo.

    2026-09-14 (feature/module-external-chart): `chart_path` is now `None` for a module whose
    `manifest.externalChart` is set — see this module's own docstring. In that case `repo_url` is
    still accepted (module.py's install() always discovers/overrides it) but simply unused here; the
    `spec.source` block points at `externalChart`'s own repo instead, the same shape
    `apps/optional/storage-seaweedfs/seaweedfs.yaml` already writes by hand. Whether `chart_path` is
    `None` is entirely the caller's call — install()'s own local-chart-directory check (module.py)
    decides which shape a given module actually gets; this function just renders whichever one it's
    told."""
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    values_block = _values_block(manifest)
    indented_values = "\n".join(
        f"        {line}" if line else "" for line in values_block.splitlines()
    )
    if manifest.externalChart is not None:
        source_block = f"""    repoURL: {manifest.externalChart.repoURL}
    chart: {manifest.externalChart.chart}
    targetRevision: {manifest.externalChart.version}
    helm:
      values: |
{indented_values}"""
        chart_comment = (
            f"# Chart: {manifest.externalChart.chart} {manifest.externalChart.version} from "
            f"{manifest.externalChart.repoURL} (not this repo — see module.yaml's own externalChart)."
        )
    else:
        source_block = f"""    repoURL: {repo_url}
    targetRevision: dev
    path: {chart_path}
    helm:
      values: |
{indented_values}"""
        chart_comment = (
            f"# see src/charts/{manifest.id}/templates/*.yaml for this module's own PVCs, if it has any."
        )
    return f"""\
# GENERATED by `platform module install {manifest.id}` at {generated_at} — do not hand-edit.
# Source descriptor: src/modules/{manifest.id}/module.yaml. To change this module's placement,
# namespace, or chart, edit that file and re-run `platform module install {manifest.id}`
# (it's safe to run again — it overwrites this file in place and commits the diff).
#
# Picked up by modules-root (src/core/argocd/apps/core/modules-root.yaml), which watches
# src/modules-enabled/ the same way root-app.yaml watches apps/core/ — see that file's header
# comment for the three-level app-of-apps structure. `syncPolicy.automated.prune: true` plus the
# finalizer below mean `platform module uninstall {manifest.id}` (which just deletes this file)
# is enough to tear the whole module back down — except any PersistentVolumeClaim the chart marks
# `argocd.argoproj.io/sync-options: Delete=false`, which survives on purpose (ARCHITECTURE.md §3;
{chart_comment}
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {manifest.id}
  namespace: argocd
  labels:
    platform.io/tier: module
  annotations:
    platform.io/display-name: {json.dumps(manifest.displayName)}
    platform.io/icon: {json.dumps(manifest.icon)}
    platform.io/nav-path: {json.dumps(manifest.navPath)}
    platform.io/proxy-to: {json.dumps(manifest.proxyTo)}
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
{source_block}
  destination:
    server: https://kubernetes.default.svc
    namespace: {manifest.resolved_namespace}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
"""
