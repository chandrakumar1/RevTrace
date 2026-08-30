"""Re-export the database fixtures for benchmark tests.

Same arrangement as `tests/integration/conftest.py`: the root `tests/conftest.py`
is Phase 1 code and must not be modified, so packages that need a database pull
the fixtures in here.
"""

from __future__ import annotations

from tests.conftest_db import (  # noqa: F401  (re-exported as pytest fixtures)
    _schema_is_current,
    db_engine,
    db_session,
)
