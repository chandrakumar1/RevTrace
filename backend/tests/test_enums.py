"""Enum vocabularies must match the CHECK constraints exactly.

Python and the database drifting apart is the failure mode these tests exist to
catch: a value added to a StrEnum but not migrated into the CHECK constraint
produces an insert failure at runtime, in production, on a money path.
"""

from __future__ import annotations

import pytest

from app.models import Base
from app.models.enums import (
    EXECUTION_AUTHORIZED_ACTORS,
    ActionType,
    ActorType,
    EventType,
    ExecutionStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    PolicyStatus,
    RecoveryStrategy,
    RiskStatus,
    RiskType,
)

ALL_ENUMS = [
    ActionType,
    ActorType,
    EventType,
    ExecutionStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    PolicyStatus,
    RecoveryStrategy,
    RiskStatus,
    RiskType,
]

#: (table, column, enum) triples that are backed by a CHECK constraint.
CHECKED_COLUMNS = [
    ("orders", "status", OrderStatus),
    ("payment_attempts", "status", PaymentStatus),
    ("payment_attempts", "payment_method", PaymentMethod),
    ("events", "event_type", EventType),
    ("revenue_risks", "risk_type", RiskType),
    ("revenue_risks", "status", RiskStatus),
    ("recovery_cases", "strategy", RecoveryStrategy),
    ("recovery_cases", "policy_status", PolicyStatus),
    ("recovery_cases", "execution_status", ExecutionStatus),
    ("recovery_actions", "action_type", ActionType),
    ("audit_events", "actor", ActorType),
]


@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_enum_values_unique_and_non_empty(enum_cls: type) -> None:
    values = enum_cls.values()  # type: ignore[attr-defined]
    assert values, f"{enum_cls.__name__} has no members"
    assert len(set(values)) == len(values), f"{enum_cls.__name__} has duplicates"
    assert all(v and v == v.strip() for v in values)


@pytest.mark.parametrize(
    ("table_name", "column_name", "enum_cls"),
    CHECKED_COLUMNS,
    ids=[f"{t}.{c}" for t, c, _ in CHECKED_COLUMNS],
)
def test_check_constraint_matches_enum(table_name: str, column_name: str, enum_cls: type) -> None:
    """Every enum value must appear in the column's CHECK constraint text."""
    table = Base.metadata.tables[table_name]
    texts = [
        str(con.sqltext)
        for con in table.constraints
        if con.__class__.__name__ == "CheckConstraint" and column_name in str(con.sqltext)
    ]
    assert texts, f"no CHECK constraint found for {table_name}.{column_name}"
    combined = " ".join(texts)

    for value in enum_cls.values():  # type: ignore[attr-defined]
        assert f"'{value}'" in combined, (
            f"{enum_cls.__name__}.{value} missing from {table_name}.{column_name} CHECK"
        )


def test_all_four_specification_scenarios_present() -> None:
    """Scenarios A-D from the specification."""
    assert set(RiskType.values()) == {
        "repeated_payment_failure",  # A
        "checkout_abandonment",  # B
        "subscription_payment_failure",  # C
        "payment_degradation",  # D
    }


def test_ai_agent_is_not_an_execution_authorized_actor() -> None:
    """The authority boundary, at the vocabulary level."""
    assert ActorType.AI_AGENT not in EXECUTION_AUTHORIZED_ACTORS
    assert ActorType.ENGINE in EXECUTION_AUTHORIZED_ACTORS
    assert ActorType.HUMAN in EXECUTION_AUTHORIZED_ACTORS


def test_policy_status_supports_rejection_and_escalation() -> None:
    """Policy violations must be able to reject or escalate."""
    assert "rejected" in PolicyStatus.values()
    assert "escalated" in PolicyStatus.values()


def test_recovery_strategy_includes_no_action_and_escalation() -> None:
    """When uncertain or unsafe, stop or escalate — both must be representable."""
    assert "no_action" in RecoveryStrategy.values()
    assert "human_escalation" in RecoveryStrategy.values()
