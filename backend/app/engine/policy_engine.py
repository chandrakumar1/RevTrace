"""The incrementality gate: decide whether acting on a risk is justified.

The product of this module is frequently a **non-action**, and that is the
point. A recovery system that acts on every detected risk cannot tell you what
its actions were worth; one that declines, records why, and can price the
decline is making a claim it can defend.

**Pure.** No session, no clock, no network, no randomness. `as_of` is injected
at every call, the same way the detectors take it, so replaying a decision with
the same inputs always produces the same output — which is what makes a gate
decision auditable rather than merely logged.

**Integer arithmetic only.** Every threshold, uplift and money figure is an
integer in minor units or basis points, matching
[ADR 0001](../../../docs/decisions/0001-money-as-integer-minor-units.md). A
decision that gates money must compare exactly.

**No fabricated economics.** The value check compares expected incremental
recovery against the intervention's own `unit_cost`, which is a real stored
integer. It deliberately does **not** apply a gross margin, take rate, MDR,
lifetime value or monetary harm valuation: none of those exist anywhere in this
codebase, and inventing one would put a made-up number inside a decision that
declines to spend money. The check is therefore a *cost-recovery* test, not a
P&L, and it is named that way throughout.

Authority boundary
------------------
`CaseDecision` is authoritative **for this gate only**. This module does not
read, write, or reconcile `recovery_cases.policy_status`, the Phase 3 recovery
vocabulary. The two overlap — both carry an `escalate` notion — and they are
**not** globally synchronised: a case may hold a Phase 3 `policy_status` and a
Day 2 `decision` that were reached by different rules at different times. Making
one derive from the other is a deliberate, later decision that needs a migration
and a constraint. Until then, do not read a `CaseDecision` as a statement about
`policy_status`, or the reverse.

This module also never creates a recovery case. An abstention is recorded
against the risk, because a holdout risk never gets a case at all and
fabricating one to hold a non-action would put five NOT NULL money fields into
the database that nobody computed.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.models.enums import AbstainReason, Arm, CaseDecision, Quadrant

#: Full basis-point scale, matching every other module that carries one.
BPS_SCALE = 10_000

#: Share of otherwise-undecidable cases that may be acted on to learn something.
#: A module constant rather than a column: it is a property of how this gate
#: behaves, not of any one experiment, and giving it a schema field would invite
#: per-experiment tuning of the exact quantity that must stay fixed to keep the
#: exploration sample interpretable.
EXPLORATION_BUDGET_BPS = 500

#: Salt for the exploration draw. **Distinct from the assignment salt and from
#: the fold salt on purpose.** Sharing one would make exploration eligibility a
#: second view of the arm a case already drew, so the explored subset would be a
#: biased slice of the treatment arm rather than an independent sample of it.
EXPLORE_SALT = "revtrace-explore-v1"

#: How many hex characters of the digest the bucket is drawn from. Mirrors
#: `app.experiments.assignment`, so both draws are read the same way.
_BUCKET_HEX_CHARS = 8

#: Recorded on the audit entry. String(128) in the column.
ABSTAIN_ACTION = "INCREMENTALITY_GATE_ABSTAIN"

#: Quadrants whose label alone settles the decision, with the reason each maps
#: to. Ordered by how the labels are defined rather than alphabetically.
QUADRANT_ABSTENTIONS: dict[Quadrant, AbstainReason] = {
    Quadrant.SLEEPING_DOG: AbstainReason.SLEEPING_DOG,
    Quadrant.SURE_THING: AbstainReason.SURE_THING,
    Quadrant.LOST_CAUSE: AbstainReason.LOST_CAUSE,
}


class GrayZonePolicy(StrEnum):
    """What the gate does with a Gray Zone unit whose effect is indistinguishable
    from zero.

    Not a stored vocabulary — this never reaches a column, so it carries no
    CHECK constraint and needs no migration. It exists so a sensitivity analysis
    can vary one policy question without editing the gate.

    The Gray Zone holds two populations that `classify` produces by the same
    fall-through rule:

    * **significant, high-baseline** — `ci_low > 0` but the control rate sits at
      or above the fold's self-recovery ceiling. A real measured lift, on people
      who largely pay anyway.
    * **null effect** — the interval spans zero, baseline in the middle tertile.

    Since DR-4 **neither variant acts on the first outside the exploration
    budget**: a significant lift on people who largely pay anyway abstains with
    `SELF_RECOVERY_LIKELY`, because acting there buys recovery the action did
    not cause. What both variants still do identically is let the exploration
    budget treat a sample of them, so the gate keeps learning about the cells it
    now refuses.

    They differ only on the second, and only in whether the exploration budget
    may rescue it. That is the whole of this enum's remit — it must not acquire
    a say over the first population.

    `CURRENT_BASELINE` reproduces the measured N=10,000 gate exactly and is the
    default, so no existing caller changes behaviour by adopting this parameter.
    """

    CURRENT_BASELINE = "current_baseline"
    NULL_ONLY = "null_only"


class PolicyError(ValueError):
    """The gate was asked something it cannot answer."""


def _round_half_up(numerator: int, denominator: int) -> int:
    """`round(numerator / denominator)`, halves away from zero, no float.

    Deliberately local. The causal package carries the same five lines, but a
    production decision path must not import the analysis package to do
    arithmetic — the gate has to keep running whether or not an estimator is
    installed, and a shared helper would make the analysis layer a runtime
    dependency of declining to spend money.
    """
    if denominator <= 0:
        raise PolicyError(f"denominator must be positive, got {denominator}")
    if numerator < 0:
        return -((-numerator * 2 + denominator) // (2 * denominator))
    return (numerator * 2 + denominator) // (2 * denominator)


# -- inputs ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpliftEvidence:
    """What the model estimated for one risk.

    Field names match `uplift_scores` exactly, so a caller reading a row and a
    caller holding a fresh estimate build this the same way. `qualified` carries
    what the row does not: whether the cell met its power requirement, or fell
    to the global rates and is therefore not a conditional estimate at all.
    """

    uplift_bps: int
    uplift_ci_low_bps: int
    uplift_ci_high_bps: int
    quadrant: Quadrant
    qualified: bool

    def __post_init__(self) -> None:
        if self.uplift_ci_low_bps > self.uplift_ci_high_bps:
            raise PolicyError(
                f"interval is inverted: [{self.uplift_ci_low_bps}, {self.uplift_ci_high_bps}]"
            )
        if not self.uplift_ci_low_bps <= self.uplift_bps <= self.uplift_ci_high_bps:
            raise PolicyError(
                f"uplift {self.uplift_bps} lies outside its interval "
                f"[{self.uplift_ci_low_bps}, {self.uplift_ci_high_bps}]"
            )

    @property
    def interval_contains_zero(self) -> bool:
        """No significant effect at the interval's level."""
        return self.uplift_ci_low_bps <= 0 <= self.uplift_ci_high_bps


class InterventionRow(Protocol):
    """The shape `from_row` reads.

    A structural type rather than the ORM class: the gate must not import a
    model to stay pure, and this states exactly which columns it depends on.
    """

    code: str
    unit_cost: int
    cooldown_hours: int
    max_per_customer_per_month: int
    requires_afa: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class InterventionTerms:
    """The stored terms of one intervention.

    Mirrors `interventions` field for field rather than taking the ORM row, so
    the gate stays pure and testable without a database. Build it with
    `from_row` at the seam.
    """

    code: str
    unit_cost: int
    cooldown_hours: int
    max_per_customer_per_month: int
    requires_afa: bool
    is_active: bool

    @classmethod
    def from_row(cls, row: InterventionRow) -> InterventionTerms:
        return cls(
            code=str(row.code),
            unit_cost=int(row.unit_cost),
            cooldown_hours=int(row.cooldown_hours),
            max_per_customer_per_month=int(row.max_per_customer_per_month),
            requires_afa=bool(row.requires_afa),
            is_active=bool(row.is_active),
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    """One decision, with the numbers it was reached from.

    `reason` is present exactly when `decision` is ABSTAIN, mirroring the two
    CHECK constraints on `recovery_cases` — an abstention without a reason is an
    unexplained non-action, and a reason on an action is a contradiction.
    """

    risk_id: uuid.UUID
    experiment_id: uuid.UUID
    decision: CaseDecision
    reason: AbstainReason | None
    rationale: str
    expected_incremental_recovery: int
    unit_cost: int
    explore_bucket: int
    explored: bool

    def __post_init__(self) -> None:
        abstained = self.decision is CaseDecision.ABSTAIN
        if abstained and self.reason is None:
            raise PolicyError("an abstention must carry a reason")
        if not abstained and self.reason is not None:
            raise PolicyError(f"{self.decision.value} must not carry an abstain reason")

    @property
    def acted(self) -> bool:
        return self.decision is CaseDecision.ACT

    def numeric_snapshot(self) -> dict[str, object]:
        """The arithmetic behind the decision, for `audit_events`.

        Everything here is an integer, a string, or a bool: a reviewer can
        recompute the decision from this alone rather than trusting the outcome.
        """
        return {
            "decision": self.decision.value,
            "abstain_reason": self.reason.value if self.reason else None,
            "expected_incremental_recovery": self.expected_incremental_recovery,
            "unit_cost": self.unit_cost,
            "explore_bucket": self.explore_bucket,
            "exploration_budget_bps": EXPLORATION_BUDGET_BPS,
            "explored": self.explored,
        }


# -- exploration ----------------------------------------------------------


def explore_digest(risk_id: uuid.UUID, experiment_id: uuid.UUID, salt: str = EXPLORE_SALT) -> str:
    """The digest exploration eligibility is drawn from.

    Same construction as the assignment digest and a different salt, so the two
    draws are independent and both are recomputable by hand from stored ids.
    """
    if not salt or not salt.strip():
        raise PolicyError("exploration salt must not be empty")
    return hashlib.sha256(f"{risk_id}:{experiment_id}:{salt}".encode()).hexdigest()


def explore_bucket_for(risk_id: uuid.UUID, experiment_id: uuid.UUID) -> int:
    """Map a risk into 0..9999 for the exploration draw."""
    return int(explore_digest(risk_id, experiment_id)[:_BUCKET_HEX_CHARS], 16) % BPS_SCALE


def is_explore_eligible(
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    budget_bps: int = EXPLORATION_BUDGET_BPS,
) -> bool:
    """Whether this risk falls inside the exploration budget.

    Deterministic and stateless: eligibility is a property of the identifiers,
    not of how many cases have already been explored today. A running counter
    would make the decision depend on processing order, and two replays of the
    same population would disagree.
    """
    if not 0 <= budget_bps <= BPS_SCALE:
        raise PolicyError(f"budget_bps must be within 0..{BPS_SCALE}, got {budget_bps}")
    return explore_bucket_for(risk_id, experiment_id) < budget_bps


# -- the value check ------------------------------------------------------


def expected_incremental_recovery(expected_recovery: int, uplift_bps: int) -> int:
    """Money this action is expected to *cause*, in minor units.

        expected_recovery x uplift_bps / 10000

    The uplift is the causal quantity, so multiplying by it is what separates
    this from expected recovery — the amount that would have been collected
    anyway is not a reason to spend anything.
    """
    if expected_recovery < 0:
        raise PolicyError(f"expected_recovery must not be negative, got {expected_recovery}")
    return _round_half_up(expected_recovery * uplift_bps, BPS_SCALE)


def clears_cost(expected_incremental: int, unit_cost: int) -> bool:
    """Whether the expected incremental recovery exceeds what the action costs.

    Strict: a break-even action is refused. Spending exactly what you expect to
    make back is a coin flip funded at par, and the estimate carries an interval
    wide enough that the true value could sit either side of it.
    """
    return expected_incremental > unit_cost


# -- the gate -------------------------------------------------------------


def decide(
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    arm: Arm,
    uplift: UpliftEvidence | None,
    intervention: InterventionTerms,
    expected_recovery: int,
    max_cost: int,
    as_of: datetime,
    contacts_this_month: int = 0,
    last_contacted_at: datetime | None = None,
    customer_contactable: bool = True,
    afa_consent: bool = False,
    budget_bps: int = EXPLORATION_BUDGET_BPS,
    gray_zone_policy: GrayZonePolicy = GrayZonePolicy.CURRENT_BASELINE,
) -> GateDecision:
    """Decide whether to act on one risk. Pure.

    The order is deliberate and each step is tested:

    1. **Holdout abstains, always.** No evidence and no economics can override
       it — the holdout is the entire basis of the counterfactual, and the
       database refuses the contradiction independently.
    2. **Evidence.** No score, a non-qualifying cell, a negative estimate, or a
       quadrant whose label settles the question — Gray Zone included, which
       never yields an ordinary action.
    3. **Cost recovery.** Expected incremental recovery against `unit_cost`.
    4. **Exploration.** A deterministic subset of otherwise-undecidable cases
       may act anyway, so the gate keeps learning about the cells it currently
       refuses.
    5. **Eligibility.** Consent, contact frequency, cooldown.

    When more than one rule would fire, the earlier one names the reason. That
    makes the reported reason a function of the order above rather than of
    dictionary iteration, which is why the order is documented here and asserted
    in the tests.

    `gray_zone_policy` governs step 4 for the null-effect branch only. Under
    `NULL_ONLY` a unit whose interval spans zero abstains even when the
    exploration budget would have covered it. It has no say over the
    self-recovery half of the Gray Zone, which abstains with
    `SELF_RECOVERY_LIKELY` under both policies and is explorable under both.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PolicyError(f"as_of must be timezone-aware, got {as_of!r}")
    if max_cost < 0:
        raise PolicyError(f"max_cost must not be negative, got {max_cost}")
    if not intervention.is_active:
        # Selecting a retired intervention is a caller bug, not a decision the
        # gate should quietly turn into an abstention.
        raise PolicyError(f"intervention {intervention.code!r} is not active")

    bucket = explore_bucket_for(risk_id, experiment_id)

    def abstain(reason: AbstainReason, rationale: str, *, incremental: int = 0) -> GateDecision:
        return GateDecision(
            risk_id=risk_id,
            experiment_id=experiment_id,
            decision=CaseDecision.ABSTAIN,
            reason=reason,
            rationale=rationale,
            expected_incremental_recovery=incremental,
            unit_cost=intervention.unit_cost,
            explore_bucket=bucket,
            explored=False,
        )

    # 1. Holdout ----------------------------------------------------------
    if arm is Arm.HOLDOUT:
        return abstain(
            AbstainReason.HOLDOUT_ARM,
            "assigned to the holdout arm; acting would destroy the counterfactual",
        )

    # 2. Evidence ---------------------------------------------------------
    explorable = is_explore_eligible(risk_id, experiment_id, budget_bps=budget_bps)

    if uplift is None:
        if explorable:
            return _act(
                risk_id,
                experiment_id,
                intervention,
                0,
                bucket,
                "no uplift estimate; acting under the exploration budget to produce one",
                explored=True,
            )
        return abstain(
            AbstainReason.INSUFFICIENT_SAMPLE,
            "no uplift estimate exists for this risk",
        )

    if not uplift.qualified:
        if explorable:
            return _act(
                risk_id,
                experiment_id,
                intervention,
                0,
                bucket,
                "cell did not qualify; acting under the exploration budget",
                explored=True,
            )
        return abstain(
            AbstainReason.INSUFFICIENT_SAMPLE,
            "the cell did not meet its power requirement, so the estimate is not conditional",
        )

    if uplift.quadrant in QUADRANT_ABSTENTIONS:
        reason = QUADRANT_ABSTENTIONS[uplift.quadrant]
        return abstain(reason, f"quadrant {uplift.quadrant.value} does not warrant an action")

    if uplift.uplift_bps < 0:
        return abstain(
            AbstainReason.NEGATIVE_UPLIFT,
            f"estimated uplift is negative ({uplift.uplift_bps} bps); acting would cost recovery",
        )

    if uplift.quadrant is Quadrant.GRAY_ZONE:
        # Every Gray Zone unit, not only the null-effect half.
        #
        # This branch previously tested `interval_contains_zero`, which is true
        # of one gray-zone population and false of the other. The other one —
        # a significant lift on a cell whose control rate is at or above the
        # fold's self-recovery ceiling — fell through every remaining rule and
        # acted as though the label had said Persuadable. On the accepted
        # N=10,000 run that was 1,757 of 4,124 actions: unlogged exploration
        # wearing an ordinary ACT, on a label whose whole meaning is "we do not
        # know whether acting is worth it".
        #
        # Reached only after the negative-uplift check above, so that rule keeps
        # its precedence and a negative estimate is still named as one.
        if uplift.interval_contains_zero:
            if explorable and gray_zone_policy is GrayZonePolicy.CURRENT_BASELINE:
                return _act(
                    risk_id,
                    experiment_id,
                    intervention,
                    0,
                    bucket,
                    "effect not distinguishable from zero; acting under the exploration budget",
                    explored=True,
                )
            return abstain(
                AbstainReason.UPLIFT_NOT_SIGNIFICANT,
                f"interval [{uplift.uplift_ci_low_bps}, {uplift.uplift_ci_high_bps}] contains zero",
            )

        # `gray_zone_policy` deliberately does not reach here. It governs the
        # null-effect question and nothing else; letting it gate this branch
        # too would give NULL_ONLY a second meaning its own documentation
        # denies, and would change a policy knob into a blanket switch.
        if explorable:
            return _act(
                risk_id,
                experiment_id,
                intervention,
                0,
                bucket,
                "gray zone, high self-recovery; acting under the exploration budget",
                explored=True,
            )
        return abstain(
            AbstainReason.SELF_RECOVERY_LIKELY,
            "a real lift on customers who largely recover unaided; the action would "
            "be credited with recovery it did not cause",
        )

    if uplift.interval_contains_zero:
        # A defensive guard, not a live rule. `classify` gives Persuadable only
        # when `ci_low > 0`, and every other label was taken above, so on
        # self-consistent evidence nothing reaches this line. It stays because
        # the alternative is acting on an effect indistinguishable from zero
        # whenever a label and an interval disagree — trusting the label over
        # the number is exactly the wrong way round for this project, and a
        # caller assembling `UpliftEvidence` by hand can produce that pair.
        if explorable and gray_zone_policy is GrayZonePolicy.CURRENT_BASELINE:
            return _act(
                risk_id,
                experiment_id,
                intervention,
                0,
                bucket,
                "effect not distinguishable from zero; acting under the exploration budget",
                explored=True,
            )
        return abstain(
            AbstainReason.UPLIFT_NOT_SIGNIFICANT,
            f"interval [{uplift.uplift_ci_low_bps}, {uplift.uplift_ci_high_bps}] contains zero",
        )

    # 3. Cost recovery ----------------------------------------------------
    incremental = expected_incremental_recovery(expected_recovery, uplift.uplift_bps)

    if intervention.unit_cost > max_cost:
        return abstain(
            AbstainReason.NEGATIVE_NET_VALUE,
            f"unit cost {intervention.unit_cost} exceeds the case maximum {max_cost}",
            incremental=incremental,
        )

    if not clears_cost(incremental, intervention.unit_cost):
        return abstain(
            AbstainReason.NEGATIVE_NET_VALUE,
            f"expected incremental recovery {incremental} does not exceed "
            f"unit cost {intervention.unit_cost}",
            incremental=incremental,
        )

    # 5. Eligibility ------------------------------------------------------
    if not customer_contactable:
        return abstain(
            AbstainReason.CUSTOMER_OPTED_OUT,
            "the customer has opted out of contact",
            incremental=incremental,
        )

    if intervention.requires_afa and not afa_consent:
        return abstain(
            AbstainReason.REGULATORY_BLOCK,
            f"{intervention.code} requires additional-factor authentication consent",
            incremental=incremental,
        )

    if contacts_this_month >= intervention.max_per_customer_per_month:
        return abstain(
            AbstainReason.CONTACT_BUDGET_EXHAUSTED,
            f"{contacts_this_month} contacts already made this month, cap is "
            f"{intervention.max_per_customer_per_month}",
            incremental=incremental,
        )

    if last_contacted_at is not None:
        if last_contacted_at.tzinfo is None or last_contacted_at.utcoffset() is None:
            raise PolicyError(
                f"last_contacted_at must be timezone-aware, got {last_contacted_at!r}"
            )
        if as_of - last_contacted_at < timedelta(hours=intervention.cooldown_hours):
            return abstain(
                AbstainReason.CONTACT_BUDGET_EXHAUSTED,
                f"within the {intervention.cooldown_hours}h cooldown for {intervention.code}",
                incremental=incremental,
            )

    return _act(
        risk_id,
        experiment_id,
        intervention,
        incremental,
        bucket,
        f"expected incremental recovery {incremental} exceeds unit cost {intervention.unit_cost}",
        explored=False,
    )


def _act(
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    intervention: InterventionTerms,
    incremental: int,
    bucket: int,
    rationale: str,
    *,
    explored: bool,
) -> GateDecision:
    return GateDecision(
        risk_id=risk_id,
        experiment_id=experiment_id,
        decision=CaseDecision.ACT,
        reason=None,
        rationale=rationale,
        expected_incremental_recovery=incremental,
        unit_cost=intervention.unit_cost,
        explore_bucket=bucket,
        explored=explored,
    )


__all__ = [
    "ABSTAIN_ACTION",
    "BPS_SCALE",
    "EXPLORATION_BUDGET_BPS",
    "EXPLORE_SALT",
    "QUADRANT_ABSTENTIONS",
    "GateDecision",
    "GrayZonePolicy",
    "InterventionTerms",
    "PolicyError",
    "UpliftEvidence",
    "clears_cost",
    "decide",
    "expected_incremental_recovery",
    "explore_bucket_for",
    "explore_digest",
    "is_explore_eligible",
]
