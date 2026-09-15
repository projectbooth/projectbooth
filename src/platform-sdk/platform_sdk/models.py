"""Typed shapes for what catalog-service's JSON responses actually look
like. Deliberately NOT imported from app.schemas/app.models over in
catalog-service — that package is a service's internals, not a published
library, and importing it would mean platform-sdk (installed wherever code
using @platform.dataset runs, per ARCHITECTURE.md §4) needing catalog-service
importable too, defeating the point of talking to it over HTTP instead. A
little duplication of field shapes here, against catalog-service's public
REST contract rather than its Python internals, is the actual decoupling —
same tradeoff models.py's own `_pg_enum` comment makes elsewhere in this
repo, applied one layer up.

Workspace, Dataset, Principal, and — as of the platform-function-promote
branch (2026-09-03) — Function/FunctionVersion exist. Pipeline/MLModel/
LineageEdge are the same shape of work again once platform-cli actually
needs them (see this package's README).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel


class Visibility(enum.StrEnum):
    """Mirrors app.models.Visibility over in catalog-service exactly (same
    three lowercase values) — this is the public contract those values are
    part of, not an internal detail, so duplicating it here (rather than
    importing it) is the point, not a shortcut. See this module's docstring."""

    PRIVATE = "private"
    WORKSPACE = "workspace"
    PUBLIC = "public"


class Workspace(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    created_at: datetime


class Dataset(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    visibility: Visibility
    description: str | None
    location_uri: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class Function(BaseModel):
    """Mirrors app.schemas.FunctionRead over in catalog-service — see this
    module's docstring for why duplicating the shape (not importing it) is
    the point. current_version=0 means "registered but never published"
    (see app/models.py's Function docstring); module_path is null until the
    first publish() sets it."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    visibility: Visibility
    description: str | None
    current_version: int
    module_path: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class FunctionVersion(BaseModel):
    """Mirrors app.schemas.FunctionVersionRead — one row per publish(), full
    history kept (see app/models.py's FunctionVersion docstring for why)."""

    id: uuid.UUID
    function_id: uuid.UUID
    version: int
    signature: str
    docstring: str | None
    module_path: str
    published_by: str | None
    published_at: datetime


class Role(enum.StrEnum):
    """Mirrors app.visibility.Role over in catalog-service exactly — same
    duplication tradeoff Visibility above makes, for the same reason. Also
    exactly the three realm-role names src/core/auth/realm-platform.yaml
    seeds (`owner`/`editor`/`viewer`) and the three role-subgroup names
    under each `/workspaces/<name>/` group — one set of spellings, not
    three independently-typed ones."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class InviteResult(BaseModel):
    """What `KeycloakAdminClient.invite()` actually did, echoed back so a
    caller (platform-cli's `workspace invite` command) isn't left guessing
    whether the Keycloak group it just wrote to already existed or had to
    be created on the spot — see keycloak_admin.py's module docstring for
    why the latter can happen at all (workspaces created via `platform
    workspace create` don't get a matching Keycloak group automatically;
    this is where that gap gets closed, lazily, on first invite)."""

    username: str
    workspace: str
    role: Role
    group_path: str
    group_created: bool


class TokenSet(BaseModel):
    """What `platform login`'s device-flow poll loop (or a later silent
    refresh) got back from Keycloak's token endpoint, plus one derived field
    — `expires_at` — computed once, right when the tokens are minted, so
    every later reader (PlatformClient's near-expiry check, `platform
    login`'s own "logged in as X" print) works off a fixed timestamp instead
    of re-deriving "how long is left" from a raw `expires_in` seconds-count
    that would drift depending on how long this struct sat in
    credentials.json before being read back.

    `refresh_token` is typed optional only for the type checker's sake —
    Keycloak's response for this client's grant type always includes one in
    practice; `keycloak_login.py`'s own docstring covers why a `None` here
    would mean something upstream actually went wrong, not a routine case.

    `preferred_username` comes from the ID token's payload (see
    `keycloak_login.py`'s `_extract_preferred_username` for why reading it
    unverified is safe here) and exists purely so `platform login` can print
    who you're now logged in as without a second round-trip — nothing
    authorization-relevant ever reads this field; that all happens
    server-side, off the verified access_token, in gateway.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    preferred_username: str | None


class ModuleRequirementStatus(BaseModel):
    """One entry of gateway's `GET /modules/check-requirements` response
    (module-lifecycle-plan.md item 6, platform-module-deps branch,
    2026-09-03) — mirrors that endpoint's response shape exactly (see
    gateway/app/modules.py's docstring for why the satisfaction check lives
    there, not here or in platform-cli). `status` is the raw Argo CD health
    string ("Healthy", "Progressing", "Degraded", ...) or the literal
    `"not installed"` when no Application exists for this module at all —
    `satisfied` is just `status == "Healthy"`, already computed
    server-side, so callers never need to know that string themselves."""

    module_id: str
    satisfied: bool
    status: str


class Principal(BaseModel):
    """Body of GET /me — see catalog-service's PrincipalRead docstring for
    why this is a "who does the server think I am" snapshot, not a
    Workspace. Role is plain str, not an enum, here: the SDK doesn't
    enforce or branch on role itself (catalog-service already does, and
    returns a 403 platform-sdk surfaces as PlatformAPIError) — this is just
    what came back."""

    workspace_id: uuid.UUID
    workspace_name: str
    user_id: str
    role: str
