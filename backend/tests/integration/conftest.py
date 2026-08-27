"""Re-export the database fixtures for integration tests.

Kept here rather than in the root `tests/conftest.py`, which is Phase 1 code and
must not be modified.
"""

from __future__ import annotations

from tests.conftest_db import (  # noqa: F401  (re-exported as pytest fixtures)
    _schema_is_current,
    db_engine,
    db_session,
)
