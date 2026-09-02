"""Re-export the database fixtures for the agent tests.

Only the audit-persistence class needs them; everything else in this package is
hermetic and never touches PostgreSQL. Kept here rather than in the root
`tests/conftest.py`, which is Phase 1 code and must not be modified — the same
arrangement `tests/integration/` and `tests/benchmark/` use.
"""

from __future__ import annotations

from tests.conftest_db import (  # noqa: F401  (re-exported as pytest fixtures)
    _schema_is_current,
    db_engine,
    db_session,
)
