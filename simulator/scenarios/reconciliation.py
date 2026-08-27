"""Reconciliation and recovery-history scenarios.

Two boundaries are enforced here.

**S10 has no RiskType.** The Phase 1 `RiskType` vocabulary has four values and
none describes "money moved but the order never reconciled". Rather than adding
one — which would mean a Phase 1 schema change — S10 records an untyped
*anomaly*. Phase 3 decides how to classify and detect it.

**S11 and S12 emit recovery.* events only** (ADR 0005). The simulator writes no
recovery_cases, recovery_actions, revenue_risks, or audit_events. It records
that a recovery action occurred in this synthetic history; it never fabricates
an approval, a policy decision, or an execution authorization. That authority
belongs to the policy engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import (
    ActionType,
    EventType,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    RiskType,
)
from simulator.config import FAILURE_REASONS, FailureCode, ScenarioCategory
from simulator.entities import build_payment_attempt, external_ref, with_status
from simulator.events import attempt_payload, order_payload, recovery_payload
from simulator.models import (
    EntitySet,
    ExpectedAnomaly,
    ExpectedRisk,
    GroundTruth,
    SyntheticCustomer,
    SyntheticMerchant,
    SyntheticOrder,
    SyntheticPaymentAttempt,
)
from simulator.scenarios._common import EventLog, base_actors, retry_gap
from simulator.scenarios.base import BuildContext, ScenarioOutput, ScenarioSpec


def build_payment_captured_order_not_reconciled(ctx: BuildContext) -> ScenarioOutput:
    """S10 — payment captured, order.paid never arrives.

    Every event that occurred was delivered. The terminal reconciliation event
    genuinely never happened: money moved, and the order never closed.
    """
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
    # No ORDER_PAID. The order is stuck at ATTEMPTED despite a captured payment.

    stuck = with_status(order, OrderStatus.ATTEMPTED.value)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(merchant,),
            customers=(customer,),
            orders=(stuck,),
            payment_attempts=(attempt,),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            expected_anomalies=(
                ExpectedAnomaly(
                    anomaly_kind="payment_captured_order_not_reconciled",
                    order_ref=order.external_order_id,
                    reason=(
                        "payment.captured occurred but order.paid never did; the "
                        "order remains in status 'attempted' while funds were taken."
                    ),
                ),
            ),
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "The payment was captured successfully but the order never "
                "reconciled to paid. This is an inconsistency rather than a lost "
                "sale, and the Phase 1 RiskType vocabulary has no value for it — it "
                "is recorded as an untyped anomaly for Phase 3 to classify."
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class _RecoveryPrelude:
    """The shared opening of S11 and S12."""

    log: EventLog
    merchant: SyntheticMerchant
    customer: SyntheticCustomer
    order: SyntheticOrder
    attempts: tuple[SyntheticPaymentAttempt, ...]
    action_offset: int
    action_ref: str


def _failed_then_recovery(ctx: BuildContext) -> _RecoveryPrelude:
    """Shared prelude: two failures, then a recovery action is executed."""
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
    for number in range(1, 3):
        code = FailureCode.CARD_DECLINED
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

    action_offset = offset + 3600
    action_ref = external_ref("act", ctx.seed, 1)
    log.add(
        EventType.RECOVERY_ACTION_EXECUTED,
        action_offset,
        recovery_payload(
            order,
            action_ref=action_ref,
            action_type=ActionType.CREATE_PAYMENT_LINK.value,
        ),
        customer_id=customer.id,
        order_id=order.id,
    )

    return _RecoveryPrelude(
        log=log,
        merchant=merchant,
        customer=customer,
        order=order,
        attempts=tuple(attempts),
        action_offset=action_offset,
        action_ref=action_ref,
    )


def build_recovery_success(ctx: BuildContext) -> ScenarioOutput:
    """S11 — a recovery action was executed and the payment then succeeded."""
    prelude = _failed_then_recovery(ctx)
    log, order, customer = prelude.log, prelude.order, prelude.customer

    recovered_offset = prelude.action_offset + 900
    final = build_payment_attempt(
        ctx.entity_rng,
        seed=ctx.seed,
        index=3,
        order=order,
        attempt_number=3,
        status=PaymentStatus.CAPTURED.value,
        attempted_at=ctx.clock.at(recovered_offset),
        payment_method=PaymentMethod.UPI.value,
    )
    log.add(
        EventType.PAYMENT_ATTEMPTED,
        recovered_offset,
        attempt_payload(final, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.PAYMENT_CAPTURED,
        recovered_offset + 6,
        attempt_payload(final, order),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.RECOVERY_SUCCEEDED,
        recovered_offset + 8,
        recovery_payload(
            order,
            action_ref=prelude.action_ref,
            action_type=ActionType.CREATE_PAYMENT_LINK.value,
        ),
        customer_id=customer.id,
        order_id=order.id,
    )
    log.add(
        EventType.ORDER_PAID,
        recovered_offset + 10,
        order_payload(with_status(order, OrderStatus.PAID.value)),
        customer_id=customer.id,
        order_id=order.id,
    )

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(prelude.merchant,),
            customers=(customer,),
            orders=(with_status(order, OrderStatus.PAID.value),),
            payment_attempts=(*prelude.attempts, final),
        ),
        events=log.as_tuple(),
        ground_truth=GroundTruth(
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "Two failures, a payment-link recovery action, then a successful "
                "payment. Revenue at risk was ultimately recovered. These are "
                "historical recovery events only — no recovery case, action record, "
                "or approval is fabricated."
            ),
        ),
    )


def build_recovery_failure(ctx: BuildContext) -> ScenarioOutput:
    """S12 — a recovery action was executed and still did not recover the revenue."""
    prelude = _failed_then_recovery(ctx)
    log, order, customer = prelude.log, prelude.order, prelude.customer

    log.add(
        EventType.RECOVERY_FAILED,
        prelude.action_offset + 86_400,
        recovery_payload(
            order,
            action_ref=prelude.action_ref,
            action_type=ActionType.CREATE_PAYMENT_LINK.value,
        ),
        customer_id=customer.id,
        order_id=order.id,
    )

    unpaid = with_status(order, OrderStatus.ATTEMPTED.value)

    return ScenarioOutput(
        entities=EntitySet(
            merchants=(prelude.merchant,),
            customers=(customer,),
            orders=(unpaid,),
            payment_attempts=prelude.attempts,
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
                        "Two failed attempts and a recovery action that did not "
                        "result in payment; revenue remains lost."
                    ),
                ),
            ),
            emitted_event_count=len(log.events),
            expected_persisted_event_count=len(log.events),
            narrative=(
                "Two failures, a payment-link recovery action, and no payment ever "
                "followed. The revenue stays lost, and expected recovery should not "
                "be counted as actual recovery."
            ),
        ),
    )


SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        id="S10",
        name="payment_captured_order_not_reconciled",
        category=ScenarioCategory.RECONCILIATION,
        description="Payment captured but order.paid never arrives.",
        purpose="Untyped anomaly — no Phase 1 RiskType describes it.",
        builder=build_payment_captured_order_not_reconciled,
    ),
    ScenarioSpec(
        id="S11",
        name="recovery_success",
        category=ScenarioCategory.RECOVERY,
        description="Failures, a recovery action, then a successful payment.",
        purpose="Historical recovery-success shape for Phase 11 evaluation.",
        builder=build_recovery_success,
    ),
    ScenarioSpec(
        id="S12",
        name="recovery_failure",
        category=ScenarioCategory.RECOVERY,
        description="Failures, a recovery action, and no payment ever follows.",
        purpose="Expected recovery must never be reported as actual recovery.",
        builder=build_recovery_failure,
    ),
)
