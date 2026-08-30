"""The analysis loader against revtrace_test.

What only a real database can demonstrate: that the join finds every enrolled
assignment including the ones with no outcome, that an unsealed window stops
the analysis rather than being skipped, that the ordering is stable across
loads, and that computing an analysis population writes nothing.

The refusals matter most. Analysing whatever happens to be ready would measure
units that resolved quickly rather than a random sample of units, and no sample
size fixes that.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.causal.analysis import (
    AnalysisRefused,
    build_population,
    load_outcome_rows,
    load_population,
)
from app.experiments.assignment import assign_risk
from app.experiments.registry import (
    ExperimentDraft,
    create_draft,
    lock_experiment,
    start_experiment,
)
from app.experiments.windows import open_window, seal
from app.models import (
    AuditEvent,
    CaseAssignment,
    CaseOutcome,
    RecoveryAction,
    RecoveryCase,
    RevenueRisk,
)
from app.models.enums import Arm, RiskType

pytestmark = pytest.mark.db

LOCKED_AT = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
STARTED_AT = LOCKED_AT + timedelta(hours=1)
DETECTED_AT = STARTED_AT + timedelta(hours=1)
AFTER_WINDOW = DETECTED_AT + timedelta(hours=72)
SALT = "revtrace-demo-salt-v1"


def a_running_experiment(session: Session):  # noqa: ANN201
    experiment = create_draft(
        session,
        ExperimentDraft(
            name=f"EXP-{uuid.uuid4().hex[:8]}",
            hypothesis="Intervention increases recovery within the window.",
            primary_metric="recovery_rate",
            holdout_bps=5_000,
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


def a_risk(session: Session, merchant_id: uuid.UUID, *, amount: int = 230_400) -> RevenueRisk:
    risk = RevenueRisk(
        merchant_id=merchant_id,
        customer_id=None,
        order_id=None,
        risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
        amount_at_risk=amount,
        currency="INR",
        confidence_bps=7_000,
        detection_rule="repeated_payment_failure.v1",
        detected_at=DETECTED_AT,
        status="detected",
    )
    session.add(risk)
    session.flush()
    return risk


def an_enrolled_unit(
    session: Session,
    experiment,  # noqa: ANN001
    merchant_id: uuid.UUID,
    *,
    recovered: bool = False,
    amount: int = 0,
    execution_failed: bool = False,
    actions_executed: int = 0,
    seal_it: bool = True,
    open_it: bool = True,
) -> tuple[RevenueRisk, CaseOutcome | None]:
    """One risk, assigned, optionally windowed and sealed."""
    risk = a_risk(session, merchant_id)
    assign_risk(session, experiment, risk, DETECTED_AT, salt=SALT)

    if not open_it:
        return risk, None

    outcome = open_window(session, risk)
    assert outcome is not None
    outcome.recovered = recovered
    outcome.recovered_amount = amount
    outcome.recovered_at = DETECTED_AT + timedelta(hours=1) if recovered else None
    outcome.execution_failed = execution_failed
    outcome.actions_executed = actions_executed
    session.flush()

    if seal_it:
        seal(session, outcome, AFTER_WINDOW)
    return risk, outcome


def a_complete_experiment(session: Session, size: int = 20):  # noqa: ANN201
    """`size` enrolled units, all windowed, all sealed."""
    experiment = a_running_experiment(session)
    merchant_id = a_merchant(session)
    for index in range(size):
        an_enrolled_unit(
            session,
            experiment,
            merchant_id,
            recovered=index % 3 == 0,
            amount=100_000 if index % 3 == 0 else 0,
        )
    return experiment, merchant_id


class TestCompleteSealedData:
    def test_every_enrolled_unit_is_loaded(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=14)
        rows = load_outcome_rows(db_session, experiment.id)
        assert len(rows) == 14

    def test_the_arm_comes_from_the_assignment(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        rows = load_outcome_rows(db_session, experiment.id)

        stored = {
            assignment.risk_id: assignment.arm
            for assignment in db_session.execute(
                select(CaseAssignment).where(CaseAssignment.experiment_id == experiment.id)
            ).scalars()
        }
        assert {row.risk_id: row.arm for row in rows} == stored

    def test_the_outcome_values_come_through(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        risk, _ = an_enrolled_unit(
            db_session, experiment, merchant_id, recovered=True, amount=451_200
        )

        (row,) = load_outcome_rows(db_session, experiment.id)
        assert row.risk_id == risk.id
        assert row.recovered is True
        assert row.recovered_amount == 451_200

    def test_both_populations_are_built(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=30)
        population = load_population(db_session, experiment.id)
        assert population.n_enrolled == 30
        assert population.itt.n_total == 30
        assert population.itt.treatment.n + population.itt.holdout.n == 30

    def test_the_arm_counts_match_what_was_stored(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=40)
        population = load_population(db_session, experiment.id)
        stored_holdout = db_session.execute(
            select(func.count())
            .select_from(CaseAssignment)
            .where(
                CaseAssignment.experiment_id == experiment.id,
                CaseAssignment.arm == Arm.HOLDOUT.value,
            )
        ).scalar()
        assert population.itt.holdout.n == stored_holdout

    def test_another_experiment_is_not_pulled_in(self, db_session: Session) -> None:
        first, _ = a_complete_experiment(db_session, size=8)
        second, _ = a_complete_experiment(db_session, size=5)
        assert len(load_outcome_rows(db_session, first.id)) == 8
        assert len(load_outcome_rows(db_session, second.id)) == 5

    def test_an_experiment_with_no_assignments_loads_empty(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        assert load_outcome_rows(db_session, experiment.id) == []


class TestMissingOutcomes:
    def test_an_assignment_without_an_outcome_refuses(self, db_session: Session) -> None:
        """An inner join would have hidden this by returning fewer rows."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(5):
            an_enrolled_unit(db_session, experiment, merchant_id)
        an_enrolled_unit(db_session, experiment, merchant_id, open_it=False)

        with pytest.raises(AnalysisRefused, match="no outcome row"):
            load_outcome_rows(db_session, experiment.id)

    def test_the_refusal_counts_them(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(4):
            an_enrolled_unit(db_session, experiment, merchant_id)
        for _ in range(3):
            an_enrolled_unit(db_session, experiment, merchant_id, open_it=False)

        with pytest.raises(AnalysisRefused) as caught:
            load_outcome_rows(db_session, experiment.id)
        assert caught.value.missing_outcomes == 3
        assert caught.value.unsealed_outcomes == 0
        assert caught.value.experiment_id == experiment.id

    def test_the_refusal_names_examples(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        an_enrolled_unit(db_session, experiment, merchant_id)
        risk, _ = an_enrolled_unit(db_session, experiment, merchant_id, open_it=False)

        with pytest.raises(AnalysisRefused, match=str(risk.id)):
            load_outcome_rows(db_session, experiment.id)

    def test_it_explains_why_rather_than_only_that(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        an_enrolled_unit(db_session, experiment, merchant_id, open_it=False)

        with pytest.raises(AnalysisRefused, match="resolved quickly"):
            load_outcome_rows(db_session, experiment.id)


class TestUnsealedOutcomes:
    def test_an_open_window_refuses(self, db_session: Session) -> None:
        """A window still open is a number still moving; reading it is peeking."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(5):
            an_enrolled_unit(db_session, experiment, merchant_id)
        an_enrolled_unit(db_session, experiment, merchant_id, seal_it=False)

        with pytest.raises(AnalysisRefused, match="not sealed"):
            load_outcome_rows(db_session, experiment.id)

    def test_the_refusal_counts_them(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(6):
            an_enrolled_unit(db_session, experiment, merchant_id)
        for _ in range(2):
            an_enrolled_unit(db_session, experiment, merchant_id, seal_it=False)

        with pytest.raises(AnalysisRefused) as caught:
            load_outcome_rows(db_session, experiment.id)
        assert caught.value.unsealed_outcomes == 2
        assert caught.value.missing_outcomes == 0

    def test_both_failures_are_reported_together(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        an_enrolled_unit(db_session, experiment, merchant_id)
        an_enrolled_unit(db_session, experiment, merchant_id, seal_it=False)
        an_enrolled_unit(db_session, experiment, merchant_id, open_it=False)

        with pytest.raises(AnalysisRefused) as caught:
            load_outcome_rows(db_session, experiment.id)
        assert caught.value.missing_outcomes == 1
        assert caught.value.unsealed_outcomes == 1

    def test_sealing_the_last_window_makes_it_analysable(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(4):
            an_enrolled_unit(db_session, experiment, merchant_id)
        _, outcome = an_enrolled_unit(db_session, experiment, merchant_id, seal_it=False)

        with pytest.raises(AnalysisRefused):
            load_outcome_rows(db_session, experiment.id)

        assert outcome is not None
        seal(db_session, outcome, AFTER_WINDOW)
        assert len(load_outcome_rows(db_session, experiment.id)) == 5


class TestZeroRecoveryPopulation:
    def test_a_population_that_recovered_nothing_still_loads(self, db_session: Session) -> None:
        """Zero is a measurement, not missing data."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(20):
            an_enrolled_unit(db_session, experiment, merchant_id, recovered=False, amount=0)

        population = load_population(db_session, experiment.id)
        assert population.n_enrolled == 20
        assert population.itt.treatment.gross == 0
        assert population.itt.holdout.gross == 0
        assert population.itt.treatment.recoveries == 0

    def test_every_unit_is_still_counted(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(20):
            an_enrolled_unit(db_session, experiment, merchant_id)

        population = load_population(db_session, experiment.id)
        assert population.itt.treatment.n + population.itt.holdout.n == 20
        assert all(value == 0 for value in population.itt.treatment.amounts)


class TestDeterministicOrdering:
    def test_two_loads_return_the_same_order(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=25)
        first = load_outcome_rows(db_session, experiment.id)
        second = load_outcome_rows(db_session, experiment.id)
        assert [row.risk_id for row in first] == [row.risk_id for row in second]

    def test_the_order_is_by_risk_id(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=25)
        rows = load_outcome_rows(db_session, experiment.id)
        assert [row.risk_id for row in rows] == sorted(row.risk_id for row in rows)

    def test_the_arm_sequences_are_stable(self, db_session: Session) -> None:
        """The bootstrap draws from these, so an interval that moved between
        loads of the same data would not be reproducible."""
        experiment, _ = a_complete_experiment(db_session, size=25)
        first = load_population(db_session, experiment.id)
        second = load_population(db_session, experiment.id)
        assert first.itt.treatment.recovered == second.itt.treatment.recovered
        assert first.itt.treatment.amounts == second.itt.treatment.amounts
        assert first.itt.treatment.risk_ids == second.itt.treatment.risk_ids

    def test_the_payload_is_reproducible(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=25)
        assert (
            load_population(db_session, experiment.id).itt.as_dict()
            == load_population(db_session, experiment.id).itt.as_dict()
        )

    def test_rebuilding_from_the_same_rows_matches(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=25)
        rows = load_outcome_rows(db_session, experiment.id)
        assert build_population(rows, experiment.id).itt.as_dict() == (
            load_population(db_session, experiment.id).itt.as_dict()
        )


class TestNonCompliance:
    def test_a_failed_execution_stays_in_itt_and_leaves_per_protocol(
        self, db_session: Session
    ) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for _ in range(30):
            an_enrolled_unit(db_session, experiment, merchant_id)

        rows = load_outcome_rows(db_session, experiment.id)
        treated = [row for row in rows if row.is_treatment]
        assert treated, "expected at least one treated unit"

        statement = select(CaseOutcome).where(CaseOutcome.risk_id == treated[0].risk_id)
        outcome = db_session.execute(statement).scalars().one()
        outcome.execution_failed = True
        db_session.flush()

        population = load_population(db_session, experiment.id)
        assert population.itt.treatment.n == len(treated)
        assert population.per_protocol.treatment.n == len(treated) - 1
        assert population.per_protocol.excluded_treatment == 1

    def test_itt_never_excludes_anything(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        population = load_population(db_session, experiment.id)
        assert population.itt.excluded_total == 0
        assert population.itt.non_compliance_bps == 0


class TestItWritesNothing:
    def test_no_recovery_case_or_action_is_created(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        load_population(db_session, experiment.id)
        db_session.flush()

        assert db_session.execute(select(func.count()).select_from(RecoveryCase)).scalar() == 0
        assert db_session.execute(select(func.count()).select_from(RecoveryAction)).scalar() == 0

    def test_no_audit_row_is_written(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        before = db_session.execute(select(func.count()).select_from(AuditEvent)).scalar()
        load_population(db_session, experiment.id)
        db_session.flush()
        after = db_session.execute(select(func.count()).select_from(AuditEvent)).scalar()
        assert before == after

    def test_no_outcome_is_altered(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        before = {
            outcome.risk_id: (outcome.sealed, outcome.sealed_at, outcome.recovered_amount)
            for outcome in db_session.execute(select(CaseOutcome)).scalars()
        }
        load_population(db_session, experiment.id)
        db_session.expire_all()
        after = {
            outcome.risk_id: (outcome.sealed, outcome.sealed_at, outcome.recovered_amount)
            for outcome in db_session.execute(select(CaseOutcome)).scalars()
        }
        assert before == after

    def test_no_assignment_is_altered(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        before = {
            assignment.risk_id: (assignment.arm, assignment.assignment_hash)
            for assignment in db_session.execute(
                select(CaseAssignment).where(CaseAssignment.experiment_id == experiment.id)
            ).scalars()
        }
        load_population(db_session, experiment.id)
        db_session.expire_all()
        after = {
            assignment.risk_id: (assignment.arm, assignment.assignment_hash)
            for assignment in db_session.execute(
                select(CaseAssignment).where(CaseAssignment.experiment_id == experiment.id)
            ).scalars()
        }
        assert before == after

    def test_the_session_has_nothing_pending_afterwards(self, db_session: Session) -> None:
        experiment, _ = a_complete_experiment(db_session, size=20)
        db_session.flush()
        load_population(db_session, experiment.id)
        assert list(db_session.new) == []
        assert list(db_session.deleted) == []
        assert list(db_session.dirty) == []
