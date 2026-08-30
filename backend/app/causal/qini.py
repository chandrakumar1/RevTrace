"""The Qini curve: does ranking by predicted uplift actually capture more?

An uplift model can look impressive and rank no better than a coin. The Qini
curve is the check. Sort the whole enrolled population by predicted uplift,
walk down it, and at every prefix ask how much *incremental* recovery has been
captured so far:

    Q(k) = Y_treated(k) - round(Y_holdout(k) * N_treated(k) / N_holdout(k))

The correction term is what makes it incremental rather than gross. Without it
a model that simply ranked easy cases first would look excellent, which is the
same mistake the whole project exists to avoid — one level up.

**Against random targeting.** A useless ranking traces the straight line
`R(k) = k * Q(N) / N`, because any prefix of a random order captures its share
and no more. The coefficient is the area between the curve and that line,
normalised, so a positive number means the ranking beat chance and a negative
one means it was worse than chance — which is a real result and is reported as
one, not clamped to zero.

**The denominator is the complete enrolled population**, including units the
model refused to label. A ranking evaluated only on the units a model was
confident about is a ranking evaluated on a sample it selected, and would
flatter itself exactly where it knows least.

**Undefined is not zero.** When `Q(N) == 0` there is no incremental recovery to
apportion, so "what share did the top decile capture" has no answer. Both the
coefficient and the capture return `None` and say so, rather than returning a
zero that reads like a measurement.

Integer throughout, from the same half-up rounding the estimators use. Nothing
here reads a `truth_*` column, touches a database, or names a quadrant.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.causal.uplift import UpliftScore

#: Full basis-point scale.
BPS_SCALE = 10_000

#: The prefix the report quotes, as pre-registered: the top 20% by predicted
#: uplift.
TOP_SHARE_BPS = 2_000


class QiniError(ValueError):
    """A curve could not be built."""


def round_half_up(numerator: int, denominator: int) -> int:
    """`round(numerator / denominator)`, halves away from zero, no float.

    Identical to the helper the estimators use. Repeated rather than imported
    because that one is private to its module, and a rounding convention that
    silently differed between the effect estimate and the curve would be a
    genuinely nasty bug to find.
    """
    if denominator <= 0:
        raise QiniError(f"denominator must be positive, got {denominator}")
    if numerator < 0:
        return -((-numerator * 2 + denominator) // (2 * denominator))
    return (numerator * 2 + denominator) // (2 * denominator)


def _sign(value: int) -> int:
    return -1 if value < 0 else 1


# -- ranking --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankedUnit:
    """One unit as the curve needs it: a prediction and what actually happened."""

    risk_id: uuid.UUID
    uplift_bps: int
    is_treatment: bool
    recovered: bool


def rank(
    arms: Mapping[uuid.UUID, tuple[bool, bool]],
    scores: Sequence[UpliftScore],
) -> tuple[RankedUnit, ...]:
    """Order the population by predicted uplift, descending.

    `arms` maps a risk to `(is_treatment, recovered)` — the assigned arm and the
    observed outcome, both taken as stored.

    **Ties break by `risk_id`.** Predicted uplift is a cell rate, so whole cells
    tie at once — often thousands of units. Left to sort stability the curve
    would depend on the order rows arrived in, and two runs over the same data
    would report different coefficients.
    """
    missing = [s.risk_id for s in scores if s.risk_id not in arms]
    if missing:
        raise QiniError(
            f"{len(missing)} scored units have no recorded arm or outcome "
            f"(for example {missing[0]}); the curve needs the complete population"
        )

    ranked = [
        RankedUnit(
            risk_id=score.risk_id,
            uplift_bps=score.uplift_bps,
            is_treatment=arms[score.risk_id][0],
            recovered=arms[score.risk_id][1],
        )
        for score in scores
    ]
    return tuple(sorted(ranked, key=lambda unit: (-unit.uplift_bps, unit.risk_id)))


# -- the curve ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QiniCurve:
    """`Q(k)` for every prefix, plus the shape of the population behind it."""

    values: tuple[int, ...]
    n_treated: int
    n_holdout: int

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def total(self) -> int:
        """`Q(N)`: the incremental recovery over the whole population."""
        return self.values[-1] if self.values else 0

    @property
    def is_defined(self) -> bool:
        """Whether there is any incremental recovery to apportion."""
        return bool(self.values) and self.total != 0

    def at(self, k: int) -> int:
        """`Q(k)`. `Q(0)` is zero: nothing chosen captures nothing."""
        if not 0 <= k <= self.n:
            raise QiniError(f"k must be within 0..{self.n}, got {k}")
        return 0 if k == 0 else self.values[k - 1]

    def random_at(self, k: int) -> int:
        """`R(k) = k * Q(N) / N`, rounded. The line a useless ranking traces."""
        if not 0 <= k <= self.n:
            raise QiniError(f"k must be within 0..{self.n}, got {k}")
        if self.n == 0:
            return 0
        return _sign(self.total) * round_half_up(k * abs(self.total), self.n)


def qini_curve(ranked: Sequence[RankedUnit]) -> QiniCurve:
    """Walk the ranking once, accumulating `Q(k)`.

    A prefix holding no control units has no counterfactual, so its correction
    term is `0/0`. Taken as zero — with no controls there are also no control
    recoveries to subtract, and the alternative is refusing to plot the first
    few points of every curve.
    """
    values: list[int] = []
    treated_recoveries = holdout_recoveries = 0
    n_treated = n_holdout = 0

    for unit in ranked:
        if unit.is_treatment:
            n_treated += 1
            treated_recoveries += int(unit.recovered)
        else:
            n_holdout += 1
            holdout_recoveries += int(unit.recovered)

        if n_holdout == 0:
            values.append(treated_recoveries)
        else:
            values.append(
                treated_recoveries - round_half_up(holdout_recoveries * n_treated, n_holdout)
            )

    return QiniCurve(values=tuple(values), n_treated=n_treated, n_holdout=n_holdout)


# -- the coefficient ------------------------------------------------------


def qini_coefficient_bps(curve: QiniCurve) -> int | None:
    """Area between the curve and the random line, normalised, in basis points.

        A        = SUM_k ( N * Q(k) - k * Q(N) )
        qini_bps = round( 10000 * A / (N^2 * |Q(N)|) )

    Scaled by `N` inside the sum so the random line stays exact and nothing
    rounds until the end.

    Roughly bounded by +/- 5000 bps: a ranking that captured the entire
    incremental effect in its first unit and held flat would score about +5000,
    random scores about 0, and a perfectly inverted ranking about -5000.

    `None` when `Q(N) == 0` — with no incremental recovery there is no area to
    normalise against, and returning zero would be indistinguishable from
    "ranked no better than chance".
    """
    if not curve.is_defined:
        return None

    n, total = curve.n, curve.total
    area = sum(n * curve.at(k) - k * total for k in range(1, n + 1))
    return round_half_up(BPS_SCALE * abs(area), n * n * abs(total)) * _sign(area)


# -- top-share capture ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capture:
    """How much of the incremental recovery a top prefix accounted for."""

    share_bps: int
    k: int
    n: int
    qini_at_k: int
    total: int
    capture_bps: int | None

    @property
    def is_defined(self) -> bool:
        return self.capture_bps is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "share_bps": self.share_bps,
            "k": self.k,
            "n": self.n,
            "qini_at_k": self.qini_at_k,
            "total": self.total,
            "capture_bps": self.capture_bps,
        }


def top_capture(curve: QiniCurve, share_bps: int = TOP_SHARE_BPS) -> Capture:
    """Incremental recovery captured by the top `share_bps` of the ranking.

    `k` truncates rather than rounds: the top 20% of 999 units is 199, not 200.
    Taking one more than the share names would quietly overstate the capture.
    """
    if not 0 < share_bps <= BPS_SCALE:
        raise QiniError(f"share_bps must be within 1..{BPS_SCALE}, got {share_bps}")

    k = curve.n * share_bps // BPS_SCALE
    at_k = curve.at(k)
    total = curve.total

    capture_bps = (
        round_half_up(BPS_SCALE * at_k * _sign(total), abs(total)) if curve.is_defined else None
    )

    return Capture(
        share_bps=share_bps,
        k=k,
        n=curve.n,
        qini_at_k=at_k,
        total=total,
        capture_bps=capture_bps,
    )


# -- assembled result -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class QiniResult:
    """Everything the report needs about one ranking."""

    curve: QiniCurve
    coefficient_bps: int | None
    top: Capture

    @property
    def is_defined(self) -> bool:
        return self.curve.is_defined

    @property
    def beats_random(self) -> bool:
        """Undefined does not beat random; it says nothing at all."""
        return self.coefficient_bps is not None and self.coefficient_bps > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.curve.n,
            "n_treated": self.curve.n_treated,
            "n_holdout": self.curve.n_holdout,
            "qini_total": self.curve.total,
            "qini_coefficient_bps": self.coefficient_bps,
            "is_defined": self.is_defined,
            "beats_random": self.beats_random,
            "top_capture": self.top.as_dict(),
        }


def evaluate(
    ranked: Sequence[RankedUnit],
    *,
    share_bps: int = TOP_SHARE_BPS,
) -> QiniResult:
    """Curve, coefficient and top-share capture from one ranking."""
    curve = qini_curve(ranked)
    return QiniResult(
        curve=curve,
        coefficient_bps=qini_coefficient_bps(curve),
        top=top_capture(curve, share_bps=share_bps),
    )


__all__ = [
    "BPS_SCALE",
    "TOP_SHARE_BPS",
    "Capture",
    "QiniCurve",
    "QiniError",
    "QiniResult",
    "RankedUnit",
    "evaluate",
    "qini_coefficient_bps",
    "qini_curve",
    "rank",
    "round_half_up",
    "top_capture",
]
