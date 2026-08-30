"""Pre-registration lifecycle and immutability, at the application layer.

Hermetic: the transition rules and draft validation are pure, so they are tested
without a database. The database-level guarantees — the CHECK constraints and
the lock-guard trigger — are tested separately in
`tests/integration/test_day1_schema.py`, because a promise that only holds in
Python is not the promise this project makes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.experiments.registry import (
    DEFAULT_ALPHA_BPS,
    DEFAULT_POWER_BPS,
    ExperimentDraft,
    PreRegistrationError,
    validate_transition,
)
from app.models.enums import EXPERIMENT_TRANSITIONS, ExperimentStatus

LOCKED_AT = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def valid_draft(**overrides: object) -> ExperimentDraft:
    fields: dict[str, object] = {
        "name": "EXP-001",
        "hypothesis": "Intervention increases recovery within the window.",
        "primary_metric": "recovery_rate",
        "holdout_bps": 5_000,
        "planned_n_per_arm": 384,
        "mde_bps": 1_000,
    }
    fields.update(overrides)
    return ExperimentDraft(**fields)  # type: ignore[arg-type]


class TestDraftValidation:
    def test_a_valid_draft_is_accepted(self) -> None:
        draft = valid_draft()
        assert draft.alpha_bps == DEFAULT_ALPHA_BPS
        assert draft.power_bps == DEFAULT_POWER_BPS

    @pytest.mark.parametrize("field", ["name", "hypothesis", "primary_metric"])
    def test_blank_text_is_rejected(self, field: str) -> None:
        for bad in ("", "   "):
            with pytest.raises(PreRegistrationError):
                valid_draft(**{field: bad})

    def test_a_holdout_of_zero_or_everything_is_not_an_experiment(self) -> None:
        for bad in (0, 10_000):
            with pytest.raises(PreRegistrationError, match="holdout_bps"):
                valid_draft(holdout_bps=bad)

    def test_statistical_parameters_must_be_integers(self) -> None:
        """Basis points, never floats: a pre-registered threshold has to
        compare exactly across runs."""
        for field in ("holdout_bps", "alpha_bps", "power_bps", "mde_bps"):
            with pytest.raises(PreRegistrationError, match="basis points"):
                valid_draft(**{field: 0.05})

    def test_booleans_are_not_integers_here(self) -> None:
        with pytest.raises(PreRegistrationError):
            valid_draft(holdout_bps=True)

    def test_planned_n_must_be_positive(self) -> None:
        for bad in (0, -1):
            with pytest.raises(PreRegistrationError, match="planned_n_per_arm"):
                valid_draft(planned_n_per_arm=bad)

    def test_draft_is_frozen(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclass
            valid_draft().name = "changed"  # type: ignore[misc]


class TestTransitions:
    def test_the_happy_path(self) -> None:
        validate_transition("draft", "locked")
        validate_transition("locked", "running")
        validate_transition("running", "closed")

    def test_a_locked_experiment_may_close_without_running(self) -> None:
        validate_transition("locked", "closed")

    def test_there_is_no_unlock(self) -> None:
        """Un-locking would destroy the property pre-registration creates."""
        with pytest.raises(PreRegistrationError, match="cannot move"):
            validate_transition("locked", "draft")

    def test_closed_is_terminal(self) -> None:
        assert EXPERIMENT_TRANSITIONS[ExperimentStatus.CLOSED] == frozenset()
        for target in ("draft", "locked", "running", "closed"):
            with pytest.raises(PreRegistrationError):
                validate_transition("closed", target)

    def test_a_draft_cannot_start_running(self) -> None:
        """Running before locking would mean collecting data against a
        specification that could still change."""
        with pytest.raises(PreRegistrationError, match="cannot move"):
            validate_transition("draft", "running")

    def test_a_draft_cannot_close(self) -> None:
        with pytest.raises(PreRegistrationError):
            validate_transition("draft", "closed")

    def test_self_transitions_are_rejected(self) -> None:
        for status in ("draft", "locked", "running", "closed"):
            with pytest.raises(PreRegistrationError):
                validate_transition(status, status)

    def test_unknown_status_is_rejected(self) -> None:
        with pytest.raises(PreRegistrationError):
            validate_transition("draft", "unlocked")
        with pytest.raises(PreRegistrationError):
            validate_transition("paused", "locked")

    def test_every_status_has_a_transition_entry(self) -> None:
        assert set(EXPERIMENT_TRANSITIONS) == set(ExperimentStatus)

    def test_no_transition_leads_back_to_draft(self) -> None:
        for allowed in EXPERIMENT_TRANSITIONS.values():
            assert ExperimentStatus.DRAFT not in allowed


class TestFrozenColumnList:
    def test_the_model_and_the_migration_agree(self) -> None:
        """The trigger body is built from the migration's list. If they drift,
        a newly pre-registered field would silently remain editable."""
        import importlib.util
        import pathlib

        from app.models.experiment import FROZEN_AFTER_LOCK

        path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "alembic"
            / "versions"
            / "865983031dd2_day_1_incrementality_ledger_schema.py"
        )
        spec = importlib.util.spec_from_file_location("day1_migration", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.FROZEN_AFTER_LOCK == FROZEN_AFTER_LOCK

    def test_the_pre_registered_specification_is_covered(self) -> None:
        from app.models.experiment import FROZEN_AFTER_LOCK

        assert set(FROZEN_AFTER_LOCK) >= {
            "hypothesis",
            "primary_metric",
            "holdout_bps",
            "planned_n_per_arm",
            "alpha_bps",
            "power_bps",
            "mde_bps",
            "locked_at",
        }

    def test_lifecycle_columns_stay_mutable(self) -> None:
        """The experiment still has to be able to advance and close."""
        from app.models.experiment import FROZEN_AFTER_LOCK

        for column in ("status", "started_at", "closed_at"):
            assert column not in FROZEN_AFTER_LOCK
