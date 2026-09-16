"""Direct unit coverage for `platform_cli.manifest.render_application_manifest()` — specifically
the `platform.io/*` annotation propagation added feature/gateway-module-registry (2026-09-08,
ui-shell-plan.md item 4). `tests/test_module.py` already exercises this indirectly through the full
`platform module install` CLI flow (a real git repo, real scaffold templates) — this file instead
calls `render_application_manifest()` directly against a hand-built `ModuleManifest`, specifically
to prove the `json.dumps()`-based YAML escaping actually works for a `displayName` a real operator
might plausibly write (a colon, a quote), which the CLI-flow test's fixed "hello" module name can't
exercise.
"""
from __future__ import annotations

import yaml

from platform_cli.manifest import ExternalChart, ModuleManifest, render_application_manifest


def _manifest(**overrides) -> ModuleManifest:
    fields = {
        "id": "hello-module",
        "displayName": "Hello Module",
        "navPath": "/hello",
        "proxyTo": "http://hello-module.hello-module.svc:80",
    }
    fields.update(overrides)
    return ModuleManifest(**fields)


def _render(manifest: ModuleManifest, *, setup_dir: str | None = None) -> str:
    return render_application_manifest(
        manifest,
        repo_url="https://example.invalid/repo.git",
        chart_path="src/charts/hello-module",
        setup_dir=setup_dir,
    )


def test_annotations_carry_display_name_icon_and_nav_path():
    manifest = _manifest(displayName="Hello Module", icon="wave", navPath="/hello")

    parsed = yaml.safe_load(_render(manifest))
    annotations = parsed["metadata"]["annotations"]
    assert annotations["platform.io/display-name"] == "Hello Module"
    assert annotations["platform.io/icon"] == "wave"
    assert annotations["platform.io/nav-path"] == "/hello"


def test_icon_defaults_to_puzzle_when_module_yaml_omits_it():
    manifest = _manifest()  # icon not overridden -> ModuleManifest's own schema default

    parsed = yaml.safe_load(_render(manifest))
    assert parsed["metadata"]["annotations"]["platform.io/icon"] == "puzzle"


def test_special_characters_in_display_name_do_not_corrupt_the_yaml():
    # A colon and a double quote are both YAML-significant — naive f-string interpolation would
    # produce invalid (or silently wrong) YAML here. json.dumps() is what
    # render_application_manifest actually uses to guard against exactly this.
    manifest = _manifest(displayName='Ops: "The Dashboard"')

    rendered = _render(manifest)
    parsed = yaml.safe_load(rendered)  # raises yaml.YAMLError if the generated document is malformed
    assert parsed["metadata"]["annotations"]["platform.io/display-name"] == 'Ops: "The Dashboard"'


def test_proxy_to_is_propagated_into_annotations():
    # item 8 (feature/module-proxy, 2026-09-10) — see manifest.py's own module docstring. Was
    # deliberately out of scope for item 4 (test_proxy_to_is_not_propagated_into_annotations,
    # this test's former self); item 8's own pass over this function adds it.
    manifest = _manifest(proxyTo="http://hello-module.hello-module.svc:80")

    parsed = yaml.safe_load(_render(manifest))
    assert parsed["metadata"]["annotations"]["platform.io/proxy-to"] == "http://hello-module.hello-module.svc:80"


# 2026-09-16 (Trino live-verification) — see manifest.py's own module docstring for the full
# reasoning: a module can carry a one-time, idempotent in-cluster setup Job (Trino's Iceberg
# JDBC-catalog bookkeeping tables), wired in as a SECOND Argo CD source via `setup_dir`.


def test_setup_dir_omitted_renders_the_original_singular_source():
    # None (the default) must still render exactly the pre-2026-09-16 shape: no test written before
    # this branch should ever need to change because of it.
    manifest = _manifest()

    parsed = yaml.safe_load(_render(manifest, setup_dir=None))
    spec = parsed["spec"]
    assert "source" in spec
    assert "sources" not in spec
    assert spec["source"]["path"] == "src/charts/hello-module"


def test_setup_dir_set_renders_a_two_item_sources_list_for_a_local_chart():
    manifest = _manifest()

    parsed = yaml.safe_load(_render(manifest, setup_dir="src/modules/hello-module/setup"))
    spec = parsed["spec"]
    assert "source" not in spec
    assert "sources" in spec
    sources = spec["sources"]
    assert len(sources) == 2

    main_source, setup_source = sources
    assert main_source["path"] == "src/charts/hello-module"
    assert main_source["repoURL"] == "https://example.invalid/repo.git"
    assert main_source["targetRevision"] == "dev"
    assert "helm" in main_source  # the chart's real values still travel with it, unchanged

    assert setup_source == {
        "repoURL": "https://example.invalid/repo.git",
        "targetRevision": "dev",
        "path": "src/modules/hello-module/setup",
    }
    assert "helm" not in setup_source  # a plain directory of raw manifests, not another chart


def test_setup_dir_set_renders_a_two_item_sources_list_for_an_external_chart():
    # The setup source is orthogonal to which shape the main chart source takes — an external-chart
    # module (2026-09-14) needs its own bookkeeping Job wired in exactly the same way Trino's is.
    manifest = _manifest(
        externalChart=ExternalChart(
            repoURL="https://trinodb.github.io/charts", chart="trino", version="1.42.2"
        )
    )

    parsed = yaml.safe_load(_render(manifest, setup_dir="src/modules/hello-module/setup"))
    sources = parsed["spec"]["sources"]
    assert len(sources) == 2

    main_source, setup_source = sources
    assert main_source["repoURL"] == "https://trinodb.github.io/charts"
    assert main_source["chart"] == "trino"
    assert main_source["targetRevision"] == "1.42.2"
    assert "helm" in main_source

    assert setup_source == {
        "repoURL": "https://example.invalid/repo.git",
        "targetRevision": "dev",
        "path": "src/modules/hello-module/setup",
    }


def test_setup_dir_is_not_confused_with_the_main_chart_path():
    # Regression guard: the setup source's `path` must be setup_dir, not chart_path, even though
    # both are plain strings passed into the same function.
    manifest = _manifest()

    parsed = yaml.safe_load(_render(manifest, setup_dir="src/modules/hello-module/setup"))
    main_source, setup_source = parsed["spec"]["sources"]
    assert main_source["path"] != setup_source["path"]
    assert setup_source["path"] == "src/modules/hello-module/setup"
