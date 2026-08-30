"""Effect estimation over sealed outcomes, in exact integers.

Three numbers come out of here, and the middle one is the product:

    gross_recovered        money that arrived among the treated
    incremental_recovered  gross minus what the holdout says would have arrived anyway
    credited_not_earned    gross - incremental

Every recovery dashboard reports the first. The third is what reporting only the
first quietly claims, and it is generally not small — in a population where a
large share of failed payments resolve themselves, most of the headline was
never caused by anyone.

**The analysis plan, implemented literally.** Effect estimate by difference in
proportions; interval by bootstrap with 10,000 resamples within arm, percentile
method; two-proportion test on the primary metric; Benjamini-Hochberg at
q = 0.10 for the pre-registered subgroups. Bootstrap rather than Wald because
the same machinery has to serve the recovery *rate* and the recovery *amount*,
and revenue is heavy-tailed.

**No number without an interval.** Both effects return one, and the interval
carries the seed and the resample count that produced it, so a reader can
reproduce it rather than take it on trust.

**Determinism.** The bootstrap draws from `random.Random(BOOTSTRAP_SEED)` with a
fixed module-level seed — never a runtime-random one. Resampling is integer
index selection; nothing here forms a float, and `round_sqrt_ratio` takes the
one square root the test statistic needs.

Nothing in this module reads a clock, touches a database, imports the generator,
or names a ground-truth column. It is handed two lists of integers per metric
and returns arithmetic.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.causal.balance import round_sqrt_ratio

#: Full basis-point scale.
BPS_SCALE = 10_000

#: Parts per million: the resolution `experiment_results.p_value_micros` holds.
P_VALUE_SCALE = 1_000_000

#: Resamples per bootstrap, as pre-registered. Not a tunable.
BOOTSTRAP_RESAMPLES = 10_000

#: The bootstrap seed. Fixed and documented rather than drawn at runtime: the
#: interval depends on it, so an interval nobody can reproduce is not evidence.
#: Reported alongside every interval this module returns.
BOOTSTRAP_SEED = 20_260_830

#: Benjamini-Hochberg false-discovery rate for subgroup analyses, q = 0.10.
DEFAULT_FDR_BPS = 1_000


class EstimatorError(ValueError):
    """An estimate could not be formed, and the caller should know why."""


# -- integer helpers ------------------------------------------------------


def _round_half_up(numerator: int, denominator: int) -> int:
    """`round(numerator / denominator)`, halves away from zero, no float."""
    if denominator <= 0:
        raise EstimatorError(f"denominator must be positive, got {denominator}")
    if numerator < 0:
        return -((-numerator * 2 + denominator) // (2 * denominator))
    return (numerator * 2 + denominator) // (2 * denominator)


def rate_bps(hits: int, total: int) -> int:
    """A share in basis points, rounded half-up.

    Deliberately the same rounding the generator uses for its own rates, so a
    measured rate and a known rate are compared on identical terms rather than
    differing by a rounding convention.
    """
    if total < 0 or hits < 0:
        raise EstimatorError(f"counts must be non-negative, got {hits}/{total}")
    if hits > total:
        raise EstimatorError(f"hits {hits} exceeds total {total}")
    if total == 0:
        return 0
    return _round_half_up(hits * BPS_SCALE, total)


def ate_bps(hits_treatment: int, n_treatment: int, hits_holdout: int, n_holdout: int) -> int:
    """Difference in proportions, treatment minus holdout, in basis points.

    The difference of two rounded rates rather than the rounded difference —
    again matching how the known effect is computed, so the two are comparable
    to the last basis point.
    """
    return rate_bps(hits_treatment, n_treatment) - rate_bps(hits_holdout, n_holdout)


def mean_minor(values: Sequence[int]) -> int:
    """Mean in minor units, **including zeros**, rounded half-up.

    Dropping the non-recoveries would measure "how much did payers pay", which
    is a different and far flatterer question than the one pre-registered.
    """
    if not values:
        raise EstimatorError("cannot take the mean of an empty arm")
    return _round_half_up(sum(values), len(values))


# -- the two-proportion test ----------------------------------------------


def two_proportion_z(
    hits_treatment: int,
    n_treatment: int,
    hits_holdout: int,
    n_holdout: int,
) -> int:
    """The pooled two-proportion statistic, at `normal.SCALE`.

        z = (p_t - p_h) / sqrt( p(1-p) (1/n_t + 1/n_h) ),  p = pooled rate

    Carried as exact ratios and closed with one integer square root, the same
    construction the balance SMD uses. When nobody recovered — or everybody did
    — the pooled variance is zero, but so is the difference, so the statistic is
    zero rather than undefined.
    """
    from app.causal.normal import SCALE

    if n_treatment < 1 or n_holdout < 1:
        raise EstimatorError("both arms need at least one unit for a test")
    if not 0 <= hits_treatment <= n_treatment or not 0 <= hits_holdout <= n_holdout:
        raise EstimatorError("hit counts must lie within their arm sizes")

    difference_num = hits_treatment * n_holdout - hits_holdout * n_treatment
    difference_den = n_treatment * n_holdout

    pooled_hits = hits_treatment + hits_holdout
    pooled_n = n_treatment + n_holdout
    variance_num = pooled_hits * (pooled_n - pooled_hits)
    variance_den = pooled_n * n_treatment * n_holdout

    if variance_num == 0:
        return 0

    magnitude = round_sqrt_ratio(
        (SCALE * abs(difference_num)) ** 2 * variance_den,
        difference_den**2 * variance_num,
    )
    return -magnitude if difference_num < 0 else magnitude


def two_proportion_p_micros(
    hits_treatment: int,
    n_treatment: int,
    hits_holdout: int,
    n_holdout: int,
) -> int:
    """Two-sided p-value in parts per million for the primary metric."""
    from app.causal.normal import two_sided_p_micros

    return two_sided_p_micros(
        two_proportion_z(hits_treatment, n_treatment, hits_holdout, n_holdout)
    )


# -- intervals ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interval:
    """A percentile bootstrap interval, with everything needed to redo it."""

    low: int
    high: int
    alpha_bps: int
    resamples: int
    seed: int

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise EstimatorError(f"interval is inverted: {self.low} > {self.high}")

    @property
    def width(self) -> int:
        return self.high - self.low

    @property
    def contains_zero(self) -> bool:
        """Whether the effect is distinguishable from none at this alpha."""
        return self.low <= 0 <= self.high

    def contains(self, value: int) -> bool:
        return self.low <= value <= self.high


#: A resample statistic: (sum_treatment, n_treatment, sum_holdout, n_holdout).
Statistic = Callable[[int, int, int, int], int]


def _percentile_ranks(resamples: int, alpha_bps: int) -> tuple[int, int]:
    """Zero-based ranks of the alpha/2 and 1-alpha/2 order statistics.

    The lower rank must reach at least one. At rank zero the "interval" is
    simply the smallest and largest resample, which is not a percentile of
    anything and understates the spread — so too few resamples for the
    requested alpha is refused rather than silently answered.
    """
    lower = resamples * alpha_bps // (2 * BPS_SCALE)
    upper = resamples - 1 - lower
    if lower < 1 or lower >= upper:
        needed = 2 * BPS_SCALE // alpha_bps + 1
        raise EstimatorError(
            f"{resamples} resamples cannot support a {alpha_bps}bps interval; "
            f"the tails would meet in the middle. At least {needed} are needed."
        )
    return lower, upper


def bootstrap_interval(
    values_treatment: Sequence[int],
    values_holdout: Sequence[int],
    statistic: Statistic,
    *,
    alpha_bps: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Interval:
    """Percentile bootstrap, resampling **within arm**.

    Each arm is resampled with replacement to its own size, independently, and
    the statistic recomputed. Resampling within arm rather than pooling is what
    keeps the interval an interval for the *difference* between two fixed
    populations rather than for a shuffle of them.

    Only sums are carried forward, because both statistics here are functions
    of a sum and a count — so a resample costs one integer selection pass and
    no intermediate list beyond it.

    Each arm is **sorted first**. Resampling with replacement draws indices, so
    a seeded run over the same values in a different order would otherwise pick
    different values and shift the interval. Sorting costs nothing — the draw
    is from a multiset, which has no order — and it makes the interval a
    function of the data rather than of how the rows happened to arrive.
    """
    n_treatment, n_holdout = len(values_treatment), len(values_holdout)
    if n_treatment < 1 or n_holdout < 1:
        raise EstimatorError("both arms need at least one unit to resample")
    if resamples < 1:
        raise EstimatorError(f"resamples must be positive, got {resamples}")

    lower_rank, upper_rank = _percentile_ranks(resamples, alpha_bps)
    generator = random.Random(seed)
    ordered_treatment = sorted(values_treatment)
    ordered_holdout = sorted(values_holdout)

    draws = sorted(
        statistic(
            sum(generator.choices(ordered_treatment, k=n_treatment)),
            n_treatment,
            sum(generator.choices(ordered_holdout, k=n_holdout)),
            n_holdout,
        )
        for _ in range(resamples)
    )

    return Interval(
        low=draws[lower_rank],
        high=draws[upper_rank],
        alpha_bps=alpha_bps,
        resamples=resamples,
        seed=seed,
    )


def _rate_statistic(sum_t: int, n_t: int, sum_h: int, n_h: int) -> int:
    return rate_bps(sum_t, n_t) - rate_bps(sum_h, n_h)


def _incremental_statistic(sum_t: int, n_t: int, sum_h: int, n_h: int) -> int:
    """`(mean_t - mean_h) * n_t`, reduced so only one rounding happens."""
    return sum_t - _round_half_up(sum_h * n_t, n_h)


# -- the ledger -----------------------------------------------------------


def gross_recovered(amounts_treatment: Sequence[int]) -> int:
    """Money that arrived among the treated. What every competitor reports."""
    return sum(amounts_treatment)


def incremental_recovered(amounts_treatment: Sequence[int], amounts_holdout: Sequence[int]) -> int:
    """`(mean_t - mean_h) * n_t`. May be negative, and that is a real result.

    Algebraically reduced to `sum_t - mean_h * n_t` so the value rounds once
    rather than three times.
    """
    if not amounts_treatment or not amounts_holdout:
        raise EstimatorError("both arms need at least one unit for a lift")
    return _incremental_statistic(
        sum(amounts_treatment),
        len(amounts_treatment),
        sum(amounts_holdout),
        len(amounts_holdout),
    )


def credited_not_earned(gross: int, incremental: int) -> int:
    """`gross - incremental`. The share of the headline nobody caused."""
    return gross - incremental


# -- assembled effects ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateEffect:
    """The primary metric: difference in recovery rate, with an interval."""

    n_treatment: int
    n_holdout: int
    hits_treatment: int
    hits_holdout: int
    rate_treatment_bps: int
    rate_holdout_bps: int
    ate_bps: int
    interval: Interval
    p_value_micros: int

    @property
    def is_significant(self) -> bool:
        """By the interval, not by the p-value alone."""
        return not self.interval.contains_zero

    def as_dict(self) -> dict[str, object]:
        return {
            "n_treatment": self.n_treatment,
            "n_holdout": self.n_holdout,
            "rate_treatment_bps": self.rate_treatment_bps,
            "rate_holdout_bps": self.rate_holdout_bps,
            "ate_bps": self.ate_bps,
            "ate_ci_low_bps": self.interval.low,
            "ate_ci_high_bps": self.interval.high,
            "p_value_micros": self.p_value_micros,
            "bootstrap_seed": self.interval.seed,
            "bootstrap_resamples": self.interval.resamples,
        }


@dataclass(frozen=True, slots=True)
class AmountEffect:
    """The ledger: gross, incremental, credited-not-earned, with an interval."""

    n_treatment: int
    n_holdout: int
    mean_treatment: int
    mean_holdout: int
    gross_recovered: int
    incremental_recovered: int
    credited_not_earned: int
    interval: Interval

    @property
    def credited_share_bps(self) -> int:
        """Share of the gross figure that was not caused. Zero gross, zero share."""
        if self.gross_recovered == 0:
            return 0
        return _round_half_up(self.credited_not_earned * BPS_SCALE, self.gross_recovered)

    def as_dict(self) -> dict[str, object]:
        return {
            "n_treatment": self.n_treatment,
            "n_holdout": self.n_holdout,
            "mean_treatment": self.mean_treatment,
            "mean_holdout": self.mean_holdout,
            "gross_recovered": self.gross_recovered,
            "incremental_recovered": self.incremental_recovered,
            "incremental_ci_low": self.interval.low,
            "incremental_ci_high": self.interval.high,
            "credited_not_earned": self.credited_not_earned,
            "credited_share_bps": self.credited_share_bps,
            "bootstrap_seed": self.interval.seed,
            "bootstrap_resamples": self.interval.resamples,
        }


def rate_effect(
    recovered_treatment: Sequence[int],
    recovered_holdout: Sequence[int],
    *,
    alpha_bps: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> RateEffect:
    """The primary metric. Arms are sequences of 0/1 per unit, zeros included."""
    for arm in (recovered_treatment, recovered_holdout):
        if any(value not in (0, 1) for value in arm):
            raise EstimatorError("recovery indicators must be 0 or 1")

    n_treatment, n_holdout = len(recovered_treatment), len(recovered_holdout)
    if n_treatment < 1 or n_holdout < 1:
        raise EstimatorError("both arms need at least one unit")

    hits_treatment, hits_holdout = sum(recovered_treatment), sum(recovered_holdout)

    return RateEffect(
        n_treatment=n_treatment,
        n_holdout=n_holdout,
        hits_treatment=hits_treatment,
        hits_holdout=hits_holdout,
        rate_treatment_bps=rate_bps(hits_treatment, n_treatment),
        rate_holdout_bps=rate_bps(hits_holdout, n_holdout),
        ate_bps=ate_bps(hits_treatment, n_treatment, hits_holdout, n_holdout),
        interval=bootstrap_interval(
            recovered_treatment,
            recovered_holdout,
            _rate_statistic,
            alpha_bps=alpha_bps,
            resamples=resamples,
            seed=seed,
        ),
        p_value_micros=two_proportion_p_micros(
            hits_treatment, n_treatment, hits_holdout, n_holdout
        ),
    )


def amount_effect(
    amounts_treatment: Sequence[int],
    amounts_holdout: Sequence[int],
    *,
    alpha_bps: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> AmountEffect:
    """The ledger. Arms are recovered amounts in minor units, zeros included.

    The interval is bootstrapped on `incremental_recovered` itself rather than
    on the mean difference and scaled afterwards — the two agree, since scaling
    by a positive constant preserves the order the percentiles are read from,
    but bootstrapping the reported quantity keeps the interval attached to it.
    """
    if not amounts_treatment or not amounts_holdout:
        raise EstimatorError("both arms need at least one unit")
    if any(value < 0 for value in (*amounts_treatment, *amounts_holdout)):
        raise EstimatorError("recovered amounts are counts of minor units and cannot be negative")

    gross = gross_recovered(amounts_treatment)
    incremental = incremental_recovered(amounts_treatment, amounts_holdout)

    return AmountEffect(
        n_treatment=len(amounts_treatment),
        n_holdout=len(amounts_holdout),
        mean_treatment=mean_minor(amounts_treatment),
        mean_holdout=mean_minor(amounts_holdout),
        gross_recovered=gross,
        incremental_recovered=incremental,
        credited_not_earned=credited_not_earned(gross, incremental),
        interval=bootstrap_interval(
            amounts_treatment,
            amounts_holdout,
            _incremental_statistic,
            alpha_bps=alpha_bps,
            resamples=resamples,
            seed=seed,
        ),
    )


# -- multiplicity ---------------------------------------------------------


def benjamini_hochberg(
    p_values_micros: Sequence[int],
    q_bps: int = DEFAULT_FDR_BPS,
) -> tuple[bool, ...]:
    """Benjamini-Hochberg, returning one verdict per input in input order.

    Reject the `k` smallest p-values, where `k` is the largest rank whose
    p-value satisfies `p <= k * q / m`. Cross-multiplied into integers, so the
    comparison that decides a discovery is exact.

    A subgroup analysis without this control would find something in one
    stratum by chance and report it as a finding — the most common way an
    honest experiment turns into a dishonest claim.
    """
    if not 0 < q_bps < BPS_SCALE:
        raise EstimatorError(f"q_bps must be within 1..{BPS_SCALE - 1}, got {q_bps}")
    total = len(p_values_micros)
    if total == 0:
        return ()
    if any(not 0 <= p <= P_VALUE_SCALE for p in p_values_micros):
        raise EstimatorError(f"p-values must be within 0..{P_VALUE_SCALE} micros")

    ordered = sorted(range(total), key=lambda index: p_values_micros[index])

    largest_rank = 0
    for rank, index in enumerate(ordered, start=1):
        # p <= rank * q / m  <=>  p * m * BPS_SCALE <= rank * q_bps * P_VALUE_SCALE
        if p_values_micros[index] * total * BPS_SCALE <= rank * q_bps * P_VALUE_SCALE:
            largest_rank = rank

    verdicts = [False] * total
    for index in ordered[:largest_rank]:
        verdicts[index] = True
    return tuple(verdicts)


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "BPS_SCALE",
    "DEFAULT_FDR_BPS",
    "P_VALUE_SCALE",
    "AmountEffect",
    "EstimatorError",
    "Interval",
    "RateEffect",
    "Statistic",
    "amount_effect",
    "ate_bps",
    "benjamini_hochberg",
    "bootstrap_interval",
    "credited_not_earned",
    "gross_recovered",
    "incremental_recovered",
    "mean_minor",
    "rate_bps",
    "rate_effect",
    "two_proportion_p_micros",
    "two_proportion_z",
]
