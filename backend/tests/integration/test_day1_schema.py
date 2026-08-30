"""Day 1 schema, against revtrace_test.

The guarantees here are the ones that must hold regardless of what any caller
believes it may do — so they are tested against the real database, not against
SQLAlchemy metadata. Everything runs inside the rolled-back transaction fixture;
no row survives a test and revtrace_dev is never involved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.experiments.registry import (
    ExperimentDraft,
    PreRegistrationError,
    close_experiment,
    create_draft,
    is_mutable,
    lock_experiment,
    start_experiment,
    update_draft,
)
from app.models import CaseAssignment, CaseOutcome, Experiment, Intervention, UpliftScore
from app.models.enums import Arm, ExperimentStatus, Quadrant

pytestmark = pytest.mark.db

LOCKED_AT = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
STARTED_AT = LOCKED_AT + timedelta(hours=1)
CLOSED_AT = STARTED_AT + timedelta(days=7)


def a_draft(name: str = "EXP-001") -> ExperimentDraft:
    return ExperimentDraft(
        name=name,
        hypothesis="Intervention increases recovery within the window.",
        primary_metric="recovery_rate",
        holdout_bps=5_000,
        planned_n_per_arm=384,
        mde_bps=1_000,
        secondary_metrics=("recovered_amount_mean",),
        strata_definition={"keys": ["risk_type", "amount_band"]},
    )


def a_risk(session: Session) -> uuid.UUID:
    """A minimal merchant -> customer -> order -> revenue_risk chain.

    Stops at the risk on purpose. The risk is the unit of randomisation, and
    building a `recovery_case` here would imply detection creates one — which is
    exactly what the authority tests assert it never does.
    """
    merchant_id, customer_id, order_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    risk_id = uuid.uuid4()

    session.execute(
        text(
            "INSERT INTO merchants (id, name, currency, timezone, created_at, updated_at) "
            "VALUES (:id, 'Acme', 'INR', 'Asia/Kolkata', now(), now())"
        ),
        {"id": merchant_id},
    )
    session.execute(
        text(
            "INSERT INTO customers (id, merchant_id, lifetime_value, contactable, "
            "contact_count, created_at, updated_at) "
            "VALUES (:id, :m, 0, true, 0, now(), now())"
        ),
        {"id": customer_id, "m": merchant_id},
    )
    session.execute(
        text(
            "INSERT INTO orders (id, merchant_id, customer_id, amount, currency, status, "
            "created_at, updated_at) "
            "VALUES (:id, :m, :c, 230400, 'INR', 'attempted', now(), now())"
        ),
        {"id": order_id, "m": merchant_id, "c": customer_id},
    )
    session.execute(
        text(
            "INSERT INTO revenue_risks (id, merchant_id, customer_id, order_id, risk_type, "
            "amount_at_risk, currency, confidence_bps, detected_at, status, created_at, "
            "updated_at) VALUES (:id, :m, :c, :o, 'repeated_payment_failure', 230400, 'INR', "
            "7000, now(), 'detected', now(), now())"
        ),
        {"id": risk_id, "m": merchant_id, "c": customer_id, "o": order_id},
    )
    session.flush()
    return risk_id


def a_recovery_case(session: Session, risk_id: uuid.UUID) -> uuid.UUID:
    """A Phase 6 style recovery case, for the decision-column tests only.

    Kept separate from `a_risk` so that needing one is visible at the call site:
    only the tests that are genuinely about `recovery_cases` create one.
    """
    case_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO recovery_cases (id, risk_id, strategy, expected_recovery, max_cost, "
            "estimated_cost, net_expected_recovery, currency, confidence_bps, policy_status, "
            "execution_status, created_at, updated_at) "
            "VALUES (:id, :r, 'no_action', 0, 0, 0, 0, 'INR', 0, 'pending', 'not_started', "
            "now(), now())"
        ),
        {"id": case_id, "r": risk_id},
    )
    session.flush()
    return case_id


class TestMigrationApplied:
    def test_the_day_one_migration_has_been_applied(self, db_session: Session) -> None:
        """Asserts the Day 1 schema is present, not that nothing followed it.

        Pinning `head` to a literal revision would fail on the day the next
        migration lands, which says nothing about whether Day 1 is correct.
        Presence of its tables is the property this file actually depends on.
        """
        applied = db_session.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert applied is not None

        tables = db_session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
        assert {
            "experiments",
            "case_assignments",
            "case_outcomes",
            "uplift_scores",
            "experiment_results",
            "interventions",
        } <= set(tables)

    @pytest.mark.parametrize(
        "table",
        [
            "experiments",
            "case_assignments",
            "case_outcomes",
            "uplift_scores",
            "experiment_results",
            "interventions",
        ],
    )
    def test_table_exists(self, db_session: Session, table: str) -> None:
        count = db_session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        assert count is not None

    def test_phase_one_authority_constraints_survive(self, db_session: Session) -> None:
        """The migration is additive. These must be untouched."""
        names = db_session.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conname IN ("
                "'ck_recovery_actions_executed_requires_approved',"
                "'ck_recovery_cases_execution_requires_policy_approval',"
                "'ck_audit_events_execution_actor_never_ai')"
            )
        ).scalars()
        assert set(names) == {
            "ck_recovery_actions_executed_requires_approved",
            "ck_recovery_cases_execution_requires_policy_approval",
            "ck_audit_events_execution_actor_never_ai",
        }

    def test_the_lock_guard_trigger_is_installed(self, db_session: Session) -> None:
        found = db_session.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname = 'trg_experiments_lock_guard'")
        ).scalar()
        assert found == "trg_experiments_lock_guard"


class TestExperimentLifecycle:
    def test_draft_to_locked_to_running_to_closed(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        assert experiment.status == ExperimentStatus.DRAFT.value
        assert experiment.locked_at is None
        assert is_mutable(experiment)

        lock_experiment(db_session, experiment, LOCKED_AT)
        assert experiment.status == ExperimentStatus.LOCKED.value
        assert experiment.locked_at == LOCKED_AT
        assert not is_mutable(experiment)

        start_experiment(db_session, experiment, STARTED_AT)
        assert experiment.status == ExperimentStatus.RUNNING.value

        close_experiment(db_session, experiment, CLOSED_AT)
        assert experiment.status == ExperimentStatus.CLOSED.value
        assert experiment.closed_at == CLOSED_AT

    def test_a_draft_may_be_edited(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        update_draft(db_session, experiment, a_draft(name="EXP-001-revised"))
        assert experiment.name == "EXP-001-revised"

    def test_a_locked_experiment_refuses_edits_in_python(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        lock_experiment(db_session, experiment, LOCKED_AT)
        with pytest.raises(PreRegistrationError, match="no unlock"):
            update_draft(db_session, experiment, a_draft(name="rewritten"))

    def test_a_draft_cannot_start_running(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        with pytest.raises(PreRegistrationError, match="cannot move"):
            start_experiment(db_session, experiment, STARTED_AT)

    def test_closed_is_terminal(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        lock_experiment(db_session, experiment, LOCKED_AT)
        close_experiment(db_session, experiment, CLOSED_AT)
        with pytest.raises(PreRegistrationError):
            start_experiment(db_session, experiment, STARTED_AT)

    def test_timestamps_must_be_ordered(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        lock_experiment(db_session, experiment, LOCKED_AT)
        with pytest.raises(PreRegistrationError, match="cannot precede"):
            start_experiment(db_session, experiment, LOCKED_AT - timedelta(hours=1))

    def test_naive_timestamps_are_rejected(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        with pytest.raises(PreRegistrationError, match="timezone-aware"):
            lock_experiment(db_session, experiment, datetime(2026, 8, 27, 12, 0))  # noqa: DTZ001


class TestLockGuardTrigger:
    """The guarantee that holds even when the service layer is bypassed."""

    def _locked(self, session: Session) -> Experiment:
        experiment = create_draft(session, a_draft())
        lock_experiment(session, experiment, LOCKED_AT)
        session.flush()
        return experiment

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("hypothesis", "'rewritten after the fact'"),
            ("primary_metric", "'a_metric_that_worked'"),
            ("holdout_bps", "1000"),
            ("planned_n_per_arm", "10"),
            ("alpha_bps", "1000"),
            ("power_bps", "5000"),
            ("mde_bps", "5000"),
            ("name", "'EXP-999'"),
        ],
    )
    def test_raw_sql_cannot_change_a_frozen_column(
        self, db_session: Session, column: str, value: str
    ) -> None:
        experiment = self._locked(db_session)
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text(f"UPDATE experiments SET {column} = {value} WHERE id = :id"),
                {"id": experiment.id},
            )

    def test_raw_sql_cannot_move_the_lock_timestamp(self, db_session: Session) -> None:
        """Back-dating a pre-registration would be the most valuable lie here."""
        experiment = self._locked(db_session)
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text("UPDATE experiments SET locked_at = now() WHERE id = :id"),
                {"id": experiment.id},
            )

    def test_raw_sql_cannot_unlock(self, db_session: Session) -> None:
        experiment = self._locked(db_session)
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text("UPDATE experiments SET locked_at = NULL WHERE id = :id"),
                {"id": experiment.id},
            )

    def test_the_lifecycle_may_still_advance(self, db_session: Session) -> None:
        experiment = self._locked(db_session)
        db_session.execute(
            text("UPDATE experiments SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": experiment.id},
        )
        db_session.flush()

    def test_a_draft_is_untouched_by_the_trigger(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        db_session.flush()
        db_session.execute(
            text("UPDATE experiments SET hypothesis = 'still editable' WHERE id = :id"),
            {"id": experiment.id},
        )
        db_session.flush()


class TestExperimentConstraints:
    def test_a_non_draft_row_requires_a_lock_timestamp(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                text(
                    "INSERT INTO experiments (id, name, hypothesis, primary_metric, "
                    "holdout_bps, planned_n_per_arm, alpha_bps, power_bps, mde_bps, status, "
                    "created_at, updated_at) VALUES (:id, 'x', 'h', 'm', 5000, 10, 500, 8000, "
                    "1000, 'running', now(), now())"
                ),
                {"id": uuid.uuid4()},
            )

    def test_a_draft_may_not_carry_a_lock_timestamp(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                text(
                    "INSERT INTO experiments (id, name, hypothesis, primary_metric, "
                    "holdout_bps, planned_n_per_arm, alpha_bps, power_bps, mde_bps, status, "
                    "locked_at, created_at, updated_at) VALUES (:id, 'x', 'h', 'm', 5000, 10, "
                    "500, 8000, 1000, 'draft', now(), now(), now())"
                ),
                {"id": uuid.uuid4()},
            )

    @pytest.mark.parametrize("holdout", [0, 10_000, -1])
    def test_a_degenerate_holdout_is_rejected(self, db_session: Session, holdout: int) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                text(
                    "INSERT INTO experiments (id, name, hypothesis, primary_metric, "
                    "holdout_bps, planned_n_per_arm, alpha_bps, power_bps, mde_bps, status, "
                    "created_at, updated_at) VALUES (:id, 'x', 'h', 'm', :hb, 10, 500, 8000, "
                    "1000, 'draft', now(), now())"
                ),
                {"id": uuid.uuid4(), "hb": holdout},
            )


class TestAssignmentUniqueness:
    def _assign(self, session: Session, risk_id: uuid.UUID, experiment_id: uuid.UUID) -> None:
        session.add(
            CaseAssignment(
                risk_id=risk_id,
                experiment_id=experiment_id,
                arm=Arm.TREATMENT.value,
                stratum_key="repeated_payment_failure|200000-500000|card|hdfc|standard",
                assignment_hash="a" * 64,
                assigned_at=STARTED_AT,
            )
        )
        session.flush()

    def test_one_case_is_assigned_once_per_experiment(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        risk_id = a_risk(db_session)

        self._assign(db_session, risk_id, experiment.id)
        with pytest.raises(IntegrityError):
            self._assign(db_session, risk_id, experiment.id)

    def test_the_same_case_may_join_a_second_experiment(self, db_session: Session) -> None:
        first = create_draft(db_session, a_draft("EXP-001"))
        second = create_draft(db_session, a_draft("EXP-002"))
        risk_id = a_risk(db_session)

        self._assign(db_session, risk_id, first.id)
        self._assign(db_session, risk_id, second.id)

    def test_an_unknown_arm_is_rejected(self, db_session: Session) -> None:
        experiment = create_draft(db_session, a_draft())
        risk_id = a_risk(db_session)
        db_session.add(
            CaseAssignment(
                risk_id=risk_id,
                experiment_id=experiment.id,
                arm="control",
                stratum_key="k",
                assignment_hash="a" * 64,
                assigned_at=STARTED_AT,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_assignments_are_append_only(self, db_session: Session) -> None:
        columns = db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'case_assignments'"
            )
        ).scalars()
        assert "updated_at" not in set(columns)


class TestOutcomeUniquenessAndSealing:
    def _outcome(self, risk_id: uuid.UUID, **overrides: object) -> CaseOutcome:
        fields: dict[str, object] = {
            "risk_id": risk_id,
            "window_opens_at": STARTED_AT,
            "window_closes_at": STARTED_AT + timedelta(hours=72),
            "recovered": False,
            "recovered_amount": 0,
        }
        fields.update(overrides)
        return CaseOutcome(**fields)  # type: ignore[arg-type]

    def test_one_outcome_per_case(self, db_session: Session) -> None:
        """Two rows would let an analysis double-count a recovery."""
        risk_id = a_risk(db_session)
        db_session.add(self._outcome(risk_id))
        db_session.flush()

        db_session.add(self._outcome(risk_id))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_sealing_requires_a_timestamp(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(self._outcome(risk_id, sealed=True))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_an_unsealed_outcome_may_not_carry_a_seal_time(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(self._outcome(risk_id, sealed=False, sealed_at=CLOSED_AT))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_sealed_outcome_is_valid(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(self._outcome(risk_id, sealed=True, sealed_at=CLOSED_AT))
        db_session.flush()

    def test_the_window_must_be_ordered(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(
            self._outcome(
                risk_id,
                window_opens_at=STARTED_AT,
                window_closes_at=STARTED_AT - timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_recovery_must_say_when(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(self._outcome(risk_id, recovered=True, recovered_amount=1000))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_an_unrecovered_case_carries_no_amount(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(self._outcome(risk_id, recovered=False, recovered_amount=1000))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_execution_failure_does_not_change_the_arm(self, db_session: Session) -> None:
        """Intention-to-treat: a failed execution stays in treatment."""
        experiment = create_draft(db_session, a_draft())
        risk_id = a_risk(db_session)
        db_session.add(
            CaseAssignment(
                risk_id=risk_id,
                experiment_id=experiment.id,
                arm=Arm.TREATMENT.value,
                stratum_key="k",
                assignment_hash="a" * 64,
                assigned_at=STARTED_AT,
            )
        )
        db_session.add(self._outcome(risk_id, execution_failed=True))
        db_session.flush()

        arm = db_session.execute(
            text("SELECT arm FROM case_assignments WHERE risk_id = :r"), {"r": risk_id}
        ).scalar()
        assert arm == Arm.TREATMENT.value


class TestRecoveryCaseDecisionColumns:
    """The decision columns live on `recovery_cases`, so these build one.

    Only this class does. Everywhere else the unit is the risk, which is what
    keeps `recovery_cases` empty until something is genuinely being recovered.
    """

    def _case(self, session: Session) -> uuid.UUID:
        return a_recovery_case(session, a_risk(session))

    def _set(self, session: Session, case_id: uuid.UUID, **values: object) -> None:
        assignments = ", ".join(f"{k} = :{k}" for k in values)
        session.execute(
            text(f"UPDATE recovery_cases SET {assignments} WHERE id = :id"),
            {"id": case_id, **values},
        )
        session.flush()

    def test_an_abstention_must_carry_a_reason(self, db_session: Session) -> None:
        case_id = self._case(db_session)
        with pytest.raises(IntegrityError):
            self._set(db_session, case_id, decision="abstain")

    def test_an_abstention_with_a_reason_is_accepted(self, db_session: Session) -> None:
        case_id = self._case(db_session)
        self._set(db_session, case_id, decision="abstain", abstain_reason="sleeping_dog")

    def test_a_reason_may_not_accompany_an_action(self, db_session: Session) -> None:
        case_id = self._case(db_session)
        with pytest.raises(IntegrityError):
            self._set(db_session, case_id, decision="act", abstain_reason="sleeping_dog")

    def test_a_holdout_case_can_never_act(self, db_session: Session) -> None:
        """The counterfactual rests on this, so the database refuses it."""
        case_id = self._case(db_session)
        with pytest.raises(IntegrityError):
            self._set(db_session, case_id, arm="holdout", decision="act")

    def test_a_holdout_case_may_abstain(self, db_session: Session) -> None:
        case_id = self._case(db_session)
        self._set(
            db_session, case_id, arm="holdout", decision="abstain", abstain_reason="holdout_arm"
        )

    def test_an_unknown_abstain_reason_is_rejected(self, db_session: Session) -> None:
        case_id = self._case(db_session)
        with pytest.raises(IntegrityError):
            self._set(db_session, case_id, decision="abstain", abstain_reason="felt_like_it")


class TestAuditDecisionTypes:
    def _audit(self, session: Session, **values: object) -> None:
        columns = ["id", "actor", "action", "is_execution", "created_at"]
        params: dict[str, object] = {
            "id": uuid.uuid4(),
            "actor": "engine",
            "action": "test",
            "is_execution": False,
        }
        params.update(values)
        columns = [c for c in columns if c != "created_at"] + list(values)
        columns = list(dict.fromkeys(columns))
        placeholders = ", ".join(f":{c}" for c in columns)
        session.execute(
            text(
                f"INSERT INTO audit_events ({', '.join(columns)}, created_at) "
                f"VALUES ({placeholders}, now())"
            ),
            {c: params[c] for c in columns},
        )
        session.flush()

    def test_every_decision_type_is_accepted(self, db_session: Session) -> None:
        from app.models.enums import DecisionType

        for decision_type in DecisionType.values():
            snapshot = '{"uplift_bps": 120}' if decision_type == "abstain" else None
            self._audit(
                db_session,
                decision_type=decision_type,
                numeric_snapshot=snapshot,
            )

    def test_an_unknown_decision_type_is_rejected(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            self._audit(db_session, decision_type="guessed")

    def test_an_abstention_must_record_its_numbers(self, db_session: Session) -> None:
        """A non-action with no numbers is an unexplained non-action."""
        with pytest.raises(IntegrityError):
            self._audit(db_session, decision_type="abstain")

    def test_an_abstention_is_never_an_execution(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            self._audit(
                db_session,
                decision_type="abstain",
                numeric_snapshot='{"uplift_bps": -30}',
                is_execution=True,
            )

    def test_an_ai_agent_still_cannot_execute(self, db_session: Session) -> None:
        """The Phase 1 authority constraint survives the migration."""
        with pytest.raises(IntegrityError):
            self._audit(db_session, actor="ai_agent", is_execution=True, decision_type="execute")


class TestInterventionCatalogue:
    def test_the_catalogue_is_seeded(self, db_session: Session) -> None:
        codes = db_session.execute(text("SELECT code FROM interventions")).scalars()
        assert {"create_payment_link", "retry_payment", "no_action"} <= set(codes)

    def test_codes_are_unique(self, db_session: Session) -> None:
        db_session.add(
            Intervention(
                code="create_payment_link",
                channel="payment_link",
                unit_cost=100,
                cooldown_hours=1,
                max_per_customer_per_month=1,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_an_unknown_channel_is_rejected(self, db_session: Session) -> None:
        db_session.add(
            Intervention(
                code="carrier_pigeon",
                channel="pigeon",
                unit_cost=0,
                cooldown_hours=1,
                max_per_customer_per_month=1,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_cost_cannot_be_negative(self, db_session: Session) -> None:
        db_session.add(
            Intervention(
                code="paid_to_send",
                channel="sms",
                unit_cost=-1,
                cooldown_hours=1,
                max_per_customer_per_month=1,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_zero_monthly_cap_is_rejected(self, db_session: Session) -> None:
        """Zero would mean 'never allowed', which is what is_active is for."""
        db_session.add(
            Intervention(
                code="never",
                channel="sms",
                unit_cost=0,
                cooldown_hours=1,
                max_per_customer_per_month=0,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestUpliftScores:
    def _score(
        self, risk_id: uuid.UUID, experiment_id: uuid.UUID, **overrides: object
    ) -> UpliftScore:
        fields: dict[str, object] = {
            "risk_id": risk_id,
            "experiment_id": experiment_id,
            "model_version": "t-learner.v1",
            "p_treat_bps": 5_500,
            "p_control_bps": 3_500,
            "uplift_bps": 2_000,
            "uplift_ci_low_bps": 1_200,
            "uplift_ci_high_bps": 2_800,
            "quadrant": Quadrant.PERSUADABLE.value,
            "scored_at": CLOSED_AT,
        }
        fields.update(overrides)
        return UpliftScore(**fields)  # type: ignore[arg-type]

    def test_a_negative_uplift_is_storable(self, db_session: Session) -> None:
        """Sleeping dogs are the finding, not an error to be constrained away."""
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(
            self._score(
                risk_id,
                experiment.id,
                p_treat_bps=5_800,
                p_control_bps=6_500,
                uplift_bps=-700,
                uplift_ci_low_bps=-1_400,
                uplift_ci_high_bps=-100,
                quadrant=Quadrant.SLEEPING_DOG.value,
            )
        )
        db_session.flush()

    def test_the_interval_must_be_ordered(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(
            self._score(risk_id, experiment.id, uplift_ci_low_bps=2_800, uplift_ci_high_bps=1_200)
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_the_estimate_must_lie_inside_its_interval(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(self._score(risk_id, experiment.id, uplift_bps=9_000))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_an_unknown_quadrant_is_rejected(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(self._score(risk_id, experiment.id, quadrant="probably_fine"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_rescoring_appends_rather_than_overwrites(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(self._score(risk_id, experiment.id, model_version="t-learner.v1"))
        db_session.add(self._score(risk_id, experiment.id, model_version="t-learner.v2"))
        db_session.flush()

        count = db_session.execute(
            text("SELECT count(*) FROM uplift_scores WHERE risk_id = :r"), {"r": risk_id}
        ).scalar()
        assert count == 2
