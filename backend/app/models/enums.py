"""Controlled vocabularies for status and type columns.

Stored as VARCHAR with a CHECK constraint rather than a native PostgreSQL ENUM
(ADR 0003): these vocabularies grow every phase, and ALTER TYPE is materially
harder to migrate than dropping and recreating a CHECK.

`values_for_check()` is the single source of truth shared by the model
definitions and the tests, so Python and the database cannot drift apart.
"""

from __future__ import annotations

from enum import StrEnum


class _RevTraceEnum(StrEnum):
    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)


class OrderStatus(_RevTraceEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(_RevTraceEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    TIMEOUT = "timeout"


class PaymentMethod(_RevTraceEnum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"
    UNKNOWN = "unknown"


class EventType(_RevTraceEnum):
    """Event vocabulary for the revenue leak graph.

    Provider-neutral by design: Razorpay webhook names are translated into
    these by integrations/razorpay/mapper.py (Phase 8), so the leak graph never
    depends on one provider's naming.
    """

    CHECKOUT_STARTED = "checkout.started"
    CHECKOUT_ABANDONED = "checkout.abandoned"
    ORDER_CREATED = "order.created"
    ORDER_PAID = "order.paid"
    PAYMENT_ATTEMPTED = "payment.attempted"
    PAYMENT_AUTHORIZED = "payment.authorized"
    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_CHARGED = "subscription.charged"
    SUBSCRIPTION_PAYMENT_FAILED = "subscription.payment_failed"
    SUBSCRIPTION_HALTED = "subscription.halted"
    REFUND_CREATED = "refund.created"
    RECOVERY_ACTION_EXECUTED = "recovery.action_executed"
    RECOVERY_SUCCEEDED = "recovery.succeeded"
    RECOVERY_FAILED = "recovery.failed"


class RiskType(_RevTraceEnum):
    """The four scenarios named in the specification."""

    REPEATED_PAYMENT_FAILURE = "repeated_payment_failure"  # Scenario A
    CHECKOUT_ABANDONMENT = "checkout_abandonment"  # Scenario B
    SUBSCRIPTION_PAYMENT_FAILURE = "subscription_payment_failure"  # Scenario C
    PAYMENT_DEGRADATION = "payment_degradation"  # Scenario D


class RiskStatus(_RevTraceEnum):
    DETECTED = "detected"
    UNDER_INVESTIGATION = "under_investigation"
    RECOVERY_IN_PROGRESS = "recovery_in_progress"
    RECOVERED = "recovered"
    UNRECOVERABLE = "unrecoverable"
    FALSE_POSITIVE = "false_positive"
    EXPIRED = "expired"


class RecoveryStrategy(_RevTraceEnum):
    NO_ACTION = "no_action"
    RETRY_PAYMENT = "retry_payment"
    PAYMENT_LINK = "payment_link"
    CUSTOMER_NOTIFICATION = "customer_notification"
    ALTERNATE_METHOD_PROMPT = "alternate_method_prompt"
    DISCOUNT_OFFER = "discount_offer"
    HUMAN_ESCALATION = "human_escalation"


class PolicyStatus(_RevTraceEnum):
    """Outcome of the deterministic policy gate.

    A policy violation must produce REJECTED or ESCALATED. There is
    deliberately no value meaning "overridden" — silent override is forbidden.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class ExecutionStatus(_RevTraceEnum):
    NOT_STARTED = "not_started"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    EXECUTED = "executed"
    VERIFIED = "verified"
    FAILED = "failed"
    ABORTED = "aborted"


class ActionType(_RevTraceEnum):
    RETRY_PAYMENT = "retry_payment"
    CREATE_PAYMENT_LINK = "create_payment_link"
    SEND_NOTIFICATION = "send_notification"
    APPLY_DISCOUNT = "apply_discount"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class ActorType(_RevTraceEnum):
    """Who took an audited action.

    The distinction between AI_AGENT and ENGINE is the audit-trail expression
    of the authority boundary: an AI_AGENT actor may only ever appear on
    recommendation entries, never on execution entries.
    """

    SYSTEM = "system"
    ENGINE = "engine"
    AI_AGENT = "ai_agent"
    HUMAN = "human"
    WEBHOOK = "webhook"
    SIMULATOR = "simulator"


#: Actors permitted to appear on an execution-class audit entry.
#: AI_AGENT is intentionally absent — the LLM never executes.
EXECUTION_AUTHORIZED_ACTORS: frozenset[ActorType] = frozenset(
    {ActorType.ENGINE, ActorType.HUMAN, ActorType.SYSTEM}
)
