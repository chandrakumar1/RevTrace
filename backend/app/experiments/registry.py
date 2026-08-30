"""Pre-registration and the lifecycle it protects.

The claim this project makes is that its results were specified *before* the
data arrived. That claim is only worth something if the specification genuinely
could not change afterwards, so the freeze is enforced three times over:

1. **Here**, in application code, which produces a readable error and is the
   layer the API surfaces.
2. **A CHECK constraint**, which refuses to store a non-draft row without a
   `locked_at`.
3. **A database trigger**, which rejects any UPDATE touching a frozen column
   once `locked_at` is set — including one issued by a psql session, a future
   migration, or a bug in this file.

Layer 3 is the one that matters. Layers 1 and 2 are conveniences; a guarantee
that only holds when callers are well-behaved is not a guarantee, and "the API
rejects it" is a weaker promise than the pitch requires.

There is deliberately no `unlock`. Un-locking a pre-registration would destroy
the property it exists to create, so the only path out of LOCKED is forward.

`as_of` is injected rather than read from a clock, matching the rest of the
codebase: a lock timestamp that a test cannot control is a lock timestamp that
cannot be asserted.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Experiment
from app.models.enums import EXPERIMENT_TRANSITIONS, ExperimentStatus
from app.models.experiment import FROZEN_AFTER_LOCK

#: Defaults matching the pre-registration document: alpha 0.05, power 0.80.
DEFAULT_ALPHA_BPS = 500
DEFAULT_POWER_BPS = 8000


class PreRegistrationError(ValueError):
    """A lifecycle or immutability rule was violated."""


@dataclass(frozen=True, slots=True)
class ExperimentDraft:
    """Everything that must be decided before an experiment may be locked.

    A draft carries no status and no timestamps: it is the specification, not
    the run. Validation happens here so a malformed pre-registration fails
    before a row exists rather than after.
    """

    name: str
    hypothesis: str
    primary_metric: str
    holdout_bps: int
    planned_n_per_arm: int
    mde_bps: int
    secondary_metrics: tuple[str, ...] = ()
    strata_definition: Mapping[str, Any] = field(default_factory=dict)
    alpha_bps: int = DEFAULT_ALPHA_BPS
    power_bps: int = DEFAULT_POWER_BPS

    def __post_init__(self) -> None:
        for label, value in (("name", self.name), ("hypothesis", self.hypothesis)):
            if not isinstance(value, str) or not value.strip():
                raise PreRegistrationError(f"{label} must be a non-empty string")

        if not isinstance(self.primary_metric, str) or not self.primary_metric.strip():
            raise PreRegistrationError("primary_metric must be a non-empty string")

        for bps_label, bps_value, low, high in (
            ("holdout_bps", self.holdout_bps, 1, 9_999),
            ("alpha_bps", self.alpha_bps, 1, 9_999),
            ("power_bps", self.power_bps, 1, 9_999),
            ("mde_bps", self.mde_bps, 1, 10_000),
        ):
            if isinstance(bps_value, bool) or not isinstance(bps_value, int):
                raise PreRegistrationError(f"{bps_label} must be an integer in basis points")
            if not low <= bps_value <= high:
                raise PreRegistrationError(
                    f"{bps_label} must be between {low} and {high}, got {bps_value}"
                )

        if isinstance(self.planned_n_per_arm, bool) or not isinstance(self.planned_n_per_arm, int):
            raise PreRegistrationError("planned_n_per_arm must be an integer")
        if self.planned_n_per_arm < 1:
            raise PreRegistrationError("planned_n_per_arm must be at least 1")


def validate_transition(current: str, target: str) -> None:
    """Pure lifecycle check. Raises rather than returning a flag."""
    try:
        current_status = ExperimentStatus(current)
        target_status = ExperimentStatus(target)
    except ValueError as exc:
        raise PreRegistrationError(str(exc)) from exc

    allowed = EXPERIMENT_TRANSITIONS[current_status]
    if target_status not in allowed:
        permitted = sorted(s.value for s in allowed) or ["nothing: closed is terminal"]
        raise PreRegistrationError(
            f"cannot move an experiment from {current} to {target}; permitted: {permitted}"
        )


def is_mutable(experiment: Experiment) -> bool:
    """Whether the pre-registered fields may still be edited."""
    return experiment.locked_at is None and experiment.status == ExperimentStatus.DRAFT.value


def _require_mutable(experiment: Experiment) -> None:
    if not is_mutable(experiment):
        raise PreRegistrationError(
            f"experiment {experiment.id} was pre-registered at {experiment.locked_at}; "
            f"{list(FROZEN_AFTER_LOCK)} are frozen. There is no unlock."
        )


def create_draft(session: Session, draft: ExperimentDraft) -> Experiment:
    """Create a DRAFT experiment. Nothing is frozen yet."""
    experiment = Experiment(
        name=draft.name,
        hypothesis=draft.hypothesis,
        primary_metric=draft.primary_metric,
        secondary_metrics=list(draft.secondary_metrics),
        holdout_bps=draft.holdout_bps,
        strata_definition=dict(draft.strata_definition),
        planned_n_per_arm=draft.planned_n_per_arm,
        alpha_bps=draft.alpha_bps,
        power_bps=draft.power_bps,
        mde_bps=draft.mde_bps,
        status=ExperimentStatus.DRAFT.value,
    )
    session.add(experiment)
    session.flush()
    return experiment


def update_draft(session: Session, experiment: Experiment, draft: ExperimentDraft) -> Experiment:
    """Replace a draft's specification. Refused once locked."""
    _require_mutable(experiment)

    experiment.name = draft.name
    experiment.hypothesis = draft.hypothesis
    experiment.primary_metric = draft.primary_metric
    experiment.secondary_metrics = list(draft.secondary_metrics)
    experiment.holdout_bps = draft.holdout_bps
    experiment.strata_definition = dict(draft.strata_definition)
    experiment.planned_n_per_arm = draft.planned_n_per_arm
    experiment.alpha_bps = draft.alpha_bps
    experiment.power_bps = draft.power_bps
    experiment.mde_bps = draft.mde_bps

    session.flush()
    return experiment


def lock_experiment(session: Session, experiment: Experiment, as_of: datetime) -> Experiment:
    """Pre-register. After this the specification cannot change.

    `as_of` is the pre-registration timestamp and is what the evaluation report
    cites. It is supplied rather than read from a clock so the moment is
    explicit and reproducible.
    """
    if as_of.tzinfo is None:
        raise PreRegistrationError("as_of must be timezone-aware")

    validate_transition(experiment.status, ExperimentStatus.LOCKED.value)

    experiment.locked_at = as_of
    experiment.status = ExperimentStatus.LOCKED.value
    session.flush()
    return experiment


def start_experiment(session: Session, experiment: Experiment, as_of: datetime) -> Experiment:
    """Begin assigning cases. Only a locked experiment may run."""
    if as_of.tzinfo is None:
        raise PreRegistrationError("as_of must be timezone-aware")

    validate_transition(experiment.status, ExperimentStatus.RUNNING.value)

    if experiment.locked_at is None:  # pragma: no cover - blocked by the transition table
        raise PreRegistrationError("an experiment cannot run before it is pre-registered")
    if as_of < experiment.locked_at:
        raise PreRegistrationError("started_at cannot precede locked_at")

    experiment.started_at = as_of
    experiment.status = ExperimentStatus.RUNNING.value
    session.flush()
    return experiment


def close_experiment(session: Session, experiment: Experiment, as_of: datetime) -> Experiment:
    """Stop the experiment. Terminal — there is no reopening.

    Closing is what makes the fixed-horizon rule real: once closed, no further
    cases enter, so a disappointing interim reading cannot be rescued by
    collecting more data.
    """
    if as_of.tzinfo is None:
        raise PreRegistrationError("as_of must be timezone-aware")

    validate_transition(experiment.status, ExperimentStatus.CLOSED.value)

    if experiment.started_at is not None and as_of < experiment.started_at:
        raise PreRegistrationError("closed_at cannot precede started_at")

    experiment.closed_at = as_of
    experiment.status = ExperimentStatus.CLOSED.value
    session.flush()
    return experiment


def get_experiment(session: Session, experiment_id: uuid.UUID) -> Experiment | None:
    return session.get(Experiment, experiment_id)


__all__ = [
    "DEFAULT_ALPHA_BPS",
    "DEFAULT_POWER_BPS",
    "ExperimentDraft",
    "PreRegistrationError",
    "close_experiment",
    "create_draft",
    "get_experiment",
    "is_mutable",
    "lock_experiment",
    "start_experiment",
    "update_draft",
    "validate_transition",
]
