"""Shared response types.

PostgreSQL returns `timestamptz` values in the session's timezone, which on this
machine is IST. The instant is correct either way, but the Phase 2 fixture
contract specifies ISO-8601 **UTC with a trailing `Z`**, and a frontend that
consumes both simulator fixtures and this API should not have to handle two
renderings of the same thing.

`UtcDatetime` normalises on the way out. It is applied in the Phase 3 schemas
rather than on the engine, because `app/db/session.py` is Phase 1 code and out
of bounds for this milestone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer


def to_utc_iso(value: datetime) -> str:
    """ISO-8601 in UTC with a `Z` suffix, matching the fixture contract."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: A datetime that always serialises as UTC with a `Z` suffix.
UtcDatetime = Annotated[datetime, PlainSerializer(to_utc_iso, return_type=str)]
