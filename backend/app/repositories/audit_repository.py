"""Audit-event persistence.

`audit_events` is the **only** table this module writes to. It cannot reach
`recovery_cases`, `recovery_actions`, `revenue_risks` or any experiment table:
recording that a decision happened is not the same as making one, and a module
that could do both would let a write imply an approval.

**Abstentions anchor on `risk_id`, never on `case_id`.** An abstention often
happens where no recovery case exists — a holdout risk never gets one — and
this project refuses to fabricate a case to hold a non-action, because
`recovery_cases` carries five NOT NULL money columns that nobody computed.
`audit_events.risk_id` exists for exactly this shape; assignment and sealing
already use it.

**An abstention is never an execution.** `is_execution` is forced False here,
and the database agrees independently: `abstain_is_never_execution` refuses the
pair, and `execution_actor_never_ai` refuses an AI actor on any execution entry.
Both guards are kept — the CHECK holds against a psql session and a future
migration, and the guard here turns a mid-flush `IntegrityError` into an error
that names what was wrong.

The table is append-only. Nothing in this module updates or deletes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.enums import AbstainReason, ActorType, CaseDecision, DecisionType

#: Actors permitted to record a gate decision. `ai_agent` is absent on purpose:
#: the LLM is not the authority over money, and an abstention is a money
#: decision even though it spends nothing.
DECIDING_ACTORS: tuple[str, ...] = (
    ActorType.ENGINE.value,
    ActorType.SYSTEM.value,
    ActorType.HUMAN.value,
)

#: JSON-safe scalar types a snapshot value may hold. A snapshot exists so a
#: reviewer can recompute the decision; anything that needs a repr() to survive
#: the round trip is not a number they can check.
_SNAPSHOT_SCALARS = (str, int, bool, type(None))


class AuditPersistenceError(ValueError):
    """An audit entry was refused. Nothing was written."""


def _validated_snapshot(snapshot: Mapping[str, object] | None) -> dict[str, object] | None:
    """Reject anything JSONB would silently mangle."""
    if snapshot is None:
        return None
    problems: list[str] = []
    for key, value in snapshot.items():
        if not isinstance(key, str):
            problems.append(f"key {key!r} is not a string")
        # bool is a subclass of int; both are fine, floats are not.
        if isinstance(value, float):
            problems.append(f"{key!r} is a float; the snapshot must stay exact")
        elif not isinstance(value, _SNAPSHOT_SCALARS):
            problems.append(f"{key!r} is a {type(value).__name__}, which is not JSON-safe")
    if problems:
        raise AuditPersistenceError(f"refusing the numeric snapshot: {'; '.join(problems)}")
    return dict(snapshot)


def record_abstention(
    session: Session,
    *,
    risk_id: uuid.UUID,
    reason: AbstainReason,
    rationale: str,
    numeric_snapshot: Mapping[str, object] | None = None,
    as_of: datetime,
    actor: ActorType = ActorType.ENGINE,
    action: str = "INCREMENTALITY_GATE_ABSTAIN",
) -> AuditEvent:
    """Record one gate abstention against a risk.

    `as_of` is injected rather than read from a clock and lands in the snapshot
    as `decided_at`, so the same decision replayed produces the same entry.
    `created_at` remains the storage timestamp; the two answer different
    questions and are deliberately not merged.

    Returns the persisted row. Flushes so the caller sees its identity, and
    **does not commit** — the caller owns the transaction. A repository that
    committed would decide on the caller's behalf that a partial pipeline run
    should survive.
    """
    problems: list[str] = []

    if risk_id is None:
        problems.append("risk_id is required: an abstention is recorded against a risk")
    if not isinstance(reason, AbstainReason):
        problems.append(f"reason must be an AbstainReason, got {reason!r}")
    if not rationale or not rationale.strip():
        problems.append(
            "rationale must not be blank: an unexplained non-action is the thing "
            "this project refuses to ship"
        )
    if actor.value not in DECIDING_ACTORS:
        problems.append(f"actor {actor.value!r} may not record a gate decision")
    if not action or len(action) > 128:
        problems.append(f"action must be 1..128 characters, got {len(action or '')}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        problems.append(f"as_of must be timezone-aware, got {as_of!r}")

    if problems:
        raise AuditPersistenceError(f"refusing to record an abstention: {'; '.join(problems)}")

    snapshot = dict(_validated_snapshot(numeric_snapshot) or {})
    snapshot.setdefault("decision", CaseDecision.ABSTAIN.value)
    snapshot.setdefault("abstain_reason", reason.value)
    snapshot["decided_at"] = as_of.isoformat()

    event = AuditEvent(
        case_id=None,
        risk_id=risk_id,
        actor=actor.value,
        action=action,
        reason=rationale,
        decision_type=DecisionType.ABSTAIN.value,
        # Never negotiable, and independently enforced by
        # `abstain_is_never_execution`. An abstention is the opposite of an
        # execution, not a quiet one.
        is_execution=False,
        numeric_snapshot=snapshot,
    )
    session.add(event)
    session.flush()
    return event


#: Recorded on a hypothesis entry.
HYPOTHESIS_ACTION = "AI_HYPOTHESIS_FALSIFICATION"


def record_hypothesis(
    session: Session,
    *,
    hypothesis: Mapping[str, object],
    result: Mapping[str, object],
    as_of: datetime,
    actor: ActorType = ActorType.AI_AGENT,
    action: str = HYPOTHESIS_ACTION,
) -> AuditEvent:
    """Record one AI hypothesis and the deterministic verdict on it.

    **Anchored on neither `case_id` nor `risk_id`.** A hypothesis is about a
    *cell* within an experiment, and neither foreign key can express that.
    `experiment_id`, `cell_key`, `ladder_level` and `hypothesis_id` travel in
    the JSONB snapshots instead — a deliberate trade recorded here so a future
    reader knows it was a choice: these rows carry no referential integrity for
    `experiment_id`, and finding them means querying JSONB rather than joining.
    Adding a column would need a migration, which this layer does not take.

    **Never an execution.** `is_execution` is forced False and the actor
    defaults to `ai_agent`, which the database independently refuses to place on
    an execution entry (`execution_actor_never_ai`). The LLM is not the
    authority over money, and this row is the shape of that rule.

    **Never touches `experiments.hypothesis`.** That column is frozen
    pre-registration data. An AI-generated hypothesis is an exploratory
    observation about a cell and belongs in the audit trail, not in the
    pre-registration it would otherwise appear to amend.

    `input_snapshot` holds what was proposed; `output_snapshot` holds what the
    deterministic evaluator concluded. Both carry the exploratory note, so a
    status can never be read back without it.
    """
    problems: list[str] = []

    for label, payload in (("hypothesis", hypothesis), ("result", result)):
        for field in ("hypothesis_id", "experiment_id", "cell_key", "ladder_level"):
            if not payload.get(field):
                problems.append(f"{label} snapshot is missing {field!r}")
        if payload.get("exploratory") is not True:
            problems.append(
                f"{label} snapshot must be marked exploratory: a hypothesis "
                "generated from these statistics and tested against them is not "
                "a confirmatory result"
            )
        if not payload.get("note"):
            problems.append(f"{label} snapshot is missing the exploratory note")

    if actor.value not in DECIDING_ACTORS and actor is not ActorType.AI_AGENT:
        problems.append(f"actor {actor.value!r} may not record a hypothesis")
    if not action or len(action) > 128:
        problems.append(f"action must be 1..128 characters, got {len(action or '')}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        problems.append(f"as_of must be timezone-aware, got {as_of!r}")

    if problems:
        raise AuditPersistenceError(f"refusing to record a hypothesis: {'; '.join(problems)}")

    proposed = dict(_validated_snapshot(_flatten(hypothesis)) or {})
    concluded = dict(_validated_snapshot(_flatten(result)) or {})
    concluded["decided_at"] = as_of.isoformat()

    event = AuditEvent(
        case_id=None,
        risk_id=None,
        actor=actor.value,
        action=action,
        reason=str(result.get("reason") or hypothesis.get("rationale") or ""),
        decision_type=DecisionType.DIAGNOSE.value,
        is_execution=False,
        input_snapshot=proposed,
        output_snapshot=concluded,
        numeric_snapshot={
            "hypothesis_id": str(hypothesis["hypothesis_id"]),
            "experiment_id": str(hypothesis["experiment_id"]),
            "cell_key": str(hypothesis["cell_key"]),
            "ladder_level": str(hypothesis["ladder_level"]),
            "status": str(result.get("status", "")),
            "exploratory": True,
        },
    )
    session.add(event)
    session.flush()
    return event


def _flatten(payload: Mapping[str, object]) -> dict[str, object]:
    """JSON-encode nested containers so the scalar guard can still apply.

    The snapshot guard exists to keep a stored number checkable, and a float
    buried in a nested dict would slip past a shallow scan. Encoding nested
    values here keeps the check meaningful without loosening it.
    """
    flat: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, dict | list | tuple):
            flat[key] = json.dumps(value, sort_keys=True, default=str)
        else:
            flat[key] = value
    return flat


__all__ = [
    "DECIDING_ACTORS",
    "HYPOTHESIS_ACTION",
    "AuditPersistenceError",
    "record_abstention",
    "record_hypothesis",
]
