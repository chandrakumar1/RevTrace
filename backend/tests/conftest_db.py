"""Database fixtures for Phase 3 integration tests.

These are the only tests that touch PostgreSQL. Everything else in the suite —
reconstruction, detectors, money — stays hermetic.

**revtrace_dev is never used here.** The DSN defaults to `revtrace_test` and a
guard refuses to run against any database whose name is not explicitly marked as
a test database, so a stray `TEST_DATABASE_URL` cannot point the suite at
development data.

Every test runs inside a transaction that is rolled back afterwards, so the
schema is reused but no row ever survives a test. The harness never drops,
truncates, or recreates anything.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

#: No role in the DSN, deliberately. libpq falls back to the operating-system
#: user, so this default works on any machine whose PostgreSQL role matches the
#: login name — and it keeps one developer's username out of a published
#: repository. Set `TEST_DATABASE_URL` when the role differs.
DEFAULT_TEST_DSN = "postgresql+psycopg://localhost:5432/revtrace_test"

#: A DSN is only accepted if its database name contains one of these. This is
#: the guard that keeps revtrace_dev out of the test suite.
REQUIRED_DSN_MARKERS = ("revtrace_test", "_test")


def _database_name(dsn: str) -> str:
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def resolve_test_dsn() -> str:
    """Resolve and validate the test DSN."""
    dsn = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DSN)
    name = _database_name(dsn)

    if not any(marker in name for marker in REQUIRED_DSN_MARKERS):
        raise RuntimeError(
            f"refusing to run tests against database {name!r}: the test DSN must "
            f"name a test database (one of {REQUIRED_DSN_MARKERS}). "
            "revtrace_dev must never be used for tests."
        )
    return dsn


@pytest.fixture(scope="session")
def db_engine() -> Generator[Engine]:
    """Session-wide engine against revtrace_test.

    Skips the whole DB suite rather than failing if PostgreSQL is unreachable,
    so the hermetic majority of the suite still runs on a machine without a
    database.
    """
    engine = create_engine(resolve_test_dsn(), future=True, pool_pre_ping=True)

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        engine.dispose()
        pytest.skip(f"revtrace_test is unreachable: {exc}")

    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def _schema_is_current(db_engine: Engine) -> None:
    """Fail loudly if revtrace_test has not been migrated."""
    with db_engine.connect() as connection:
        result = connection.execute(text("SELECT version_num FROM alembic_version")).first()

    if result is None:
        pytest.fail("revtrace_test has no alembic_version row; run alembic upgrade head")


@pytest.fixture
def db_session(db_engine: Engine, _schema_is_current: None) -> Generator[Session]:
    """A session inside a transaction that is always rolled back.

    Nested transactions in the code under test bind to a SAVEPOINT, so a
    service that commits internally still leaves no trace after the outer
    rollback.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
