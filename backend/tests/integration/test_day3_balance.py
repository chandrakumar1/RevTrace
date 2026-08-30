"""Balance diagnostics against revtrace_test.

What only a real database can demonstrate: that the report loads the whole
randomised population and not a filtered slice of it, that `payment_method` is
resolved from the attempt that actually happened, that a risk with no attempt
is reported missing rather than dropped, and that computing a balance table
writes nothing at all.

The last one is the point. A diagnostic that could also act would be a
diagnostic no one could trust.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.causal.balance import (
    BALANCE_COVARIATES,
    MISSING_LEVEL,
    covariate_rows,
    report_for_experiment,
)
from app.experiments.assignment import assign_risk
from app.experiments.registry import (
    ExperimentDraft,
    create_draft,
    lock_experiment,
    start_experiment,
)
from app.models import (
    AuditEvent,
    CaseAssignment,
    CaseOutcome,
    Order,
    PaymentAttempt,
    RecoveryAction,
    RecoveryCase,
    RevenueRisk,
)
from app.models.enums import Arm, PaymentMethod, RiskType

pytestmark = pytest.mark.db

LOCKED_AT = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
STARTED_AT = LOCKED_AT + timedelta(hours=1)
AS_OF = STARTED_AT + timedelta(hours=1)
SALT = "revtrace-demo-salt-v1"


def a_running_experiment(session: Session, holdout_bps: int = 5_000):  # noqa: ANN201
    experiment = create_draft(
        session,
        ExperimentDraft(
            name=f"EXP-{uuid.uuid4().hex[:8]}",
            hypothesis="Intervention increases recovery within the window.",
            primary_metric="recovery_rate",
            holdout_bps=holdout_bps,
            planned_n_per_arm=384,
            mde_bps=1_000,
        ),
    )
    lock_experiment(session, experiment, LOCKED_AT)
    start_experiment(session, experiment, STARTED_AT)
    return experiment


def a_merchant(session: Session) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO merchants (id, name, currency, timezone, created_at, updated_at) "
            "VALUES (:id, 'Acme', 'INR', 'Asia/Kolkata', now(), now())"
        ),
        {"id": merchant_id},
    )
    session.flush()
    return merchant_id


def an_order(session: Session, merchant_id: uuid.UUID, *, amount: int = 230_400) -> Order:
    order = Order(
        merchant_id=merchant_id,
        customer_id=None,
        external_order_id=f"order_{uuid.uuid4().hex[:12]}",
        amount=amount,
        currency="INR",
        status="created",
    )
    session.add(order)
    session.flush()
    return order


def an_attempt(
    session: Session,
    order: Order,
    *,
    method: str = PaymentMethod.CARD.value,
    attempt_number: int = 1,
    attempted_at: datetime | None = AS_OF,
) -> PaymentAttempt:
    attempt = PaymentAttempt(
        order_id=order.id,
        customer_id=None,
        external_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        amount=order.amount,
        currency="INR",
        payment_method=method,
        provider="razorpay",
        status="failed",
        failure_code="BANK_DECLINE",
        attempt_number=attempt_number,
        attempted_at=attempted_at,
    )
    session.add(attempt)
    session.flush()
    return attempt


def a_risk(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    risk_type: str = RiskType.REPEATED_PAYMENT_FAILURE.value,
    amount: int = 230_400,
    confidence_bps: int = 7_000,
    order: Order | None = None,
) -> RevenueRisk:
    risk = RevenueRisk(
        merchant_id=merchant_id,
        customer_id=None,
        order_id=order.id if order is not None else None,
        risk_type=risk_type,
        amount_at_risk=amount,
        currency="INR",
        confidence_bps=confidence_bps,
        detection_rule=f"{risk_type}.v1",
        detected_at=AS_OF,
        status="detected",
    )
    session.add(risk)
    session.flush()
    return risk


def a_population(session: Session, size: int = 40) -> tuple[object, uuid.UUID]:
    """`size` risks, each with a card attempt, all assigned."""
    experiment = a_running_experiment(session)
    merchant_id = a_merchant(session)
    for _ in range(size):
        order = an_order(session, merchant_id)
        an_attempt(session, order)
        risk = a_risk(session, merchant_id, order=order)
        assign_risk(session, experiment, risk, AS_OF, salt=SALT)
    return experiment, merchant_id


class TestLoadingTheRandomisedPopulation:
    def test_every_assignment_becomes_a_row(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=12)
        rows = covariate_rows(db_session, experiment.id)  # type: ignore[attr-defined]
        assert len(rows) == 12

    def test_arms_are_carried_from_the_assignment(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        rows = covariate_rows(db_session, experiment.id)  # type: ignore[attr-defined]
        assert {row.arm for row in rows} <= set(Arm.values())

        stored = {
            assignment.risk_id: assignment.arm
            for assignment in db_session.execute(
                select(CaseAssignment).where(
                    CaseAssignment.experiment_id == experiment.id  # type: ignore[attr-defined]
                )
            ).scalars()
        }
        assert {row.risk_id: row.arm for row in rows} == stored

    def test_another_experiment_is_not_pulled_in(self, db_session: Session) -> None:
        first, _ = a_population(db_session, size=8)
        second, _ = a_population(db_session, size=5)
        assert len(covariate_rows(db_session, first.id)) == 8  # type: ignore[attr-defined]
        assert len(covariate_rows(db_session, second.id)) == 5  # type: ignore[attr-defined]

    def test_an_experiment_with_no_assignments_loads_empty(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        assert covariate_rows(db_session, experiment.id) == []

    def test_the_load_is_ordered_deterministically(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=15)
        first = covariate_rows(db_session, experiment.id)  # type: ignore[attr-defined]
        second = covariate_rows(db_session, experiment.id)  # type: ignore[attr-defined]
        assert [row.risk_id for row in first] == [row.risk_id for row in second]

    def test_the_denominator_is_fixed_at_randomisation(self, db_session: Session) -> None:
        """No window, no outcome, no recovery case — the unit is still in the
        population. Loading only units that progressed would replace the
        randomised denominator with a selected one, which is the bias the
        holdout exists to remove."""
        experiment, _ = a_population(db_session, size=10)
        assert db_session.execute(select(func.count()).select_from(CaseOutcome)).scalar() == 0
        assert db_session.execute(select(func.count()).select_from(RecoveryCase)).scalar() == 0
        assert len(covariate_rows(db_session, experiment.id)) == 10  # type: ignore[attr-defined]


class TestCovariateExtraction:
    def test_risk_type_and_band_come_from_the_stratum_key(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        risk = a_risk(db_session, merchant_id, amount=230_400)
        assignment = assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)
        assert assignment is not None

        (loaded,) = covariate_rows(db_session, experiment.id)
        assert assignment.stratum_key == f"{loaded.risk_type}|{loaded.amount_band}"
        assert loaded.risk_type == RiskType.REPEATED_PAYMENT_FAILURE.value
        assert loaded.amount_band == "2000-5000"

    def test_amount_and_confidence_come_from_the_risk(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        risk = a_risk(db_session, merchant_id, amount=987_600, confidence_bps=4_250)
        assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        (loaded,) = covariate_rows(db_session, experiment.id)
        assert loaded.amount_at_risk == 987_600
        assert loaded.confidence_bps == 4_250

    def test_the_payment_method_comes_from_the_attempt(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        order = an_order(db_session, merchant_id)
        an_attempt(db_session, order, method=PaymentMethod.UPI.value)
        risk = a_risk(db_session, merchant_id, order=order)
        assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        (loaded,) = covariate_rows(db_session, experiment.id)
        assert loaded.payment_method == PaymentMethod.UPI.value

    def test_the_latest_attempt_wins(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        order = an_order(db_session, merchant_id)
        an_attempt(db_session, order, method=PaymentMethod.CARD.value, attempt_number=1)
        an_attempt(db_session, order, method=PaymentMethod.NETBANKING.value, attempt_number=2)
        risk = a_risk(db_session, merchant_id, order=order)
        assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        (loaded,) = covariate_rows(db_session, experiment.id)
        assert loaded.payment_method == PaymentMethod.NETBANKING.value

    def test_a_risk_with_no_order_has_no_payment_method(self, db_session: Session) -> None:
        """A subscription failure carries no `order_id` at all, so there is no
        attempt to read a method from. Missing, not guessed."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        risk = a_risk(
            db_session,
            merchant_id,
            risk_type=RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
        )
        assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        (loaded,) = covariate_rows(db_session, experiment.id)
        assert loaded.payment_method is None

    def test_an_order_with_no_attempt_has_no_payment_method(self, db_session: Session) -> None:
        """A checkout abandonment has an order but no attempt by construction."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        order = an_order(db_session, merchant_id)
        risk = a_risk(
            db_session,
            merchant_id,
            risk_type=RiskType.CHECKOUT_ABANDONMENT.value,
            order=order,
        )
        assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        (loaded,) = covariate_rows(db_session, experiment.id)
        assert loaded.payment_method is None


class TestTheReport:
    def test_it_covers_the_pre_registered_covariates(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        report = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        assert tuple(c.name for c in report.covariates) == BALANCE_COVARIATES

    def test_arm_counts_sum_to_the_population(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=40)
        report = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        assert report.total_n == 40
        assert report.treatment_n + report.holdout_n == 40

    def test_the_arm_counts_match_what_was_stored(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=40)
        report = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        stored_holdout = db_session.execute(
            select(func.count())
            .select_from(CaseAssignment)
            .where(
                CaseAssignment.experiment_id == experiment.id,  # type: ignore[attr-defined]
                CaseAssignment.arm == Arm.HOLDOUT.value,
            )
        ).scalar()
        assert report.holdout_n == stored_holdout

    def test_a_missing_payment_method_is_an_explicit_level(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for index in range(30):
            if index % 2:
                order = an_order(db_session, merchant_id)
                an_attempt(db_session, order)
                risk = a_risk(db_session, merchant_id, order=order)
            else:
                risk = a_risk(
                    db_session,
                    merchant_id,
                    risk_type=RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
                )
            assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        report = report_for_experiment(db_session, experiment.id)
        method = next(c for c in report.covariates if c.name == "payment_method")
        levels = {level.level: level for level in method.levels}
        assert MISSING_LEVEL in levels
        missing = levels[MISSING_LEVEL]
        assert missing.treatment_count + missing.holdout_count == 15

    def test_a_deliberately_skewed_population_is_flagged(self, db_session: Session) -> None:
        """Hash assignment cannot be steered, so the skew is built by giving
        every unit in one arm a large amount after the fact — which is exactly
        the situation the balance table exists to catch."""
        experiment, _ = a_population(db_session, size=40)
        for assignment in db_session.execute(
            select(CaseAssignment).where(
                CaseAssignment.experiment_id == experiment.id  # type: ignore[attr-defined]
            )
        ).scalars():
            risk = db_session.get(RevenueRisk, assignment.risk_id)
            assert risk is not None
            risk.amount_at_risk = 2_000_000 if assignment.arm == Arm.TREATMENT.value else 100_000
        db_session.flush()

        report = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        assert "amount_at_risk" in report.flagged
        assert not report.is_balanced

    def test_the_serialised_report_is_integers_only(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        report = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]

        def walk(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(report.as_dict())

    def test_it_is_reproducible(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=30)
        first = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        second = report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        assert first.as_dict() == second.as_dict()


class TestItWritesNothing:
    def test_no_recovery_case_or_action_is_created(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        db_session.flush()

        assert db_session.execute(select(func.count()).select_from(RecoveryCase)).scalar() == 0
        assert db_session.execute(select(func.count()).select_from(RecoveryAction)).scalar() == 0

    def test_no_audit_row_is_written(self, db_session: Session) -> None:
        """Balance is a diagnostic, not a decision. It records nothing of its
        own; whoever reports it decides what to persist."""
        experiment, _ = a_population(db_session, size=20)
        before = db_session.execute(select(func.count()).select_from(AuditEvent)).scalar()
        report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        db_session.flush()
        after = db_session.execute(select(func.count()).select_from(AuditEvent)).scalar()
        assert before == after

    def test_no_assignment_is_altered(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        before = {
            assignment.risk_id: (assignment.arm, assignment.stratum_key, assignment.assignment_hash)
            for assignment in db_session.execute(
                select(CaseAssignment).where(
                    CaseAssignment.experiment_id == experiment.id  # type: ignore[attr-defined]
                )
            ).scalars()
        }
        report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        db_session.expire_all()
        after = {
            assignment.risk_id: (assignment.arm, assignment.stratum_key, assignment.assignment_hash)
            for assignment in db_session.execute(
                select(CaseAssignment).where(
                    CaseAssignment.experiment_id == experiment.id  # type: ignore[attr-defined]
                )
            ).scalars()
        }
        assert before == after

    def test_no_risk_is_altered(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        before = {
            risk.id: (risk.amount_at_risk, risk.confidence_bps, risk.status)
            for risk in db_session.execute(select(RevenueRisk)).scalars()
        }
        report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        db_session.expire_all()
        after = {
            risk.id: (risk.amount_at_risk, risk.confidence_bps, risk.status)
            for risk in db_session.execute(select(RevenueRisk)).scalars()
        }
        assert before == after

    def test_the_session_has_nothing_pending_afterwards(self, db_session: Session) -> None:
        experiment, _ = a_population(db_session, size=20)
        db_session.flush()
        report_for_experiment(db_session, experiment.id)  # type: ignore[attr-defined]
        assert list(db_session.new) == []
        assert list(db_session.deleted) == []
        assert list(db_session.dirty) == []
