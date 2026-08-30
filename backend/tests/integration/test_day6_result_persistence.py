"""The experiment-result writer, against the real database.

Two properties carry this file. Nothing may be fabricated — the four columns
with no honest source must arrive as NULL, and a Qini coefficient of `None` must
never become a zero. And nothing may be recomputed — every stored figure has to
equal what the Day 5 report already calculated, byte for byte, because a second
implementation in the repository would be unverified by construction.

Runs on revtrace_test inside the rolled-back transaction fixture. No row
survives a test, and revtrace_dev is never involved.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.causal.estimators import Interval
from app.models import ExperimentResult, RecoveryAction, RecoveryCase, UpliftScore
from app.reporting.evaluation import build_report
from app.repositories import experiment_result_repository as repo
from app.repositories.experiment_result_repository import (
    ExperimentResultPersistenceError,
    ResultValues,
    persist_result,
)
from tests.benchmark.bridge import materialise

pytestmark = pytest.mark.db

#: Small enough for the fast suite; the writer does not care about size.
SIZE = 300

#: Far below the pre-registered 10,000. Persistence is indifferent to it.
FAST_RESAMPLES = 40

COMPUTED_AT = datetime(2026, 9, 3, 13, 0, tzinfo=UTC)

REPO_SOURCE = pathlib.Path(repo.__file__)


@pytest.fixture
def run(db_session: Session):  # noqa: ANN201
    return materialise(db_session, case_count=SIZE)


@pytest.fixture
def report(db_session: Session, run):  # noqa: ANN001, ANN201
    return build_report(db_session, run.experiment_id, resamples=FAST_RESAMPLES)


def stored(session: Session) -> list[ExperimentResult]:
    return list(session.execute(select(ExperimentResult)).scalars())


def values_of(report) -> ResultValues:  # noqa: ANN001
    """Map a Day 5 report onto the repository's narrow input.

    This mapping lives on the test side because a Phase 3 isolation guard
    forbids any module under `app/` from importing `app.reporting.evaluation`.
    That is why `persist_result` takes fifteen scalars rather than the report
    itself — see `ResultValues`. When a production caller exists it will need
    this same mapping, and it will need somewhere outside `app/` to live, or the
    guard revisited.

    `qini_coefficient_bps` is `None` both when no uplift model was fitted and
    when the fitted model's Q(N) was zero. Neither is a zero coefficient.
    """
    qini = None if report.uplift is None else report.uplift.qini.coefficient_bps
    return ResultValues(
        experiment_id=report.experiment_id,
        n_treat=report.recovery.n_treatment,
        n_control=report.recovery.n_holdout,
        rate_treat_bps=report.recovery.rate_treatment_bps,
        rate_control_bps=report.recovery.rate_holdout_bps,
        ate_bps=report.recovery.ate_bps,
        ate_ci_low_bps=report.recovery.interval.low,
        ate_ci_high_bps=report.recovery.interval.high,
        p_value_micros=report.recovery.p_value_micros,
        gross_recovered=report.ledger.gross_recovered,
        incremental_recovered=report.ledger.incremental_recovered,
        credited_not_earned=report.ledger.credited_not_earned,
        harm_ate_bps=report.harm.ate_bps,
        qini_coefficient_bps=qini,
        is_underpowered=report.is_underpowered,
    )


# -- the honest happy path ------------------------------------------------


class TestSuccessfulPersistence:
    def test_a_complete_result_is_stored(self, db_session: Session, run, report) -> None:  # noqa: ANN001
        row = persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)

        assert row.id is not None
        assert len(stored(db_session)) == 1

    def test_every_stored_value_equals_the_report(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """Nothing is recomputed. Each column is the report's own number."""
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        row = stored(db_session)[0]

        assert row.experiment_id == run.experiment_id
        assert row.computed_at == COMPUTED_AT
        assert row.n_treat == report.recovery.n_treatment
        assert row.n_control == report.recovery.n_holdout
        assert row.rate_treat_bps == report.recovery.rate_treatment_bps
        assert row.rate_control_bps == report.recovery.rate_holdout_bps
        assert row.ate_bps == report.recovery.ate_bps
        assert row.ate_ci_low_bps == report.recovery.interval.low
        assert row.ate_ci_high_bps == report.recovery.interval.high
        assert row.p_value_micros == report.recovery.p_value_micros
        assert row.gross_recovered == report.ledger.gross_recovered
        assert row.incremental_recovered == report.ledger.incremental_recovered
        assert row.credited_not_earned == report.ledger.credited_not_earned
        assert row.harm_ate_bps == report.harm.ate_bps
        assert row.is_underpowered == report.is_underpowered

    def test_the_ledger_identity_is_stored_not_derived(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """gross - incremental = credited-not-earned, as the estimator computed it.

        Checked as a property of the stored row, not recomputed by the writer —
        if the repository were deriving any of the three, a disagreement here
        would be silent.
        """
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        row = stored(db_session)[0]
        assert row.gross_recovered - row.incremental_recovered == row.credited_not_earned


class TestNullsAreNotZeroes:
    def test_the_three_economic_columns_are_null(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """Not zero. Zero would assert a measurement nobody made."""
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        row = stored(db_session)[0]

        assert row.harm_cost is None
        assert row.action_cost is None
        assert row.net_incremental_value is None

    def test_they_are_null_in_the_database_too(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """Read back through SQL, so an ORM default cannot mask a stored zero."""
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        row = db_session.execute(
            text(
                "SELECT harm_cost, action_cost, net_incremental_value "
                "FROM experiment_results WHERE experiment_id = :e"
            ),
            {"e": run.experiment_id},
        ).one()
        assert row == (None, None, None)

    ECONOMIC = {"harm_cost", "action_cost", "net_incremental_value"}

    def test_the_writer_offers_no_way_to_set_them(self) -> None:
        """Structural: they are not parameters, so no caller can pass a zero."""
        tree = ast.parse(REPO_SOURCE.read_text(encoding="utf-8"))
        signature = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "persist_result"
        )
        names = {a.arg for a in signature.args.args} | {a.arg for a in signature.args.kwonlyargs}
        assert names & self.ECONOMIC == set()

    def test_the_input_type_has_no_field_for_them(self) -> None:
        """The stronger version: no field exists to carry a fabricated value."""
        fields = {field.name for field in dataclasses.fields(ResultValues)}
        assert fields & self.ECONOMIC == set()
        assert len(fields) == 15

    def test_a_defined_qini_is_stored_as_its_value(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        full = build_report(
            db_session, run.experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )
        assert full.uplift is not None
        persist_result(db_session, run.experiment_id, values_of(full), as_of=COMPUTED_AT)
        row = stored(db_session)[0]
        assert row.qini_coefficient_bps == full.uplift.qini.coefficient_bps

    def test_an_undefined_qini_is_stored_as_null(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """`None` survives to the column. It is never coerced to 0."""
        full = build_report(
            db_session, run.experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )
        assert full.uplift is not None
        undefined = replace(
            full,
            uplift=replace(
                full.uplift,
                qini=replace(full.uplift.qini, coefficient_bps=None),
            ),
        )

        persist_result(db_session, run.experiment_id, values_of(undefined), as_of=COMPUTED_AT)
        row = stored(db_session)[0]
        assert row.qini_coefficient_bps is None

    def test_a_report_without_an_uplift_model_stores_a_null_qini(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        assert report.uplift is None
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        assert stored(db_session)[0].qini_coefficient_bps is None


# -- refusals -------------------------------------------------------------


class TestValidationRefusesBeforeAdd:
    def _bad_recovery(self, report, **fields):  # noqa: ANN001, ANN202
        return replace(report, recovery=replace(report.recovery, **fields))

    def test_an_ate_outside_its_interval_is_refused(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        bad = self._bad_recovery(
            report, interval=Interval(low=9_000, high=9_500, alpha_bps=500, resamples=40, seed=1)
        )
        with pytest.raises(ExperimentResultPersistenceError, match="outside its interval"):
            persist_result(db_session, run.experiment_id, values_of(bad), as_of=COMPUTED_AT)
        assert stored(db_session) == []

    def test_a_rate_outside_the_scale_is_refused(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        bad = self._bad_recovery(report, rate_treatment_bps=10_001)
        with pytest.raises(ExperimentResultPersistenceError, match="rate_treat_bps"):
            persist_result(db_session, run.experiment_id, values_of(bad), as_of=COMPUTED_AT)
        assert stored(db_session) == []

    def test_a_p_value_out_of_range_is_refused(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        bad = self._bad_recovery(report, p_value_micros=1_000_001)
        with pytest.raises(ExperimentResultPersistenceError, match="p_value_micros"):
            persist_result(db_session, run.experiment_id, values_of(bad), as_of=COMPUTED_AT)

    def test_a_negative_gross_is_refused(self, db_session: Session, run, report) -> None:  # noqa: ANN001
        bad = replace(report, ledger=replace(report.ledger, gross_recovered=-1))
        with pytest.raises(ExperimentResultPersistenceError, match="gross_recovered"):
            persist_result(db_session, run.experiment_id, values_of(bad), as_of=COMPUTED_AT)

    def test_a_naive_as_of_is_refused(self, db_session: Session, run, report) -> None:  # noqa: ANN001
        with pytest.raises(ExperimentResultPersistenceError, match="timezone-aware"):
            persist_result(
                db_session, run.experiment_id, values_of(report), as_of=datetime(2026, 9, 3, 13, 0)
            )
        assert stored(db_session) == []

    def test_an_unknown_experiment_is_refused(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        stranger = uuid.uuid4()
        mismatched = replace(report, experiment_id=stranger)
        with pytest.raises(ExperimentResultPersistenceError, match="no experiment"):
            persist_result(db_session, stranger, values_of(mismatched), as_of=COMPUTED_AT)

    def test_a_report_for_a_different_experiment_is_refused(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """A measurement attributed to the wrong design is worse than none."""
        elsewhere = replace(report, experiment_id=uuid.uuid4())
        with pytest.raises(ExperimentResultPersistenceError, match="not"):
            persist_result(db_session, run.experiment_id, values_of(elsewhere), as_of=COMPUTED_AT)
        assert stored(db_session) == []

    def test_a_refusal_leaves_the_session_usable(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """Refusal happens before any `session.add`, so no rollback is needed."""
        bad = self._bad_recovery(report, rate_holdout_bps=-5)
        with pytest.raises(ExperimentResultPersistenceError):
            persist_result(db_session, run.experiment_id, values_of(bad), as_of=COMPUTED_AT)

        row = persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        assert row.id is not None

    def test_every_problem_is_reported_not_just_the_first(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        bad = self._bad_recovery(report, rate_treatment_bps=10_001, p_value_micros=-1)
        with pytest.raises(ExperimentResultPersistenceError) as raised:
            persist_result(db_session, run.experiment_id, values_of(bad), as_of=COMPUTED_AT)
        assert "rate_treat_bps" in str(raised.value)
        assert "p_value_micros" in str(raised.value)


class TestAppendOnly:
    def test_a_second_computation_appends(self, db_session: Session, run, report) -> None:  # noqa: ANN001
        """One row per computation. An interim and a final reading both survive."""
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        later = COMPUTED_AT.replace(hour=18)
        persist_result(db_session, run.experiment_id, values_of(report), as_of=later)

        rows = stored(db_session)
        assert len(rows) == 2
        assert {r.computed_at for r in rows} == {COMPUTED_AT, later}

    def test_the_same_instant_twice_is_refused(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        """Two rows at one instant would be indistinguishable."""
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        with pytest.raises(ExperimentResultPersistenceError, match="already has"):
            persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        assert len(stored(db_session)) == 1

    def test_the_row_has_no_updated_at(self, db_session: Session) -> None:
        """Append-only in the schema, not merely by convention."""
        columns = {c.name for c in ExperimentResult.__table__.columns}
        assert "updated_at" not in columns


# -- authority ------------------------------------------------------------


class TestWritesOnlyExperimentResults:
    OTHER = ("uplift_scores", "recovery_cases", "recovery_actions", "audit_events")

    def test_no_other_table_gains_a_row(self, db_session: Session, run, report) -> None:  # noqa: ANN001
        before = {
            t: db_session.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in self.OTHER
        }
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        after = {
            t: db_session.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in self.OTHER
        }
        assert before == after

    def test_the_models_it_writes_are_only_experiment_results(  # noqa: ANN201
        self, db_session: Session, run, report
    ):
        persist_result(db_session, run.experiment_id, values_of(report), as_of=COMPUTED_AT)
        db_session.flush()
        for model in (UpliftScore, RecoveryCase, RecoveryAction):
            assert db_session.execute(select(func.count()).select_from(model)).scalar() == 0

    def test_the_writer_never_commits(self) -> None:
        """Transaction ownership stays with the caller."""
        tree = ast.parse(REPO_SOURCE.read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "commit" not in called
        assert "rollback" not in called
        assert {"add", "flush"} <= called

    def test_the_writer_never_reads_a_clock(self) -> None:
        """`computed_at` may only come from the injected `as_of`."""
        tree = ast.parse(REPO_SOURCE.read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called & {"now", "utcnow", "today"} == set()

    def test_the_writer_imports_no_simulator_or_tests(self) -> None:
        tree = ast.parse(REPO_SOURCE.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(m.startswith(("simulator", "tests")) for m in modules)
