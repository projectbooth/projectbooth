"""Direct unit coverage for `platform_cli.module_index.build_static_module_index()` —
ui-shell-plan.md item 6. Pure `tmp_path`-built fixture module.yaml files, no CLI/Typer machinery,
matching `test_manifest.py`'s own style: this proves the scan/validate/sort logic in isolation,
`test_cli.py` (if it grows a `build-index` case) would cover the thin Typer wrapper on top.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from platform_cli.manifest import ManifestError
from platform_cli.module_index import build_static_module_index

_BASE_FIELDS = {
    "displayName": "Some Module",
    "navPath": "/some-module",
    "proxyTo": "http://some-module.some-module.svc:80",
}


def _write_module(modules_dir: Path, name: str, **overrides) -> None:
    fields = {"id": name, **_BASE_FIELDS, **overrides}
    module_dir = modules_dir / name
    module_dir.mkdir(parents=True)
    (module_dir / "module.yaml").write_text(yaml.safe_dump(fields))


def test_scans_every_module_directory(tmp_path):
    _write_module(tmp_path, "hello-module", displayName="Hello Module", navPath="/hello")
    _write_module(tmp_path, "notebook-jupyterhub", displayName="Notebooks", navPath="/notebooks")

    entries = build_static_module_index(tmp_path)

    ids = [e["id"] for e in entries]
    assert ids == ["hello-module", "notebook-jupyterhub"]


def test_template_directory_is_always_skipped_even_if_it_would_otherwise_validate(tmp_path):
    # A _template with a genuinely valid module.yaml (unlike the real
    # src/modules/_template/, whose placeholder tokens would fail validation
    # anyway) still must never appear in the catalog — skipped by name, not
    # by accident of its contents happening to be invalid.
    _write_module(tmp_path, "_template", displayName="Template", navPath="/template")
    _write_module(tmp_path, "hello-module")

    entries = build_static_module_index(tmp_path)

    assert [e["id"] for e in entries] == ["hello-module"]


def test_malformed_real_module_yaml_raises_rather_than_being_silently_dropped(tmp_path):
    _write_module(tmp_path, "hello-module")
    broken_dir = tmp_path / "broken-module"
    broken_dir.mkdir()
    (broken_dir / "module.yaml").write_text("not: valid\nfor_the: schema\nwrong_field: true\n")

    with pytest.raises(ManifestError):
        build_static_module_index(tmp_path)


def test_empty_modules_dir_returns_empty_list(tmp_path):
    assert build_static_module_index(tmp_path) == []


def test_output_is_sorted_by_id_regardless_of_directory_iteration_order(tmp_path):
    _write_module(tmp_path, "zzz-module")
    _write_module(tmp_path, "aaa-module")
    _write_module(tmp_path, "mmm-module")

    entries = build_static_module_index(tmp_path)

    assert [e["id"] for e in entries] == ["aaa-module", "mmm-module", "zzz-module"]


def test_requires_and_optional_round_trip_their_real_values(tmp_path):
    _write_module(tmp_path, "notebook-jupyterhub", requires=["auth", "catalog"], optional=False)
    _write_module(tmp_path, "hello-module")  # requires: [] / optional: True, schema defaults

    entries = {e["id"]: e for e in build_static_module_index(tmp_path)}

    assert entries["notebook-jupyterhub"]["requires"] == ["auth", "catalog"]
    assert entries["notebook-jupyterhub"]["optional"] is False
    assert entries["hello-module"]["requires"] == []
    assert entries["hello-module"]["optional"] is True


def test_display_icon_and_nav_path_are_read_from_the_module_yaml(tmp_path):
    _write_module(tmp_path, "hello-module", displayName="Hello Module", icon="wave", navPath="/hello")

    entries = build_static_module_index(tmp_path)

    assert entries[0]["displayName"] == "Hello Module"
    assert entries[0]["icon"] == "wave"
    assert entries[0]["navPath"] == "/hello"


def test_non_directory_entries_in_modules_dir_are_ignored(tmp_path):
    (tmp_path / "README.md").write_text("# modules\n")
    _write_module(tmp_path, "hello-module")

    entries = build_static_module_index(tmp_path)

    assert [e["id"] for e in entries] == ["hello-module"]
