"""The one place the read/write rule from ARCHITECTURE.md §4 is implemented
— every router calls into this rather than re-deriving it, so "who can see
what" (and now "who can change it") has exactly one definition in this
codebase.

Rule (matches models.py's Visibility docstring):
  read   — public: anyone. workspace: any member of the owning workspace.
           private: only the entity's own creator.
  write  — ARCHITECTURE.md §4: "Editing rights stay with the owning workspace
           regardless of visibility: 'public' means readable by everyone
           else, not writable." So write is read's rule minus the "public
           means anyone" case — always gated on owning-workspace membership —
           AND (added alongside role enforcement) the principal's role: a
           viewer never writes, full stop, even to something they created.
  create — same role gate as write, minus the entity-ownership checks (there
           is no entity yet — a create always lands in the principal's own
           workspace, see app/crud.py).

Role (owner/editor/viewer) is Keycloak-group territory, not a column in this
service's own database — see app/models.py's Workspace docstring and
src/core/auth/realm-platform.yaml's `/workspaces/<name>/<role>` group path.
This module doesn't decide *membership* (who's in which group); it decides
what a given role is allowed to do once app/deps.py has resolved one from the
request, same relationship this module already has with workspace_id/user_id.
Today owner and editor are equivalent for catalog data — ARCHITECTURE.md's
role descriptions give owner extra workspace-management power (manage
membership, delete the workspace) that no endpoint here exercises yet; add a
distinction only once something actually needs one, not preemptively.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_

from app.models import Visibility


class Role(enum.StrEnum):
    """Matches the three realm roles seeded by realm-platform.yaml exactly
    (owner/editor/viewer, lowercase) — not a Postgres enum like Visibility,
    since this is never stored in this service's own database; see the
    module docstring above."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Principal:
    """The requester, as resolved by app/deps.py. See that module's
    docstring for how this is currently a placeholder, not real auth —
    role included, same caveat as workspace_id/user_id."""

    workspace_id: UUID
    workspace_name: str
    user_id: str
    # Defaulted (not just present-with-no-default) so every existing
    # Principal(...) call site and test — written before role existed —
    # keeps compiling and behaving exactly as it did, per DEFAULT_ROLE's
    # reasoning in app/deps.py.
    role: Role = Role.OWNER


class _VisibleEntity(Protocol):
    workspace_id: UUID
    visibility: Visibility
    created_by: str | None


def can_read(principal: Principal, entity: _VisibleEntity) -> bool:
    if entity.visibility == Visibility.PUBLIC:
        return True
    if entity.workspace_id != principal.workspace_id:
        return False
    if entity.visibility == Visibility.WORKSPACE:
        return True
    # PRIVATE: only the creator, even within the owning workspace.
    return entity.created_by == principal.user_id


def can_write(principal: Principal, entity: _VisibleEntity) -> bool:
    # Role gate first, before any of the ownership/visibility logic below —
    # a viewer doesn't write, period, regardless of whether they'd otherwise
    # pass every other check (e.g. they created the thing themselves).
    if principal.role == Role.VIEWER:
        return False
    # Same "must be in the owning workspace" gate as read's workspace/private
    # cases — write never gets the "public means anyone" carve-out.
    if entity.workspace_id != principal.workspace_id:
        return False
    if entity.visibility == Visibility.PRIVATE:
        return entity.created_by == principal.user_id
    return True


def can_create(principal: Principal) -> bool:
    """The create-time equivalent of can_write, for the one case can_write
    can't cover: there's no entity yet to check workspace/visibility/
    created_by against (app/crud.py's create() always stamps the new row
    with the principal's own workspace_id and user_id, so those checks would
    be tautologies anyway). Just the role gate."""
    return principal.role != Role.VIEWER


def read_filter(principal: Principal, model) -> ColumnElement[bool]:
    """The same rule as can_read, expressed as a SQLAlchemy filter for list
    endpoints — so listing datasets/functions/pipelines/models doesn't mean
    "load everything, then filter in Python."
    """
    return or_(
        model.visibility == Visibility.PUBLIC,
        and_(
            model.workspace_id == principal.workspace_id,
            or_(
                model.visibility == Visibility.WORKSPACE,
                and_(model.visibility == Visibility.PRIVATE, model.created_by == principal.user_id),
            ),
        ),
    )
