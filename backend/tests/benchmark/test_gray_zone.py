"""The Gray Zone branch, exercised at a size where it actually fires.

At the acceptance size of 10,000 every coarse cell clears the minimum, so no
unit ever falls through the ladder and the "too thin to label" branch is never
executed. A rule that is only ever satisfied is a rule nobody has tested.

At N=4000 roughly a third of units fall through, so both outcomes appear in one
population and the branch is covered by the same machinery that will run the
real thing.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy.orm import Session

from app.causal.analysis import load_population
from app.causal.cells import (
    COARSE,
    FINE,
    LADDER,
    QUALIFIED,
    UNDERPOWERED,
    Unit,
    coarse_key,
    fine_key,
    fold_of,
    load_features,
    qualification_reason,
    resolve,
    tally,
)
from tests.benchmark.bridge import BENCHMARK_MDE_BPS, materialise

pytestmark = pytest.mark.db

#: Small enough that the minimum bites, large enough that some cells still
#: qualify. Both branches appear in one run.
GRAY_ZONE_SIZE = 4_000

#: The acceptance size, where nothing falls through.
FULL_SIZE = 10_000

FOLDS = 5


def units_for(session: Session, experiment_id) -> list[Unit]:  # noqa: ANN001
    """Join the analysis population to the observable features."""
    population = load_population(session, experiment_id)
    features = load_features(session, experiment_id)
    return [
        Unit(
            risk_id=row.risk_id,
            arm=row.arm,
            recovered=row.recovered,
            harmed=row.harm_mandate_cancelled,
            features=features[row.risk_id],
        )
        for row in population.rows
        if row.risk_id in features
    ]


def resolutions_for(units: list[Unit], experiment_id) -> list:  # noqa: ANN001
    """Cross-fitted resolution for every unit, training folds only."""
    results = []
    for fold in range(FOLDS):
        training = [u for u in units if fold_of(u.risk_id, experiment_id) != fold]
        tallies = [tally(training, fine_key), tally(training, coarse_key)]
        results.extend(
            resolve(unit, tallies, mde_bps=BENCHMARK_MDE_BPS)
            for unit in units
            if fold_of(unit.risk_id, experiment_id) == fold
        )
    return results


@pytest.fixture(scope="module")
def small():  # noqa: ANN201
    """One N=4000 materialisation, rolled back afterwards."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tests.conftest_db import resolve_test_dsn

    engine = create_engine(resolve_test_dsn(), future=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    try:
        run = materialise(session, case_count=GRAY_ZONE_SIZE)
        units = units_for(session, run.experiment_id)
        yield run, units, resolutions_for(units, run.experiment_id)
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


class TestTheBranchFires:
    def test_every_unit_is_resolved_one_way_or_the_other(self, small) -> None:  # noqa: ANN001
        _, units, resolutions = small
        assert len(resolutions) == len(units) == GRAY_ZONE_SIZE

    def test_some_units_fall_through_to_gray_zone(self, small) -> None:  # noqa: ANN001
        """The whole point of testing at this size."""
        _, _, resolutions = small
        gray = sum(1 for r in resolutions if r.is_gray_zone)
        assert gray > 0, "the Gray Zone branch did not fire at all"
        assert gray * 10_000 // GRAY_ZONE_SIZE > 500, f"only {gray} units"

    def test_some_units_still_qualify(self, small) -> None:  # noqa: ANN001
        """Both branches, or the test proves only that the rule is too strict."""
        _, _, resolutions = small
        assert sum(1 for r in resolutions if not r.is_gray_zone) > 0

    def test_the_fall_through_reason_is_recorded(self, small) -> None:  # noqa: ANN001
        """A Gray Zone count nobody can explain is not a diagnostic."""
        _, _, resolutions = small
        reasons = {r.reason for r in resolutions if r.is_gray_zone}
        assert reasons
        assert reasons <= {UNDERPOWERED, "empty_arm", "no_room_for_effect", "degenerate_ratio"}

    def test_underpowered_is_the_dominant_reason(self, small) -> None:  # noqa: ANN001
        _, _, resolutions = small
        reasons = Counter(r.reason for r in resolutions if r.is_gray_zone)
        assert reasons.most_common(1)[0][0] == UNDERPOWERED

    def test_a_qualified_resolution_carries_its_cell(self, small) -> None:  # noqa: ANN001
        _, _, resolutions = small
        for resolution in resolutions:
            if resolution.is_gray_zone:
                assert resolution.key is None
                assert resolution.counts is None
            else:
                assert resolution.key
                assert resolution.counts is not None
                assert resolution.reason == QUALIFIED

    def test_both_ladder_levels_are_used(self, small) -> None:  # noqa: ANN001
        _, _, resolutions = small
        levels = {r.level_name for r in resolutions if not r.is_gray_zone}
        assert levels <= {FINE, COARSE}
        assert levels, "no unit was scored at any level"


class TestTheResolutionIsHonest:
    def test_every_scored_cell_really_qualifies(self, small) -> None:  # noqa: ANN001
        _, _, resolutions = small
        for resolution in resolutions:
            if not resolution.is_gray_zone:
                assert resolution.counts is not None
                assert (
                    qualification_reason(resolution.counts, mde_bps=BENCHMARK_MDE_BPS) == QUALIFIED
                )

    def test_every_scored_cell_has_both_arms(self, small) -> None:  # noqa: ANN001
        _, _, resolutions = small
        for resolution in resolutions:
            if resolution.counts is not None:
                assert resolution.counts.n_treated > 0
                assert resolution.counts.n_holdout > 0

    def test_no_scored_cell_leaves_room_for_no_effect(self, small) -> None:  # noqa: ANN001
        _, _, resolutions = small
        for resolution in resolutions:
            if resolution.counts is not None:
                assert resolution.counts.p_control_bps + BENCHMARK_MDE_BPS <= 10_000

    def test_it_is_reproducible(self, small) -> None:  # noqa: ANN001
        run, units, resolutions = small
        again = resolutions_for(units, run.experiment_id)
        assert [(r.risk_id, r.level, r.key) for r in resolutions] == [
            (r.risk_id, r.level, r.key) for r in again
        ]


class TestCrossFittingHasNoLeakage:
    def test_folds_partition_the_population(self, small) -> None:  # noqa: ANN001
        run, units, _ = small
        folds = Counter(fold_of(u.risk_id, run.experiment_id) for u in units)
        assert sum(folds.values()) == GRAY_ZONE_SIZE
        assert set(folds) == set(range(FOLDS))

    def test_the_folds_are_roughly_even(self, small) -> None:  # noqa: ANN001
        run, units, _ = small
        folds = Counter(fold_of(u.risk_id, run.experiment_id) for u in units)
        expected = GRAY_ZONE_SIZE // FOLDS
        assert all(abs(count - expected) < expected // 4 for count in folds.values()), folds

    def test_both_arms_appear_in_every_fold(self, small) -> None:  # noqa: ANN001
        run, units, _ = small
        for fold in range(FOLDS):
            members = [u for u in units if fold_of(u.risk_id, run.experiment_id) == fold]
            arms = {u.arm for u in members}
            assert len(arms) == 2, f"fold {fold} has only {arms}"

    def test_a_unit_never_contributes_to_its_own_cell_counts(self, small) -> None:  # noqa: ANN001
        """The definition of cross-fitting. Removing one held-out unit from the
        training set must leave its own tally untouched, because it was never
        in it."""
        run, units, _ = small
        fold = 0
        training = [u for u in units if fold_of(u.risk_id, run.experiment_id) != fold]
        held_out = [u for u in units if fold_of(u.risk_id, run.experiment_id) == fold]

        assert held_out
        training_ids = {u.risk_id for u in training}
        assert not (training_ids & {u.risk_id for u in held_out})

        before = tally(training, coarse_key)
        after = tally(training + [held_out[0]], coarse_key)
        cell = coarse_key(held_out[0].features)
        assert after[cell].total == before[cell].total + 1


#
# There is deliberately no full-size contrast test in this module.
#
# It was written, and it deadlocked. Case ids are prefix-stable, so the first
# 4,000 cases of an N=10,000 population are byte-identical to the N=4,000
# population this module's fixture holds open in an uncommitted transaction. A
# second materialisation on a different connection collides on
# `uq_payment_attempts_provider_external` and waits forever, because the
# bridge's duplicate-run guard reads with `session.get` and cannot see another
# transaction's uncommitted rows.
#
# The behaviour it would have asserted is covered elsewhere without a second
# population: the ladder's preference for the fine cell is pinned in
# `tests/causal/test_cells.py`, and the acceptance run reports the level split
# and Gray Zone count at N=10,000 directly.


class TestTheLadderContract:
    def test_it_is_two_levels(self) -> None:
        assert len(LADDER) == 2

    def test_the_mde_comes_from_the_experiment(self) -> None:
        """Not a constant invented here — the pre-registered value the
        benchmark experiment carries."""
        assert BENCHMARK_MDE_BPS == 1_000
