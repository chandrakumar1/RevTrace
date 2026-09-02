"""Propose, validate, falsify, record — in that order, or not at all.

Thin by design, in the same shape as `services/recovery/gate.py`: it calls what
already exists and holds no logic of its own. Proposing lives in `app.agents`,
validation in `hypothesis_agent.validate`, the verdict in
`app.engine.falsification.falsify`, and the write in
`app.repositories.audit_repository.record_hypothesis`.

**Nothing is persisted unless every step succeeds.** `validate` raises on an
invented cell key, a contradicted ladder level, a blank rationale or invented
evidence; `falsify` raises when the statistics do not match the claim's cell.
Both raise *before* `record_hypothesis` is reached, so a refused proposal leaves
no row — not a row marked invalid, no row. A model's rejected answer is not a
finding about the population; it is a finding about the model, and the caller
gets it as an exception.

**No commit.** The caller owns the transaction, exactly as the repository and
the gate seam do. A service that committed would decide on the caller's behalf
that a partial run should survive.

**`max_retries` is set per call, not on the provider.** Retrying a transient
failure is reasonable behaviour for the agent in general; guaranteeing exactly
one request is a property of a deliberate operator run. Keeping it here means
the guarantee is visible at the place that needs it and does not silently
weaken the agent elsewhere.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.contracts import HypothesisRequest, ValidatedHypothesis
from app.agents.hypothesis_agent import (
    FallbackProvider,
    HypothesisProvider,
    free_only_chain,
    validate,
)
from app.core.config import Settings
from app.engine.falsification import FalsificationResult, falsify
from app.models.audit_event import AuditEvent
from app.repositories.audit_repository import record_hypothesis
from app.services.hypothesis.loader import load_hypothesis_request

#: Requests this service is allowed to issue for one hypothesis. One. A free
#: tier's 429 is a signal to stop, not to try again — and a retry that produced
#: a second proposal would make "the model said X" ambiguous.
MAX_RETRIES = 0


class HypothesisServiceError(RuntimeError):
    """The operation could not be completed. Nothing was written."""


@dataclass(frozen=True, slots=True)
class HypothesisOutcome:
    """What was proposed, what was concluded, and the row that recorded it."""

    request: HypothesisRequest
    hypothesis: ValidatedHypothesis
    result: FalsificationResult
    audit_event: AuditEvent | None

    @property
    def recorded(self) -> bool:
        return self.audit_event is not None


def _pin_single_request(provider: HypothesisProvider) -> None:
    """Force `max_retries=0` on a live client, if this provider has one.

    Reaches into the provider's client deliberately: the alternative is a
    constructor argument on `OpenAICompatibleProvider`, which would make every
    caller inherit a policy that belongs to this operation. A provider with no
    client — a recorded replay — needs nothing and is left alone.
    """
    client = getattr(provider, "_client", None)
    if client is None:
        return
    with_options = getattr(client, "with_options", None)
    if with_options is None:  # pragma: no cover - defensive
        raise HypothesisServiceError("provider client cannot pin its retry count")
    provider._client = with_options(max_retries=MAX_RETRIES)  # type: ignore[attr-defined]
    if getattr(provider._client, "max_retries", None) != MAX_RETRIES:  # type: ignore[attr-defined]
        raise HypothesisServiceError("refusing to proceed: retries are not pinned to zero")


def live_provider(settings: Settings) -> FallbackProvider:
    """The only provider chain this service builds for a live run.

    `free_only_chain` constructs OpenRouter alone on the pinned free model, and
    refuses to exist if any member fails to declare itself free. Featherless is
    never instantiated.
    """
    chain = free_only_chain(settings)
    for member in chain._providers:  # noqa: SLF001 - the chain owns no public iterator
        _pin_single_request(member)
    return chain


def generate_and_record(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    provider: HypothesisProvider,
    as_of: datetime,
    alpha_bps: int,
    mde_bps: int,
    request: HypothesisRequest | None = None,
    live: bool = False,
) -> HypothesisOutcome:
    """One hypothesis, end to end. Does not commit.

    `request` exists for tests and replays that already hold a payload. In every
    other case it is assembled here from stored data, because a caller-supplied
    request is a caller-supplied answer key: `validate` grades the proposal
    against it.

    `live` records how the answer was obtained, in the operational snapshot
    rather than in provenance. `ProviderInfo` says *what* produced a proposal;
    whether the call went over a socket is an operational fact, and the
    numeric snapshot is where this project already keeps those.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise HypothesisServiceError(f"as_of must be timezone-aware, got {as_of!r}")

    if request is None:
        request = load_hypothesis_request(
            session, experiment_id, alpha_bps=alpha_bps, mde_bps=mde_bps
        )
    if request.experiment_id != experiment_id:
        raise HypothesisServiceError(
            f"request describes experiment {request.experiment_id}, not {experiment_id}"
        )

    proposal = provider.propose(request)
    validated = validate(request, proposal, provider.info)
    cell = request.cell(validated.cell_key)
    result = falsify(validated, cell, request.population)

    event = record_hypothesis(
        session,
        hypothesis=validated.as_dict(),
        result=result.as_dict(),
        as_of=as_of,
    )
    snapshot = dict(event.numeric_snapshot or {})
    snapshot["live"] = live
    event.numeric_snapshot = snapshot

    return HypothesisOutcome(
        request=request, hypothesis=validated, result=result, audit_event=event
    )


__all__ = [
    "MAX_RETRIES",
    "HypothesisOutcome",
    "HypothesisServiceError",
    "generate_and_record",
    "live_provider",
]
