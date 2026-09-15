"""function_versions.function_id: add ON DELETE CASCADE

Found live 2026-09-03 (platform-function-promote branch), via `platform
function delete` on a published function: the unnamed FK 0001 created had
no ON DELETE clause, so Postgres defaults to NO ACTION — deleting a
Function with at least one published FunctionVersion raised a foreign-key
violation, uncaught by app/crud.py's generic delete(), surfaced as a bare
500 all the way up through gateway. See app/models.py's FunctionVersion
comment for why cascading here (rather than, say, blocking the delete with
a clearer error) is the right fix: a version only means anything in the
context of the function that owns it.

The constraint name is looked up dynamically rather than hardcoded
(`function_versions_function_id_fkey` is what Postgres's own default-naming
convention would produce for an unnamed single-column FK, and is very
likely correct) because this migration runs unattended, against the real
production database, via the catalog-service-migrate PreSync Job — a wrong
guess here fails loudly on `DROP CONSTRAINT`, not silently, but there's no
reason to guess when information_schema can just be asked.

Revision ID: 0002_function_versions_cascade
Revises: 0001_initial_schema
Create Date: 2026-09-03

Note on the revision id's length: Alembic's default `alembic_version.
version_num` column is `VARCHAR(32)` — a longer id (the first version of
this file used `..._cascade_delete`, 38 chars) fails silently at migration
time with a `StringDataRightTruncation` DataError, not at authoring time.
Found live running this migration against a real Postgres before shipping
it. Keep future revision ids under 32 characters.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_function_versions_cascade"
down_revision: str | None = "0001_initial_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _existing_fk_name(conn: sa.Connection) -> str:
    result = conn.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_name = 'function_versions'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'function_id'
            """
        )
    )
    return result.scalar_one()


def upgrade() -> None:
    conn = op.get_bind()
    existing_name = _existing_fk_name(conn)
    op.drop_constraint(existing_name, "function_versions", type_="foreignkey")
    op.create_foreign_key(
        "function_versions_function_id_fkey",
        "function_versions",
        "functions",
        ["function_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "function_versions_function_id_fkey", "function_versions", type_="foreignkey"
    )
    op.create_foreign_key(
        "function_versions_function_id_fkey",
        "function_versions",
        "functions",
        ["function_id"],
        ["id"],
        # No ondelete= — restores 0001's original NO ACTION behavior exactly.
    )
