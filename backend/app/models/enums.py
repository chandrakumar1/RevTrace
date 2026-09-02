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
    """Revenue-risk classifications.

    The first four are the scenarios named in the specification. The vocabulary
    is additive: values are added when a detector needs one, which is exactly
    the growth path ADR 0003 chose VARCHAR + CHECK over a native ENUM for.
    """

    REPEATED_PAYMENT_FAILURE = "repeated_payment_failure"  # Scenario A
    CHECKOUT_ABANDONMENT = "checkout_abandonment"  # Scenario B
    SUBSCRIPTION_PAYMENT_FAILURE = "subscription_payment_failure"  # Scenario C
    PAYMENT_DEGRADATION = "payment_degradation"  # Scenario D

    #: Phase 3. Money was captured but the order never reconciled to paid.
    #: An integrity anomaly rather than lost revenue, so its amount_at_risk is
    #: always 0 — the captured funds did arrive. See ADR 0007.
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"


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


# -- incrementality ledger ------------------------------------------------
#
# Added for the revised roadmap. The vocabulary below exists so that a
# randomised holdout, its observation window, and the decision not to act are
# all first-class, constrained values rather than free text.


class ExperimentStatus(_RevTraceEnum):
    """Pre-registration lifecycle.

    `LOCKED` is the pre-registration moment: after it, the hypothesis, metrics,
    strata, holdout share, and power parameters are frozen. The point of
    recording it is that a result nobody could have re-specified after seeing
    the data is worth more than one that could.
    """

    DRAFT = "draft"
    LOCKED = "locked"
    RUNNING = "running"
    CLOSED = "closed"


#: The only legal forward transitions. There is deliberately no path back to
#: DRAFT: un-locking a pre-registration would destroy the guarantee it exists
#: to make.
EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: frozenset({ExperimentStatus.LOCKED}),
    ExperimentStatus.LOCKED: frozenset({ExperimentStatus.RUNNING, ExperimentStatus.CLOSED}),
    ExperimentStatus.RUNNING: frozenset({ExperimentStatus.CLOSED}),
    ExperimentStatus.CLOSED: frozenset(),
}


class Arm(_RevTraceEnum):
    """Randomised assignment. Assigned once, never changed.

    A case whose execution failed stays in TREATMENT — that is what
    intention-to-treat means, and silently reclassifying it as a control would
    inflate the measured effect.
    """

    TREATMENT = "treatment"
    HOLDOUT = "holdout"


class CaseDecision(_RevTraceEnum):
    """What the policy gate decided to do about a case.

    ABSTAIN is a real, audited outcome, not an absence of one.
    """

    ACT = "act"
    ABSTAIN = "abstain"
    ESCALATE = "escalate"


class AbstainReason(_RevTraceEnum):
    """Why acting was declined. Always recorded alongside the numbers."""

    NEGATIVE_UPLIFT = "negative_uplift"
    UPLIFT_NOT_SIGNIFICANT = "uplift_not_significant"
    NEGATIVE_NET_VALUE = "negative_net_value"
    SLEEPING_DOG = "sleeping_dog"
    SURE_THING = "sure_thing"
    LOST_CAUSE = "lost_cause"
    #: A significant positive lift (`ci_low > 0`) on customers whose control
    #: rate sits at or above the fold's self-recovery ceiling. Not an evidence
    #: failure — the effect is measured, positive, and excludes zero. A value
    #: judgement: most of what the action would be credited with was going to
    #: happen anyway, so acting buys recovery it did not cause.
    SELF_RECOVERY_LIKELY = "self_recovery_likely"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    CONTACT_BUDGET_EXHAUSTED = "contact_budget_exhausted"
    CUSTOMER_OPTED_OUT = "customer_opted_out"
    REGULATORY_BLOCK = "regulatory_block"
    HOLDOUT_ARM = "holdout_arm"


class DecisionType(_RevTraceEnum):
    """Which step of the pipeline an audit entry records.

    ABSTAIN sits alongside EXECUTE deliberately: the audit trail must be as
    rich for a non-action as for an action.
    """

    DETECT = "detect"
    ASSIGN = "assign"
    DIAGNOSE = "diagnose"
    RECOMMEND = "recommend"
    POLICY = "policy"
    ABSTAIN = "abstain"
    EXECUTE = "execute"
    VERIFY = "verify"
    SEAL = "seal"


class Quadrant(_RevTraceEnum):
    """Uplift segmentation.

    GRAY_ZONE is the honest default: below a minimum sample per stratum, a case
    gets no confident label and becomes eligible for the exploration budget
    instead of being acted on as though its uplift were known.
    """

    PERSUADABLE = "persuadable"
    SURE_THING = "sure_thing"
    LOST_CAUSE = "lost_cause"
    SLEEPING_DOG = "sleeping_dog"
    GRAY_ZONE = "gray_zone"


class InterventionChannel(_RevTraceEnum):
    PAYMENT_LINK = "payment_link"
    RETRY = "retry"
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    NONE = "none"
