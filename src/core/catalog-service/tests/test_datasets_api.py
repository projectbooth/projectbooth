"""Integration test, real DB (see conftest.py). Datasets stand in for
pipelines/models too — they all go through the same app/crud.py, so one
API-level test suite per entity type would mostly be re-testing crud.py
three more times; datasets gets the full walk, the others get the plain-CRUD
smoke test in test_pipelines_and_models_smoke below.
"""
from __future__ import annotations


def _create_workspace(client, name: str) -> str:
    resp = client.post("/workspaces", json={"name": name, "display_name": name.title()})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_create_and_read_own_dataset(client):
    resp = client.post(
        "/datasets",
        headers={"X-Workspace": "personal", "X-User": "alice"},
        json={"name": "reddit-sentiment", "visibility": "private", "description": "raw pulls"},
    )
    assert resp.status_code == 201, resp.text
    dataset_id = resp.json()["id"]

    resp = client.get(f"/datasets/{dataset_id}", headers={"X-Workspace": "personal", "X-User": "alice"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "reddit-sentiment"


def test_private_dataset_hidden_from_other_workspace_public_is_not(client):
    other_id = _create_workspace(client, "other")

    private_resp = client.post(
        "/datasets",
        headers={"X-Workspace": "personal", "X-User": "alice"},
        json={"name": "private-one", "visibility": "private"},
    )
    public_resp = client.post(
        "/datasets",
        headers={"X-Workspace": "personal", "X-User": "alice"},
        json={"name": "public-one", "visibility": "public"},
    )
    private_id = private_resp.json()["id"]
    public_id = public_resp.json()["id"]

    # "other" workspace: private one is a 404 (not a 403 — see crud.py's
    # get_visible_or_404 docstring), public one is readable.
    resp = client.get(f"/datasets/{private_id}", headers={"X-Workspace": "other"})
    assert resp.status_code == 404

    resp = client.get(f"/datasets/{public_id}", headers={"X-Workspace": "other"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "public-one"

    # And "other" can't write it even though they can read it.
    resp = client.patch(
        f"/datasets/{public_id}", headers={"X-Workspace": "other"}, json={"description": "hijacked"}
    )
    assert resp.status_code == 403

    assert other_id  # workspace really was created, not just assumed


def test_duplicate_name_in_same_workspace_conflicts(client):
    body = {"name": "dupe", "visibility": "private"}
    first = client.post("/datasets", headers={"X-Workspace": "personal"}, json=body)
    assert first.status_code == 201

    second = client.post("/datasets", headers={"X-Workspace": "personal"}, json=body)
    assert second.status_code == 409


def test_pipelines_and_models_smoke(client):
    for path, body in (
        ("/pipelines", {"name": "nfl-weekly-pull", "visibility": "workspace"}),
        ("/models", {"name": "march-madness-bracket", "visibility": "private", "framework": "sklearn"}),
    ):
        created = client.post(path, headers={"X-Workspace": "personal"}, json=body)
        assert created.status_code == 201, created.text
        entity_id = created.json()["id"]

        listed = client.get(path, headers={"X-Workspace": "personal"})
        assert any(row["id"] == entity_id for row in listed.json())

        deleted = client.delete(f"{path}/{entity_id}", headers={"X-Workspace": "personal"})
        assert deleted.status_code == 204


def test_function_publish_bumps_version_and_promote_makes_it_public(client):
    created = client.post(
        "/functions",
        headers={"X-Workspace": "personal"},
        json={"name": "clean_text", "visibility": "private"},
    )
    function_id = created.json()["id"]
    assert created.json()["current_version"] == 0

    published = client.post(
        f"/functions/{function_id}/publish",
        headers={"X-Workspace": "personal"},
        json={
            "signature": "clean_text(s: str) -> str",
            "docstring": "Strips markdown and emoji.",
            "module_path": "platform_sdk.examples.clean_text",
        },
    )
    assert published.status_code == 201, published.text
    assert published.json()["version"] == 1

    refreshed = client.get(f"/functions/{function_id}", headers={"X-Workspace": "personal"})
    assert refreshed.json()["current_version"] == 1

    promoted = client.post(f"/functions/{function_id}/promote", headers={"X-Workspace": "personal"})
    assert promoted.status_code == 200
    assert promoted.json()["visibility"] == "public"

    # Now visible cross-workspace without ever joining "personal".
    other = client.post("/workspaces", json={"name": "another", "display_name": "Another"})
    assert other.status_code == 201
    cross = client.get(f"/functions/{function_id}", headers={"X-Workspace": "another"})
    assert cross.status_code == 200


def test_deleting_a_published_function_cascades_its_versions(client):
    # Regression test for a real bug found live 2026-09-03
    # (platform-function-promote branch): deleting a Function with at least
    # one published FunctionVersion used to 500 (an unnamed FK with no ON
    # DELETE clause rejected the delete outright, uncaught by crud.py) — see
    # app/models.py's FunctionVersion comment and migrations/versions/0002_*.
    created = client.post(
        "/functions",
        headers={"X-Workspace": "personal"},
        json={"name": "to-be-deleted", "visibility": "private"},
    )
    function_id = created.json()["id"]

    published = client.post(
        f"/functions/{function_id}/publish",
        headers={"X-Workspace": "personal"},
        json={"signature": "f() -> None", "module_path": "pkg.mod.f"},
    )
    assert published.status_code == 201, published.text

    deleted = client.delete(f"/functions/{function_id}", headers={"X-Workspace": "personal"})
    assert deleted.status_code == 204, deleted.text

    missing = client.get(f"/functions/{function_id}", headers={"X-Workspace": "personal"})
    assert missing.status_code == 404


def test_viewer_role_reads_but_cannot_create_or_write(client):
    # Set up as owner (the default — no X-Role needed) so there's something
    # for the viewer to read and to fail to write.
    created = client.post(
        "/datasets",
        headers={"X-Workspace": "personal", "X-User": "alice"},
        json={"name": "viewer-target", "visibility": "workspace"},
    )
    assert created.status_code == 201, created.text
    dataset_id = created.json()["id"]

    viewer_headers = {"X-Workspace": "personal", "X-User": "bob", "X-Role": "viewer"}

    # Read still works — role only gates writes.
    resp = client.get(f"/datasets/{dataset_id}", headers=viewer_headers)
    assert resp.status_code == 200

    # Create is blocked.
    resp = client.post(
        "/datasets", headers=viewer_headers, json={"name": "should-not-exist", "visibility": "private"}
    )
    assert resp.status_code == 403

    # Write to an existing, otherwise-writable (same workspace) entity is blocked.
    resp = client.patch(f"/datasets/{dataset_id}", headers=viewer_headers, json={"description": "nope"})
    assert resp.status_code == 403

    # Delete is blocked too.
    resp = client.delete(f"/datasets/{dataset_id}", headers=viewer_headers)
    assert resp.status_code == 403


def test_me_reflects_headers_including_default_role(client):
    # No X-Role sent — should reflect DEFAULT_ROLE (owner), not error or 422.
    resp = client.get("/me", headers={"X-Workspace": "personal", "X-User": "alice"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "alice"
    assert body["workspace_name"] == "personal"
    assert body["role"] == "owner"

    resp = client.get(
        "/me", headers={"X-Workspace": "personal", "X-User": "bob", "X-Role": "Viewer"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "viewer"  # case-insensitive header value normalized


def test_unknown_role_header_rejected(client):
    resp = client.get("/me", headers={"X-Role": "superadmin"})
    assert resp.status_code == 400
