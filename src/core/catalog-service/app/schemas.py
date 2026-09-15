"""Pydantic request/response models — one Create/Update/Read trio per
catalog entity. Read schemas use `from_attributes=True` so they can be built
straight off the SQLAlchemy ORM objects in app/models.py (`model_validate`).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import Visibility
from app.visibility import Role


# ---- Workspace --------------------------------------------------------
class WorkspaceCreate(BaseModel):
    name: str = Field(max_length=63)
    display_name: str = Field(max_length=255)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    display_name: str
    created_at: datetime


class PrincipalRead(BaseModel):
    """Body of GET /me — "who does the server think I am, given my current
    headers." Deliberately separate from WorkspaceRead: role/user_id are
    properties of the REQUEST (this Principal), not of the Workspace row
    itself — two different callers hitting the same workspace get the same
    WorkspaceRead but a different PrincipalRead. Mainly for platform-cli/
    platform-sdk to self-check what they're allowed to do before trying."""

    workspace_id: uuid.UUID
    workspace_name: str
    user_id: str
    role: Role


# ---- Shared entity fields ----------------------------------------------
class _EntityCreate(BaseModel):
    name: str = Field(max_length=255)
    visibility: Visibility = Visibility.PRIVATE
    description: str | None = None


class _EntityUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    visibility: Visibility | None = None
    description: str | None = None


class _EntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    visibility: Visibility
    description: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


# ---- Dataset -------------------------------------------------------------
class DatasetCreate(_EntityCreate):
    location_uri: str | None = None


class DatasetUpdate(_EntityUpdate):
    location_uri: str | None = None


class DatasetRead(_EntityRead):
    location_uri: str | None


# ---- Pipeline --------------------------------------------------------------
class PipelineCreate(_EntityCreate):
    pass


class PipelineUpdate(_EntityUpdate):
    pass


class PipelineRead(_EntityRead):
    pass


# ---- Model (ML) ------------------------------------------------------------
class MLModelCreate(_EntityCreate):
    framework: str | None = None


class MLModelUpdate(_EntityUpdate):
    framework: str | None = None


class MLModelRead(_EntityRead):
    framework: str | None


# ---- Function + versions ----------------------------------------------
class FunctionCreate(_EntityCreate):
    module_path: str | None = None


class FunctionUpdate(_EntityUpdate):
    pass


class FunctionRead(_EntityRead):
    current_version: int
    module_path: str | None


class FunctionPublish(BaseModel):
    """Body of POST /functions/{id}/publish — platform-cli's `publish`
    command is the intended caller (ARCHITECTURE.md §4)."""

    signature: str
    docstring: str | None = None
    module_path: str
    published_by: str | None = None


class FunctionVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    function_id: uuid.UUID
    version: int
    signature: str
    docstring: str | None
    module_path: str
    published_by: str | None
    published_at: datetime


# ---- Lineage ---------------------------------------------------------------
class LineageEdgeCreate(BaseModel):
    source_kind: str
    source_id: uuid.UUID
    target_kind: str
    target_id: uuid.UUID
    relation: str


class LineageEdgeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    workspace_id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID
    target_kind: str
    target_id: uuid.UUID
    relation: str
    created_at: datetime
