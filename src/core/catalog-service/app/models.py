"""SQLAlchemy ORM models for catalog-lite.

Schema shape follows ARCHITECTURE.md §4 ("Tenancy & isolation") and §12
("Catalog: build vs adopt" — Postgres + FastAPI, picked over OpenMetadata/
DataHub for being right-sized for a handful of personal workspaces).

Every entity table (datasets, functions, pipelines, models) carries the same
two tenancy columns: workspace_id (who owns it) and visibility (who else can
see it) — deliberately one pattern repeated four times rather than a clever
shared-mixin abstraction, because ARCHITECTURE.md §4 is explicit that the
catalog's whole tenancy story IS "a workspace_id and a visibility flag ... on
every dataset, function, pipeline, and model," and a little repetition here
is easier for a solo maintainer to read than a mixin using declared_attr for
every ForeignKey/UniqueConstraint. See app/visibility.py for what the three
visibility values mean and how they're enforced — this file is schema only.

No cross-table foreign keys from LineageEdge into the four entity tables —
source_id/target_id are polymorphic (a function's lineage edge can point at a
dataset OR another function OR a pipeline; see source_kind/target_kind), and
Postgres has no native polymorphic FK. Integrity there is an application-
layer concern (see app/routers/lineage.py) — a known "lite" simplification,
worth revisiting if this ever needs to survive orphaned references at scale.

Name uniqueness is per-workspace (UniqueConstraint(workspace_id, name)), not
global — so two workspaces can each have their own "clean_text" function.
What happens when BOTH are made `public` (ARCHITECTURE.md §4's cross-
workspace visibility) and something tries to call "clean_text" by name alone
is a platform-sdk resolution question, not a catalog-service schema question
— flagging it here since it's the kind of thing that bites later, not
solving it now.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum as PgEnum
from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Visibility(enum.StrEnum):
    """Who else can see an entity, beyond its owning workspace.

    private   — only its creator (created_by), even within the owning workspace
    workspace — any member of the owning workspace
    public    — anyone, in any workspace, on this instance

    See app/visibility.py for the read/write rule this drives.
    """

    PRIVATE = "private"
    WORKSPACE = "workspace"
    PUBLIC = "public"


class EntityKind(enum.StrEnum):
    """The four catalog entity types — used polymorphically by LineageEdge
    since a lineage edge can point at any of them."""

    DATASET = "dataset"
    FUNCTION = "function"
    PIPELINE = "pipeline"
    MODEL = "model"


class LineageRelation(enum.StrEnum):
    """ARCHITECTURE.md §4: a function's lineage is "which pipelines call it,
    which datasets it reads or writes" — these three relations cover that.
    Not exhaustive by design (e.g. no dataset-to-dataset relation yet) — add
    as real lineage-recording use cases show up, per §12's "revisit once
    real usage tells you otherwise" philosophy."""

    READS = "reads"
    WRITES = "writes"
    CALLS = "calls"


_PK = lambda: mapped_column(  # noqa: E731
    PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
)
# gen_random_uuid() is a Postgres 13+ built-in (no pgcrypto CREATE EXTENSION
# needed) — CNPG's image here is postgresql:18.4, comfortably past that.


def _pg_enum(enum_cls: type[enum.StrEnum], name: str) -> PgEnum:
    """SQLAlchemy's Enum type persists a Python enum MEMBER'S NAME by
    default (e.g. "PRIVATE"), not its .value ("private") — for our
    lowercase-valued StrEnums that means it would try to write "PRIVATE"
    into a Postgres enum column whose labels are the lowercase values from
    migrations/versions/0001_initial_schema.py, and fail with "invalid
    input value for enum" on every single insert. values_callable fixes
    that at the type level, once, here, instead of seven repeated call
    sites each getting it right (or wrong) independently."""
    return PgEnum(enum_cls, name=name, create_type=False, values_callable=lambda obj: [e.value for e in obj])


class Workspace(Base):
    """The tenancy unit (ARCHITECTURE.md §4). Rows here are meant to stay in
    lockstep with Keycloak groups (src/core/auth/realm-platform.yaml) by
    `name` — this table doesn't own that relationship yet (no FK or sync job
    back to Keycloak; `platform workspace create`, not built yet, is where
    that gets wired up properly). migrations/versions/0001_initial_schema.py
    seeds the one `personal` workspace the realm import already seeds on its
    side, so the two don't start out already inconsistent.
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _PK()
    name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_datasets_workspace_name"),)

    id: Mapped[uuid.UUID] = _PK()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        _pg_enum(Visibility, "visibility"),
        nullable=False,
        server_default=Visibility.PRIVATE.value,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where the data actually lives — s3://<seaweedfs-or-external-bucket>/<workspace>/...
    # (ARCHITECTURE.md §4's bucket-prefix-per-workspace convention). Nullable:
    # a dataset can be registered before its location is decided.
    location_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Pipeline(Base):
    __tablename__ = "pipelines"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_pipelines_workspace_name"),)

    id: Mapped[uuid.UUID] = _PK()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        _pg_enum(Visibility, "visibility"),
        nullable=False,
        server_default=Visibility.PRIVATE.value,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MLModel(Base):
    """Table name `models`, class name `MLModel` — `Model` would collide
    with the general "an ORM model class" vocabulary all over this file."""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_models_workspace_name"),)

    id: Mapped[uuid.UUID] = _PK()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        _pg_enum(Visibility, "visibility"),
        nullable=False,
        server_default=Visibility.PRIVATE.value,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free text for now (e.g. "sklearn", "mlflow-pyfunc") — becomes a real
    # registry pointer once ml-mlflow (Phase 6) exists to point at.
    framework: Mapped[str | None] = mapped_column(String(63), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Function(Base):
    """The "current" pointer for a function — current_version denormalizes
    FunctionVersion so listing/reading a function doesn't need a join for
    the common case. current_version=0 means "registered but never
    published" (platform-sdk's @platform.function decorator can register a
    function's existence before platform-cli publish ever runs)."""

    __tablename__ = "functions"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_functions_workspace_name"),)

    id: Mapped[uuid.UUID] = _PK()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        _pg_enum(Visibility, "visibility"),
        nullable=False,
        server_default=Visibility.PRIVATE.value,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    module_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class FunctionVersion(Base):
    """One row per `platform-cli publish` (ARCHITECTURE.md §4's "a version,
    bumped on each publish"). Kept as full history, not overwritten in
    place, so a function's lineage edges (which pipeline called version 3
    vs version 4) stay meaningful after a later publish changes the
    signature."""

    __tablename__ = "function_versions"
    __table_args__ = (
        UniqueConstraint("function_id", "version", name="uq_function_versions_function_version"),
    )

    id: Mapped[uuid.UUID] = _PK()
    # ondelete="CASCADE" added 2026-09-03 (platform-function-promote branch)
    # — found live: DELETE /functions/{id} on a published function 500'd,
    # because Postgres rejected the delete outright (an unnamed FK defaults
    # to NO ACTION, and app/crud.py's generic delete() doesn't catch the
    # resulting IntegrityError). A version only means anything in the
    # context of the function that owns it — unlike LineageEdge's source_id/
    # target_id (this file's own module docstring covers why THOSE are
    # allowed to dangle), there's no reason to keep orphaned version rows
    # around once their function is gone, so cascading here is the right
    # fix, not just a workaround. See migrations/versions/0002_* for the
    # matching live-DB constraint change.
    function_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("functions.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    docstring: Mapped[str | None] = mapped_column(Text, nullable=True)
    module_path: Mapped[str] = mapped_column(Text, nullable=False)
    published_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class LineageEdge(Base):
    """See this file's module docstring for why source/target are
    polymorphic (kind + id) rather than four sets of FK columns."""

    __tablename__ = "lineage_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            "relation",
            name="uq_lineage_edges_edge",
        ),
    )

    id: Mapped[uuid.UUID] = _PK()
    # The owning workspace of this edge — set to the source entity's
    # workspace_id at insert time (app/routers/lineage.py), so listing "this
    # workspace's lineage" doesn't need to join out to four different tables
    # just to find out who owns each edge.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    source_kind: Mapped[EntityKind] = mapped_column(
        _pg_enum(EntityKind, "entity_kind"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_kind: Mapped[EntityKind] = mapped_column(
        _pg_enum(EntityKind, "entity_kind"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    relation: Mapped[LineageRelation] = mapped_column(
        _pg_enum(LineageRelation, "lineage_relation"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
