"""The normal distribution in fixed-point integers.

The analysis plan needs two things from the Gaussian: a **tail probability**,
to turn the two-proportion test statistic into a p-value, and a **quantile**,
to turn a pre-registered alpha and power into the multiplier in the sample-size
formula. Both are wanted as exact integers, for the same reason money is
(ADR 0001): a number that decides something must be reproducible on any machine,
and a float is not.

There is no float-free route to the Gaussian in the standard library, and
`scipy` is not a dependency this project may add. So the error function is
computed here directly, in fixed point.

**Representation.** A value `v` is carried as the integer `round(v * SCALE)`
with `SCALE = 10**12`. The series runs at `_INTERNAL = 10**30` and the result is
scaled down once at the end, so the ~15 digits lost to cancellation in the
alternating series still leave far more precision than the 12 that survive.

**Method.** The Maclaurin series

    erf(x) = (2/sqrt(pi)) * SUM_{n>=0} (-1)^n x^(2n+1) / (n! (2n+1))

evaluated by carrying `u_n = x^(2n+1)/n!` forward one term at a time, which
needs only integer multiplication and truncating division. The series converges
for every real `x`; past `|x| = 6` it is not evaluated at all, because
`erf(6) = 1 - 2.2e-17` is indistinguishable from 1 at this precision.

**Accuracy.** Agreement with `statistics.NormalDist` is better than `1e-11`
absolute across the tested range, verified in `tests/causal/test_normal.py`
against that independent implementation.

**One honest limit.** p-values are stored in parts per million
(`experiment_results.p_value_micros`), so a two-sided p below `5e-7` rounds to
zero micros — around `|z| > 5`. That is a limit of the storage resolution, not
of the arithmetic here, and the report says "p < 0.000001" rather than "p = 0".
"""

from __future__ import annotations

import math

#: Fixed-point unit for every public value here. A value `v` is the integer
#: `round(v * SCALE)`.
SCALE = 10**12

#: Working precision for the series. Generous on purpose: the alternating terms
#: peak far above the result and the cancellation has to come out of somewhere.
_INTERNAL = 10**30

#: Ratio between the two scales.
_STEP = _INTERNAL // SCALE

#: pi to 30 decimal places, as an integer at `_INTERNAL` scale. Written out
#: rather than computed so it carries no float anywhere in its provenance.
_PI = 3_141592653589793238462643383280

#: sqrt(2) and 2/sqrt(pi), derived from `_PI` with an integer square root.
_SQRT2 = math.isqrt(2 * _INTERNAL**2)
_TWO_OVER_SQRT_PI = math.isqrt(4 * _INTERNAL**3 // _PI)

#: Past this the series is skipped: erf(6) differs from 1 by 2.2e-17, which is
#: five orders of magnitude below the last digit `SCALE` can hold.
_ERF_SATURATION = 6 * _INTERNAL

#: Hard ceiling on series terms. The series has converged long before this for
#: any argument below saturation; the bound exists so a bug cannot spin.
_MAX_TERMS = 600

#: Parts per million, the resolution p-values are stored at.
P_VALUE_SCALE = 1_000_000


class NormalError(ValueError):
    """An argument outside the range these functions are defined for."""


def _erf_internal(x: int) -> int:
    """erf(x) at `_INTERNAL` scale, for `x` at `_INTERNAL` scale."""
    sign = -1 if x < 0 else 1
    x = abs(x)

    if x >= _ERF_SATURATION:
        return sign * _INTERNAL

    total = 0
    term_base = x  # u_0 = x
    for n in range(_MAX_TERMS):
        if n:
            # u_n = u_{n-1} * x^2 / n
            term_base = term_base * x // _INTERNAL * x // _INTERNAL // n
        if term_base == 0 and n > 8:
            break
        contribution = term_base // (2 * n + 1)
        total += -contribution if n % 2 else contribution

    return sign * (_TWO_OVER_SQRT_PI * total // _INTERNAL)


def _scale_down(value: int) -> int:
    """`_INTERNAL` scale to `SCALE`, truncating toward zero.

    Python's floor division rounds toward negative infinity, which would make
    `f(-x)` differ from `-f(x)` in the last digit. Taking the magnitude and
    reapplying the sign keeps every odd function here exactly odd.
    """
    return -(abs(value) // _STEP) if value < 0 else value // _STEP


def _erf_argument(z: int) -> int:
    """`z / sqrt(2)` at `_INTERNAL` scale, for `z` at `SCALE`.

    Sign applied outside the division for the same reason as `_scale_down`.
    """
    magnitude = abs(z) * _STEP * _INTERNAL // _SQRT2
    return -magnitude if z < 0 else magnitude


def erf(x: int) -> int:
    """The error function. `x` and the result are at `SCALE`. Exactly odd."""
    return _scale_down(_erf_internal(x * _STEP))


def normal_cdf(z: int) -> int:
    """P(Z <= z) for a standard normal. `z` and the result are at `SCALE`.

    Exactly `SCALE // 2` at `z = 0`, and monotone non-decreasing in `z`.
    `cdf(z) + cdf(-z)` equals `SCALE` to within one unit in the last place —
    the halving and the rescaling each truncate once, and forcing exactness
    would cost more than the 1e-12 it buys.
    """
    return (_INTERNAL + _erf_internal(_erf_argument(z))) // 2 // _STEP


def normal_sf(z: int) -> int:
    """P(Z > z), the upper tail. `z` and the result are at `SCALE`.

    Computed as `1 - cdf`, which is fine here: the caller converts to parts per
    million, and everything the subtraction costs is already below that.
    """
    return SCALE - normal_cdf(z)


def two_sided_p_micros(z: int) -> int:
    """Two-sided p-value in parts per million, for a statistic `z` at `SCALE`.

    Rounds half-up and clamps into `0..P_VALUE_SCALE`, matching the range
    `ck_experiment_results_p_value_in_range` enforces on the stored column. A
    result of zero means "below the stored resolution", not "impossible".
    """
    tail = normal_sf(abs(z))
    doubled = 2 * tail
    micros = (doubled * P_VALUE_SCALE + SCALE // 2) // SCALE
    return max(0, min(P_VALUE_SCALE, micros))


def normal_quantile(p: int) -> int:
    """The `p`-quantile of a standard normal. `p` and the result are at `SCALE`.

    Found by bisection on `normal_cdf`, which is monotone, so the bracket is
    always valid and the search is exact to the last representable step. Sixty
    halvings of a `[-10, 10]` bracket resolve far below `1/SCALE`.
    """
    if not 0 < p < SCALE:
        raise NormalError(
            f"p must be strictly within 0..{SCALE} exclusive, got {p}; "
            "the normal quantile is unbounded at both ends"
        )

    low, high = -10 * SCALE, 10 * SCALE
    for _ in range(60):
        if high - low <= 1:
            break
        middle = (low + high) // 2
        if normal_cdf(middle) < p:
            low = middle
        else:
            high = middle
    return high


def z_for_confidence(alpha_bps: int) -> int:
    """The two-sided critical value `z_{1 - alpha/2}`, at `SCALE`.

    `alpha_bps` is basis points, as the experiment stores it: 500 is the
    pre-registered alpha of 0.05, giving the familiar 1.959964.
    """
    if not 0 < alpha_bps < 10_000:
        raise NormalError(f"alpha_bps must be within 1..9999, got {alpha_bps}")
    return normal_quantile(SCALE - alpha_bps * SCALE // 20_000)


def z_for_power(power_bps: int) -> int:
    """The one-sided value `z_{1 - beta}`, at `SCALE`.

    `power_bps` is basis points: 8000 is the pre-registered power of 0.80,
    giving 0.841621.
    """
    if not 0 < power_bps < 10_000:
        raise NormalError(f"power_bps must be within 1..9999, got {power_bps}")
    return normal_quantile(power_bps * SCALE // 10_000)


__all__ = [
    "P_VALUE_SCALE",
    "SCALE",
    "NormalError",
    "erf",
    "normal_cdf",
    "normal_quantile",
    "normal_sf",
    "two_sided_p_micros",
    "z_for_confidence",
    "z_for_power",
]
