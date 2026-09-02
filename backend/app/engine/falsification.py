"""Testing a hypothesis against statistics that were already computed.

Deterministic and pure. It reads a `CellStat` the caller assembled and a
`ValidatedHypothesis`, applies four rules in a fixed order, and returns a status.
It **recomputes nothing** — no estimator, no bootstrap, no resampling. If a
number is not already in the `CellStat`, this module does not have it and does
not go and get it.

That constraint is what keeps the deterministic layer authoritative. The model
proposed which cell to look at; the arithmetic that decides whether the claim
holds is here, in code, and would return the same answer if the proposal had
come from a coin toss.

The rules
---------
Qualification comes first, for every claim::

    cell did not qualify                     -> insufficient_evidence

`no_effect` is a claim about **zero**, so it is decided against zero::

    interval contains zero                   -> insufficient_evidence
    interval excludes zero                   -> refuted

The comparative claims are claims about **the population effect**, so they are
decided against it — and by the whole interval, never the point estimate::

    higher: ci_low  > ate                    -> confirmed
            ci_high < ate                    -> refuted
            otherwise (interval spans ate)   -> insufficient_evidence

    lower:  ci_high < ate                    -> confirmed
            ci_low  > ate                    -> refuted
            otherwise                        -> insufficient_evidence

**Why the interval and not the point estimate.** A point estimate above the
population effect is not evidence that the cell is above it. On the accepted
N=10,000 population the coarse cell `insufficient_funds` estimates 1875 bps
against a population effect of 1564, but its interval [1507, 2245] contains
1564 — it looks higher and is not shown to be. An earlier version of this module
compared the point estimate and confirmed that cell; this one records it as
insufficient.

**`no_effect` can never be confirmed, and that is correct.** An interval
containing zero is a failure to detect an effect, not a demonstration that none
exists; separating those needs equivalence testing against a declared margin,
which `UPLIFT_LIMITATIONS` already records as future work. Returning
`confirmed` for a null result would be exactly the overclaim this project
refuses everywhere else.

What this comparison is not
---------------------------
An **exploratory approximation**, not a formal test of heterogeneity. Four
limitations, none of which the rules above remove:

* It compares the cell interval to a **point** population effect, and so
  **ignores the population estimate's own uncertainty** — on the accepted run
  the ATE is itself an interval, [1370, 1757].
* The cell is a **subset of the population**, not an independent sample, so the
  two estimates are correlated and the comparison is not between independent
  quantities.
* It is **not a complement-based test**: a rigorous question would compare the
  cell against the rest of the population with an interval for *that*
  difference. That needs a new estimator, which this layer deliberately does not
  introduce.
* Cell intervals are nominal 95% and **uncorrected for multiplicity**, and the
  hypothesis was generated from these same statistics.

Every result is therefore labelled exploratory, and none is a pre-registered
confirmatory finding. `EXPLORATORY_NOTE` is attached to every result so the
caveat cannot be quoted away from the status.

Integers only. Every comparison is between integer basis points; no float is
formed anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.contracts import (
    EXPLORATORY_NOTE,
    CellStat,
    Claim,
    PopulationSummary,
    Status,
    ValidatedHypothesis,
)

#: Why a status was reached, recorded alongside it so a result can be explained
#: rather than merely stated. Mirrors the rule-naming convention in
#: `app.causal.quadrants`.
RULE_NOT_QUALIFIED = "cell_did_not_qualify"
#: `no_effect` is decided against zero; the comparative claims are not.
RULE_INTERVAL_SPANS_ZERO = "interval_contains_zero"
RULE_NULL_CLAIM_REFUTED = "interval_excludes_zero_so_no_effect_is_refuted"
#: The comparative claims are decided by where the whole interval sits relative
#: to the population effect.
RULE_ABOVE_POPULATION = "interval_lies_entirely_above_the_population_effect"
RULE_BELOW_POPULATION = "interval_lies_entirely_below_the_population_effect"
RULE_INTERVAL_SPANS_POPULATION = "interval_contains_the_population_effect"


class FalsificationError(ValueError):
    """The hypothesis could not be tested."""


@dataclass(frozen=True, slots=True)
class FalsificationResult:
    """One tested hypothesis, with the numbers it was decided from."""

    hypothesis_id: object
    experiment_id: object
    cell_key: str
    ladder_level: str
    claim: Claim
    status: Status
    rule: str
    reason: str
    evidence: tuple[tuple[str, int], ...]

    @property
    def exploratory(self) -> bool:
        """Always true. There is no confirmatory path through this module."""
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "hypothesis_id": str(self.hypothesis_id),
            "experiment_id": str(self.experiment_id),
            "cell_key": self.cell_key,
            "ladder_level": self.ladder_level,
            "claim": self.claim.value,
            "status": self.status.value,
            "rule": self.rule,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "exploratory": True,
            "note": EXPLORATORY_NOTE,
        }


def compare_to_population(
    claim: Claim, ci_low_bps: int, ci_high_bps: int, population_ate_bps: int
) -> tuple[Status, str]:
    """Whether the cell's **interval** establishes the claimed comparison.

    The whole interval is compared against the population effect, not the point
    estimate. A point estimate above the population effect is not evidence that
    the cell is above it: if the interval brackets the population effect, the
    data are equally consistent with the cell sitting below.

    That distinction is not academic. On the accepted N=10,000 population the
    coarse cell `insufficient_funds` has a point estimate of 1875 bps against a
    population effect of 1564, but an interval of [1507, 2245] that contains
    1564 — so it looks higher and is not shown to be. The earlier rule confirmed
    it. This one does not.

    Only `HIGHER` and `LOWER` reach here; `NO_EFFECT` is decided against zero.
    """
    if claim is Claim.HIGHER:
        if ci_low_bps > population_ate_bps:
            return Status.CONFIRMED, RULE_ABOVE_POPULATION
        if ci_high_bps < population_ate_bps:
            return Status.REFUTED, RULE_BELOW_POPULATION
        return Status.INSUFFICIENT_EVIDENCE, RULE_INTERVAL_SPANS_POPULATION
    if claim is Claim.LOWER:
        if ci_high_bps < population_ate_bps:
            return Status.CONFIRMED, RULE_BELOW_POPULATION
        if ci_low_bps > population_ate_bps:
            return Status.REFUTED, RULE_ABOVE_POPULATION
        return Status.INSUFFICIENT_EVIDENCE, RULE_INTERVAL_SPANS_POPULATION
    raise FalsificationError(f"{claim.value} is not a comparative claim")


def falsify(
    hypothesis: ValidatedHypothesis,
    cell: CellStat,
    population: PopulationSummary,
) -> FalsificationResult:
    """Test one hypothesis against already-computed statistics. Pure."""
    if cell.cell_key != hypothesis.cell_key:
        raise FalsificationError(
            f"hypothesis names {hypothesis.cell_key!r} but was given statistics "
            f"for {cell.cell_key!r}"
        )

    evidence: tuple[tuple[str, int], ...] = (
        ("n_treated", cell.n_treated),
        ("n_holdout", cell.n_holdout),
        ("recovered_treated", cell.recovered_treated),
        ("recovered_holdout", cell.recovered_holdout),
        ("uplift_bps", cell.uplift_bps),
        ("ci_low_bps", cell.ci_low_bps),
        ("ci_high_bps", cell.ci_high_bps),
        ("population_ate_bps", population.ate_bps),
    )

    def result(status: Status, rule: str, reason: str) -> FalsificationResult:
        return FalsificationResult(
            hypothesis_id=hypothesis.hypothesis_id,
            experiment_id=hypothesis.experiment_id,
            cell_key=cell.cell_key,
            ladder_level=cell.ladder_level,
            claim=hypothesis.claim,
            status=status,
            rule=rule,
            reason=reason,
            evidence=evidence,
        )

    # 1. Qualification comes first, exactly as it does in `quadrants.classify`:
    #    a thin cell's interval is not a weak finding, it is not a finding.
    if not cell.qualified:
        return result(
            Status.INSUFFICIENT_EVIDENCE,
            RULE_NOT_QUALIFIED,
            f"the cell did not qualify ({cell.qualification_reason}), so its "
            "interval cannot support or refute a claim",
        )

    # 2. `no_effect` is a claim about zero, so it is decided against zero.
    if hypothesis.claim is Claim.NO_EFFECT:
        if cell.interval_contains_zero:
            return result(
                Status.INSUFFICIENT_EVIDENCE,
                RULE_INTERVAL_SPANS_ZERO,
                f"interval [{cell.ci_low_bps}, {cell.ci_high_bps}] contains zero; "
                "failing to detect an effect is not evidence that none exists",
            )
        return result(
            Status.REFUTED,
            RULE_NULL_CLAIM_REFUTED,
            f"interval [{cell.ci_low_bps}, {cell.ci_high_bps}] excludes zero, so "
            "the claim of no effect does not hold",
        )

    # 3. The comparative claims are about the population effect, so they are
    #    decided against it — by the whole interval, not the point estimate.
    status, rule = compare_to_population(
        hypothesis.claim, cell.ci_low_bps, cell.ci_high_bps, population.ate_bps
    )
    if rule == RULE_INTERVAL_SPANS_POPULATION:
        reason = (
            f"interval [{cell.ci_low_bps}, {cell.ci_high_bps}] contains the "
            f"population effect of {population.ate_bps} bps, so the data are "
            f"consistent with this cell sitting either side of it — the point "
            f"estimate of {cell.uplift_bps} bps is not evidence on its own"
        )
    else:
        where = "above" if rule == RULE_ABOVE_POPULATION else "below"
        reason = (
            f"interval [{cell.ci_low_bps}, {cell.ci_high_bps}] lies entirely "
            f"{where} the population effect of {population.ate_bps} bps "
            f"(point estimate {cell.uplift_bps} bps)"
        )
    return result(status, rule, reason)


__all__ = [
    "RULE_ABOVE_POPULATION",
    "RULE_BELOW_POPULATION",
    "RULE_INTERVAL_SPANS_POPULATION",
    "RULE_INTERVAL_SPANS_ZERO",
    "RULE_NOT_QUALIFIED",
    "RULE_NULL_CLAIM_REFUTED",
    "FalsificationError",
    "FalsificationResult",
    "compare_to_population",
    "falsify",
]
