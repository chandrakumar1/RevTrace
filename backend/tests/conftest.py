"""Shared test fixtures.

Phase 1 tests are hermetic: nothing here connects to PostgreSQL, and nothing
writes to revtrace_dev. Model assertions use SQLAlchemy metadata introspection,
and the database dependency is overridden in API tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def client() -> TestClient:
    """TestClient over the real app. No database connection is made."""
    from app.main import create_app

    return TestClient(create_app())
