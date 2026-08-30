"""The fixed-point normal, checked against an independent implementation.

`statistics.NormalDist` is stdlib, float, and written by someone else — which
makes it exactly the right reference. Floats are allowed here and nowhere in
`app/causal/`; the whole point of this file is to prove the integer version
agrees with a implementation that had nothing to do with it.
"""

from __future__ import annotations

import ast
import inspect
import math
import pathlib
from statistics import NormalDist

import pytest

from app.causal.normal import (
    P_VALUE_SCALE,
    SCALE,
    NormalError,
    erf,
    normal_cdf,
    normal_quantile,
    normal_sf,
    two_sided_p_micros,
    z_for_confidence,
    z_for_power,
)

REFERENCE = NormalDist()

#: Absolute tolerance against the reference, as a fraction of 1.
TOLERANCE = 1e-11


def as_float(scaled: int) -> float:
    """Fixed-point back to float, for comparison against the reference only."""
    return scaled / SCALE


def scaled(value: float) -> int:
    """Float to fixed-point, for driving the functions under test."""
    return round(value * SCALE)


class TestConstants:
    def test_the_scale_is_a_million_millions(self) -> None:
        assert SCALE == 10**12

    def test_pi_is_carried_to_thirty_places(self) -> None:
        from app.causal.normal import _INTERNAL, _PI

        assert abs(_PI / _INTERNAL - math.pi) < 1e-15

    def test_sqrt_two_is_derived_exactly(self) -> None:
        from app.causal.normal import _INTERNAL, _SQRT2

        assert abs(_SQRT2 / _INTERNAL - math.sqrt(2)) < 1e-25

    def test_two_over_root_pi_is_derived_exactly(self) -> None:
        from app.causal.normal import _INTERNAL, _TWO_OVER_SQRT_PI

        assert abs(_TWO_OVER_SQRT_PI / _INTERNAL - 2 / math.sqrt(math.pi)) < 1e-25


class TestErrorFunction:
    def test_it_is_zero_at_zero(self) -> None:
        assert erf(0) == 0

    def test_it_matches_the_reference(self) -> None:
        for step in range(-600, 601, 7):
            x = step / 100
            assert abs(as_float(erf(scaled(x))) - math.erf(x)) < TOLERANCE, x

    def test_it_is_odd(self) -> None:
        for step in range(1, 600, 13):
            x = scaled(step / 100)
            assert erf(x) == -erf(-x)

    def test_it_saturates_rather_than_overshooting(self) -> None:
        """Past the saturation point the series is skipped. It must return
        exactly one, never one-plus-a-rounding-error."""
        for x in (6, 7, 20, 100):
            assert erf(scaled(x)) == SCALE
            assert erf(scaled(-x)) == -SCALE

    def test_it_is_monotone(self) -> None:
        previous = -SCALE - 1
        for step in range(-600, 601, 5):
            current = erf(scaled(step / 100))
            assert current >= previous, step
            previous = current


class TestCumulative:
    def test_it_is_one_half_at_zero(self) -> None:
        assert normal_cdf(0) == SCALE // 2

    def test_it_matches_the_reference(self) -> None:
        for step in range(-800, 801, 7):
            z = step / 100
            assert abs(as_float(normal_cdf(scaled(z))) - REFERENCE.cdf(z)) < TOLERANCE, z

    def test_the_familiar_values(self) -> None:
        assert abs(as_float(normal_cdf(scaled(1.959964))) - 0.975) < 1e-6
        assert abs(as_float(normal_cdf(scaled(1.644854))) - 0.95) < 1e-6
        assert abs(as_float(normal_cdf(scaled(2.575829))) - 0.995) < 1e-6

    def test_the_sixty_eight_ninety_five_rule(self) -> None:
        one = as_float(normal_cdf(scaled(1))) - as_float(normal_cdf(scaled(-1)))
        two = as_float(normal_cdf(scaled(2))) - as_float(normal_cdf(scaled(-2)))
        assert abs(one - 0.6826894921) < 1e-9
        assert abs(two - 0.9544997361) < 1e-9

    def test_it_is_monotone(self) -> None:
        previous = -1
        for step in range(-800, 801, 5):
            current = normal_cdf(scaled(step / 100))
            assert current >= previous, step
            previous = current

    def test_it_is_symmetric(self) -> None:
        """To within one unit in the last place: the halving and the rescaling
        each truncate once, at 1e-12 apiece."""
        for step in range(1, 500, 11):
            z = scaled(step / 100)
            assert abs(normal_cdf(z) + normal_cdf(-z) - SCALE) <= 1

    def test_the_survival_function_is_the_complement(self) -> None:
        for step in range(-400, 401, 17):
            z = scaled(step / 100)
            assert normal_sf(z) == SCALE - normal_cdf(z)


class TestQuantile:
    def test_it_is_zero_at_one_half(self) -> None:
        assert abs(normal_quantile(SCALE // 2)) <= 1

    def test_it_matches_the_reference(self) -> None:
        for permille in range(1, 1000, 7):
            p = permille / 1000
            assert abs(as_float(normal_quantile(scaled(p))) - REFERENCE.inv_cdf(p)) < 1e-9, p

    def test_the_cumulative_inverts_the_quantile(self) -> None:
        """The well-conditioned direction, so this one is exact to a hair."""
        for permille in range(1, 1000, 7):
            p = scaled(permille / 1000)
            assert abs(normal_cdf(normal_quantile(p)) - p) <= 1, permille

    def test_it_inverts_the_cumulative(self) -> None:
        """The badly-conditioned direction. Recovering `z` from `cdf(z)` in the
        tail cannot be exact: at z = -3 the density is 0.0044, so one unit of
        probability is worth ~226 units of z. The tolerance is that factor,
        not a fudge — asserting anything tighter would be asserting that
        arithmetic beats information."""
        for step in range(-300, 301, 13):
            z = step / 100
            density = REFERENCE.pdf(z)
            tolerance = round(1 / density) + 2
            assert abs(normal_quantile(normal_cdf(scaled(z))) - scaled(z)) <= tolerance, step

    def test_it_is_monotone(self) -> None:
        previous = -11 * SCALE
        for permille in range(1, 1000, 3):
            current = normal_quantile(scaled(permille / 1000))
            assert current >= previous, permille
            previous = current

    def test_the_ends_are_rejected(self) -> None:
        for bad in (0, SCALE, -1, SCALE + 1):
            with pytest.raises(NormalError, match="strictly within"):
                normal_quantile(bad)


class TestPreRegisteredCriticalValues:
    def test_alpha_five_percent_gives_the_familiar_multiplier(self) -> None:
        """`alpha_bps = 500` is what the pre-registration stores."""
        assert abs(as_float(z_for_confidence(500)) - 1.959963985) < 1e-9

    def test_power_eighty_percent_gives_the_familiar_multiplier(self) -> None:
        """`power_bps = 8000` is what the pre-registration stores."""
        assert abs(as_float(z_for_power(8000)) - 0.841621234) < 1e-9

    def test_their_sum_squared_is_the_documented_constant(self) -> None:
        """Section 7 of the pre-registration quotes 7.84 for this."""
        total = as_float(z_for_confidence(500)) + as_float(z_for_power(8000))
        assert abs(total**2 - 7.848879) < 1e-5

    def test_a_tighter_alpha_demands_a_larger_multiplier(self) -> None:
        assert z_for_confidence(100) > z_for_confidence(500) > z_for_confidence(1000)

    def test_more_power_demands_a_larger_multiplier(self) -> None:
        assert z_for_power(9000) > z_for_power(8000) > z_for_power(5000)

    def test_out_of_range_parameters_are_rejected(self) -> None:
        for bad in (0, 10_000, -1, 20_000):
            with pytest.raises(NormalError, match="alpha_bps"):
                z_for_confidence(bad)
            with pytest.raises(NormalError, match="power_bps"):
                z_for_power(bad)


class TestTwoSidedPValue:
    def test_a_zero_statistic_is_certainty_of_nothing(self) -> None:
        assert two_sided_p_micros(0) == P_VALUE_SCALE

    def test_the_familiar_thresholds(self) -> None:
        assert abs(two_sided_p_micros(scaled(1.959964)) - 50_000) <= 2
        assert abs(two_sided_p_micros(scaled(1.644854)) - 100_000) <= 2
        assert abs(two_sided_p_micros(scaled(2.575829)) - 10_000) <= 2

    def test_it_matches_the_reference(self) -> None:
        for step in range(0, 400, 7):
            z = step / 100
            expected = 2 * (1 - REFERENCE.cdf(z)) * P_VALUE_SCALE
            assert abs(two_sided_p_micros(scaled(z)) - expected) <= 1, z

    def test_it_is_symmetric_in_sign(self) -> None:
        for step in range(0, 500, 11):
            z = scaled(step / 100)
            assert two_sided_p_micros(z) == two_sided_p_micros(-z)

    def test_it_is_non_increasing_in_the_statistic(self) -> None:
        previous = P_VALUE_SCALE + 1
        for step in range(0, 600, 5):
            current = two_sided_p_micros(scaled(step / 100))
            assert current <= previous, step
            previous = current

    def test_a_huge_statistic_bottoms_out_at_the_stored_resolution(self) -> None:
        """Zero micros means "below what the column can hold", and the report
        must render it as `p < 0.000001` rather than as `p = 0`."""
        assert two_sided_p_micros(scaled(8)) == 0
        assert two_sided_p_micros(scaled(40)) == 0

    def test_it_never_leaves_the_stored_range(self) -> None:
        for step in range(-1000, 1001, 9):
            value = two_sided_p_micros(scaled(step / 100))
            assert 0 <= value <= P_VALUE_SCALE


class TestPurity:
    """No float may enter the implementation, whatever this test file does."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import normal as module

        return ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))

    @classmethod
    def _identifiers(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                found.add(node.name)
        return found

    def test_there_is_no_true_division(self) -> None:
        for node in ast.walk(self._tree()):
            assert not isinstance(node, ast.Div), ast.dump(node)

    def test_no_float_constant_appears(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), node.value

    def test_no_float_or_decimal_is_constructed(self) -> None:
        identifiers = self._identifiers()
        for banned in ("float", "Decimal", "getcontext", "fsum", "sqrt", "exp", "log"):
            assert banned not in identifiers, banned

    def test_it_uses_the_integer_square_root(self) -> None:
        assert "isqrt" in self._identifiers()

    def test_no_scientific_stack_dependency(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported == {"math", "__future__"}

    def test_it_reads_no_clock_and_no_randomness(self) -> None:
        identifiers = self._identifiers()
        for banned in ("now", "utcnow", "today", "random", "seed"):
            assert banned not in identifiers, banned

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name
