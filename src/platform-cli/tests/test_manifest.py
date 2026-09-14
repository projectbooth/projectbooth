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

from platform_cli.manifest import ModuleManifest, render_application_manifest


def _manifest(**overrides) -> ModuleManifest:
    fields = {
        "id": "hello-module",
        "displayName": "Hello Module",
        "navPath": "/hello",
        "proxyTo": "http://hello-module.hello-module.svc:80",
    }
    fields.update(overrides)
    return ModuleManifest(**fields)


def _render(manifest: ModuleManifest) -> str:
    return render_application_manifest(
        manifest, repo_url="https://example.invalid/repo.git", chart_path="src/charts/hello-module"
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


# --- externalChart (feature/module-external-chart, 2026-09-14, ARCHITECTURE.md §11 Phase 3) -----
#
# See manifest.py's own module docstring for the full "why" — a module's chart no longer has to
# live in this repo. These tests cover render_application_manifest() directly (unit-level, like
# every other test in this file); test_module.py's install()-level tests cover the filesystem-based
# local-vs-external decision (chart_dir existence, the ambiguous-both-sources guard) that lives in
# module.py, not here.


def test_local_chart_source_is_unchanged_by_default():
    # Every module written before this branch (hello-module, _template) has no externalChart at
    # all — this proves that default renders exactly as it always did: a path into this repo, no
    # `chart:`/external `repoURL` anywhere in the output.
    manifest = _manifest()

    parsed = yaml.safe_load(_render(manifest))
    source = parsed["spec"]["source"]
    assert source["path"] == "src/charts/hello-module"
    assert source["repoURL"] == "https://example.invalid/repo.git"
    assert source["targetRevision"] == "dev"
    assert "chart" not in source


def test_external_chart_source_replaces_the_local_path():
    manifest = _manifest(
        externalChart={
            "repoURL": "https://trinodb.github.io/charts",
            "chart": "trino",
            "version": "1.42.2",
        }
    )

    rendered = render_application_manifest(
        manifest, repo_url="https://example.invalid/repo.git", chart_path=None
    )
    parsed = yaml.safe_load(rendered)
    source = parsed["spec"]["source"]
    assert source["repoURL"] == "https://trinodb.github.io/charts"
    assert source["chart"] == "trino"
    assert source["targetRevision"] == "1.42.2"
    assert "path" not in source
    # The module's OWN repo_url (this monorepo) must not leak into an external module's source —
    # it's simply not relevant to where the chart actually comes from.
    assert "example.invalid" not in rendered


def test_values_merge_alongside_placement_in_the_rendered_helm_values():
    # An external-chart module has no src/charts/<id>/values.yaml of its own — its real
    # configuration has to come from manifest.values instead. Proves it lands in the same
    # spec.source.helm.values block placement already used, not a separate/lost location.
    manifest = _manifest(
        externalChart={"repoURL": "https://example.invalid/charts", "chart": "thing", "version": "1.0.0"},
        values={"catalogs": {"iceberg": "connector.name=iceberg\n"}},
    )

    rendered = render_application_manifest(
        manifest, repo_url="https://example.invalid/repo.git", chart_path=None
    )
    parsed = yaml.safe_load(rendered)
    values = yaml.safe_load(parsed["spec"]["source"]["helm"]["values"])
    assert values["placement"] == {}
    assert values["catalogs"]["iceberg"] == "connector.name=iceberg\n"


def test_placement_still_renders_the_same_shape_after_the_values_block_refactor():
    # Regression coverage for _values_block() replacing the old hand-rolled
    # _placement_values_block(): a Toleration's field order (key/operator/value/effect) must
    # survive yaml.safe_dump(sort_keys=False), not get alphabetized.
    manifest = _manifest(
        placement={
            "role": "compute",
            "tolerations": [
                {"key": "platform.io/role", "operator": "Equal", "value": "compute", "effect": "NoSchedule"}
            ],
        }
    )

    rendered = _render(manifest)
    parsed = yaml.safe_load(rendered)
    values = yaml.safe_load(parsed["spec"]["source"]["helm"]["values"])
    assert values["placement"]["role"] == "compute"
    assert values["placement"]["tolerations"] == [
        {"key": "platform.io/role", "operator": "Equal", "value": "compute", "effect": "NoSchedule"}
    ]
    # Field order within each toleration mapping is preserved, not alphabetized — asserted via the
    # raw text rather than a parsed dict (dict equality above already ignores key order).
    values_text = parsed["spec"]["source"]["helm"]["values"]
    key_pos = values_text.index("key:")
    operator_pos = values_text.index("operator:")
    value_pos = values_text.index("value:")
    effect_pos = values_text.index("effect:")
    assert key_pos < operator_pos < value_pos < effect_pos
