"""Assignment against revtrace_test.

What is tested here rather than in the pure suite: the storage-layer guarantees
— that a second assignment is refused, that re-running is a no-op, that
excluded risk types never enter, and that assignment writes to
`case_assignments` and nothing else.

The last one matters most. Assignment is a separate phase from detection, but it
is still not allowed to create a recovery case, take an action, or move money.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.experiments.assignment import (
    ASSIGN_ACTION,
    AssignmentError,
    assign_for_merchant,
    assign_risk,
    audit_entry_for,
    decide,
    existing_assignment,
)
from app.experiments.registry import (
    ExperimentDraft,
    close_experiment,
    create_draft,
    lock_experiment,
    start_experiment,
)
from app.models import (
    AuditEvent,
    CaseAssignment,
    RecoveryAction,
    RecoveryCase,
    RevenueRisk,
)
from app.models.enums import Arm, RiskType

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


def a_risk(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    risk_type: str = RiskType.REPEATED_PAYMENT_FAILURE.value,
    amount: int = 230_400,
    detected_at: datetime = AS_OF,
) -> RevenueRisk:
    risk = RevenueRisk(
        merchant_id=merchant_id,
        customer_id=None,
        order_id=None,
        risk_type=risk_type,
        amount_at_risk=amount,
        currency="INR",
        confidence_bps=7_000,
        detection_rule=f"{risk_type}.v1",
        detected_at=detected_at,
        status="detected",
    )
    session.add(risk)
    session.flush()
    return risk


class TestSingleAssignment:
    def test_a_risk_is_assigned(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))

        assignment = assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)
        assert assignment is not None
        assert assignment.risk_id == risk.id
        assert assignment.arm in Arm.values()
        assert assignment.stratum_key == "repeated_payment_failure|2000-5000"
        assert len(assignment.assignment_hash) == 64
        assert assignment.assigned_at == AS_OF

    def test_the_stored_arm_matches_the_pure_computation(self, db_session: Session) -> None:
        """An auditor recomputes the draw from stored inputs and gets the same
        answer — which is the whole point of hashing rather than drawing."""
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))

        assignment = assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)
        expected = decide(
            risk.id,
            experiment.id,
            risk_type=risk.risk_type,
            amount_minor=risk.amount_at_risk,
            holdout_bps=experiment.holdout_bps,
            salt=SALT,
        )
        assert assignment is not None
        assert assignment.arm == expected.arm.value
        assert assignment.assignment_hash == expected.assignment_hash

    def test_assigning_twice_returns_the_same_row(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))

        first = assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)
        second = assign_risk(db_session, experiment, risk, AS_OF + timedelta(days=1), salt=SALT)

        assert first is not None and second is not None
        assert first.id == second.id
        assert second.assigned_at == AS_OF  # the original moment, not the retry

    def test_only_one_row_ever_exists(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))

        for _ in range(4):
            assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        count = db_session.scalar(
            select(func.count())
            .select_from(CaseAssignment)
            .where(CaseAssignment.risk_id == risk.id)
        )
        assert count == 1

    def test_a_duplicate_insert_is_refused_by_storage(self, db_session: Session) -> None:
        """The application check is a convenience; this is the guarantee."""
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))
        assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

        db_session.add(
            CaseAssignment(
                risk_id=risk.id,
                experiment_id=experiment.id,
                arm=Arm.TREATMENT.value,
                stratum_key="forced|duplicate",
                assignment_hash="f" * 64,
                assigned_at=AS_OF,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_the_same_risk_may_join_a_second_experiment(self, db_session: Session) -> None:
        first = a_running_experiment(db_session)
        second = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))

        a = assign_risk(db_session, first, risk, AS_OF, salt=SALT)
        b = assign_risk(db_session, second, risk, AS_OF, salt=SALT)
        assert a is not None and b is not None
        assert a.id != b.id

    def test_a_naive_timestamp_is_rejected(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))
        with pytest.raises(AssignmentError, match="timezone-aware"):
            assign_risk(
                db_session,
                experiment,
                risk,
                datetime(2026, 8, 29, 12),  # noqa: DTZ001
                salt=SALT,
            )


class TestExperimentStateGating:
    def test_a_draft_experiment_cannot_enrol(self, db_session: Session) -> None:
        """Its specification can still change; enrolling against a moving
        specification is what pre-registration exists to prevent."""
        experiment = create_draft(
            db_session,
            ExperimentDraft(
                name="EXP-draft",
                hypothesis="h",
                primary_metric="recovery_rate",
                holdout_bps=5_000,
                planned_n_per_arm=10,
                mde_bps=1_000,
            ),
        )
        risk = a_risk(db_session, a_merchant(db_session))
        with pytest.raises(AssignmentError, match="only a running experiment"):
            assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

    def test_a_locked_but_unstarted_experiment_cannot_enrol(self, db_session: Session) -> None:
        experiment = create_draft(
            db_session,
            ExperimentDraft(
                name="EXP-locked",
                hypothesis="h",
                primary_metric="recovery_rate",
                holdout_bps=5_000,
                planned_n_per_arm=10,
                mde_bps=1_000,
            ),
        )
        lock_experiment(db_session, experiment, LOCKED_AT)
        risk = a_risk(db_session, a_merchant(db_session))
        with pytest.raises(AssignmentError, match="only a running experiment"):
            assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)

    def test_a_closed_experiment_cannot_enrol(self, db_session: Session) -> None:
        """Fixed horizon: admitting a late unit would break it."""
        experiment = a_running_experiment(db_session)
        close_experiment(db_session, experiment, AS_OF + timedelta(days=7))
        risk = a_risk(db_session, a_merchant(db_session))
        with pytest.raises(AssignmentError, match="only a running experiment"):
            assign_risk(db_session, experiment, risk, AS_OF, salt=SALT)


class TestExclusions:
    def test_a_reconciliation_mismatch_is_never_assigned(self, db_session: Session) -> None:
        """Zero at risk (ADR 0007): nothing to recover, and including it would
        drag the effect estimate toward zero."""
        experiment = a_running_experiment(db_session)
        risk = a_risk(
            db_session,
            a_merchant(db_session),
            risk_type=RiskType.RECONCILIATION_MISMATCH.value,
            amount=0,
        )

        assert assign_risk(db_session, experiment, risk, AS_OF, salt=SALT) is None
        assert existing_assignment(db_session, risk.id, experiment.id) is None

    def test_the_three_measurable_types_are_assigned(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)

        for risk_type in (
            RiskType.REPEATED_PAYMENT_FAILURE.value,
            RiskType.CHECKOUT_ABANDONMENT.value,
            RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
        ):
            risk = a_risk(db_session, merchant_id, risk_type=risk_type)
            assert assign_risk(db_session, experiment, risk, AS_OF, salt=SALT) is not None


class TestMerchantRun:
    def _population(self, session: Session, merchant_id: uuid.UUID, n: int) -> None:
        for index in range(n):
            a_risk(
                session,
                merchant_id,
                amount=50_000 + index * 9_137,
                detected_at=AS_OF + timedelta(minutes=index),
            )

    def test_a_run_assigns_every_eligible_risk(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        self._population(db_session, merchant_id, 60)

        summary = assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        assert summary.risks_examined == 60
        assert summary.assigned == 60
        assert summary.excluded == 0
        assert summary.treatment + summary.holdout == 60

    def test_both_arms_are_populated(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        self._population(db_session, merchant_id, 60)

        summary = assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        assert summary.treatment > 0
        assert summary.holdout > 0

    def test_re_running_assigns_nothing_new(self, db_session: Session) -> None:
        """The idempotence that makes a redelivered webhook harmless."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        self._population(db_session, merchant_id, 25)

        first = assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        second = assign_for_merchant(
            db_session, experiment, merchant_id, AS_OF + timedelta(days=2), salt=SALT
        )

        assert first.assigned == 25
        assert second.assigned == 0
        assert second.already_assigned == 25

        total = db_session.scalar(select(func.count()).select_from(CaseAssignment))
        assert total == 25

    def test_arms_survive_a_re_run_unchanged(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        self._population(db_session, merchant_id, 25)

        assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        before = {a.risk_id: a.arm for a in db_session.execute(select(CaseAssignment)).scalars()}
        assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        after = {a.risk_id: a.arm for a in db_session.execute(select(CaseAssignment)).scalars()}
        assert before == after

    def test_excluded_risks_are_counted_not_assigned(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        self._population(db_session, merchant_id, 10)
        for _ in range(4):
            a_risk(
                db_session,
                merchant_id,
                risk_type=RiskType.RECONCILIATION_MISMATCH.value,
                amount=0,
            )

        summary = assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        assert summary.risks_examined == 14
        assert summary.assigned == 10
        assert summary.excluded == 4

    def test_strata_are_recorded(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        self._population(db_session, merchant_id, 40)
        assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)

        strata = {a.stratum_key for a in db_session.execute(select(CaseAssignment)).scalars()}
        assert len(strata) > 1
        for key in strata:
            assert key.count("|") == 1
            assert key.startswith("repeated_payment_failure|")

    def test_another_merchants_risks_are_untouched(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        mine, theirs = a_merchant(db_session), a_merchant(db_session)
        self._population(db_session, mine, 5)
        self._population(db_session, theirs, 5)

        summary = assign_for_merchant(db_session, experiment, mine, AS_OF, salt=SALT)
        assert summary.assigned == 5
        assert db_session.scalar(select(func.count()).select_from(CaseAssignment)) == 5


class TestAuthorityBoundary:
    """Assignment is a separate phase from detection — and still may not act."""

    def test_assignment_creates_no_recovery_rows(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for index in range(20):
            a_risk(db_session, merchant_id, amount=50_000 + index * 11_000)

        assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        db_session.flush()

        for model in (RecoveryCase, RecoveryAction):
            assert db_session.scalar(select(func.count()).select_from(model)) == 0

    def test_a_holdout_risk_gets_no_recovery_case(self, db_session: Session) -> None:
        """Stronger than the `holdout_never_acts` CHECK: there is no row at all
        for the constraint to be applied to."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        for index in range(30):
            a_risk(db_session, merchant_id, amount=50_000 + index * 7_000)

        assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        holdouts = [
            a
            for a in db_session.execute(select(CaseAssignment)).scalars()
            if a.arm == Arm.HOLDOUT.value
        ]
        assert holdouts
        assert db_session.scalar(select(func.count()).select_from(RecoveryCase)) == 0

    def test_assignment_writes_no_audit_row_of_its_own(self, db_session: Session) -> None:
        """`audit_entry_for` returns a payload; persisting it is the caller's
        call. Assignment cannot quietly become an audit writer."""
        experiment = a_running_experiment(db_session)
        merchant_id = a_merchant(db_session)
        a_risk(db_session, merchant_id)

        assign_for_merchant(db_session, experiment, merchant_id, AS_OF, salt=SALT)
        db_session.flush()
        assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 0

    def test_an_assignment_audit_row_anchors_on_the_risk(self, db_session: Session) -> None:
        """The Day 3 column doing its job: an assignment happens before any
        recovery case exists, so `case_id` could never identify it."""
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))
        decision = decide(
            risk.id,
            experiment.id,
            risk_type=risk.risk_type,
            amount_minor=risk.amount_at_risk,
            holdout_bps=experiment.holdout_bps,
            salt=SALT,
        )
        entry = audit_entry_for(decision, AS_OF)

        db_session.add(
            AuditEvent(
                risk_id=entry["risk_id"],
                actor=entry["actor"],
                action=entry["action"],
                decision_type=entry["decision_type"],
                is_execution=False,
                numeric_snapshot=entry["numeric_snapshot"],
            )
        )
        db_session.flush()

        stored = db_session.execute(select(AuditEvent)).scalars().one()
        assert stored.risk_id == risk.id
        assert stored.case_id is None
        assert stored.action == ASSIGN_ACTION
        assert stored.decision_type == "assign"
        assert stored.is_execution is False
        assert stored.numeric_snapshot["bucket"] == decision.bucket

    def test_an_assignment_audit_row_is_never_an_execution(self, db_session: Session) -> None:
        experiment = a_running_experiment(db_session)
        risk = a_risk(db_session, a_merchant(db_session))
        decision = decide(
            risk.id,
            experiment.id,
            risk_type=risk.risk_type,
            amount_minor=risk.amount_at_risk,
            holdout_bps=experiment.holdout_bps,
            salt=SALT,
        )
        assert audit_entry_for(decision, AS_OF)["is_execution"] is False

    def test_an_ai_agent_still_cannot_execute(self, db_session: Session) -> None:
        """The Phase 1 authority constraint survives the new column."""
        risk = a_risk(db_session, a_merchant(db_session))
        db_session.add(
            AuditEvent(
                risk_id=risk.id,
                actor="ai_agent",
                action="something",
                is_execution=True,
                decision_type="execute",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestAuditLinkageColumn:
    def test_case_id_is_retained(self, db_session: Session) -> None:
        """The new column is additive; recovery-case entries still use case_id."""
        columns = db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events'"
            )
        ).scalars()
        names = set(columns)
        assert {"case_id", "risk_id"} <= names

    def test_risk_id_is_nullable(self, db_session: Session) -> None:
        """A run-level entry is about neither a case nor a single risk."""
        nullable = db_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'audit_events' AND column_name = 'risk_id'"
            )
        ).scalar()
        assert nullable == "YES"

    def test_risk_id_references_revenue_risks(self, db_session: Session) -> None:
        target = db_session.execute(
            text(
                "SELECT cf.relname FROM pg_constraint con "
                "JOIN pg_class c ON c.oid = con.conrelid "
                "JOIN pg_class cf ON cf.oid = con.confrelid "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(con.conkey) "
                "WHERE con.contype = 'f' AND c.relname = 'audit_events' "
                "AND a.attname = 'risk_id'"
            )
        ).scalar()
        assert target == "revenue_risks"

    def test_an_entry_may_carry_neither_anchor(self, db_session: Session) -> None:
        """Run-level entries are legitimate, so there is deliberately no
        'must have a subject' constraint."""
        db_session.add(AuditEvent(actor="engine", action="DETECTION_RUN", decision_type="detect"))
        db_session.flush()
