"""Sample size, detectable effect, and what a holdout costs to run.

Three questions, all answered before the data arrives rather than after:

* **How many cases per arm?** `required_n_per_arm`, from the pre-registered
  alpha, power, baseline and minimum detectable effect.
* **What could this many cases have detected?** `mde_for_n`, the inverse —
  which is how an underpowered subgroup gets described honestly instead of
  being reported as a null.
* **What does holding out actually cost?** `size_holdout`, which turns a
  monthly case volume into a holdout share, a number of weeks, and the money
  forgone by not treating the held-out arm.

The last one is a product feature, not a statistics exercise. A merchant asked
to give up revenue for measurement deserves the size of the bill before
agreeing, and "we held some back" is not an answer.

**Everything is integer.** Basis points in, whole cases out, with the critical
values coming from `normal.py`'s fixed-point quantile. A required sample size
that differed between machines would make a pre-registration meaningless.

Sample sizes always round **up**: a fractional case cannot be enrolled, and
rounding down would claim power the design does not have.

--------------------------------------------------------------------------
A note on 373 against the 384 in section 7 of the pre-registration
--------------------------------------------------------------------------

Both numbers are correct, for different formulas, and both appear in the
source material:

* The **exact formula** section 7 prints gives **373**::

      (1.959964 + 0.841621)^2 * [0.35*0.65 + 0.45*0.55] / 0.10^2
      = 7.848880 * 0.475 / 0.01 = 372.82  ->  373

* The **rule of thumb**, `16 * pbar(1-pbar) / delta^2` with `pbar = 0.40`,
  gives **384**: `16 * 0.24 / 0.01`. It is more conservative because it rounds
  the constant 15.698 up to 16 and uses a pooled variance of `2*pbar(1-pbar)`
  = 0.48 in place of the actual 0.475.

Section 7 presents 384 as the output of the exact formula, which it is not.
The stored `planned_n_per_arm = 384` is nonetheless a sound *plan*: it exceeds
the requirement, and being over-powered is not an error. Neither value is
changed here. `required_n_per_arm` implements the exact formula and returns
373; `rule_of_thumb_n_per_arm` returns 384, so a report can show both and name
the difference rather than hiding it behind one number.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.causal.normal import SCALE, z_for_confidence, z_for_power

#: Full basis-point scale.
BPS_SCALE = 10_000

#: Pre-registered defaults, as the experiment row stores them.
DEFAULT_ALPHA_BPS = 500
DEFAULT_POWER_BPS = 8_000

#: A balanced design, which is what the pre-registration fixes for the benchmark.
BALANCED_HOLDOUT_BPS = 5_000

#: Weeks in a month, as an exact ratio: 12 months over 52 weeks.
_MONTHS_PER_YEAR = 12
_WEEKS_PER_YEAR = 52


class PowerError(ValueError):
    """A power calculation could not be made, and the reason is the caller's."""


def _ceil_div(numerator: int, denominator: int) -> int:
    """Ceiling division for positive denominators. Never rounds a case away."""
    if denominator <= 0:
        raise PowerError(f"denominator must be positive, got {denominator}")
    return -(-numerator // denominator)


def _validate_rates(baseline_bps: int, mde_bps: int) -> int:
    """Check the pair and return the treated rate in basis points."""
    for name, value in (("baseline_bps", baseline_bps), ("mde_bps", mde_bps)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise PowerError(f"{name} must be an integer count of basis points")
    if not 0 <= baseline_bps <= BPS_SCALE:
        raise PowerError(f"baseline_bps must be within 0..{BPS_SCALE}, got {baseline_bps}")
    if mde_bps < 1:
        raise PowerError(
            f"mde_bps must be at least 1, got {mde_bps}; an effect of zero needs "
            "infinitely many cases to detect"
        )

    treated_bps = baseline_bps + mde_bps
    if treated_bps > BPS_SCALE:
        raise PowerError(
            f"baseline {baseline_bps}bps plus an effect of {mde_bps}bps exceeds "
            f"{BPS_SCALE}bps; a recovery rate cannot pass 100%"
        )
    return treated_bps


def _z_sum(alpha_bps: int, power_bps: int) -> int:
    """`z_{1-alpha/2} + z_{1-beta}`, at `normal.SCALE`."""
    return z_for_confidence(alpha_bps) + z_for_power(power_bps)


def required_n_holdout(
    baseline_bps: int,
    mde_bps: int,
    *,
    holdout_bps: int = BALANCED_HOLDOUT_BPS,
    alpha_bps: int = DEFAULT_ALPHA_BPS,
    power_bps: int = DEFAULT_POWER_BPS,
) -> int:
    """Cases needed in the **holdout** arm, for an arbitrary allocation.

        n_h = (z_a + z_b)^2 * [p_c(1-p_c) + p_t(1-p_t)/k] / delta^2

    with `k` the ratio of treated traffic to held-out traffic. At an even split
    `k = 1` and this reduces exactly to the balanced formula.

    Shrinking the holdout makes the *arm* slightly smaller, not larger: as `k`
    grows the treated arm stops contributing variance and the requirement falls
    toward a floor of `(z_a + z_b)^2 * p_c(1-p_c) / delta^2` — about 179 cases
    at the pre-registered parameters. What explodes is the **total**. Going
    from an even split to a 1% holdout takes the requirement from 746 cases to
    roughly 18,100, because the tiny arm still has to reach its floor and every
    case in it drags 99 treated cases along.

    That is the honest shape of the trade-off, and it is why the ratio is
    carried through the arithmetic rather than assumed away: quoting the
    balanced number for an uneven design would misstate both ends of it.
    """
    treated_bps = _validate_rates(baseline_bps, mde_bps)
    if not 0 < holdout_bps < BPS_SCALE:
        raise PowerError(
            f"holdout_bps must be within 1..{BPS_SCALE - 1}, got {holdout_bps}; "
            "an empty arm is not an experiment"
        )

    treated_share = BPS_SCALE - holdout_bps
    control_variance = baseline_bps * (BPS_SCALE - baseline_bps)
    treated_variance = treated_bps * (BPS_SCALE - treated_bps)

    z_total = _z_sum(alpha_bps, power_bps)
    variance_term = control_variance * treated_share + treated_variance * holdout_bps
    numerator = z_total * z_total * variance_term
    denominator = SCALE * SCALE * mde_bps * mde_bps * treated_share
    return _ceil_div(numerator, denominator)


def required_n_per_arm(
    baseline_bps: int,
    mde_bps: int,
    *,
    alpha_bps: int = DEFAULT_ALPHA_BPS,
    power_bps: int = DEFAULT_POWER_BPS,
) -> int:
    """Cases needed **per arm** in a balanced design.

    The formula section 7 of the pre-registration prints. With the registered
    parameters — alpha 0.05, power 0.80, baseline 0.35, MDE 10pp — this returns
    **373**. See the module docstring for why section 7 says 384.
    """
    return required_n_holdout(
        baseline_bps,
        mde_bps,
        holdout_bps=BALANCED_HOLDOUT_BPS,
        alpha_bps=alpha_bps,
        power_bps=power_bps,
    )


def rule_of_thumb_n_per_arm(baseline_bps: int, mde_bps: int) -> int:
    """`16 * pbar(1-pbar) / delta^2`, the approximation that yields 384.

    Kept so a report can print both numbers and name the gap, rather than
    quoting one and leaving a reader to wonder why it disagrees with the
    document. Carries no alpha or power: the constant 16 has them baked in at
    0.05 and 0.80, which is exactly why it cannot be adjusted.

    Computed at double scale so an odd `mde_bps` does not lose its half.
    """
    _validate_rates(baseline_bps, mde_bps)
    doubled_mean = 2 * baseline_bps + mde_bps  # 2 * pbar, in basis points
    return _ceil_div(
        4 * doubled_mean * (2 * BPS_SCALE - doubled_mean),
        mde_bps * mde_bps,
    )


def mde_for_n(
    n_per_arm: int,
    baseline_bps: int,
    *,
    alpha_bps: int = DEFAULT_ALPHA_BPS,
    power_bps: int = DEFAULT_POWER_BPS,
) -> int:
    """The smallest effect `n_per_arm` cases per arm could detect, in bps.

    The inverse of `required_n_per_arm`, found by bisection because the treated
    variance moves with the effect size and there is no clean closed form.

    This is how an underpowered result gets stated honestly. "No significant
    effect" from a small subgroup means almost nothing on its own; "this
    subgroup could only have detected an effect of 900bps or larger" is a fact
    a reader can weigh.
    """
    if n_per_arm < 1:
        raise PowerError(f"n_per_arm must be at least 1, got {n_per_arm}")
    if not 0 <= baseline_bps < BPS_SCALE:
        raise PowerError(f"baseline_bps must be within 0..{BPS_SCALE - 1}, got {baseline_bps}")

    largest = BPS_SCALE - baseline_bps
    smallest_possible = required_n_per_arm(
        baseline_bps, largest, alpha_bps=alpha_bps, power_bps=power_bps
    )
    if smallest_possible > n_per_arm:
        raise PowerError(
            f"{n_per_arm} cases per arm cannot detect any effect at this baseline, "
            f"alpha and power — even the largest possible effect of {largest}bps needs more"
        )

    low, high = 1, largest
    while low < high:
        middle = (low + high) // 2
        if required_n_per_arm(baseline_bps, middle, alpha_bps=alpha_bps, power_bps=power_bps) <= (
            n_per_arm
        ):
            high = middle
        else:
            low = middle + 1
    return low


@dataclass(frozen=True, slots=True)
class HoldoutPlan:
    """What running a holdout of this size would take, and what it would cost."""

    monthly_volume: int
    weekly_volume: int
    baseline_bps: int
    mde_bps: int
    alpha_bps: int
    power_bps: int
    holdout_bps: int
    required_n_holdout: int
    required_n_treatment: int
    weeks_to_significance: int
    holdout_cases_at_completion: int
    #: Minor units of lift given up by not treating the holdout. `None` when no
    #: average case value was supplied — an unknown cost is reported as unknown,
    #: never as zero.
    revenue_forgone: int | None
    avg_amount_minor: int | None

    @property
    def total_cases_at_completion(self) -> int:
        return self.weekly_volume * self.weeks_to_significance

    def as_dict(self) -> dict[str, object]:
        return {
            "monthly_volume": self.monthly_volume,
            "weekly_volume": self.weekly_volume,
            "baseline_bps": self.baseline_bps,
            "mde_bps": self.mde_bps,
            "alpha_bps": self.alpha_bps,
            "power_bps": self.power_bps,
            "holdout_bps": self.holdout_bps,
            "required_n_holdout": self.required_n_holdout,
            "required_n_treatment": self.required_n_treatment,
            "weeks_to_significance": self.weeks_to_significance,
            "holdout_cases_at_completion": self.holdout_cases_at_completion,
            "total_cases_at_completion": self.total_cases_at_completion,
            "revenue_forgone": self.revenue_forgone,
            "avg_amount_minor": self.avg_amount_minor,
        }


def size_holdout(
    monthly_volume: int,
    baseline_bps: int,
    mde_bps: int,
    *,
    holdout_bps: int = BALANCED_HOLDOUT_BPS,
    avg_amount_minor: int | None = None,
    alpha_bps: int = DEFAULT_ALPHA_BPS,
    power_bps: int = DEFAULT_POWER_BPS,
) -> HoldoutPlan:
    """Turn a monthly case volume into a run length and a bill.

    The holdout arm is the binding constraint whenever it is the smaller one,
    which is the usual case, so the run length is how long that arm takes to
    fill. Both arms are checked regardless.

    **Revenue forgone** is the lift given up on the held-out cases: the effect
    being measured, applied to the cases deliberately left untreated. It is the
    honest price of knowing whether the effect is real. Reported as `None`
    rather than zero when no average case value is supplied — this module does
    not invent a figure it was not given.

    A week is `12/52` of a month, taken as an exact ratio rather than a
    four-week approximation.
    """
    if monthly_volume < 1:
        raise PowerError(f"monthly_volume must be at least 1, got {monthly_volume}")
    if avg_amount_minor is not None and avg_amount_minor < 0:
        raise PowerError(f"avg_amount_minor cannot be negative, got {avg_amount_minor}")

    n_holdout = required_n_holdout(
        baseline_bps,
        mde_bps,
        holdout_bps=holdout_bps,
        alpha_bps=alpha_bps,
        power_bps=power_bps,
    )
    treated_share = BPS_SCALE - holdout_bps
    n_treatment = _ceil_div(n_holdout * treated_share, holdout_bps)

    weekly_volume = monthly_volume * _MONTHS_PER_YEAR // _WEEKS_PER_YEAR
    weekly_holdout = weekly_volume * holdout_bps // BPS_SCALE
    weekly_treatment = weekly_volume - weekly_holdout

    if weekly_holdout < 1 or weekly_treatment < 1:
        raise PowerError(
            f"a volume of {monthly_volume}/month split at {holdout_bps}bps leaves an arm "
            "with under one case a week; it would never finish"
        )

    weeks = max(
        _ceil_div(n_holdout, weekly_holdout),
        _ceil_div(n_treatment, weekly_treatment),
    )
    holdout_cases = weekly_holdout * weeks

    forgone = None
    if avg_amount_minor is not None:
        forgone = holdout_cases * mde_bps * avg_amount_minor // BPS_SCALE

    return HoldoutPlan(
        monthly_volume=monthly_volume,
        weekly_volume=weekly_volume,
        baseline_bps=baseline_bps,
        mde_bps=mde_bps,
        alpha_bps=alpha_bps,
        power_bps=power_bps,
        holdout_bps=holdout_bps,
        required_n_holdout=n_holdout,
        required_n_treatment=n_treatment,
        weeks_to_significance=weeks,
        holdout_cases_at_completion=holdout_cases,
        revenue_forgone=forgone,
        avg_amount_minor=avg_amount_minor,
    )


def is_underpowered(achieved_n_per_arm: int, planned_n_per_arm: int) -> bool:
    """Whether a result must be labelled `INTERIM - UNDERPOWERED`.

    Section 9 of the pre-registration: below the planned N the interface shows
    that label instead of a point estimate. Comparison against the *planned*
    figure, not against a requirement recomputed after the fact — recomputing
    it from the observed data would be exactly the post-hoc move a fixed
    horizon exists to prevent.
    """
    return achieved_n_per_arm < planned_n_per_arm


__all__ = [
    "BALANCED_HOLDOUT_BPS",
    "BPS_SCALE",
    "DEFAULT_ALPHA_BPS",
    "DEFAULT_POWER_BPS",
    "HoldoutPlan",
    "PowerError",
    "is_underpowered",
    "mde_for_n",
    "required_n_holdout",
    "required_n_per_arm",
    "rule_of_thumb_n_per_arm",
    "size_holdout",
]
