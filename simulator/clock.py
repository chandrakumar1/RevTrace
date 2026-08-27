"""Simulation time.

Every timestamp is a fixed epoch plus an integer second offset. `datetime.now()`
and `datetime.utcnow()` are never called — wall-clock time would make output
non-reproducible.

All timestamps are timezone-aware UTC. RevTrace reconstructs timelines across
delayed and out-of-order delivery, so a naive datetime is never acceptable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: Fixed anchor for all simulated time. Deliberately a constant.
SIMULATION_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class SimulationClock:
    """Converts integer second offsets into timezone-aware UTC timestamps."""

    __slots__ = ("_epoch",)

    def __init__(self, epoch: datetime = SIMULATION_EPOCH) -> None:
        if epoch.tzinfo is None:
            raise ValueError("simulation epoch must be timezone-aware")
        if epoch.utcoffset() != timedelta(0):
            raise ValueError("simulation epoch must be UTC")
        self._epoch = epoch

    @property
    def epoch(self) -> datetime:
        return self._epoch

    def at(self, offset_seconds: int) -> datetime:
        """Return epoch + offset. Offsets must be whole, non-negative seconds."""
        if not isinstance(offset_seconds, int) or isinstance(offset_seconds, bool):
            raise TypeError(f"offset_seconds must be an int, got {type(offset_seconds).__name__}")
        if offset_seconds < 0:
            raise ValueError(f"offset_seconds must be non-negative, got {offset_seconds}")
        return self._epoch + timedelta(seconds=offset_seconds)


def is_utc(value: datetime) -> bool:
    """True when a datetime is timezone-aware and at UTC offset."""
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)
