"""The abstention gate against revtrace_test.

What is tested here rather than in the pure suite: the storage-layer
guarantees. That an abstention writes exactly one `audit_events` row and
nothing else; that the row is anchored on `risk_id` rather than `case_id`; that
it can never be an execution; and — the one that matters most — that **no
abstention creates a recovery case**.

That last guarantee is the reason this layer exists at all. Fabricating a
recovery case to hold a non-action would mean inventing five NOT NULL money
figures nobody computed, and it would make "we declined to act" indistinguishable
from "we opened a case and did nothing".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.engine.policy_engine import ABSTAIN_ACTION, InterventionTerms, UpliftEvidence, decide
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
    RecoveryAction,
    RecoveryCase,
    RevenueRisk,
)
from app.models.enums import AbstainReason, ActorType, Arm, CaseDecision, DecisionType, Quadrant
from app.models.intervention import Intervention
from app.models.uplift_score import UpliftScore
from app.repositories.audit_repository import (
    AuditPersistenceError,
    record_abstention,
)
from app.services.recovery.gate import (
    GateError,
    evaluate_risk,
    load_arm,
    load_evidence,
    load_intervention,
)

pytestmark = pytest.mark.db

LOCKED_AT = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
STARTED_AT = LOCKED_AT + timedelta(hours=1)
AS_OF = STARTED_AT + timedelta(hours=1)
SALT = "revtrace-demo-salt-v1"
MODEL_VERSION = "cell-rate-t-learner-v1"


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


def a_risk(session: Session, merchant_id: uuid.UUID, *, amount: int = 230_400) -> RevenueRisk:
    risk = RevenueRisk(
        merchant_id=merchant_id,
        customer_id=None,
        order_id=None,
        risk_type="repeated_payment_failure",
        amount_at_risk=amount,
        currency="INR",
        confidence_bps=7_000,
        detection_rule="repeated_payment_failure.v1",
        detected_at=AS_OF,
        status="detected",
    )
    session.add(risk)
    session.flush()
    return risk


def a_score(
    session: Session,
    risk: RevenueRisk,
    experiment_id: uuid.UUID,
    *,
    uplift_bps: int = 1_500,
    low: int = 1_000,
    high: int = 2_000,
    quadrant: Quadrant = Quadrant.PERSUADABLE,
) -> UpliftScore:
    score = UpliftScore(
        risk_id=risk.id,
        experiment_id=experiment_id,
        model_version=MODEL_VERSION,
        p_treat_bps=5_000,
        p_control_bps=5_000 - uplift_bps,
        uplift_bps=uplift_bps,
        uplift_ci_low_bps=low,
        uplift_ci_high_bps=high,
        quadrant=quadrant.value,
        scored_at=AS_OF,
    )
    session.add(score)
    session.flush()
    return score


def an_enrolled_risk(session: Session, arm: Arm) -> tuple[RevenueRisk, uuid.UUID]:
    """A risk assigned into the requested arm.

    The arm is a hash of the identifiers, so it cannot be chosen — risks are
    created until one lands in the arm the test needs. That keeps the stored
    assignment honest instead of writing an arm the assignment code would not
    have produced.
    """
    experiment = a_running_experiment(session)
    merchant_id = a_merchant(session)
    for _ in range(200):
        risk = a_risk(session, merchant_id)
        assignment = assign_risk(session, experiment, risk, AS_OF, salt=SALT)
        assert assignment is not None
        if assignment.arm == arm.value:
            return risk, experiment.id
    raise AssertionError(f"no risk landed in {arm.value} after 200 attempts")


def counts(session: Session) -> dict[str, int]:
    return {
        "audit_events": session.execute(select(func.count()).select_from(AuditEvent)).scalar_one(),
        "recovery_cases": session.execute(
            select(func.count()).select_from(RecoveryCase)
        ).scalar_one(),
        "recovery_actions": session.execute(
            select(func.count()).select_from(RecoveryAction)
        ).scalar_one(),
    }


# -- loading --------------------------------------------------------------


class TestLoading:
    def test_the_seeded_interventions_are_readable(self, db_session: Session) -> None:
        terms = load_intervention(db_session, "create_payment_link")
        assert isinstance(terms, InterventionTerms)
        assert terms.unit_cost == 200
        assert terms.cooldown_hours == 24
        assert terms.max_per_customer_per_month == 3
        assert terms.is_active

    def test_an_unknown_intervention_is_refused(self, db_session: Session) -> None:
        with pytest.raises(GateError, match="no intervention"):
            load_intervention(db_session, "teleportation")

    def test_the_arm_is_read_not_recomputed(self, db_session: Session) -> None:
        risk, experiment_id = an_enrolled_risk(db_session, Arm.TREATMENT)
        assert load_arm(db_session, risk.id, experiment_id) is Arm.TREATMENT

    def test_an_unenrolled_risk_is_refused(self, db_session: Session) -> None:
        merchant_id = a_merchant(db_session)
        risk = a_risk(db_session, merchant_id)
        with pytest.raises(GateError, match="not enrolled"):
            load_arm(db_session, risk.id, uuid.uuid4())

    def test_a_missing_score_loads_as_none_not_zero(self, db_session: Session) -> None:
        risk, experiment_id = an_enrolled_risk(db_session, Arm.TREATMENT)
        loaded = load_evidence(db_session, risk.id, experiment_id, model_version=MODEL_VERSION)
        assert loaded is None

    def test_a_stored_score_round_trips(self, db_session: Session) -> None:
        risk, experiment_id = an_enrolled_risk(db_session, Arm.TREATMENT)
        a_score(db_session, risk, experiment_id)
        loaded = load_evidence(db_session, risk.id, experiment_id, model_version=MODEL_VERSION)
        assert loaded == UpliftEvidence(
            uplift_bps=1_500,
            uplift_ci_low_bps=1_000,
            uplift_ci_high_bps=2_000,
            quadrant=Quadrant.PERSUADABLE,
            qualified=True,
        )

    def test_a_gray_zone_score_loads_as_unqualified(self, db_session: Session) -> None:
        """A GRAY_ZONE row written *without* a qualified cell loads unqualified.

        The docstring here used to say GRAY_ZONE *is* the label for a cell that
        never qualified. That is one of two ways to reach it and, on the
        accepted N=10,000 population, the rarer one: all 1,879 GRAY_ZONE units
        there had `qualified=True` and reached the label through
        `RULE_UNDECIDED`. This test fixes what `load_evidence` does with the
        row it is given; it says nothing about which rule produced the label.
        """
        risk, experiment_id = an_enrolled_risk(db_session, Arm.TREATMENT)
        a_score(db_session, risk, experiment_id, quadrant=Quadrant.GRAY_ZONE)
        loaded = load_evidence(db_session, risk.id, experiment_id, model_version=MODEL_VERSION)
        assert loaded is not None
        assert not loaded.qualified


# -- the seam -------------------------------------------------------------


class TestHoldoutAbstains:
    def test_a_holdout_risk_abstains_and_is_audited(self, db_session: Session) -> None:
        risk, experiment_id = an_enrolled_risk(db_session, Arm.HOLDOUT)
        a_score(db_session, risk, experiment_id)
        before = counts(db_session)

        outcome = evaluate_risk(
            db_session,
            risk.id,
            experiment_id,
            intervention_code="create_payment_link",
            model_version=MODEL_VERSION,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )

        assert outcome.decision.decision is CaseDecision.ABSTAIN
        assert outcome.decision.reason is AbstainReason.HOLDOUT_ARM
        assert outcome.recorded

        after = counts(db_session)
        assert after["audit_events"] == before["audit_events"] + 1


class TestNoRecoveryCaseIsEverCreated:
    """The guarantee this whole design exists to hold."""

    @pytest.mark.parametrize("arm", [Arm.HOLDOUT, Arm.TREATMENT])
    def test_an_abstention_creates_no_case_and_no_action(
        self, db_session: Session, arm: Arm
    ) -> None:
        risk, experiment_id = an_enrolled_risk(db_session, arm)
        # No score at all, so a treatment risk abstains for insufficient sample.
        before = counts(db_session)

        outcome = evaluate_risk(
            db_session,
            risk.id,
            experiment_id,
            intervention_code="create_payment_link",
            model_version=MODEL_VERSION,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )
        after = counts(db_session)

        if outcome.decision.decision is CaseDecision.ABSTAIN:
            assert after["recovery_cases"] == before["recovery_cases"]
            assert after["recovery_actions"] == before["recovery_actions"]
            assert after["audit_events"] == before["audit_events"] + 1

    def test_the_seam_imports_no_recovery_case_writer(self) -> None:
        """`gate.py` may read models, but it must never construct a case."""
        import inspect

        from app.services.recovery import gate

        source = inspect.getsource(gate)
        assert "RecoveryCase(" not in source
        assert "RecoveryAction(" not in source

    def test_an_acting_decision_persists_nothing(self, db_session: Session) -> None:
        """Execution belongs to a later step that does not exist yet."""
        risk, experiment_id = an_enrolled_risk(db_session, Arm.TREATMENT)
        a_score(db_session, risk, experiment_id)
        before = counts(db_session)

        outcome = evaluate_risk(
            db_session,
            risk.id,
            experiment_id,
            intervention_code="create_payment_link",
            model_version=MODEL_VERSION,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )
        if outcome.decision.decision is CaseDecision.ACT:
            assert outcome.audit_event is None
            assert counts(db_session) == before


# -- the audit row --------------------------------------------------------


class TestTheAuditRow:
    def _an_abstention(self, session: Session) -> AuditEvent:
        risk, experiment_id = an_enrolled_risk(session, Arm.HOLDOUT)
        outcome = evaluate_risk(
            session,
            risk.id,
            experiment_id,
            intervention_code="create_payment_link",
            model_version=MODEL_VERSION,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )
        assert outcome.audit_event is not None
        return outcome.audit_event

    def test_it_is_anchored_on_risk_id_not_case_id(self, db_session: Session) -> None:
        event = self._an_abstention(db_session)
        assert event.risk_id is not None
        assert event.case_id is None

    def test_it_is_never_an_execution(self, db_session: Session) -> None:
        event = self._an_abstention(db_session)
        assert event.is_execution is False
        assert event.decision_type == DecisionType.ABSTAIN.value

    def test_the_database_refuses_an_abstain_execution(self, db_session: Session) -> None:
        """`abstain_is_never_execution`, independent of the repository."""
        risk, _ = an_enrolled_risk(db_session, Arm.TREATMENT)
        db_session.add(
            AuditEvent(
                risk_id=risk.id,
                actor=ActorType.ENGINE.value,
                action="FORCED",
                decision_type=DecisionType.ABSTAIN.value,
                is_execution=True,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_it_carries_the_action_and_a_rationale(self, db_session: Session) -> None:
        event = self._an_abstention(db_session)
        assert event.action == ABSTAIN_ACTION
        assert event.reason
        assert "holdout" in event.reason

    def test_the_snapshot_is_exact_and_recomputable(self, db_session: Session) -> None:
        event = self._an_abstention(db_session)
        snapshot = event.numeric_snapshot
        assert snapshot is not None
        assert snapshot["decision"] == CaseDecision.ABSTAIN.value
        assert snapshot["abstain_reason"] == AbstainReason.HOLDOUT_ARM.value
        assert snapshot["arm"] == Arm.HOLDOUT.value
        assert snapshot["decided_at"] == AS_OF.isoformat()
        assert snapshot["intervention_code"] == "create_payment_link"
        for key, value in snapshot.items():
            assert not isinstance(value, float), key


class TestTheRepositoryGuards:
    def test_a_blank_rationale_is_refused(self, db_session: Session) -> None:
        risk, _ = an_enrolled_risk(db_session, Arm.TREATMENT)
        with pytest.raises(AuditPersistenceError, match="rationale"):
            record_abstention(
                db_session,
                risk_id=risk.id,
                reason=AbstainReason.SURE_THING,
                rationale="   ",
                as_of=AS_OF,
            )

    def test_an_ai_actor_may_not_record_a_decision(self, db_session: Session) -> None:
        """The LLM is not the authority over money, including over not spending it."""
        risk, _ = an_enrolled_risk(db_session, Arm.TREATMENT)
        with pytest.raises(AuditPersistenceError, match="may not record"):
            record_abstention(
                db_session,
                risk_id=risk.id,
                reason=AbstainReason.SURE_THING,
                rationale="declined",
                as_of=AS_OF,
                actor=ActorType.AI_AGENT,
            )

    def test_a_naive_as_of_is_refused(self, db_session: Session) -> None:
        risk, _ = an_enrolled_risk(db_session, Arm.TREATMENT)
        with pytest.raises(AuditPersistenceError, match="timezone-aware"):
            record_abstention(
                db_session,
                risk_id=risk.id,
                reason=AbstainReason.SURE_THING,
                rationale="declined",
                as_of=datetime(2026, 8, 30, 12, 0),
            )

    def test_a_float_in_the_snapshot_is_refused(self, db_session: Session) -> None:
        """A money figure that arrived as a float is not a number to check."""
        risk, _ = an_enrolled_risk(db_session, Arm.TREATMENT)
        with pytest.raises(AuditPersistenceError, match="float"):
            record_abstention(
                db_session,
                risk_id=risk.id,
                reason=AbstainReason.SURE_THING,
                rationale="declined",
                numeric_snapshot={"uplift": 0.15},
                as_of=AS_OF,
            )

    def test_it_does_not_commit(self, db_session: Session) -> None:
        """The caller owns the transaction; the fixture's rollback must win."""
        import inspect

        from app.repositories import audit_repository

        assert "commit()" not in inspect.getsource(audit_repository)


# -- boundaries -----------------------------------------------------------


class TestBoundariesHold:
    def test_the_gate_never_writes_policy_status(self, db_session: Session) -> None:
        """CaseDecision is authoritative for this gate and nothing else.

        The Phase 3 `policy_status` vocabulary is deliberately untouched, and
        the two are **not** globally synchronised. Recording that here so a
        future reader does not assume a relationship that was never built.

        Checked over identifiers rather than raw text: both modules *document*
        the boundary in prose by naming the column, and a substring scan would
        fail on the sentence that states the constraint.
        """
        import ast
        import pathlib

        for relative in (
            "app/engine/policy_engine.py",
            "app/services/recovery/gate.py",
        ):
            tree = ast.parse(pathlib.Path(relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    assert node.id != "policy_status", relative
                elif isinstance(node, ast.Attribute):
                    assert node.attr != "policy_status", relative
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # A string literal naming the column would mean a query or a
                    # write; a docstring mentioning it in a sentence would not.
                    assert node.value != "policy_status", relative

    def test_no_causal_module_was_touched(self) -> None:
        """The gate reaches no estimator, and no estimator reaches the gate."""
        import ast
        import pathlib

        causal_root = pathlib.Path("app/causal")
        for path in sorted(causal_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    assert "policy_engine" not in module, f"{path.name} -> {module}"
                    assert "audit_repository" not in module, f"{path.name} -> {module}"

    def test_truth_isolation_is_intact(self) -> None:
        """No new module names a truth column."""
        import pathlib

        for relative in (
            "app/engine/policy_engine.py",
            "app/repositories/audit_repository.py",
            "app/services/recovery/gate.py",
        ):
            source = pathlib.Path(relative).read_text(encoding="utf-8")
            assert "truth_" not in source, relative
            assert "import simulator" not in source
            assert "from simulator" not in source

    def test_the_abstention_path_writes_only_audit_events(self, db_session: Session) -> None:
        """Every other table is untouched by an abstention."""
        risk, experiment_id = an_enrolled_risk(db_session, Arm.HOLDOUT)
        before = {
            "uplift_scores": db_session.execute(
                select(func.count()).select_from(UpliftScore)
            ).scalar_one(),
            "case_assignments": db_session.execute(
                select(func.count()).select_from(CaseAssignment)
            ).scalar_one(),
            "revenue_risks": db_session.execute(
                select(func.count()).select_from(RevenueRisk)
            ).scalar_one(),
            "interventions": db_session.execute(
                select(func.count()).select_from(Intervention)
            ).scalar_one(),
        }

        evaluate_risk(
            db_session,
            risk.id,
            experiment_id,
            intervention_code="create_payment_link",
            model_version=MODEL_VERSION,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )

        assert {
            "uplift_scores": db_session.execute(
                select(func.count()).select_from(UpliftScore)
            ).scalar_one(),
            "case_assignments": db_session.execute(
                select(func.count()).select_from(CaseAssignment)
            ).scalar_one(),
            "revenue_risks": db_session.execute(
                select(func.count()).select_from(RevenueRisk)
            ).scalar_one(),
            "interventions": db_session.execute(
                select(func.count()).select_from(Intervention)
            ).scalar_one(),
        } == before


class TestDeterminism:
    def test_the_same_inputs_decide_identically(self, db_session: Session) -> None:
        risk, experiment_id = an_enrolled_risk(db_session, Arm.TREATMENT)
        a_score(db_session, risk, experiment_id)

        kwargs = {
            "intervention_code": "create_payment_link",
            "model_version": MODEL_VERSION,
            "expected_recovery": risk.amount_at_risk,
            "max_cost": 5_000,
            "as_of": AS_OF,
        }
        first = evaluate_risk(db_session, risk.id, experiment_id, **kwargs)  # type: ignore[arg-type]
        second = evaluate_risk(db_session, risk.id, experiment_id, **kwargs)  # type: ignore[arg-type]

        assert first.decision == second.decision
        assert first.decision.numeric_snapshot() == second.decision.numeric_snapshot()

    def test_the_stored_arm_drives_the_decision(self, db_session: Session) -> None:
        """Re-deciding uses the arm as stored, never a recomputed one."""
        risk, experiment_id = an_enrolled_risk(db_session, Arm.HOLDOUT)
        terms = load_intervention(db_session, "create_payment_link")
        pure = decide(
            risk.id,
            experiment_id,
            arm=Arm.HOLDOUT,
            uplift=None,
            intervention=terms,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )
        seam = evaluate_risk(
            db_session,
            risk.id,
            experiment_id,
            intervention_code="create_payment_link",
            model_version=MODEL_VERSION,
            expected_recovery=risk.amount_at_risk,
            max_cost=5_000,
            as_of=AS_OF,
        )
        assert seam.decision.decision == pure.decision
        assert seam.decision.reason == pure.reason
