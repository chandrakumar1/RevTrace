"""Detector contract.

A detector is a **pure function**::

    detect(timeline, as_of, config) -> tuple[RiskFinding, ...]

No database, no network, no LLM, and — importantly — no clock. The evaluation
instant arrives as `as_of` rather than being read from the system, so a run is
reproducible: replaying the same timeline with the same `as_of` always yields
the same findings.

A `RiskFinding` is a *statement that something is wrong*. It carries no
recommendation, no approval, no policy decision, and no recovery action.
Detection identifies risk; it never authorises anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.models.enums import RiskType
from app.services.tracing.state import EventLike


@dataclass(frozen=True, slots=True)
class RiskFinding:
    """One deterministic detection result.

    Deliberately absent: recommended action, expected recovery, approval state,
    policy status. Those belong to Phases 6 and 7, and a detector inventing them
    would cross the authority boundary.
    """

    risk_type: str
    merchant_id: uuid.UUID
    order_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    order_ref: str | None
    #: Integer minor units.
    amount_at_risk: int
    currency: str | None
    #: Basis points 0-10000. A synthetic/demo heuristic, never a probability.
    confidence_bps: int
    #: Versioned identifier of the rule that fired, for auditability.
    detection_rule: str
    reason: str
    #: external_event_ids that support the finding.
    evidence_event_ids: tuple[str, ...]
    detected_at: datetime

    def __post_init__(self) -> None:
        if self.risk_type not in RiskType.values():
            raise ValueError(f"invalid risk_type {self.risk_type!r}")
        if isinstance(self.amount_at_risk, bool) or not isinstance(self.amount_at_risk, int):
            raise TypeError("amount_at_risk must be an integer count of minor units")
        if self.amount_at_risk < 0:
            raise ValueError("amount_at_risk must be non-negative")
        if isinstance(self.confidence_bps, bool) or not isinstance(self.confidence_bps, int):
            raise TypeError("confidence_bps must be an integer")
        if not 0 <= self.confidence_bps <= 10_000:
            raise ValueError(f"confidence_bps {self.confidence_bps} outside 0..10000")
        if self.detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")

    @property
    def natural_key(self) -> tuple[uuid.UUID, uuid.UUID | None, str]:
        """The application-level upsert key: (merchant, order, risk type).

        Re-running detection over an extended timeline must update the existing
        risk rather than create a second one.
        """
        return (self.merchant_id, self.order_id, self.risk_type)


def evidence_ids(events: Iterable[EventLike], event_types: Sequence[str]) -> tuple[str, ...]:
    """External ids of the events supporting a finding, in causal order."""
    wanted = set(event_types)
    return tuple(
        event.external_event_id
        for event in events
        if event.event_type in wanted and event.external_event_id is not None
    )


def seconds_between(earlier: datetime, later: datetime) -> int:
    """Whole seconds between two instants. Never negative."""
    return max(0, int((later - earlier).total_seconds()))
