"""Pure logic tests for app/visibility.py — no DB needed, since can_read/
can_write/can_create only look at plain attributes. The two things worth
locking down with tests before anything else builds on them:
ARCHITECTURE.md §4's "public means readable by everyone else, not writable"
asymmetry, and (added alongside role enforcement) "a viewer never writes,
full stop" — including cases that would otherwise pass (their own workspace,
their own creation).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models import Visibility
from app.visibility import Principal, Role, can_create, can_read, can_write

WORKSPACE_A = uuid.uuid4()
WORKSPACE_B = uuid.uuid4()


@dataclass
class FakeEntity:
    workspace_id: uuid.UUID
    visibility: Visibility
    created_by: str | None


def _principal(workspace_id: uuid.UUID, user_id: str = "alice", role: Role = Role.OWNER) -> Principal:
    return Principal(
        workspace_id=workspace_id, workspace_name="doesn't matter here", user_id=user_id, role=role
    )


def test_public_readable_by_anyone_but_not_writable_outside_owning_workspace():
    entity = FakeEntity(workspace_id=WORKSPACE_A, visibility=Visibility.PUBLIC, created_by="alice")
    outsider = _principal(WORKSPACE_B)

    assert can_read(outsider, entity) is True
    assert can_write(outsider, entity) is False


def test_workspace_visible_to_any_member_not_just_creator():
    entity = FakeEntity(workspace_id=WORKSPACE_A, visibility=Visibility.WORKSPACE, created_by="alice")
    teammate = _principal(WORKSPACE_A, user_id="bob")

    assert can_read(teammate, entity) is True
    assert can_write(teammate, entity) is True


def test_private_visible_only_to_its_creator_even_within_the_same_workspace():
    entity = FakeEntity(workspace_id=WORKSPACE_A, visibility=Visibility.PRIVATE, created_by="alice")
    teammate = _principal(WORKSPACE_A, user_id="bob")
    creator = _principal(WORKSPACE_A, user_id="alice")

    assert can_read(teammate, entity) is False
    assert can_read(creator, entity) is True
    assert can_write(teammate, entity) is False
    assert can_write(creator, entity) is True


def test_nothing_visible_or_writable_from_an_unrelated_workspace_unless_public():
    outsider = _principal(WORKSPACE_B)
    for visibility in (Visibility.PRIVATE, Visibility.WORKSPACE):
        entity = FakeEntity(workspace_id=WORKSPACE_A, visibility=visibility, created_by="alice")
        assert can_read(outsider, entity) is False
        assert can_write(outsider, entity) is False


def test_viewer_never_writes_even_their_own_creation_in_their_own_workspace():
    viewer = _principal(WORKSPACE_A, user_id="alice", role=Role.VIEWER)
    for visibility in (Visibility.PRIVATE, Visibility.WORKSPACE, Visibility.PUBLIC):
        entity = FakeEntity(workspace_id=WORKSPACE_A, visibility=visibility, created_by="alice")
        # Read still follows the ordinary visibility rule...
        assert can_read(viewer, entity) is True
        # ...but write is blocked purely on role, before any of that logic runs.
        assert can_write(viewer, entity) is False


def test_owner_and_editor_write_identically_today():
    entity = FakeEntity(workspace_id=WORKSPACE_A, visibility=Visibility.WORKSPACE, created_by="alice")
    owner = _principal(WORKSPACE_A, user_id="bob", role=Role.OWNER)
    editor = _principal(WORKSPACE_A, user_id="bob", role=Role.EDITOR)

    assert can_write(owner, entity) is True
    assert can_write(editor, entity) is True


def test_can_create_gated_on_role_alone():
    assert can_create(_principal(WORKSPACE_A, role=Role.OWNER)) is True
    assert can_create(_principal(WORKSPACE_A, role=Role.EDITOR)) is True
    assert can_create(_principal(WORKSPACE_A, role=Role.VIEWER)) is False
