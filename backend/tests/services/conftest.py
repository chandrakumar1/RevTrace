"""Re-export the database fixtures for the service-seam tests.

Only the persistence classes need them; the isolation guards below are
hermetic and never open a connection. Kept here rather than in the root
`tests/conftest.py`, which is Phase 1 code and must not be modified — the same
arrangement `tests/agents/`, `tests/integration/` and `tests/benchmark/` use.
"""

from __future__ import annotations

from tests.conftest_db import (  # noqa: F401  (re-exported as pytest fixtures)
    _schema_is_current,
    db_engine,
    db_session,
)
