"""Schema invariants, asserted against SQLAlchemy metadata.

No database connection is made. These tests guard the properties that are
expensive to fix once migrations exist in history — money typing, timezone
awareness, and the authority-boundary constraints.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.models import Base

EXPECTED_TABLES = {
    "merchants",
    "customers",
    "orders",
    "payment_attempts",
    "events",
    "revenue_risks",
    "recovery_cases",
    "recovery_actions",
    "audit_events",
}

#: Every column that holds money, in minor units.
MONEY_COLUMNS = {
    ("customers", "lifetime_value"),
    ("orders", "amount"),
    ("payment_attempts", "amount"),
    ("revenue_risks", "amount_at_risk"),
    ("recovery_cases", "expected_recovery"),
    ("recovery_cases", "max_cost"),
    ("recovery_cases", "estimated_cost"),
    ("recovery_cases", "net_expected_recovery"),
    ("recovery_cases", "actual_recovery"),
}

JSONB_COLUMNS = {
    ("events", "payload"),
    ("recovery_actions", "parameters"),
    ("recovery_actions", "result"),
    ("audit_events", "input_snapshot"),
    ("audit_events", "output_snapshot"),
}

#: Tables that are append-only and must therefore have no updated_at.
APPEND_ONLY_TABLES = {"events", "audit_events"}


def test_all_expected_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_primary_key_is_uuid() -> None:
    for name, table in Base.metadata.tables.items():
        pk_cols = list(table.primary_key.columns)
        assert len(pk_cols) == 1, f"{name} must have a single-column primary key"
        assert isinstance(pk_cols[0].type, PGUUID), f"{name}.id must be UUID"


@pytest.mark.parametrize(("table_name", "column_name"), sorted(MONEY_COLUMNS))
def test_money_columns_are_bigint(table_name: str, column_name: str) -> None:
    col = Base.metadata.tables[table_name].columns[column_name]
    assert isinstance(col.type, BigInteger), (
        f"{table_name}.{column_name} must be BigInteger (minor units)"
    )


def test_no_float_or_numeric_anywhere() -> None:
    """Money and confidence must never be float-typed (ADR 0001).

    Float arithmetic is not reproducible, and every downstream recovery
    calculation is deterministic code that must be exactly reproducible.
    """
    offenders = [
        f"{t}.{c.name}"
        for t, table in Base.metadata.tables.items()
        for c in table.columns
        if isinstance(c.type, (Float, Numeric))
    ]
    assert offenders == [], f"float/numeric columns found: {offenders}"


def test_confidence_stored_as_integer_basis_points() -> None:
    for table_name in ("revenue_risks", "recovery_cases"):
        cols = Base.metadata.tables[table_name].columns
        assert "confidence_bps" in cols
        assert not isinstance(cols["confidence_bps"].type, (Float, Numeric))


@pytest.mark.parametrize(("table_name", "column_name"), sorted(JSONB_COLUMNS))
def test_jsonb_columns(table_name: str, column_name: str) -> None:
    col = Base.metadata.tables[table_name].columns[column_name]
    assert isinstance(col.type, JSONB), f"{table_name}.{column_name} must be JSONB"


def test_all_datetimes_are_timezone_aware() -> None:
    """Timelines span delayed and out-of-order events; naive datetimes are unsafe."""
    offenders = [
        f"{t}.{c.name}"
        for t, table in Base.metadata.tables.items()
        for c in table.columns
        if isinstance(c.type, DateTime) and not c.type.timezone
    ]
    assert offenders == [], f"timezone-naive datetime columns: {offenders}"


@pytest.mark.parametrize("table_name", sorted(APPEND_ONLY_TABLES))
def test_append_only_tables_have_no_updated_at(table_name: str) -> None:
    """An audit trail that can be edited is not an audit trail."""
    assert "updated_at" not in Base.metadata.tables[table_name].columns
    assert "created_at" in Base.metadata.tables[table_name].columns


def test_events_idempotency_constraint_exists() -> None:
    """Duplicate webhook delivery must be rejected at the storage layer."""
    table = Base.metadata.tables["events"]
    unique_col_sets = {
        frozenset(c.name for c in con.columns)
        for con in table.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert frozenset({"merchant_id", "external_event_id"}) in unique_col_sets


def test_events_has_both_occurred_and_received_at() -> None:
    """Out-of-order detection needs both times."""
    cols = Base.metadata.tables["events"].columns
    assert "occurred_at" in cols
    assert "received_at" in cols
    assert not cols["occurred_at"].nullable
    assert not cols["received_at"].nullable


def _check_constraint_names(table_name: str) -> set[str]:
    return {
        con.name
        for con in Base.metadata.tables[table_name].constraints
        if con.__class__.__name__ == "CheckConstraint" and con.name
    }


class TestAuthorityBoundary:
    """The architecture rule, asserted at the schema level.

    These constraints exist so that the database refuses to store a
    contradiction regardless of what any caller — including a future agent —
    believes it is allowed to do.
    """

    def test_executed_action_requires_approval(self) -> None:
        assert any(
            "executed_requires_approved" in n for n in _check_constraint_names("recovery_actions")
        )

    def test_execution_requires_policy_approval(self) -> None:
        assert any(
            "execution_requires_policy_approval" in n
            for n in _check_constraint_names("recovery_cases")
        )

    def test_ai_agent_can_never_be_execution_actor(self) -> None:
        assert any("execution_actor_never_ai" in n for n in _check_constraint_names("audit_events"))

    def test_audit_events_has_is_execution_flag(self) -> None:
        col = Base.metadata.tables["audit_events"].columns["is_execution"]
        assert isinstance(col.type, Boolean)
        assert not col.nullable

    def test_policy_status_has_no_override_value(self) -> None:
        """Policy violations must reject or escalate, never silently override."""
        from app.models.enums import PolicyStatus

        assert "override" not in " ".join(PolicyStatus.values())
