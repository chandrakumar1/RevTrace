"""M0 — the database test harness itself.

Proves the harness is safe before anything relies on it: it reaches
revtrace_test, the schema is migrated, rollback isolation actually works, and it
structurally refuses to run against revtrace_dev.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from tests.conftest_db import DEFAULT_TEST_DSN, _database_name, resolve_test_dsn

pytestmark = pytest.mark.db


class TestDsnSafety:
    """The guard that keeps development data out of the test suite."""

    def test_default_dsn_targets_revtrace_test(self) -> None:
        assert _database_name(DEFAULT_TEST_DSN) == "revtrace_test"

    def test_resolved_dsn_is_a_test_database(self) -> None:
        assert "test" in _database_name(resolve_test_dsn())

    def test_revtrace_dev_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "TEST_DATABASE_URL", "postgresql+psycopg://sancha@localhost:5432/revtrace_dev"
        )
        with pytest.raises(RuntimeError, match="revtrace_dev must never be used"):
            resolve_test_dsn()

    def test_arbitrary_database_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "TEST_DATABASE_URL", "postgresql+psycopg://sancha@localhost:5432/postgres"
        )
        with pytest.raises(RuntimeError, match="refusing to run tests"):
            resolve_test_dsn()


class TestConnectivity:
    def test_engine_connects(self, db_engine: Engine) -> None:
        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1

    def test_connected_to_revtrace_test(self, db_engine: Engine) -> None:
        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar() == (
                "revtrace_test"
            )

    def test_schema_is_migrated(self, db_engine: Engine) -> None:
        with db_engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version

    def test_all_nine_domain_tables_exist(self, db_engine: Engine) -> None:
        expected = {
            "merchants",
            "customers",
            "orders",
            "payment_attempts",
            "events",
            "revenue_risks",
            "recovery_cases",
            "recovery_actions",
            "audit_events",
        }
        with db_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            ).scalars()
        assert expected <= set(rows)


class TestRollbackIsolation:
    """A test must never leave a row behind."""

    def test_insert_is_visible_within_the_test(self, db_session: Session) -> None:
        from app.models import Merchant

        merchant = Merchant(name="Harness Merchant", currency="INR", timezone="Asia/Kolkata")
        db_session.add(merchant)
        db_session.flush()

        assert db_session.get(Merchant, merchant.id) is not None

    def test_previous_insert_did_not_survive(self, db_engine: Engine) -> None:
        """Runs after the test above; its row must be gone."""
        with db_engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM merchants WHERE name = 'Harness Merchant'")
            ).scalar()
        assert count == 0

    def test_commit_inside_a_test_still_rolls_back(self, db_session: Session) -> None:
        from app.models import Merchant

        db_session.add(Merchant(name="Committed Merchant", currency="INR", timezone="UTC"))
        db_session.commit()
        assert db_session.query(Merchant).filter_by(name="Committed Merchant").count() == 1

    def test_committed_row_did_not_survive_either(self, db_engine: Engine) -> None:
        with db_engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM merchants WHERE name = 'Committed Merchant'")
            ).scalar()
        assert count == 0
