"""Needs a real reachable Postgres — set TEST_DATABASE_URL (defaults to a
`catalog_test` database on localhost, so it doesn't collide with the
DATABASE_URL/.env.example dev database by accident). Not run in CI yet —
.github/workflows/ci.yml doesn't exist yet either (ARCHITECTURE.md §10's CI
table is still aspirational at this point) — but written to run cleanly
once a Postgres service container is wired up alongside it, same shape as
that table's `src/**/*.py` -> ruff + pytest row.

`docker run --rm -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=catalog_test -p 5432:5432 postgres:18`
is enough to run these locally against a throwaway DB.
"""
from __future__ import annotations

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/catalog_test",
)
# Set BEFORE importing anything from `app` (below, and in every fixture that
# imports app.* lazily) — app/config.py's `settings` is a module-level
# singleton read once at import time, and migrations/env.py deliberately
# takes its DB URL from that same settings object, not from whatever
# alembic.Config it's handed (see that file's own comment: one source of
# truth for the URL). Overriding os.environ here, before either module gets
# imported for the first time, is what makes both of them point at the test
# database instead of silently falling back to DATABASE_URL/.env's dev one —
# learned the hard way: an earlier version of this fixture set the URL only
# on the alembic Config object, which env.py's settings-based override then
# clobbered right back to the dev default, so migrations quietly ran against
# the wrong database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# Every table this cleans up between tests — kept as one explicit list
# (rather than introspecting Base.metadata) so it's obvious at a glance
# what "clean slate" means here, and ordered child-to-parent so the FKs
# (workspace_id, function_id) don't need CASCADE to satisfy.
_TABLES_TO_RESET = (
    "lineage_edges",
    "function_versions",
    "functions",
    "models",
    "pipelines",
    "datasets",
)


@pytest.fixture(scope="session")
def _migrated_engine():
    """Runs the real Alembic migrations against TEST_DATABASE_URL once per
    test session — deliberately not Base.metadata.create_all(): these tests
    should exercise the same migration path production goes through, and
    the enum types (created only by the migration — see models.py's
    create_type=False) only exist after it runs."""
    engine = create_engine(TEST_DATABASE_URL)
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")
    yield engine
    command.downgrade(cfg, "base")
    engine.dispose()


@pytest.fixture()
def db_session(_migrated_engine):
    """Deliberately NOT a rollback-per-test transaction: app code under test
    (app/crud.py etc.) calls plain session.commit() — same as production —
    and a real Postgres error (e.g. the unique-constraint violation
    test_duplicate_name_in_same_workspace_conflicts exercises on purpose)
    aborts the whole surrounding transaction, not just the failed
    statement, which made an earlier savepoint-nesting version of this
    fixture unreliable in exactly that test. Simpler and more robust: let
    commits be real, then explicitly wipe every table (except `workspaces`,
    which keeps its migration-seeded `personal` row and any workspace rows
    a test created — extra workspace rows don't collide with anything,
    since datasets/pipelines/etc. are unique per-workspace, not globally)
    after each test."""
    session = sessionmaker(bind=_migrated_engine)()
    try:
        yield session
    finally:
        session.close()
        with _migrated_engine.begin() as connection:
            for table in _TABLES_TO_RESET:
                connection.execute(text(f"DELETE FROM {table}"))
            connection.execute(text("DELETE FROM workspaces WHERE name != 'personal'"))


@pytest.fixture()
def client(db_session):
    from app.database import get_db
    from app.main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
