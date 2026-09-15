"""catalog-lite initial schema

Hand-written, not `alembic revision --autogenerate` (there's no live DB in
this environment to diff against) — kept in exact lockstep with
app/models.py; if the two ever drift, models.py is what's wrong (it's the
source of truth going forward, this migration just has to match it once).

Revision ID: 0001_initial_schema
Revises: (none — first migration)
Create Date: 2026-09-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

visibility_enum = postgresql.ENUM("private", "workspace", "public", name="visibility")
entity_kind_enum = postgresql.ENUM("dataset", "function", "pipeline", "model", name="entity_kind")
lineage_relation_enum = postgresql.ENUM("reads", "writes", "calls", name="lineage_relation")


def _entity_columns() -> list[sa.Column]:
    """The columns every catalog entity table shares — see app/models.py's
    module docstring for why this is copy-pasted per table (both here and
    there) rather than abstracted."""
    return [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "visibility",
            postgresql.ENUM(name="visibility", create_type=False),
            nullable=False,
            server_default="private",
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    visibility_enum.create(bind, checkfirst=True)
    entity_kind_enum.create(bind, checkfirst=True)
    lineage_relation_enum.create(bind, checkfirst=True)

    op.create_table(
        "workspaces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(63), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "datasets",
        *_entity_columns(),
        sa.Column("location_uri", sa.Text(), nullable=True),
        sa.UniqueConstraint("workspace_id", "name", name="uq_datasets_workspace_name"),
    )
    op.create_index("ix_datasets_workspace_id", "datasets", ["workspace_id"])
    op.create_index(
        "ix_datasets_public", "datasets", ["visibility"], postgresql_where=sa.text("visibility = 'public'")
    )

    op.create_table(
        "pipelines",
        *_entity_columns(),
        sa.UniqueConstraint("workspace_id", "name", name="uq_pipelines_workspace_name"),
    )
    op.create_index("ix_pipelines_workspace_id", "pipelines", ["workspace_id"])
    op.create_index(
        "ix_pipelines_public", "pipelines", ["visibility"], postgresql_where=sa.text("visibility = 'public'")
    )

    op.create_table(
        "models",
        *_entity_columns(),
        sa.Column("framework", sa.String(63), nullable=True),
        sa.UniqueConstraint("workspace_id", "name", name="uq_models_workspace_name"),
    )
    op.create_index("ix_models_workspace_id", "models", ["workspace_id"])
    op.create_index(
        "ix_models_public", "models", ["visibility"], postgresql_where=sa.text("visibility = 'public'")
    )

    op.create_table(
        "functions",
        *_entity_columns(),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("module_path", sa.Text(), nullable=True),
        sa.UniqueConstraint("workspace_id", "name", name="uq_functions_workspace_name"),
    )
    op.create_index("ix_functions_workspace_id", "functions", ["workspace_id"])
    op.create_index(
        "ix_functions_public", "functions", ["visibility"], postgresql_where=sa.text("visibility = 'public'")
    )

    op.create_table(
        "function_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "function_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("functions.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("docstring", sa.Text(), nullable=True),
        sa.Column("module_path", sa.Text(), nullable=False),
        sa.Column("published_by", sa.String(255), nullable=True),
        sa.Column(
            "published_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("function_id", "version", name="uq_function_versions_function_version"),
    )
    op.create_index("ix_function_versions_function_id", "function_versions", ["function_id"])

    op.create_table(
        "lineage_edges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("source_kind", postgresql.ENUM(name="entity_kind", create_type=False), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", postgresql.ENUM(name="entity_kind", create_type=False), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "relation", postgresql.ENUM(name="lineage_relation", create_type=False), nullable=False
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "source_kind", "source_id", "target_kind", "target_id", "relation", name="uq_lineage_edges_edge"
        ),
    )
    op.create_index("ix_lineage_edges_workspace_id", "lineage_edges", ["workspace_id"])
    op.create_index("ix_lineage_edges_source", "lineage_edges", ["source_kind", "source_id"])
    op.create_index("ix_lineage_edges_target", "lineage_edges", ["target_kind", "target_id"])

    # Seed the one workspace every install has from day one (ARCHITECTURE.md
    # §4) — mirrors the `personal` group src/core/auth/realm-platform.yaml's
    # KeycloakRealmImport already seeds, so the two aren't inconsistent from
    # the very first migration. Fixed id (all zeros but the version nibble)
    # so it's stable/predictable across environments rather than random —
    # useful for anything that needs to reference "the default workspace"
    # before real workspace-lookup-by-name plumbing exists everywhere.
    op.execute(
        """
        INSERT INTO workspaces (id, name, display_name)
        VALUES ('00000000-0000-0000-0000-000000000001', 'personal', 'Personal')
        """
    )


def downgrade() -> None:
    op.drop_table("lineage_edges")
    op.drop_table("function_versions")
    op.drop_table("functions")
    op.drop_table("models")
    op.drop_table("pipelines")
    op.drop_table("datasets")
    op.drop_table("workspaces")
    lineage_relation_enum.drop(op.get_bind(), checkfirst=True)
    entity_kind_enum.drop(op.get_bind(), checkfirst=True)
    visibility_enum.drop(op.get_bind(), checkfirst=True)
