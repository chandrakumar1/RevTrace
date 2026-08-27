"""Detector thresholds.

Every value is an integer. Windows are whole seconds, counts are whole numbers,
and nothing here is a float — a threshold that gates money must compare exactly.

These are tuning parameters, not discoveries. They are chosen to match the
scenarios the Phase 2 simulator produces and will need revisiting against real
Razorpay traffic in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A single failure is not a leak. Two is the point at which repeated failure
#: becomes the specification's Scenario A.
DEFAULT_MIN_FAILED_ATTEMPTS = 2

#: Failures must cluster to count as one struggling checkout rather than two
#: unrelated purchases months apart.
DEFAULT_FAILURE_WINDOW_SECONDS = 24 * 60 * 60

#: How long a started checkout may stay silent before it counts as abandoned,
#: when no explicit abandonment event ever arrives.
DEFAULT_ABANDONMENT_SILENCE_SECONDS = 30 * 60

#: Grace period before a captured-but-unreconciled order is an anomaly rather
#: than an `order.paid` event still in flight. Deliberately generous: a delayed
#: webhook must not be mistaken for a missing one.
DEFAULT_RECONCILIATION_GRACE_SECONDS = 60 * 60

#: Consecutive failed billing cycles before recurring revenue is at risk.
DEFAULT_MIN_SUBSCRIPTION_FAILURES = 2

#: How long an unresolved risk stays open before it is written off. Expiry is a
#: bookkeeping outcome, not a recovery: nothing was collected, the case simply
#: stopped being actionable.
DEFAULT_RISK_EXPIRY_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Thresholds for one detection run.

    Passed explicitly rather than read from module state so a run's parameters
    are always visible in its inputs and reproducible from them.
    """

    min_failed_attempts: int = DEFAULT_MIN_FAILED_ATTEMPTS
    failure_window_seconds: int = DEFAULT_FAILURE_WINDOW_SECONDS
    abandonment_silence_seconds: int = DEFAULT_ABANDONMENT_SILENCE_SECONDS
    reconciliation_grace_seconds: int = DEFAULT_RECONCILIATION_GRACE_SECONDS
    min_subscription_failures: int = DEFAULT_MIN_SUBSCRIPTION_FAILURES
    risk_expiry_seconds: int = DEFAULT_RISK_EXPIRY_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "min_failed_attempts",
            "failure_window_seconds",
            "abandonment_silence_seconds",
            "reconciliation_grace_seconds",
            "min_subscription_failures",
            "risk_expiry_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, got {type(value).__name__}")
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")

        if self.min_failed_attempts < 1:
            raise ValueError("min_failed_attempts must be at least 1")
        if self.min_subscription_failures < 1:
            raise ValueError("min_subscription_failures must be at least 1")


DEFAULT_CONFIG = DetectorConfig()
