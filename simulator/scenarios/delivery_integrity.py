"""Delivery-integrity scenarios.

The specification is explicit: never assume a webhook is delivered exactly once.
These scenarios corrupt delivery while leaving causal truth intact.

Every one of them has the SAME expected detection outcome as its clean
counterpart. That equality is the entire assertion — pathological delivery must
not change what RevTrace concludes.
"""

from __future__ import annotations

from simulator.config import LONG_DELIVERY_DELAY_SECONDS, ScenarioCategory
from simulator.delivery import DeliveryPlan
from simulator.models import GroundTruth
from simulator.scenarios.base import BuildContext, ScenarioOutput, ScenarioSpec
from simulator.scenarios.baseline import build_healthy_payment
from simulator.scenarios.leak import build_repeated_payment_failure


def _with_ground_truth(base: ScenarioOutput, **overrides: object) -> GroundTruth:
    current = base.ground_truth
    fields = {
        "expected_risks": current.expected_risks,
        "expected_anomalies": current.expected_anomalies,
        "emitted_event_count": current.emitted_event_count,
        "expected_persisted_event_count": current.expected_persisted_event_count,
        "dropped_events": current.dropped_events,
        "duplicated_events": current.duplicated_events,
        "narrative": current.narrative,
    }
    fields.update(overrides)
    return GroundTruth(**fields)  # type: ignore[arg-type]


def build_duplicate_webhook_delivery(ctx: BuildContext) -> ScenarioOutput:
    """S07 — two events redelivered under the same external_event_id.

    The simulator emits the duplicates. It never deduplicates them: suppression
    is the database's job via UNIQUE(merchant_id, external_event_id), and that
    is precisely the behaviour Phase 3 needs to demonstrate.
    """
    base = build_healthy_payment(ctx)
    events = base.events

    duplicate_count = ctx.params.duplicate_count or 1
    targets = {1: duplicate_count, 3: duplicate_count}  # attempted, captured

    unique_count = len(events)
    extra = sum(targets.values())

    return ScenarioOutput(
        entities=base.entities,
        events=events,
        delivery_plan=DeliveryPlan(duplicates=targets),
        ground_truth=_with_ground_truth(
            base,
            emitted_event_count=unique_count + extra,
            expected_persisted_event_count=unique_count,
            duplicated_events=tuple(events[index].external_event_id for index in sorted(targets)),
            narrative=(
                "A healthy payment whose payment.attempted and payment.captured "
                "webhooks are each delivered twice. Ingestion must persist each "
                "event once; revenue must not be double-counted."
            ),
        ),
    )


def build_out_of_order_delivery(ctx: BuildContext) -> ScenarioOutput:
    """S08 — arrival order reversed; occurred_at untouched."""
    base = build_repeated_payment_failure(ctx)
    events = base.events

    # Deliver the whole stream backwards. Causal times are unchanged, so
    # sorting by occurred_at must restore the true timeline exactly.
    reorder = tuple(reversed(range(len(events))))

    return ScenarioOutput(
        entities=base.entities,
        events=events,
        delivery_plan=DeliveryPlan(reorder=reorder),
        ground_truth=_with_ground_truth(
            base,
            narrative=(
                "The repeated-failure history arrives in exactly reverse order. "
                "occurred_at still carries the truth, so the reconstructed timeline "
                "and the detected risk must be identical to S04."
            ),
        ),
    )


def build_delayed_event_arrival(ctx: BuildContext) -> ScenarioOutput:
    """S09 — one failure event arrives six hours late."""
    base = build_repeated_payment_failure(ctx)
    events = base.events

    delay = ctx.params.delay_seconds or LONG_DELIVERY_DELAY_SECONDS
    # Index 2 is the first payment.failed in the repeated-failure shape.
    target = 2 if len(events) > 2 else len(events) - 1

    return ScenarioOutput(
        entities=base.entities,
        events=events,
        delivery_plan=DeliveryPlan(delays={target: delay}),
        ground_truth=_with_ground_truth(
            base,
            narrative=(
                f"One payment.failed webhook arrives {delay} seconds after it "
                "occurred. The event is late, not wrong: the conclusion must match S04."
            ),
        ),
    )


def build_missing_event(ctx: BuildContext) -> ScenarioOutput:
    """S12b — an intermediate event is generated but never delivered."""
    base = build_repeated_payment_failure(ctx)
    events = base.events

    # Drop the second payment.attempted: the attempt happened, we never heard.
    target = 3 if len(events) > 3 else len(events) - 1
    dropped_id = events[target].external_event_id

    return ScenarioOutput(
        entities=base.entities,
        events=events,
        delivery_plan=DeliveryPlan(drops=frozenset({target})),
        ground_truth=_with_ground_truth(
            base,
            emitted_event_count=len(events) - 1,
            expected_persisted_event_count=len(events) - 1,
            dropped_events=(dropped_id,),
            narrative=(
                "One payment.attempted webhook is never delivered. Evidence is "
                "incomplete, but the remaining failures still justify the same "
                "conclusion — detection must degrade gracefully rather than go silent."
            ),
        ),
    )


SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        id="S07",
        name="duplicate_webhook_delivery",
        category=ScenarioCategory.DELIVERY_INTEGRITY,
        description="Two events each delivered twice under one external_event_id.",
        purpose="Idempotency: duplicates must be emitted, then rejected by storage.",
        builder=build_duplicate_webhook_delivery,
    ),
    ScenarioSpec(
        id="S08",
        name="out_of_order_delivery",
        category=ScenarioCategory.DELIVERY_INTEGRITY,
        description="The full event history arrives in reverse order.",
        purpose="Timelines rebuild from occurred_at, never from arrival order.",
        builder=build_out_of_order_delivery,
    ),
    ScenarioSpec(
        id="S09",
        name="delayed_event_arrival",
        category=ScenarioCategory.DELIVERY_INTEGRITY,
        description="One failure event arrives six hours late.",
        purpose="A late event is not a wrong event.",
        builder=build_delayed_event_arrival,
    ),
    ScenarioSpec(
        id="S12b",
        name="missing_event",
        category=ScenarioCategory.DELIVERY_INTEGRITY,
        description="One intermediate event is never delivered.",
        purpose="Detection must degrade gracefully on incomplete evidence.",
        builder=build_missing_event,
    ),
)
