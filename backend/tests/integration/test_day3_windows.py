"""Observation windows against revtrace_test.

The guarantees that only a real database can demonstrate: that a window is
created once and never moved, that sealing refuses to run early, that sealing
twice changes nothing, and that the sweeper touches only what is due.

And the one that matters most for the result: sealing writes no recovery row,
approves nothing, and cannot tell a holdout from a treated unit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.experiments.windows import (
    LATE_EVENT_ACTION,
    CaseOutcome,
    WindowError,
    due_for_sealing,
    existing_outcome,
    is_late,
    is_sealable,
    late_event_entry,
    open_window,
    open_windows_for,
    seal,
    seal_due,
)
from app.models import AuditEvent, RecoveryAction, RecoveryCase, RevenueRisk
from app.models.enums import RiskType

pytestmark = pytest.mark.db

DETECTED_AT = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
AFTER_72H = DETECTED_AT + timedelta(hours=72)
AFTER_24H = DETECTED_AT + timedelta(hours=24)


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
    detected_at: datetime = DETECTED_AT,
    amount: int = 230_400,
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


class TestOpening:
    def test_a_window_is_created_from_detection(self, db_session: Session) -> None:
        risk = a_risk(db_session, a_merchant(db_session))
        outcome = open_window(db_session, risk)

        assert outcome is not None
        assert outcome.risk_id == risk.id
        assert outcome.window_opens_at == DETECTED_AT
        assert outcome.window_closes_at == AFTER_72H
        assert outcome.sealed is False
        assert outcome.sealed_at is None

    def test_each_risk_type_gets_its_own_duration(self, db_session: Session) -> None:
        merchant_id = a_merchant(db_session)
        expected = {
            RiskType.REPEATED_PAYMENT_FAILURE.value: 72,
            RiskType.CHECKOUT_ABANDONMENT.value: 24,
            RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value: 168,
        }
        for risk_type, hours in expected.items():
            risk = a_risk(db_session, merchant_id, risk_type=risk_type)
            outcome = open_window(db_session, risk)
            assert outcome is not None
            assert outcome.window_closes_at == DETECTED_AT + timedelta(hours=hours)

    def test_an_excluded_risk_type_gets_no_window(self, db_session: Session) -> None:
        risk = a_risk(
            db_session,
            a_merchant(db_session),
            risk_type=RiskType.RECONCILIATION_MISMATCH.value,
            amount=0,
        )
        assert open_window(db_session, risk) is None
        assert existing_outcome(db_session, risk.id) is None

    def test_opening_twice_returns_the_same_row(self, db_session: Session) -> None:
        risk = a_risk(db_session, a_merchant(db_session))
        first = open_window(db_session, risk)
        second = open_window(db_session, risk)

        assert first is not None and second is not None
        assert first.id == second.id

    def test_a_window_boundary_is_never_moved(self, db_session: Session) -> None:
        """Re-opening would silently shift a boundary an analysis may already
        have relied on."""
        risk = a_risk(db_session, a_merchant(db_session))
        open_window(db_session, risk)

        risk.detected_at = DETECTED_AT + timedelta(days=5)
        db_session.flush()
        reopened = open_window(db_session, risk)

        assert reopened is not None
        assert reopened.window_closes_at == AFTER_72H

    def test_only_one_outcome_can_exist_per_risk(self, db_session: Session) -> None:
        risk = a_risk(db_session, a_merchant(db_session))
        open_window(db_session, risk)

        db_session.add(
            CaseOutcome(
                risk_id=risk.id,
                window_opens_at=DETECTED_AT,
                window_closes_at=AFTER_72H,
                recovered=False,
                recovered_amount=0,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_bulk_open_counts_skips(self, db_session: Session) -> None:
        merchant_id = a_merchant(db_session)
        risks = [a_risk(db_session, merchant_id) for _ in range(5)]
        risks += [
            a_risk(
                db_session,
                merchant_id,
                risk_type=RiskType.RECONCILIATION_MISMATCH.value,
                amount=0,
            )
            for _ in range(3)
        ]

        opened, skipped = open_windows_for(db_session, risks)
        assert (opened, skipped) == (5, 3)


class TestSealingRefusesToRunEarly:
    def _outcome(self, session: Session) -> CaseOutcome:
        outcome = open_window(session, a_risk(session, a_merchant(session)))
        assert outcome is not None
        return outcome

    def test_sealing_before_the_close_time_is_refused(self, db_session: Session) -> None:
        """Sealing early freezes a number that is still moving — the same error
        as peeking, made permanent."""
        outcome = self._outcome(db_session)
        with pytest.raises(WindowError, match="refusing to seal"):
            seal(db_session, outcome, AFTER_72H - timedelta(seconds=1))

    def test_a_refused_seal_leaves_the_row_untouched(self, db_session: Session) -> None:
        outcome = self._outcome(db_session)
        with pytest.raises(WindowError):
            seal(db_session, outcome, DETECTED_AT)

        assert outcome.sealed is False
        assert outcome.sealed_at is None

    def test_sealing_exactly_at_the_close_time_is_allowed(self, db_session: Session) -> None:
        outcome = self._outcome(db_session)
        assert seal(db_session, outcome, AFTER_72H) is True
        assert outcome.sealed is True
        assert outcome.sealed_at == AFTER_72H

    def test_sealing_after_the_close_time_is_allowed(self, db_session: Session) -> None:
        outcome = self._outcome(db_session)
        later = AFTER_72H + timedelta(days=3)
        assert seal(db_session, outcome, later) is True
        assert outcome.sealed_at == later

    def test_is_sealable_agrees_with_seal(self, db_session: Session) -> None:
        outcome = self._outcome(db_session)
        assert not is_sealable(outcome, AFTER_72H - timedelta(seconds=1))
        assert is_sealable(outcome, AFTER_72H)

        seal(db_session, outcome, AFTER_72H)
        assert not is_sealable(outcome, AFTER_72H)

    def test_a_naive_timestamp_is_rejected(self, db_session: Session) -> None:
        outcome = self._outcome(db_session)
        with pytest.raises(WindowError, match="timezone-aware"):
            seal(db_session, outcome, datetime(2026, 9, 1, 9))  # noqa: DTZ001


class TestSealingIsIdempotent:
    def _sealed(self, session: Session) -> CaseOutcome:
        outcome = open_window(session, a_risk(session, a_merchant(session)))
        assert outcome is not None
        seal(session, outcome, AFTER_72H)
        return outcome

    def test_a_second_seal_reports_that_it_did_nothing(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        assert seal(db_session, outcome, AFTER_72H + timedelta(days=1)) is False

    def test_the_original_seal_time_survives(self, db_session: Session) -> None:
        """Re-stamping would make the seal moment depend on when someone last
        ran the sweeper."""
        outcome = self._sealed(db_session)
        seal(db_session, outcome, AFTER_72H + timedelta(days=30))
        assert outcome.sealed_at == AFTER_72H

    def test_a_sealed_outcome_is_otherwise_unchanged(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        before = (
            outcome.window_opens_at,
            outcome.window_closes_at,
            outcome.recovered,
            outcome.recovered_amount,
        )
        seal(db_session, outcome, AFTER_72H + timedelta(days=1))
        assert (
            outcome.window_opens_at,
            outcome.window_closes_at,
            outcome.recovered,
            outcome.recovered_amount,
        ) == before

    def test_sealing_an_already_sealed_row_never_raises(self, db_session: Session) -> None:
        """Even asked with an early instant: it is already final, so there is
        nothing to refuse."""
        outcome = self._sealed(db_session)
        assert seal(db_session, outcome, DETECTED_AT) is False


class TestSweeper:
    def _population(self, session: Session) -> uuid.UUID:
        merchant_id = a_merchant(session)
        for _ in range(4):
            open_window(
                session,
                a_risk(session, merchant_id, risk_type=RiskType.CHECKOUT_ABANDONMENT.value),
            )
        for _ in range(3):
            open_window(session, a_risk(session, merchant_id))
        return merchant_id

    def test_it_seals_only_what_is_due(self, db_session: Session) -> None:
        """At +24h the abandonment windows have closed and the 72h ones have
        not."""
        self._population(db_session)
        summary = seal_due(db_session, AFTER_24H)

        assert summary.examined == 4
        assert summary.sealed == 4
        assert summary.still_open == 3

    def test_a_later_sweep_catches_the_rest(self, db_session: Session) -> None:
        self._population(db_session)
        seal_due(db_session, AFTER_24H)
        summary = seal_due(db_session, AFTER_72H)

        assert summary.sealed == 3
        assert summary.still_open == 0
        assert summary.already_sealed == 4

    def test_a_sweep_before_anything_closes_does_nothing(self, db_session: Session) -> None:
        self._population(db_session)
        summary = seal_due(db_session, DETECTED_AT + timedelta(hours=1))

        assert summary.examined == 0
        assert summary.sealed == 0
        assert summary.still_open == 7

    def test_re_running_a_sweep_seals_nothing_new(self, db_session: Session) -> None:
        self._population(db_session)
        first = seal_due(db_session, AFTER_72H)
        second = seal_due(db_session, AFTER_72H)

        assert first.sealed == 7
        assert second.sealed == 0
        assert second.already_sealed == 7

    def test_seal_times_survive_a_repeat_sweep(self, db_session: Session) -> None:
        self._population(db_session)
        seal_due(db_session, AFTER_24H)
        before = {
            o.risk_id: o.sealed_at
            for o in db_session.execute(select(CaseOutcome)).scalars()
            if o.sealed
        }
        seal_due(db_session, AFTER_72H + timedelta(days=7))
        after = {
            o.risk_id: o.sealed_at
            for o in db_session.execute(select(CaseOutcome)).scalars()
            if o.risk_id in before
        }
        assert before == after

    def test_due_for_sealing_is_ordered(self, db_session: Session) -> None:
        self._population(db_session)
        due = due_for_sealing(db_session, AFTER_72H)
        closes = [o.window_closes_at for o in due]
        assert closes == sorted(closes)

    def test_a_naive_sweep_instant_is_rejected(self, db_session: Session) -> None:
        with pytest.raises(WindowError, match="timezone-aware"):
            seal_due(db_session, datetime(2026, 9, 1, 9))  # noqa: DTZ001

    def test_every_sealed_row_satisfies_the_database_constraint(self, db_session: Session) -> None:
        """`ck_case_outcomes_sealed_requires_timestamp`, both directions."""
        self._population(db_session)
        seal_due(db_session, AFTER_72H)
        db_session.flush()

        for outcome in db_session.execute(select(CaseOutcome)).scalars():
            assert outcome.sealed is True
            assert outcome.sealed_at is not None


class TestLateEvents:
    def _sealed(self, session: Session) -> CaseOutcome:
        outcome = open_window(session, a_risk(session, a_merchant(session)))
        assert outcome is not None
        seal(session, outcome, AFTER_72H)
        return outcome

    def test_an_event_inside_the_window_is_not_late(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        assert not is_late(outcome, DETECTED_AT + timedelta(hours=1))

    def test_an_event_after_the_close_time_is_late(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        assert is_late(outcome, AFTER_72H + timedelta(minutes=1))

    def test_lateness_keys_on_the_window_not_the_seal(self, db_session: Session) -> None:
        """The sweeper may run whenever. Keying on `sealed_at` would make
        lateness a function of operational timing rather than the window."""
        outcome = open_window(db_session, a_risk(db_session, a_merchant(db_session)))
        assert outcome is not None
        seal(db_session, outcome, AFTER_72H + timedelta(days=10))

        just_after_close = AFTER_72H + timedelta(seconds=1)
        assert is_late(outcome, just_after_close)

    def test_the_audit_payload_uses_the_agreed_convention(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        entry = late_event_entry(outcome, AFTER_72H + timedelta(hours=2))

        assert entry["decision_type"] == "verify"
        assert entry["action"] == LATE_EVENT_ACTION
        assert entry["is_execution"] is False
        assert entry["risk_id"] == outcome.risk_id

    def test_the_payload_records_how_late_the_event_was(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        entry = late_event_entry(outcome, AFTER_72H + timedelta(hours=2))
        assert entry["numeric_snapshot"]["seconds_late"] == 7_200

    def test_a_late_event_audit_row_persists(self, db_session: Session) -> None:
        outcome = self._sealed(db_session)
        entry = late_event_entry(
            outcome,
            AFTER_72H + timedelta(hours=1),
            external_event_id="sim_evt_late_1",
            event_type="payment.captured",
        )
        db_session.add(
            AuditEvent(
                risk_id=entry["risk_id"],
                actor=entry["actor"],
                action=entry["action"],
                reason=entry["reason"],
                decision_type=entry["decision_type"],
                is_execution=False,
                numeric_snapshot=entry["numeric_snapshot"],
            )
        )
        db_session.flush()

        stored = db_session.execute(select(AuditEvent)).scalars().one()
        assert stored.action == LATE_EVENT_ACTION
        assert stored.decision_type == "verify"
        assert stored.risk_id == outcome.risk_id
        assert stored.case_id is None
        assert stored.numeric_snapshot["external_event_id"] == "sim_evt_late_1"

    def test_recording_a_late_event_does_not_change_the_outcome(self, db_session: Session) -> None:
        """Absorbing it would make the measured effect depend on when anyone
        looked."""
        outcome = self._sealed(db_session)
        before = (outcome.recovered, outcome.recovered_amount, outcome.sealed_at)

        late_event_entry(outcome, AFTER_72H + timedelta(days=1))
        assert (outcome.recovered, outcome.recovered_amount, outcome.sealed_at) == before


class TestAuthorityBoundary:
    def test_sealing_creates_no_recovery_rows(self, db_session: Session) -> None:
        merchant_id = a_merchant(db_session)
        for _ in range(6):
            open_window(db_session, a_risk(db_session, merchant_id))

        seal_due(db_session, AFTER_72H)
        db_session.flush()

        for model in (RecoveryCase, RecoveryAction):
            assert db_session.scalar(select(func.count()).select_from(model)) == 0

    def test_sealing_writes_no_audit_row_of_its_own(self, db_session: Session) -> None:
        """`late_event_entry` returns a payload; persisting it is the caller's
        decision, so the sealer cannot quietly become an audit writer."""
        merchant_id = a_merchant(db_session)
        open_window(db_session, a_risk(db_session, merchant_id))

        seal_due(db_session, AFTER_72H)
        db_session.flush()
        assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 0

    def test_sealing_never_records_a_recovery(self, db_session: Session) -> None:
        """Deciding what the outcome was is verification's job. A sealer that
        also set the number could edit what it is meant to freeze."""
        outcome = open_window(db_session, a_risk(db_session, a_merchant(db_session)))
        assert outcome is not None
        seal(db_session, outcome, AFTER_72H)

        assert outcome.recovered is False
        assert outcome.recovered_amount == 0
        assert outcome.recovered_at is None

    def test_sealing_is_blind_to_the_arm(self, db_session: Session) -> None:
        """A holdout window closes exactly like a treated one. Knowing which is
        which could only invite bias into when a number is frozen."""
        from app.experiments.assignment import assign_for_merchant
        from app.experiments.registry import (
            ExperimentDraft,
            create_draft,
            lock_experiment,
            start_experiment,
        )

        experiment = create_draft(
            db_session,
            ExperimentDraft(
                name="EXP-windows",
                hypothesis="h",
                primary_metric="recovery_rate",
                holdout_bps=5_000,
                planned_n_per_arm=10,
                mde_bps=1_000,
            ),
        )
        lock_experiment(db_session, experiment, DETECTED_AT - timedelta(hours=2))
        start_experiment(db_session, experiment, DETECTED_AT - timedelta(hours=1))

        merchant_id = a_merchant(db_session)
        risks = [a_risk(db_session, merchant_id) for _ in range(30)]
        assign_for_merchant(
            db_session, experiment, merchant_id, DETECTED_AT, salt="revtrace-demo-salt-v1"
        )
        open_windows_for(db_session, risks)

        summary = seal_due(db_session, AFTER_72H)
        assert summary.sealed == 30

        for outcome in db_session.execute(select(CaseOutcome)).scalars():
            assert outcome.sealed_at == AFTER_72H
