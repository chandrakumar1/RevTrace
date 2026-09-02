"""What the hypothesis agent is allowed to see, say, and have believed.

Three contracts, deliberately separate:

* `CellStat` / `PopulationSummary` — the **only** things a model is shown. Every
  field is an integer count, an integer basis point, a bool, or a controlled
  string. Nothing here identifies a person, an order, an amount, or a case, and
  nothing here is a ground-truth column.
* `HypothesisProposal` — the **only** shape a model may return. Five fields, a
  closed claim vocabulary, and a cell key that is checked against the real
  population before anything downstream trusts it.
* `ValidatedHypothesis` — a proposal that survived that check, carrying the
  identity the audit trail will use.

**Exploratory by construction.** A hypothesis here is generated *from* observed
cell statistics and then evaluated *against those same statistics*. That is a
circular test and it is not a pre-registered confirmatory one. `EXPLORATORY_NOTE`
travels with every proposal and every result so the caveat cannot be separated
from the number — the same discipline the ledger's honesty block uses.

**The model proposes; it never decides.** A `HypothesisProposal` carries no
threshold, no monetary value, no intervention, no action, and no new feature.
The claim vocabulary is three values wide and closed. Everything the model could
say that would matter financially is simply not representable in this type.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: Stated on every proposal and every falsification result.
EXPLORATORY_NOTE = (
    "EXPLORATORY. This hypothesis was generated from the observed cell "
    "statistics and evaluated against those same observations, so the test is "
    "circular: it is not a pre-registered confirmatory result, and no "
    "multiplicity correction has been applied. Cell intervals are nominal 95% "
    "and uncorrected. Treat a status as a description of what the observed "
    "interval already showed, never as a finding that survived a corrected test."
)

#: Ladder levels the model may name. Mirrors `app.causal.cells.LADDER` — the
#: model may point at a rung, never invent one.
LADDER_LEVELS: tuple[str, ...] = ("fine", "coarse")


class Claim(StrEnum):
    """The closed set of things a hypothesis may assert.

    Three values, all comparative or null, none of them actionable. There is
    deliberately no claim about money, about what to do, or about a feature that
    is not already persisted.
    """

    HIGHER = "higher_uplift_than_population"
    LOWER = "lower_uplift_than_population"
    NO_EFFECT = "no_effect"


class Status(StrEnum):
    """What the deterministic evaluator concluded."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


# -- what the model is shown ----------------------------------------------


@dataclass(frozen=True, slots=True)
class CellStat:
    """One cell's aggregate statistics. Integers only.

    Everything a model needs to reason about heterogeneity and nothing else.
    There is no `risk_id`, no customer, no order, no amount, and no `truth_*`
    field — not because a rule forbids adding one, but because this type is the
    entire channel and it has nowhere to put one.
    """

    cell_key: str
    ladder_level: str
    n_treated: int
    n_holdout: int
    recovered_treated: int
    recovered_holdout: int
    p_treat_bps: int
    p_control_bps: int
    uplift_bps: int
    ci_low_bps: int
    ci_high_bps: int
    qualified: bool
    qualification_reason: str

    def __post_init__(self) -> None:
        if self.ladder_level not in LADDER_LEVELS:
            raise ValueError(f"unknown ladder level {self.ladder_level!r}")
        if self.ci_low_bps > self.ci_high_bps:
            raise ValueError(
                f"{self.cell_key}: interval inverted [{self.ci_low_bps}, {self.ci_high_bps}]"
            )

    @property
    def interval_contains_zero(self) -> bool:
        return self.ci_low_bps <= 0 <= self.ci_high_bps

    def as_dict(self) -> dict[str, object]:
        return {
            "cell_key": self.cell_key,
            "ladder_level": self.ladder_level,
            "n_treated": self.n_treated,
            "n_holdout": self.n_holdout,
            "recovered_treated": self.recovered_treated,
            "recovered_holdout": self.recovered_holdout,
            "p_treat_bps": self.p_treat_bps,
            "p_control_bps": self.p_control_bps,
            "uplift_bps": self.uplift_bps,
            "ci_low_bps": self.ci_low_bps,
            "ci_high_bps": self.ci_high_bps,
            "qualified": self.qualified,
            "qualification_reason": self.qualification_reason,
        }


@dataclass(frozen=True, slots=True)
class PopulationSummary:
    """The population-level effect every cell is compared against.

    `feature_vocabulary` names the observable fields that exist, so the model
    can see what a cell key is made of without being handed a single row.
    """

    ate_bps: int
    ci_low_bps: int
    ci_high_bps: int
    n_treatment: int
    n_holdout: int
    feature_vocabulary: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ate_bps": self.ate_bps,
            "ci_low_bps": self.ci_low_bps,
            "ci_high_bps": self.ci_high_bps,
            "n_treatment": self.n_treatment,
            "n_holdout": self.n_holdout,
            "feature_vocabulary": list(self.feature_vocabulary),
        }


@dataclass(frozen=True, slots=True)
class HypothesisRequest:
    """Everything one agent call may read. Assembled by the caller, never by
    the model."""

    experiment_id: uuid.UUID
    population: PopulationSummary
    cells: tuple[CellStat, ...]

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError("a hypothesis request needs at least one cell")
        keys = [cell.cell_key for cell in self.cells]
        if len(set(keys)) != len(keys):
            raise ValueError("cell keys must be distinct")

    @property
    def cell_keys(self) -> frozenset[str]:
        """The keys a proposal may name. Anything else is invented."""
        return frozenset(cell.cell_key for cell in self.cells)

    def cell(self, cell_key: str) -> CellStat:
        for candidate in self.cells:
            if candidate.cell_key == cell_key:
                return candidate
        raise KeyError(cell_key)

    def as_prompt_payload(self) -> dict[str, object]:
        """The exact JSON the model receives. Nothing is added downstream.

        `experiment_id` is deliberately absent: the model has no use for an
        identifier it cannot look anything up with, and leaving it out keeps the
        payload a pure function of the statistics.
        """
        return {
            "population": self.population.as_dict(),
            "cells": [cell.as_dict() for cell in self.cells],
            "allowed_cell_keys": sorted(self.cell_keys),
            "allowed_claims": [claim.value for claim in Claim],
            "note": EXPLORATORY_NOTE,
        }


# -- what the model may return --------------------------------------------


class HypothesisProposal(BaseModel):
    """The model's structured output. Five fields, nothing else.

    A Pydantic model rather than a dataclass because it is the schema handed to
    `messages.parse`, which constrains the response to exactly this shape. The
    model cannot return a threshold, a monetary value, an intervention or a new
    feature, because there is no field for one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_key: str = Field(description="A cell key present in the supplied population.")
    ladder_level: str = Field(description="Which rung the cell was scored at: fine or coarse.")
    claim: Claim = Field(description="How this cell's uplift compares to the population.")
    rationale: str = Field(description="Why, in one or two sentences, from the counts shown.")
    evidence_cited: list[str] = Field(
        default_factory=list,
        description="Cell keys consulted when forming the claim.",
    )


class HypothesisError(ValueError):
    """A proposal was refused. Nothing downstream saw it."""


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Which provider produced a proposal, and how its shape was constrained.

    Carried through to the audit trail so a stored hypothesis is never
    provenance-ambiguous: two providers can return the same claim about the
    same cell, and only this says which one did, under which schema mode.

    Deliberately **three fields**. Billing — whether a provider is free to call
    — is a provider-layer concern and lives on `ProviderConfig`, not here: this
    type is the audit record's provenance, and widening it to carry an
    operational safety flag would put two unrelated jobs in one contract. The
    free marker is still visible in the trail, because a free model says so in
    its own name (`…:free`) and `model` is recorded verbatim.
    """

    provider: str
    model: str
    schema_mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "schema_mode": self.schema_mode,
        }


@dataclass(frozen=True, slots=True)
class ValidatedHypothesis:
    """A proposal that survived deterministic validation.

    `hypothesis_id` is minted here rather than by the model: an identifier the
    model chose would be an identifier the model could reuse or collide.

    `provenance` is None only for a proposal that reached validation without
    passing through a provider — a hand-built one in a test, say. A recorded
    replay carries its own provenance and says so.
    """

    hypothesis_id: uuid.UUID
    experiment_id: uuid.UUID
    cell_key: str
    ladder_level: str
    claim: Claim
    rationale: str
    evidence_cited: tuple[str, ...]
    provenance: ProviderInfo | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "hypothesis_id": str(self.hypothesis_id),
            "experiment_id": str(self.experiment_id),
            "cell_key": self.cell_key,
            "ladder_level": self.ladder_level,
            "claim": self.claim.value,
            "rationale": self.rationale,
            "evidence_cited": list(self.evidence_cited),
            "exploratory": True,
            "note": EXPLORATORY_NOTE,
        }
        if self.provenance is not None:
            payload.update(self.provenance.as_dict())
        return payload


__all__ = [
    "EXPLORATORY_NOTE",
    "LADDER_LEVELS",
    "CellStat",
    "Claim",
    "HypothesisError",
    "HypothesisProposal",
    "HypothesisRequest",
    "PopulationSummary",
    "ProviderInfo",
    "Status",
    "ValidatedHypothesis",
]
