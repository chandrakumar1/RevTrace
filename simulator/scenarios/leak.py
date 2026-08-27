"""Revenue-leak scenarios — money that was genuinely lost.

Each maps to a scenario named in the specification, and each carries ground
truth stating what a correct Phase 3 detector should find. The simulator states
*what should be detected*; it never states a score, confidence, or
recommendation.
"""

from __future__ import annotations

from app.models.enums import (
    EventType,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    RiskType,
)
from simulator.config import (
    FAILURE_REASONS,
    HIGH_LIFETIME_VALUE_PAISE,
    HIGH_VALUE_ORDER_PAISE,
    SUBSCRIPTION_CHARGE_PAISE,
    SUBSCRIPTION_CYCLE_SECONDS,
    TYPICAL_ORDER_PAISE,
    FailureCode,
    ScenarioCategory,
)
from simulator.entities import build_payment_attempt, external_ref, with_status
from simulator.events import (
    attempt_payload,
    checkout_payload,
    order_payload,
    refund_payload,
    subscription_payload,
)
from simulator.models import EntitySet, ExpectedRisk, GroundTruth, SyntheticPaymentAttempt
from simulator.scenarios._common import EventLog, base_actors, retry_gap
from simulator.scenarios.base import BuildContext, ScenarioOutput, ScenarioSpec


def _repeated_failure(
    ctx: BuildContext,
    *,
    attempt_count: int,
    method: str,
    failure_code: FailureCode,
    failed_status: str = PaymentStatus.FAILED.value,
    amount_range: tuple[int, int, int] | None = None,
    lifetime_value_range: tuple[int, int, int] | None = None,
    narrative: str,
) -> ScenarioOutput:
    """Shared shape for Scenario A and its variants."""
    merchant, customer, order = base_actors(
        ctx,
        amount_range=amount_range or TYPICAL_ORDER_PAISE,
        lifetime_value_range=lifetime_value_range,
    )
    log = EventLog(ctx, merchant)

    log.add(
        EventType.ORDER_CREATED,
        0,
        order_payload(order),
        customer_id=customer.id,
        order_id=order.id,
    )

    attempts: list[SyntheticPaymentAttempt] = []
    offset = 30

    for number in range(1, attempt_count + 1):
        attempt = build_payment_attempt(
            ctx.entity_rng,
            seed=ctx.seed,
            index=number,
            order=order,
            attempt_number=number,
            status=failed_status,
            attempted_at=ctx.clock.at(offset),
            payment_method=method,
            failure_code=failure_code.value,
            failure_reason=FAILURE_REASONS[failure_code.value],
        )
        attempts.append(attempt)
        log.add(
            EventType.PAYMENT_ATTEMPTED,
            offset,
            attempt_payload(attempt, order),
            customer_id=customer.id,
            order_id=order.id,
        )
        log.add(
            EventType.PAYMENT_FAILED,
            offset + 3,
            attempt_payload(attempt, order),
            customer_id=customer.id,
            order_id=order.id,
        )
        offset = offset + 3 + retry_gap(ctx)

    unpaid = with_status(order, OrderStatus.ATTEMPTED.value)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(unpaid,),
            payment_attempts=tuple(attempts),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            expected_risks=(
                ExpectedRisk(
                    risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                    amount_at_risk=order.amount,
                    currency=order.currency,
                    order_ref=order.external_order_id,
                    reason=(
                        f"{attempt_count} consecutive failed attempts with no successful "
                        f"payment; failure_code={failure_code.value}"
                    ),
                ),
            ),
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=narrative,
        ),
    )


def build_repeated_payment_failure(ctx: BuildContext) -> ScenarioOutput:
    """S04 — specification Scenario A."""
    return _repeated_failure(
        ctx,
        attempt_count=ctx.params.attempt_count or 3,
        method=PaymentMethod.CARD.value,
        failure_code=FailureCode.CARD_DECLINED,
        narrative=(
            "Repeated card declines with no successful payment. The full order amount is at risk."
        ),
    )


def build_high_value_repeated_failure(ctx: BuildContext) -> ScenarioOutput:
    """S04b — Scenario A on a high-value order from a high-LTV customer."""
    return _repeated_failure(
        ctx,
        attempt_count=ctx.params.attempt_count or 3,
        method=PaymentMethod.CARD.value,
        failure_code=FailureCode.INSUFFICIENT_FUNDS,
        amount_range=HIGH_VALUE_ORDER_PAISE,
        lifetime_value_range=HIGH_LIFETIME_VALUE_PAISE,
        narrative=(
            "The same failure shape as S04 but on a high-value order from a "
            "high-lifetime-value customer. Prioritisation must be amount-aware."
        ),
    )


def build_upi_timeout_failures(ctx: BuildContext) -> ScenarioOutput:
    """S04c — UPI-like timeouts rather than hard declines."""
    return _repeated_failure(
        ctx,
        attempt_count=ctx.params.attempt_count or 2,
        method=PaymentMethod.UPI.value,
        failure_code=FailureCode.GATEWAY_TIMEOUT,
        failed_status=PaymentStatus.TIMEOUT.value,
        narrative=(
            "UPI attempts that time out rather than being declined. A timeout is "
            "a different diagnosis from a hard decline and may warrant a different "
            "recovery strategy."
        ),
    )


def build_checkout_abandonment(ctx: BuildContext) -> ScenarioOutput:
    """S05 — specification Scenario B. Checkout starts, payment never attempted."""
    merchant, customer, order = base_actors(ctx)
    log = EventLog(ctx, merchant)

    session_ref = external_ref("sess", ctx.seed, 1)

    log.add(
        EventType.CHECKOUT_STARTED,
        0,
        checkout_payload(order, session_ref=session_ref),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.ORDER_CREATED,
        5,
        order_payload(order),
        customer_id=customer.id,
        order_id=order.id,
    )
    # Silence. No payment.attempted ever occurs.
    log.add(
        EventType.CHECKOUT_ABANDONED,
        1800,
        checkout_payload(order, session_ref=session_ref),
        customer_id=customer.id,
        order_id=order.id,
    )

    abandoned = with_status(order, OrderStatus.ABANDONED.value)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(abandoned,),
            payment_attempts=(),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            expected_risks=(
                ExpectedRisk(
                    risk_type=RiskType.CHECKOUT_ABANDONMENT.value,
                    amount_at_risk=order.amount,
                    currency=order.currency,
                    order_ref=order.external_order_id,
                    reason="Checkout started and abandoned with no payment attempt.",
                ),
            ),
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "The customer began checkout and left without ever attempting "
                "payment. There are no payment attempts to analyse — the absence "
                "of events is the signal."
            ),
        ),
    )


def build_subscription_payment_failure(ctx: BuildContext) -> ScenarioOutput:
    """S06 — specification Scenario C. Recurring revenue stops."""
    merchant, customer, _ = base_actors(ctx)
    log = EventLog(ctx, merchant)

    low, high, step = SUBSCRIPTION_CHARGE_PAISE
    charge_minor = ctx.amount_rng.randrange(low, high, step)
    subscription_ref = external_ref("sub", ctx.seed, 1)

    offset = 0
    for cycle in (1, 2):
        log.add(
            EventType.SUBSCRIPTION_CHARGED,
            offset,
            subscription_payload(
                subscription_ref=subscription_ref,
                amount_minor=charge_minor,
                currency=ctx.currency,
                cycle=cycle,
            ),
            customer_id=customer.id,
        )
        offset += SUBSCRIPTION_CYCLE_SECONDS

    code = FailureCode.INSUFFICIENT_FUNDS
    for cycle in (3, 4):
        log.add(
            EventType.SUBSCRIPTION_PAYMENT_FAILED,
            offset,
            subscription_payload(
                subscription_ref=subscription_ref,
                amount_minor=charge_minor,
                currency=ctx.currency,
                cycle=cycle,
                failure_code=code.value,
                failure_reason=FAILURE_REASONS[code.value],
            ),
            customer_id=customer.id,
        )
        offset += SUBSCRIPTION_CYCLE_SECONDS

    log.add(
        EventType.SUBSCRIPTION_HALTED,
        offset,
        subscription_payload(
            subscription_ref=subscription_ref,
            amount_minor=charge_minor,
            currency=ctx.currency,
            cycle=5,
        ),
        customer_id=customer.id,
    )

    return ScenarioOutput(
        entities=EntitySet(merchants=(merchant,), customers=(customer,)),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            expected_risks=(
                ExpectedRisk(
                    risk_type=RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
                    amount_at_risk=charge_minor * 2,
                    currency=ctx.currency,
                    order_ref=None,
                    reason=(
                        "Two consecutive failed billing cycles followed by the "
                        "subscription being halted."
                    ),
                ),
            ),
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "Two healthy billing cycles, then two consecutive failures, then "
                "the subscription halts. Recurring revenue has stopped; the amount "
                "at risk is the value of the failed cycles."
            ),
        ),
    )


def build_refund_after_capture(ctx: BuildContext) -> ScenarioOutput:
    """S13 — a legitimate full refund. Deliberately NOT a revenue leak."""
    merchant, customer, order = base_actors(ctx)
    log = EventLog(ctx, merchant)

    log.add(
        EventType.ORDER_CREATED,
        0,
        order_payload(order),
        customer_id=customer.id,
        order_id=order.id,
    )
    attempt = build_payment_attempt(
        ctx.entity_rng,
        seed=ctx.seed,
        index=1,
        order=order,
        attempt_number=1,
        status=PaymentStatus.REFUNDED.value,
        attempted_at=ctx.clock.at(30),
        payment_method=PaymentMethod.CARD.value,
    )
    log.add(
        EventType.PAYMENT_ATTEMPTED,
        30,
        attempt_payload(attempt, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.PAYMENT_AUTHORIZED,
        34,
        attempt_payload(attempt, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.PAYMENT_CAPTURED,
        40,
        attempt_payload(attempt, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.ORDER_PAID,
        42,
        order_payload(with_status(order, OrderStatus.PAID.value)),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.REFUND_CREATED,
        86_400,
        refund_payload(
            order,
            refund_ref=external_ref("rfnd", ctx.seed, 1),
            amount_minor=order.amount,
        ),
        customer_id=customer.id,
        order_id=order.id,
    )

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(with_status(order, OrderStatus.REFUNDED.value),),
            payment_attempts=(attempt,),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "A payment captured normally and refunded a day later at the "
                "customer's request. A refund is not a revenue leak, and a detector "
                "that flags this one is wrong."
            ),
        ),
    )


SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        id="S04",
        name="repeated_payment_failure",
        category=ScenarioCategory.LEAK,
        description="Three consecutive card declines, order never paid.",
        purpose="Specification Scenario A — the primary detectable leak.",
        builder=build_repeated_payment_failure,
    ),
    ScenarioSpec(
        id="S04b",
        name="high_value_repeated_failure",
        category=ScenarioCategory.LEAK,
        description="Scenario A on a high-value order from a high-LTV customer.",
        purpose="Prioritisation must be amount-aware, not just count-aware.",
        builder=build_high_value_repeated_failure,
    ),
    ScenarioSpec(
        id="S04c",
        name="upi_timeout_failures",
        category=ScenarioCategory.LEAK,
        description="UPI attempts that time out rather than decline.",
        purpose="A timeout is a different diagnosis from a hard decline.",
        builder=build_upi_timeout_failures,
    ),
    ScenarioSpec(
        id="S05",
        name="checkout_abandonment",
        category=ScenarioCategory.LEAK,
        description="Checkout started and abandoned; payment never attempted.",
        purpose="Specification Scenario B — absence of events is the signal.",
        builder=build_checkout_abandonment,
    ),
    ScenarioSpec(
        id="S06",
        name="subscription_payment_failure",
        category=ScenarioCategory.LEAK,
        description="Two healthy cycles, two failures, then halted.",
        purpose="Specification Scenario C — recurring revenue stops.",
        builder=build_subscription_payment_failure,
    ),
    ScenarioSpec(
        id="S13",
        name="refund_after_capture",
        category=ScenarioCategory.LEAK,
        description="A captured payment fully refunded a day later.",
        purpose="Negative case: a refund must not be mistaken for a leak.",
        builder=build_refund_after_capture,
    ),
)
