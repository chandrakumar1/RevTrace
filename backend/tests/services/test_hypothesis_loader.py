"""The loader: stored data to the one payload a model may see.

Two things are worth testing here and one of them is easy to get wrong.

The easy one is refusal — an empty population, an empty arm, a duplicate key.

The hard one is **agreement with the estimator**. The loader mirrors the branch
in `uplift.score` to find the cell that scores a unit. A copy can drift, and a
drifted copy would show the model numbers the estimator never used, silently,
while every other test still passed. `TestItAgreesWithTheEstimator` pins that
field by field against `uplift.score`'s own output for the same unit, which is
the only check that would catch it.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.agents.contracts import LADDER_LEVELS, CellStat
from app.causal.cells import LADDER, CellCounts, Features
from app.causal.estimators import BOOTSTRAP_SEED, Interval
from app.causal.uplift import (
    DEFAULT_FOLD_COUNT,
    GLOBAL_CELL,
    CellModel,
    fit_fold,
    fold_of,
    load_units,
    score,
)
from app.services.hypothesis.loader import (
    FEATURE_VOCABULARY,
    TRUTH_PREFIX,
    LoaderError,
    _cell_stat,
    _ladder_level,
    collect_cells,
    load_hypothesis_request,
    load_population_summary,
)
from tests.benchmark.bridge import materialise


def a_cell_model(
    *,
    level: int | None,
    level_name: str | None,
    key: str = "insufficient_funds|upi",
) -> CellModel:
    """A `CellModel` built by hand, so the mapping is testable without a run."""
    counts = CellCounts(n_treated=100, n_holdout=100, recovered_treated=40, recovered_holdout=10)
    return CellModel(
        key=key,
        level=level,
        level_name=level_name,
        counts=counts,
        harm_counts=CellCounts(n_treated=100, n_holdout=100),
        interval=Interval(low=1_000, high=5_000, alpha_bps=500, resamples=40, seed=1),
        qualified=level is not None,
        reason="qualified" if level is not None else "global_fallback",
    )


#: Small enough for the fast suite; the mapping does not depend on size.
CASE_COUNT = 400
SEED = 91
FAST_RESAMPLES = 40
ALPHA_BPS = 500
MDE_BPS = 1_000


#: The committed canonical population. Large enough that units resolve to real
#: ladder rungs — both of them — which the small fixture never does.
CANONICAL_EXPERIMENT_ID = uuid.UUID("2b3e9c9f-60e8-5413-adc2-456b89e017b1")

#: Its own database, separate from the ephemeral pytest one. Read-only here
#: and written only by a deliberate `run_materialise.py --commit`.
HYPOTHESIS_DATABASE = "revtrace_hypothesis_test"
#: No role in the DSN: libpq falls back to the operating-system user. Override
#: with `HYPOTHESIS_DATABASE_URL` when the PostgreSQL role differs from it.
HYPOTHESIS_DSN = os.environ.get(
    "HYPOTHESIS_DATABASE_URL",
    f"postgresql+psycopg://localhost:5432/{HYPOTHESIS_DATABASE}",
)


@pytest.fixture(scope="module")
def materialised(db_engine):  # noqa: ANN001, ANN201
    """A **sparse** population: 400 cases, every unit on the global fallback.

    Kept deliberately. It is the fixture that exercises the refusal path, and
    the reason the ladder bug survived 45 green tests — the tests and the real
    data were hitting disjoint branches.
    """
    from sqlalchemy.orm import sessionmaker

    connection = db_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    try:
        run = materialise(session, seed=SEED, case_count=CASE_COUNT)
        yield session, run.experiment_id
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="module")
def canonical():  # noqa: ANN201
    """The committed N=10,000 population, read-only, from its **own** database.

    Deliberately not `db_engine`. `revtrace_test` is contractually ephemeral —
    every test materialises, asserts and rolls back, and `conftest_db` says so —
    so a population committed there breaks that invariant for the whole suite.
    It did: eleven modules failed, six on the bridge's seed-collision guard and
    five simply because they count rows and expect none.

    The persistent population therefore lives in `revtrace_hypothesis_test`,
    which satisfies the same `_test` marker rule and is never written by pytest.
    Read rather than materialised: 10,000 cases is minutes per run, and the
    committed experiment exists precisely so a hypothesis run can read it.

    Skipped rather than failed when absent, so the suite still runs on a machine
    that has never created it — the ladder mapping itself is pinned by the
    hermetic tests above, which need no population at all.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session

    engine = create_engine(HYPOTHESIS_DSN, future=True)
    try:
        with _Session(engine) as session:
            exists = session.execute(
                sa_text("SELECT count(*) FROM experiments WHERE id = :i"),
                {"i": str(CANONICAL_EXPERIMENT_ID)},
            ).scalar_one()
            if not exists:
                pytest.skip(
                    f"the canonical experiment {CANONICAL_EXPERIMENT_ID} is not "
                    f"materialised in {HYPOTHESIS_DATABASE}; run "
                    f"TEST_DATABASE_URL=…/{HYPOTHESIS_DATABASE} "
                    f"run_materialise.py --commit"
                )
            yield session, CANONICAL_EXPERIMENT_ID
    except OperationalError:
        pytest.skip(f"{HYPOTHESIS_DATABASE} is unreachable")
    finally:
        engine.dispose()


# -- the vocabulary is derived, and carries no ground truth ----------------


class TestTheFeatureVocabulary:
    def test_it_is_derived_from_features_rather_than_written_out(self) -> None:
        from dataclasses import fields

        assert FEATURE_VOCABULARY == tuple(f.name for f in fields(Features))

    def test_it_names_no_truth_field(self) -> None:
        assert not [n for n in FEATURE_VOCABULARY if n.startswith(TRUTH_PREFIX)]

    def test_it_names_no_identifier_or_amount(self) -> None:
        """A cell key is made of observables; nothing here identifies a unit."""
        for banned in ("risk_id", "customer", "order", "amount_at_risk", "lifetime_value"):
            assert banned not in FEATURE_VOCABULARY


# -- refusals --------------------------------------------------------------


class TestItRefusesRatherThanDegrades:
    def test_no_units_is_refused(self) -> None:
        with pytest.raises(LoaderError, match="no units"):
            collect_cells([], uuid.uuid4(), alpha_bps=ALPHA_BPS, mde_bps=MDE_BPS)

    @pytest.mark.db
    def test_an_unknown_experiment_is_refused(self, db_session: Session) -> None:
        with pytest.raises(Exception):  # noqa: B017 - the causal layer names its own error
            load_hypothesis_request(db_session, uuid.uuid4(), alpha_bps=ALPHA_BPS, mde_bps=MDE_BPS)


# -- the ladder mapping: by index, never by name ---------------------------


class TestTheLadderMapping:
    """The bug the first live preflight caught, pinned three ways.

    `CellModel.level_name` is the ladder's key *expression*; the contract's rung
    names are `('fine', 'coarse')`. The loader mapped one to the other by
    passing the name straight through, which `CellStat.__post_init__` refused
    with `unknown ladder level 'failure_code|payment_method'`.

    It survived 45 green tests because the 400-case fixture resolves *every*
    unit to the global fallback, where `level_name` is None and a fallback
    supplied a valid value. The tests and the real data exercised disjoint
    branches. These tests exercise both, without a database.
    """

    def test_the_two_vocabularies_differ_by_name(self) -> None:
        """The premise. If these ever coincided, the mapping below would look
        correct for the wrong reason."""
        assert LADDER_LEVELS == ("fine", "coarse")
        assert [name for name, _ in LADDER] == ["failure_code|payment_method", "failure_code"]
        assert set(LADDER_LEVELS).isdisjoint({name for name, _ in LADDER})

    def test_they_are_parallel_by_index(self) -> None:
        assert len(LADDER_LEVELS) == len(LADDER)

    @pytest.mark.parametrize(
        ("level", "expected"), [(0, "fine"), (1, "coarse")], ids=["fine", "coarse"]
    )
    def test_a_real_rung_maps_by_index(self, level: int, expected: str) -> None:
        cell = a_cell_model(level=level, level_name=LADDER[level][0])
        assert _ladder_level(cell) == expected
        assert _cell_stat(cell).ladder_level == expected

    def test_the_causal_name_is_never_used_as_the_contract_name(self) -> None:
        """The exact failure: passing `level_name` through would raise here."""
        cell = a_cell_model(level=0, level_name="failure_code|payment_method")
        assert _cell_stat(cell).ladder_level == "fine"

    def test_the_global_fallback_has_no_rung(self) -> None:
        cell = a_cell_model(level=None, level_name=None, key=GLOBAL_CELL)
        with pytest.raises(LoaderError, match="sits on no ladder rung"):
            _ladder_level(cell)

    def test_the_global_fallback_is_never_labelled_coarse(self) -> None:
        """The rejected Option A, asserted so it cannot come back quietly."""
        cell = a_cell_model(level=None, level_name=None, key=GLOBAL_CELL)
        with pytest.raises(LoaderError):
            _cell_stat(cell)

    def test_a_rung_the_contract_cannot_name_is_refused(self) -> None:
        """If the causal ladder grew a third level, this fails rather than
        silently dropping or mislabelling it."""
        cell = a_cell_model(level=len(LADDER_LEVELS), level_name="whatever")
        with pytest.raises(LoaderError, match="have diverged"):
            _ladder_level(cell)


# -- the global fallback is excluded, and an all-fallback run is refused ----


@pytest.mark.db
class TestTheGlobalFallbackIsExcluded:
    def test_a_sparse_population_is_refused_explicitly(self, db_session: Session) -> None:
        """The 400-case fixture: every unit falls to the global fallback.

        It used to pass through this branch by accident. Now it is the branch
        under test, and the refusal is the assertion.
        """
        run = materialise(db_session, seed=SEED, case_count=CASE_COUNT)
        units = load_units(db_session, run.experiment_id)
        assert units, "the fixture must produce units for this test to mean anything"

        # Every unit really does fall to the fallback — otherwise the refusal
        # below would be testing something else.
        fallbacks = 0
        by_fold: dict[int, list] = {}
        for unit in units:
            by_fold.setdefault(
                fold_of(unit.risk_id, run.experiment_id, folds=DEFAULT_FOLD_COUNT), []
            ).append(unit)
        for fold in sorted(by_fold):
            model = fit_fold(
                units,
                run.experiment_id,
                fold,
                alpha_bps=ALPHA_BPS,
                mde_bps=MDE_BPS,
                resamples=FAST_RESAMPLES,
            )
            fallbacks += sum(1 for unit in by_fold[fold] if score(unit, model).level is None)
        assert fallbacks == len(units)

        with pytest.raises(LoaderError, match="no qualifying ladder cells"):
            collect_cells(
                units,
                run.experiment_id,
                alpha_bps=ALPHA_BPS,
                mde_bps=MDE_BPS,
                resamples=FAST_RESAMPLES,
            )

    def test_no_empty_request_is_ever_constructed(self, db_session: Session) -> None:
        """The refusal comes from the loader, not from the contract.

        `HypothesisRequest` would also reject an empty tuple, but by then the
        failure would name a contract the caller never touched. This asserts the
        loader refuses first, with a reason that says what actually happened.
        """
        run = materialise(db_session, seed=SEED + 1, case_count=CASE_COUNT)
        with pytest.raises(LoaderError, match="global fallback"):
            load_hypothesis_request(
                db_session,
                run.experiment_id,
                alpha_bps=ALPHA_BPS,
                mde_bps=MDE_BPS,
                resamples=FAST_RESAMPLES,
            )

    def test_the_refusal_names_the_fallback_count(self, db_session: Session) -> None:
        run = materialise(db_session, seed=SEED + 2, case_count=CASE_COUNT)
        units = load_units(db_session, run.experiment_id)
        with pytest.raises(LoaderError) as caught:
            collect_cells(
                units,
                run.experiment_id,
                alpha_bps=ALPHA_BPS,
                mde_bps=MDE_BPS,
                resamples=FAST_RESAMPLES,
            )
        assert f"{len(units):,}" in str(caught.value)


# -- the mapping, field by field -------------------------------------------


@pytest.mark.db
class TestItAgreesWithTheEstimator:
    """Run against the canonical population, where real ladder rungs exist."""

    def test_every_cell_matches_the_score_the_estimator_produced(
        self,
        canonical,  # noqa: ANN001
    ) -> None:
        """The check that catches a drifted copy of `uplift.score`'s branch."""
        session, experiment_id = canonical
        units = load_units(session, experiment_id)
        cells = collect_cells(
            units,
            experiment_id,
            alpha_bps=ALPHA_BPS,
            mde_bps=MDE_BPS,
            resamples=FAST_RESAMPLES,
            seed=BOOTSTRAP_SEED,
        )
        by_key = {cell.cell_key: cell for cell in cells}

        by_fold: dict[int, list] = {}
        for unit in units:
            fold = fold_of(unit.risk_id, experiment_id, folds=DEFAULT_FOLD_COUNT)
            by_fold.setdefault(fold, []).append(unit)

        checked = 0
        for fold in sorted(by_fold):
            model = fit_fold(
                units,
                experiment_id,
                fold,
                alpha_bps=ALPHA_BPS,
                mde_bps=MDE_BPS,
                resamples=FAST_RESAMPLES,
                seed=BOOTSTRAP_SEED,
            )
            for unit in by_fold[fold]:
                scored = score(unit, model)
                candidates = [
                    stat
                    for key, stat in by_key.items()
                    if key == scored.cell_key or key.startswith(f"{scored.cell_key}@fold")
                ]
                assert candidates, f"{scored.cell_key} is absent from the payload"
                match = [
                    stat
                    for stat in candidates
                    if stat.uplift_bps == scored.uplift_bps
                    and stat.ci_low_bps == scored.interval.low
                    and stat.ci_high_bps == scored.interval.high
                    and stat.n_treated == scored.n_treated
                    and stat.n_holdout == scored.n_holdout
                    and stat.p_treat_bps == scored.p_treat_bps
                    and stat.p_control_bps == scored.p_control_bps
                    and stat.qualified == scored.qualified
                    and stat.qualification_reason == scored.reason
                ]
                assert match, (
                    f"no payload cell matches the estimator's score for {scored.cell_key}: {scored}"
                )
                checked += 1
        assert checked == len(units)

    def test_the_recovered_counts_are_carried_not_reconstructed(
        self,
        canonical,  # noqa: ANN001
    ) -> None:
        """`p_treat_bps` rounds; recovering counts from it would be lossy.

        Asserted by recomputing the rate from the carried counts and requiring
        it to reproduce the estimator's own value exactly.
        """
        session, experiment_id = canonical
        units = load_units(session, experiment_id)
        cells = collect_cells(
            units,
            experiment_id,
            alpha_bps=ALPHA_BPS,
            mde_bps=MDE_BPS,
            resamples=FAST_RESAMPLES,
        )
        for cell in cells:
            if cell.n_treated:
                expected = (cell.recovered_treated * 2 * 10_000 + cell.n_treated) // (
                    2 * cell.n_treated
                )
                assert cell.p_treat_bps == expected, cell.cell_key
            if cell.n_holdout:
                expected = (cell.recovered_holdout * 2 * 10_000 + cell.n_holdout) // (
                    2 * cell.n_holdout
                )
                assert cell.p_control_bps == expected, cell.cell_key


# -- the assembled request -------------------------------------------------


@pytest.mark.db
class TestTheAssembledRequest:
    @pytest.fixture(scope="class")
    @classmethod
    def request_payload(cls, canonical):  # noqa: ANN001, ANN201
        session, experiment_id = canonical
        return load_hypothesis_request(
            session,
            experiment_id,
            alpha_bps=ALPHA_BPS,
            mde_bps=MDE_BPS,
            resamples=FAST_RESAMPLES,
        )

    def test_it_names_the_experiment_it_was_asked_for(self, request_payload, canonical) -> None:  # noqa: ANN001
        _session, experiment_id = canonical
        assert request_payload.experiment_id == experiment_id

    def test_cell_keys_are_distinct(self, request_payload) -> None:  # noqa: ANN001
        keys = [cell.cell_key for cell in request_payload.cells]
        assert len(set(keys)) == len(keys)

    def test_every_ladder_level_is_one_the_contract_knows(self, request_payload) -> None:  # noqa: ANN001
        for cell in request_payload.cells:
            assert cell.ladder_level in LADDER_LEVELS

    def test_the_population_carries_both_arms_and_an_interval(self, request_payload) -> None:  # noqa: ANN001
        population = request_payload.population
        assert population.n_treatment > 0
        assert population.n_holdout > 0
        assert population.ci_low_bps <= population.ate_bps <= population.ci_high_bps

    def test_the_prompt_payload_carries_only_aggregates(self, request_payload) -> None:  # noqa: ANN001
        """The same guard the agent tests apply, on a payload built from real
        stored rows rather than from a fixture."""
        import json

        rendered = json.dumps(request_payload.as_prompt_payload())
        for banned in (
            "truth_y0",
            "truth_y1",
            "truth_harm_0",
            "truth_harm_1",
            "truth_segment",
            "risk_id",
            "customer",
            "order_id",
            "amount_at_risk",
            "unit_cost",
            "experiment_id",
            "lifetime_value",
        ):
            assert banned not in rendered, banned

    def test_the_allowed_keys_are_exactly_the_cells_shown(self, request_payload) -> None:  # noqa: ANN001
        payload = request_payload.as_prompt_payload()
        assert set(payload["allowed_cell_keys"]) == {cell["cell_key"] for cell in payload["cells"]}

    def test_it_reads_the_same_twice(self, canonical) -> None:  # noqa: ANN001
        """Deterministic, so a recorded response stays replayable."""
        session, experiment_id = canonical
        kwargs = {
            "alpha_bps": ALPHA_BPS,
            "mde_bps": MDE_BPS,
            "resamples": FAST_RESAMPLES,
        }
        first = load_hypothesis_request(session, experiment_id, **kwargs)
        second = load_hypothesis_request(session, experiment_id, **kwargs)
        assert first.as_prompt_payload() == second.as_prompt_payload()


# -- the population summary ------------------------------------------------


@pytest.mark.db
class TestThePopulationSummary:
    def test_it_estimates_from_the_causal_layer(self, canonical) -> None:  # noqa: ANN001
        session, experiment_id = canonical
        summary = load_population_summary(
            session, experiment_id, alpha_bps=ALPHA_BPS, resamples=FAST_RESAMPLES
        )
        assert isinstance(summary.ate_bps, int)
        assert summary.ci_low_bps <= summary.ci_high_bps
        assert summary.feature_vocabulary == FEATURE_VOCABULARY


# -- the contract still refuses what it always refused ---------------------


class TestTheContractStillGuards:
    def test_a_duplicate_key_is_refused_by_the_contract(self) -> None:
        from app.agents.contracts import HypothesisRequest, PopulationSummary

        cell = CellStat(
            cell_key="a|b",
            ladder_level="fine",
            n_treated=10,
            n_holdout=10,
            recovered_treated=5,
            recovered_holdout=1,
            p_treat_bps=5_000,
            p_control_bps=1_000,
            uplift_bps=4_000,
            ci_low_bps=1_000,
            ci_high_bps=7_000,
            qualified=True,
            qualification_reason="qualified",
        )
        population = PopulationSummary(
            ate_bps=100,
            ci_low_bps=0,
            ci_high_bps=200,
            n_treatment=10,
            n_holdout=10,
            feature_vocabulary=FEATURE_VOCABULARY,
        )
        with pytest.raises(ValueError, match="distinct"):
            HypothesisRequest(
                experiment_id=uuid.uuid4(),
                population=population,
                cells=(cell, replace(cell)),
            )

    def test_an_empty_cell_tuple_is_refused_by_the_contract(self) -> None:
        from app.agents.contracts import HypothesisRequest, PopulationSummary

        with pytest.raises(ValueError, match="at least one cell"):
            HypothesisRequest(
                experiment_id=uuid.uuid4(),
                population=PopulationSummary(
                    ate_bps=0,
                    ci_low_bps=0,
                    ci_high_bps=0,
                    n_treatment=1,
                    n_holdout=1,
                    feature_vocabulary=FEATURE_VOCABULARY,
                ),
                cells=(),
            )
