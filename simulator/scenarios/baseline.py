"""Baseline scenarios — nothing is wrong.

These matter more than they look. Without a "nothing is wrong" floor, detection
precision is unmeasurable and every detector looks perfect. S02 in particular is
the false-positive guard: a single failure followed by organic success must NOT
be reported as a revenue leak.
"""

from __future__ import annotations

from app.models.enums import EventType, OrderStatus, PaymentMethod, PaymentStatus
from simulator.config import FAILURE_REASONS, FailureCode, ScenarioCategory
from simulator.entities import build_order, build_payment_attempt, with_status
from simulator.events import attempt_payload, order_payload
from simulator.models import EntitySet, ExpectedRisk, GroundTruth, SyntheticPaymentAttempt
from simulator.scenarios._common import EventLog, base_actors, retry_gap
from simulator.scenarios.base import BuildContext, ScenarioOutput, ScenarioSpec


def _successful_tail(
    ctx: BuildContext,
    log: EventLog,
    order,
    attempt: SyntheticPaymentAttempt,
    start_offset: int,
) -> int:
    """Emit authorized → captured → order.paid. Returns the final offset."""
    authorized_at = start_offset + 4
    captured_at = authorized_at + 6
    paid_at = captured_at + 2

    log.add(
        EventType.PAYMENT_AUTHORIZED,
        authorized_at,
        attempt_payload(attempt, order),
        customer_id=order.customer_id,
        order_id=order.id,
    )
    log.add(
        EventType.PAYMENT_CAPTURED,
        captured_at,
        attempt_payload(attempt, order),
        customer_id=order.customer_id,
        order_id=order.id,
    )
    paid_order = with_status(order, OrderStatus.PAID.value)
    log.add(
        EventType.ORDER_PAID,
        paid_at,
        order_payload(paid_order),
        customer_id=order.customer_id,
        order_id=order.id,
    )
    return paid_at


def build_healthy_payment(ctx: BuildContext) -> ScenarioOutput:
    """S01 — the happy path. Must produce zero detections."""
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
        status=PaymentStatus.CAPTURED.value,
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
    _successful_tail(ctx, log, order, attempt, 30)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(with_status(order, OrderStatus.PAID.value),),
            payment_attempts=(attempt,),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative="A single payment authorized and captured on the first attempt.",
        ),
    )


def build_failure_then_retry_success(ctx: BuildContext) -> ScenarioOutput:
    """S02 — one failure, then organic success. The false-positive guard."""
    merchant, customer, order = base_actors(ctx)
    log = EventLog(ctx, merchant)

    log.add(
        EventType.ORDER_CREATED,
        0,
        order_payload(order),
        customer_id=customer.id,
        order_id=order.id,
    )

    failed = build_payment_attempt(
        ctx.entity_rng,
        seed=ctx.seed,
        index=1,
        order=order,
        attempt_number=1,
        status=PaymentStatus.FAILED.value,
        attempted_at=ctx.clock.at(30),
        payment_method=PaymentMethod.CARD.value,
        failure_code=FailureCode.INSUFFICIENT_FUNDS.value,
        failure_reason=FAILURE_REASONS[FailureCode.INSUFFICIENT_FUNDS.value],
    )
    log.add(
        EventType.PAYMENT_ATTEMPTED,
        30,
        attempt_payload(failed, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.PAYMENT_FAILED,
        33,
        attempt_payload(failed, order),
        customer_id=customer.id,
        order_id=order.id,
    )

    second_offset = 33 + retry_gap(ctx)
    succeeded = build_payment_attempt(
        ctx.entity_rng,
        seed=ctx.seed,
        index=2,
        order=order,
        attempt_number=2,
        status=PaymentStatus.CAPTURED.value,
        attempted_at=ctx.clock.at(second_offset),
        payment_method=PaymentMethod.UPI.value,
    )
    log.add(
        EventType.PAYMENT_ATTEMPTED,
        second_offset,
        attempt_payload(succeeded, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    _successful_tail(ctx, log, order, succeeded, second_offset)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(with_status(order, OrderStatus.PAID.value),),
            payment_attempts=(failed, succeeded),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "One failed attempt followed by an organically successful retry. "
                "Revenue was never actually lost, so no risk should be reported."
            ),
        ),
    )


def build_multiple_attempts_eventual_success(ctx: BuildContext) -> ScenarioOutput:
    """S03 — three attempts on one order, the third succeeds."""
    merchant, customer, order = base_actors(ctx)
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
    failure_codes = (FailureCode.CARD_DECLINED, FailureCode.BANK_UNAVAILABLE)

    for number, code in enumerate(failure_codes, start=1):
        attempt = build_payment_attempt(
            ctx.entity_rng,
            seed=ctx.seed,
            index=number,
            order=order,
            attempt_number=number,
            status=PaymentStatus.FAILED.value,
            attempted_at=ctx.clock.at(offset),
            payment_method=PaymentMethod.CARD.value,
            failure_code=code.value,
            failure_reason=FAILURE_REASONS[code.value],
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

    final = build_payment_attempt(
        ctx.entity_rng,
        seed=ctx.seed,
        index=3,
        order=order,
        attempt_number=3,
        status=PaymentStatus.CAPTURED.value,
        attempted_at=ctx.clock.at(offset),
        payment_method=PaymentMethod.NETBANKING.value,
    )
    attempts.append(final)
    log.add(
        EventType.PAYMENT_ATTEMPTED,
        offset,
        attempt_payload(final, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    _successful_tail(ctx, log, order, final, offset)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(with_status(order, OrderStatus.PAID.value),),
            payment_attempts=tuple(attempts),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "Three attempts on one order; the third succeeds. Multiple failures "
                "alone are not a leak when the order is ultimately paid."
            ),
        ),
    )


def build_mixed_merchant_baseline(ctx: BuildContext) -> ScenarioOutput:
    """S14 — a merchant's ordinary week. Provides the historical baseline.

    Roughly 85% of orders succeed on the first attempt. This is what Scenario D
    (payment degradation) would eventually be measured against.
    """
    order_count = ctx.params.order_count or 20
    merchant, _, _ = base_actors(ctx)
    log = EventLog(ctx, merchant)

    customers = []
    orders = []
    attempts: list[SyntheticPaymentAttempt] = []
    risks: list[ExpectedRisk] = []

    from simulator.entities import build_customer

    offset = 0
    for index in range(1, order_count + 1):
        customer = build_customer(
            ctx.entity_rng, seed=ctx.seed, index=index, merchant_id=merchant.id
        )
        order = build_order(
            ctx.amount_rng,
            seed=ctx.seed,
            index=index,
            merchant_id=merchant.id,
            customer_id=customer.id,
            currency=ctx.currency,
        )
        customers.append(customer)

        log.add(
            EventType.ORDER_CREATED,
            offset,
            order_payload(order),
            customer_id=customer.id,
            order_id=order.id,
        )

        # Deterministic 85/15 split from the timing sub-stream.
        succeeds = ctx.timing_rng.randint(1, 100) <= 85
        attempt_offset = offset + 30

        if succeeds:
            attempt = build_payment_attempt(
                ctx.entity_rng,
                seed=ctx.seed,
                index=index,
                order=order,
                attempt_number=1,
                status=PaymentStatus.CAPTURED.value,
                attempted_at=ctx.clock.at(attempt_offset),
                payment_method=PaymentMethod.CARD.value,
            )
            log.add(
                EventType.PAYMENT_ATTEMPTED,
                attempt_offset,
                attempt_payload(attempt, order),
                customer_id=customer.id,
                order_id=order.id,
            )
            _successful_tail(ctx, log, order, attempt, attempt_offset)
            orders.append(with_status(order, OrderStatus.PAID.value))
        else:
            code = FailureCode.CARD_DECLINED
            attempt = build_payment_attempt(
                ctx.entity_rng,
                seed=ctx.seed,
                index=index,
                order=order,
                attempt_number=1,
                status=PaymentStatus.FAILED.value,
                attempted_at=ctx.clock.at(attempt_offset),
                payment_method=PaymentMethod.CARD.value,
                failure_code=code.value,
                failure_reason=FAILURE_REASONS[code.value],
            )
            log.add(
                EventType.PAYMENT_ATTEMPTED,
                attempt_offset,
                attempt_payload(attempt, order),
                customer_id=customer.id,
                order_id=order.id,
            )
            log.add(
                EventType.PAYMENT_FAILED,
                attempt_offset + 3,
                attempt_payload(attempt, order),
                customer_id=customer.id,
                order_id=order.id,
            )
            orders.append(with_status(order, OrderStatus.ATTEMPTED.value))

        attempts.append(attempt)
        offset += 3600

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=tuple(customers),
            orders=tuple(orders),
            payment_attempts=tuple(attempts),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            expected_risks=tuple(risks),  # single failures are not yet risks
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "An ordinary trading period with roughly an 85% first-attempt success "
                "rate. Provides the historical baseline that degradation is measured "
                "against. Single isolated failures are not classified as risks here."
            ),
        ),
    )


SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        id="S01",
        name="healthy_payment",
        category=ScenarioCategory.BASELINE,
        description="Order created, paid on the first attempt.",
        purpose="The happy path must produce zero detections.",
        builder=build_healthy_payment,
    ),
    ScenarioSpec(
        id="S02",
        name="failure_then_retry_success",
        category=ScenarioCategory.BASELINE,
        description="One failed attempt, then an organically successful retry.",
        purpose="False-positive guard: a recovered failure is not a leak.",
        builder=build_failure_then_retry_success,
    ),
    ScenarioSpec(
        id="S03",
        name="multiple_attempts_eventual_success",
        category=ScenarioCategory.BASELINE,
        description="Three attempts on one order; the third succeeds.",
        purpose="Multiple attempts alone do not constitute a revenue leak.",
        builder=build_multiple_attempts_eventual_success,
    ),
    ScenarioSpec(
        id="S14",
        name="mixed_merchant_baseline",
        category=ScenarioCategory.BASELINE,
        description="Twenty orders at roughly an 85% first-attempt success rate.",
        purpose="Historical baseline for degradation comparison and precision measurement.",
        builder=build_mixed_merchant_baseline,
    ),
)
