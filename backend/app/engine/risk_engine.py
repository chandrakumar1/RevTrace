"""Deterministic money computations for revenue risk.

Pure arithmetic over a reconstructed timeline. No database, no network, no LLM,
no clock. Given the same timeline this always returns the same integers, which
is what makes an audit trail worth having.

Every value is an integer count of minor units (ADR 0001). Nothing here accepts
or produces a float.

The single most important rule in this module — the one that would otherwise
quietly triple a headline figure:

    Amount at risk is the ORDER amount, counted ONCE.

Three failed attempts on one ₹2,304 order put ₹2,304 at risk, not ₹6,912. The
attempts are three tries at collecting one debt, not three debts. Summing the
attempt ledger is the natural mistake and it is explicitly tested against.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.money import CurrencyMismatchError, clamp_non_negative, require_same_currency
from app.models.enums import EventType
from app.services.tracing.state import OrderTimeline, SubscriptionTimeline

#: The reconciliation anomaly carries no revenue at risk: the money arrived.
#: What is broken is integrity, not revenue. Reporting the order amount here
#: would inflate every dashboard total with funds that were actually collected.
RECONCILIATION_AMOUNT_AT_RISK = 0


@dataclass(frozen=True, slots=True)
class MoneyBreakdown:
    """Every monetary figure for one order, in integer minor units."""

    currency: str | None
    order_amount: int
    captured: int
    failed: int
    refunded: int
    recovered: int
    outstanding: int

    def __post_init__(self) -> None:
        for name in ("order_amount", "captured", "failed", "refunded", "recovered", "outstanding"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer count of minor units")


def resolve_currency(timeline: OrderTimeline) -> str | None:
    """The order's currency, verified consistent across its attempts.

    Raises `CurrencyMismatchError` rather than silently picking one. Combining
    amounts in different currencies is never a rounding problem — it is a wrong
    answer, and it should stop the calculation.
    """
    currency = timeline.currency
    for attempt in timeline.attempts:
        if attempt.currency is None:
            continue
        if currency is None:
            currency = attempt.currency
            continue
        require_same_currency(currency, attempt.currency)
    return currency


def captured_amount(timeline: OrderTimeline) -> int:
    """Money that actually arrived.

    Successful attempts are deduplicated by payment reference during
    reconstruction, so a redelivered capture cannot be counted twice.
    """
    return clamp_non_negative(timeline.captured_amount_minor)


def failed_amount(timeline: OrderTimeline) -> int:
    """Revenue this order failed to collect.

    An order-level figure, not an attempt-level sum: zero once the order is
    collected, the whole order amount while it is not.
    """
    if timeline.reached_terminal_success:
        return 0
    return clamp_non_negative(timeline.amount_minor)


def refunded_amount(timeline: OrderTimeline) -> int:
    """Money returned to the customer.

    Clamped at the captured amount — a refund larger than the capture is
    incoherent, and the clamp keeps a malformed payload from producing a
    negative outstanding balance.
    """
    total = 0
    for event in timeline.events:
        if event.event_type != EventType.REFUND_CREATED.value:
            continue
        amount = event.payload.get("amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int):
            continue
        total += amount

    return min(clamp_non_negative(total), captured_amount(timeline))


def recovered_amount(timeline: OrderTimeline) -> int:
    """Revenue collected after a recovery action was executed.

    Recognised only when the timeline holds both a successful recovery event
    and an actual capture. A recovery event alone proves an attempt was made,
    never that money moved — expected recovery must never be reported as actual
    recovery.
    """
    if not timeline.has_recovery_succeeded:
        return 0
    return captured_amount(timeline)


def outstanding_amount(timeline: OrderTimeline) -> int:
    """Order amount minus what was collected and kept."""
    kept = captured_amount(timeline) - refunded_amount(timeline)
    return clamp_non_negative(timeline.amount_minor - kept)


def money_breakdown(timeline: OrderTimeline) -> MoneyBreakdown:
    """Every figure for one order, computed once and consistently."""
    return MoneyBreakdown(
        currency=resolve_currency(timeline),
        order_amount=clamp_non_negative(timeline.amount_minor),
        captured=captured_amount(timeline),
        failed=failed_amount(timeline),
        refunded=refunded_amount(timeline),
        recovered=recovered_amount(timeline),
        outstanding=outstanding_amount(timeline),
    )


# -- amount at risk, per risk type ---------------------------------------


def amount_at_risk_repeated_failure(timeline: OrderTimeline) -> int:
    """The order amount, once.

    Never the sum of the failed attempts. See the module docstring.
    """
    if timeline.reached_terminal_success:
        return 0
    return clamp_non_negative(timeline.amount_minor)


def amount_at_risk_checkout_abandonment(timeline: OrderTimeline) -> int:
    """The order amount the customer walked away from."""
    if timeline.reached_terminal_success:
        return 0
    return clamp_non_negative(timeline.amount_minor)


def amount_at_risk_subscription(subscription: SubscriptionTimeline) -> int:
    """The value of the billing cycles that failed.

    Here a sum IS correct, and for the opposite reason: each failed cycle is a
    separate charge that did not happen, not repeated tries at one charge.
    """
    return clamp_non_negative(subscription.failed_amount_minor)


def amount_at_risk_reconciliation(timeline: OrderTimeline) -> int:
    """Always zero. The money arrived; only the bookkeeping is wrong."""
    return RECONCILIATION_AMOUNT_AT_RISK


__all__ = [
    "RECONCILIATION_AMOUNT_AT_RISK",
    "CurrencyMismatchError",
    "MoneyBreakdown",
    "amount_at_risk_checkout_abandonment",
    "amount_at_risk_reconciliation",
    "amount_at_risk_repeated_failure",
    "amount_at_risk_subscription",
    "captured_amount",
    "failed_amount",
    "money_breakdown",
    "outstanding_amount",
    "recovered_amount",
    "refunded_amount",
    "resolve_currency",
]
