"""Static module-catalog index — ui-shell-plan.md item 6 (Add-ons page's static release-time
index; feature/gateway-module-catalog branch). ARCHITECTURE.md §3: "platform-gateway reads a
static module index built from every modules/*/module.yaml at release time... so it can list
modules that aren't installed yet." This is the build half of that — `build_static_module_index()`
below is the pure scan/validate logic, wired up as `platform module build-index <out-path>`
(module.py's new command) so `build-and-push-gateway` (ci.yml) can call it as a plain CLI
invocation without importing this package's internals directly. Gateway's own
`app/module_index.py` (a sibling module in the gateway package, not this one) is the read half —
it loads the JSON this produces and overlays it with live Argo CD status; nothing here ever serves
it, only produces it.

Deliberately its own module, not folded into manifest.py or module.py: `build_static_module_index()`
is pure (a `Path` in, a list of dicts out — no Typer, no git, no network) so it's unit-testable with
plain `tmp_path` fixtures, the same reasoning manifest.py's own functions already follow.
"""
from __future__ import annotations

from pathlib import Path

from platform_cli.manifest import load_module_manifest

# src/modules/_template/ is the `platform module scaffold` skeleton (its own module.yaml has
# placeholder tokens like __MODULE_ID__ that fail ModuleManifest's `id` pattern) — skipped by
# directory name, deliberately never by catching the ManifestError it would raise: that would also
# silently swallow a genuinely broken REAL module.yaml sitting right next to it, which is exactly
# the failure mode this function needs to surface loudly (see its own docstring below).
_TEMPLATE_DIR_NAME = "_template"


def build_static_module_index(modules_dir: Path) -> list[dict]:
    """Scans `modules_dir/*/module.yaml` (skipping `_template`) and validates each via
    `load_module_manifest` — a malformed real module.yaml raises `ManifestError` here exactly like
    it does at `install`/`scaffold` time, not silently dropped from the catalog. Returns one dict
    per module, sorted by `id` for deterministic JSON output (stable diffs across regenerations):
    `{id, displayName, icon, navPath, requires, optional}` — the same field set ARCHITECTURE.md
    §3's own module.yaml example shows, minus `proxyTo`/`healthCheck`/`namespace`/`placement`,
    which are deploy-time concerns a catalog card never needs. Field names stay module.yaml's own
    camelCase, not gateway's snake_case API convention — this is a source-of-truth artifact, not a
    response shape; gateway's own `app/module_index.py` does that presentation-layer translation
    when it builds `GET /modules/catalog`.
    """
    entries = []
    for module_dir in modules_dir.iterdir():
        if not module_dir.is_dir() or module_dir.name == _TEMPLATE_DIR_NAME:
            continue
        manifest = load_module_manifest(module_dir / "module.yaml")
        entries.append(
            {
                "id": manifest.id,
                "displayName": manifest.displayName,
                "icon": manifest.icon,
                "navPath": manifest.navPath,
                "requires": manifest.requires,
                "optional": manifest.optional,
            }
        )
    entries.sort(key=lambda entry: entry["id"])
    return entries
