"""Observation windows — the pure half.

Durations, boundaries, and lateness are arithmetic over injected instants, so
they are tested without a database. The storage half — opening, sealing,
idempotence, the sweeper — is in `tests/integration/test_day3_windows.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.experiments.windows import (
    LATE_EVENT_ACTION,
    WINDOW_HOURS,
    Window,
    WindowError,
    has_window,
    window_for,
    window_hours,
)
from app.models.enums import RiskType

DETECTED_AT = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
RISK_ID = uuid.UUID("cccccccc-0000-4000-8000-000000000001")


def a_window(risk_type: str = RiskType.REPEATED_PAYMENT_FAILURE.value) -> Window:
    opens_at, closes_at = window_for(risk_type, DETECTED_AT)
    return Window(risk_id=RISK_ID, risk_type=risk_type, opens_at=opens_at, closes_at=closes_at)


class TestPreRegisteredDurations:
    def test_the_three_durations(self) -> None:
        assert window_hours(RiskType.REPEATED_PAYMENT_FAILURE.value) == 72
        assert window_hours(RiskType.CHECKOUT_ABANDONMENT.value) == 24
        assert window_hours(RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value) == 168

    def test_exactly_three_risk_types_are_observed(self) -> None:
        assert set(WINDOW_HOURS) == {
            RiskType.REPEATED_PAYMENT_FAILURE.value,
            RiskType.CHECKOUT_ABANDONMENT.value,
            RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
        }

    def test_a_subscription_window_is_a_full_billing_week(self) -> None:
        assert window_hours(RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value) == 7 * 24

    def test_every_duration_is_a_positive_whole_number_of_hours(self) -> None:
        for risk_type, hours in WINDOW_HOURS.items():
            assert isinstance(hours, int) and not isinstance(hours, bool), risk_type
            assert hours > 0, risk_type


class TestExclusions:
    def test_reconciliation_mismatch_gets_no_window(self) -> None:
        """Zero at risk (ADR 0007) — there is nothing to observe."""
        assert not has_window(RiskType.RECONCILIATION_MISMATCH.value)
        with pytest.raises(WindowError, match="excluded from observation"):
            window_hours(RiskType.RECONCILIATION_MISMATCH.value)

    def test_payment_degradation_gets_no_window(self) -> None:
        assert not has_window(RiskType.PAYMENT_DEGRADATION.value)

    def test_the_observed_set_matches_what_assignment_randomises(self) -> None:
        """A risk that is never randomised must never acquire a window, or the
        sweeper would seal outcomes for units no experiment contains."""
        from app.experiments.assignment import EXCLUDED_RISK_TYPES

        observed = {t for t in RiskType.values() if has_window(t)}
        assert observed == set(RiskType.values()) - EXCLUDED_RISK_TYPES

    def test_an_unknown_risk_type_is_rejected(self) -> None:
        with pytest.raises(WindowError, match="unknown risk_type"):
            window_hours("vibes")


class TestWindowArithmetic:
    def test_the_window_opens_at_detection(self) -> None:
        opens_at, _ = window_for(RiskType.REPEATED_PAYMENT_FAILURE.value, DETECTED_AT)
        assert opens_at == DETECTED_AT

    def test_the_close_time_is_the_duration_later(self) -> None:
        _, closes_at = window_for(RiskType.REPEATED_PAYMENT_FAILURE.value, DETECTED_AT)
        assert closes_at == DETECTED_AT + timedelta(hours=72)

    def test_each_risk_type_closes_at_its_own_time(self) -> None:
        closes = {risk_type: window_for(risk_type, DETECTED_AT)[1] for risk_type in WINDOW_HOURS}
        assert len(set(closes.values())) == 3

    def test_it_is_deterministic(self) -> None:
        assert window_for("checkout_abandonment", DETECTED_AT) == window_for(
            "checkout_abandonment", DETECTED_AT
        )

    def test_a_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(WindowError, match="timezone-aware"):
            window_for("checkout_abandonment", datetime(2026, 8, 29, 9))  # noqa: DTZ001

    def test_duration_hours_round_trips(self) -> None:
        for risk_type, hours in WINDOW_HOURS.items():
            assert a_window(risk_type).duration_hours == hours

    def test_the_window_is_always_ordered(self) -> None:
        """`ck_case_outcomes_window_ordered` requires closes > opens."""
        for risk_type in WINDOW_HOURS:
            window = a_window(risk_type)
            assert window.closes_at > window.opens_at


class TestClosureBoundary:
    def test_it_is_open_before_the_close_time(self) -> None:
        window = a_window()
        assert not window.is_closed_at(window.closes_at - timedelta(seconds=1))

    def test_it_is_closed_exactly_at_the_close_time(self) -> None:
        """Inclusive at the boundary: the window covers up to, not through."""
        window = a_window()
        assert window.is_closed_at(window.closes_at)

    def test_it_stays_closed_afterwards(self) -> None:
        window = a_window()
        assert window.is_closed_at(window.closes_at + timedelta(days=30))

    def test_it_is_open_at_the_moment_it_opens(self) -> None:
        window = a_window()
        assert not window.is_closed_at(window.opens_at)

    def test_contains_covers_the_half_open_interval(self) -> None:
        window = a_window()
        assert window.contains(window.opens_at)
        assert window.contains(window.closes_at - timedelta(seconds=1))
        assert not window.contains(window.closes_at)
        assert not window.contains(window.opens_at - timedelta(seconds=1))


class TestLateEventConvention:
    def test_the_action_is_the_agreed_constant(self) -> None:
        assert LATE_EVENT_ACTION == "LATE_EVENT"

    def test_no_new_decision_type_was_invented(self) -> None:
        """The convention reuses `verify`; the vocabulary did not grow."""
        from app.models.enums import DecisionType

        assert "late_event" not in DecisionType.values()
        assert DecisionType.VERIFY.value == "verify"


class TestPurity:
    """Windows compute and seal. They do not act, and they read no clock."""

    @staticmethod
    def _identifiers() -> set[str]:
        import ast
        import inspect
        import pathlib

        from app.experiments import windows as module

        tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                found.add(node.name)
        return found

    @staticmethod
    def _imports() -> set[str]:
        import ast
        import inspect
        import pathlib

        from app.experiments import windows as module

        tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    def test_it_reads_no_clock(self) -> None:
        for name in ("now", "utcnow", "today"):
            assert name not in self._identifiers(), name

    def test_there_is_no_scheduler(self) -> None:
        """`seal_due` is a plain callable; the caller supplies the instant."""
        for module in self._imports():
            for banned in ("celery", "apscheduler", "schedule", "crontab", "asyncio"):
                assert banned not in module.lower(), module

    def test_it_creates_no_recovery_or_policy_concept(self) -> None:
        identifiers = self._identifiers()
        for banned in (
            "RecoveryCase",
            "RecoveryAction",
            "approve",
            "approved",
            "policy_status",
            "execute_action",
            "recommend",
        ):
            assert banned not in identifiers, banned

    def test_it_touches_only_outcomes_and_risks(self) -> None:
        import ast
        import inspect
        import pathlib

        from app.experiments import windows as module

        source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                imported.update(alias.name for alias in node.names)
        assert imported == {"CaseOutcome", "RevenueRisk"}

    def test_it_never_names_an_arm(self) -> None:
        """Sealing is arm-blind: a holdout window closes exactly like a treated
        one, and knowing which is which could only invite bias."""
        identifiers = self._identifiers()
        assert "Arm" not in identifiers
        assert "arm" not in identifiers
        assert "treatment" not in identifiers
        assert "holdout" not in identifiers

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_")

    def test_it_does_not_record_what_the_outcome_was(self) -> None:
        """Sealing freezes a number; deciding the number is verification's job.
        Keeping them apart is what stops the sealer editing what it freezes."""
        identifiers = self._identifiers()
        assert "recovered_amount" not in identifiers
        assert "recovered_at" not in identifiers
