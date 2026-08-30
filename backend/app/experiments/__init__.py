"""Experiment pre-registration, assignment, and observation windows.

Day 1 ships the registry only. Assignment (`assignment.py`) and window
management (`windows.py`) land on Day 3.
"""

from app.experiments.registry import (
    ExperimentDraft,
    PreRegistrationError,
    close_experiment,
    create_draft,
    is_mutable,
    lock_experiment,
    start_experiment,
    update_draft,
    validate_transition,
)

__all__ = [
    "ExperimentDraft",
    "PreRegistrationError",
    "close_experiment",
    "create_draft",
    "is_mutable",
    "lock_experiment",
    "start_experiment",
    "update_draft",
    "validate_transition",
]
