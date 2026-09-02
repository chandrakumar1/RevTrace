"""The materialisation script's guards, tested without materialising anything.

This is the only script in the project that deliberately persists synthetic
data, so the tests that matter are the ones proving it refuses: a wrong
database, a missing environment variable, a missing `--commit`.

**Nothing here commits.** The two tests that touch PostgreSQL use a handful of
cases and assert the transaction was rolled back by counting rows afterwards on
a *separate* connection — checking on the same one would see the uncommitted
rows and pass for the wrong reason.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text

import run_materialise
from run_materialise import (
    DEFAULT_CASE_COUNT,
    DEFAULT_SEED,
    FORBIDDEN_DATABASE,
    WATCHED_TABLES,
    MaterialiseRefused,
    canonical_experiment_id,
    database_name,
    execute,
    guard_database_name,
    resolve_dsn,
)
from tests.benchmark.bridge import BridgeError, benchmark_experiment_id

#: The population the operator run will create. Asserted rather than recomputed,
#: so a change to the derivation fails here instead of silently pointing a later
#: run at a different experiment.
CANONICAL_EXPERIMENT_ID = uuid.UUID("2b3e9c9f-60e8-5413-adc2-456b89e017b1")

#: Small enough that the two database tests stay in the fast suite. A seed that
#: nothing else in the suite uses, so a collision here means a real bug.
TINY_CASES = 6
TINY_SEED = 8_617


# -- the database guard ----------------------------------------------------


class TestTheDatabaseGuard:
    @pytest.mark.parametrize(
        "name", ["revtrace_test", "revtrace_test_2", "scratch_test", "ci_test_db"]
    )
    def test_a_test_database_is_accepted(self, name: str) -> None:
        assert guard_database_name(name) == name

    def test_revtrace_dev_is_refused_by_name(self) -> None:
        """Named explicitly, so the failure says which database was refused."""
        with pytest.raises(MaterialiseRefused, match="refusing to materialise into revtrace_dev"):
            guard_database_name(FORBIDDEN_DATABASE)

    @pytest.mark.parametrize(
        "name", ["revtrace", "revtrace_dev", "postgres", "production", "revtrace_staging"]
    )
    def test_anything_unmarked_is_refused(self, name: str) -> None:
        with pytest.raises(MaterialiseRefused):
            guard_database_name(name)

    def test_the_dev_refusal_is_not_reachable_by_a_marker(self) -> None:
        """`revtrace_dev` contains neither marker, so both guards agree.

        Asserted rather than assumed: if a marker were ever loosened to
        something `revtrace_dev` matched, the named guard would be the only
        thing left, and this test says so.
        """
        from tests.benchmark.bridge import PERMITTED_DATABASE_MARKERS

        assert not any(marker in FORBIDDEN_DATABASE for marker in PERMITTED_DATABASE_MARKERS)

    def test_the_name_is_read_without_its_credentials(self) -> None:
        dsn = "postgresql+psycopg://someone:hunter2@localhost:5432/revtrace_test?sslmode=disable"
        assert database_name(dsn) == "revtrace_test"


class TestTheEnvironmentIsRequired:
    def test_a_missing_variable_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No fall back to DATABASE_URL: that one points at the dev database."""
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        with pytest.raises(MaterialiseRefused, match="TEST_DATABASE_URL is not set"):
            resolve_dsn()

    def test_it_never_reads_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/revtrace_test")
        with pytest.raises(MaterialiseRefused, match="TEST_DATABASE_URL is not set"):
            resolve_dsn()

    def test_a_dev_dsn_is_refused_before_any_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "TEST_DATABASE_URL", f"postgresql+psycopg://localhost:5432/{FORBIDDEN_DATABASE}"
        )
        with pytest.raises(MaterialiseRefused, match=FORBIDDEN_DATABASE):
            resolve_dsn()

    def test_a_test_dsn_is_returned_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dsn = "postgresql+psycopg://localhost:5432/revtrace_test"
        monkeypatch.setenv("TEST_DATABASE_URL", dsn)
        assert resolve_dsn() == dsn


# -- the canonical identity ------------------------------------------------


class TestTheCanonicalIdentity:
    def test_the_experiment_id_is_the_documented_one(self) -> None:
        assert canonical_experiment_id(42, 10_000) == CANONICAL_EXPERIMENT_ID

    def test_it_is_derived_by_the_bridge_rather_than_written_here(self) -> None:
        assert canonical_experiment_id(42, 10_000) == benchmark_experiment_id(42, 10_000)

    def test_the_defaults_are_the_accepted_population(self) -> None:
        assert (DEFAULT_SEED, DEFAULT_CASE_COUNT) == (42, 10_000)
        assert canonical_experiment_id(DEFAULT_SEED, DEFAULT_CASE_COUNT) == CANONICAL_EXPERIMENT_ID

    @pytest.mark.parametrize(("seed", "cases"), [(42, 9_999), (43, 10_000), (1, 1)])
    def test_a_different_input_is_a_different_experiment(self, seed: int, cases: int) -> None:
        assert canonical_experiment_id(seed, cases) != CANONICAL_EXPERIMENT_ID

    def test_it_is_stable_across_calls(self) -> None:
        assert canonical_experiment_id(42, 10_000) == canonical_experiment_id(42, 10_000)


# -- commit is opt-in, and failure rolls back ------------------------------


def persisted_counts(engine: Engine) -> dict[str, int]:
    """Counts on a *fresh* connection, so uncommitted rows are invisible."""
    with engine.connect() as probe:
        return {
            table: probe.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in WATCHED_TABLES
        }


@pytest.mark.db
class TestCommitIsExplicit:
    def test_without_commit_nothing_survives(self, db_engine: Engine) -> None:
        """The default. Rows are written, counted, and rolled back."""
        before = persisted_counts(db_engine)

        with db_engine.connect() as connection:
            run, inner_before, inner_after = execute(
                connection, seed=TINY_SEED, case_count=TINY_CASES, commit=False
            )

        # Inside the transaction the rows existed — otherwise this test would
        # pass against a script that silently materialised nothing.
        assert run.enrolled == TINY_CASES
        assert inner_after["revenue_risks"] - inner_before["revenue_risks"] == TINY_CASES
        assert inner_after["case_outcomes"] - inner_before["case_outcomes"] == TINY_CASES

        assert persisted_counts(db_engine) == before

    def test_an_exception_rolls_back(self, db_engine: Engine) -> None:
        """A real failure path: the bridge refuses a seed already present.

        Materialising the same seed twice inside one transaction trips the
        bridge's own collision guard, which is the closest thing to a genuine
        mid-run error without reaching into the bridge to break it.
        """
        before = persisted_counts(db_engine)

        with db_engine.connect() as connection:
            transaction = connection.begin()
            from sqlalchemy.orm import sessionmaker

            session = sessionmaker(bind=connection, expire_on_commit=False)()
            from tests.benchmark.bridge import materialise

            materialise(session, seed=TINY_SEED + 1, case_count=TINY_CASES)
            session.flush()
            with pytest.raises(BridgeError, match="already materialised"):
                materialise(session, seed=TINY_SEED + 1, case_count=TINY_CASES)
            session.close()
            transaction.rollback()

        assert persisted_counts(db_engine) == before

    def test_execute_rolls_back_and_reraises_after_a_successful_write(
        self, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`execute` owns the transaction, so its failure path is its own.

        The failure is injected *after* a real materialisation, not instead of
        one. A test that raised before any row was written would prove only that
        an empty transaction rolls back cleanly, which is not the property that
        matters — the risk is a run that half-succeeds and keeps what it wrote.
        """
        real = run_materialise.materialise
        marker = RuntimeError("injected after the population was written")

        def materialise_then_fail(session, **kwargs):  # noqa: ANN001, ANN202
            real(session, **kwargs)
            session.flush()
            raise marker

        monkeypatch.setattr(run_materialise, "materialise", materialise_then_fail)
        before = persisted_counts(db_engine)

        with db_engine.connect() as connection:
            with pytest.raises(RuntimeError) as caught:
                execute(connection, seed=TINY_SEED + 2, case_count=TINY_CASES, commit=False)
            assert caught.value is marker

        assert persisted_counts(db_engine) == before

    def test_a_failure_rolls_back_even_when_commit_was_requested(
        self, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--commit` is permission to keep a *successful* run, not any run."""
        real = run_materialise.materialise

        def materialise_then_fail(session, **kwargs):  # noqa: ANN001, ANN202
            real(session, **kwargs)
            session.flush()
            raise RuntimeError("injected after the population was written")

        monkeypatch.setattr(run_materialise, "materialise", materialise_then_fail)
        before = persisted_counts(db_engine)

        with db_engine.connect() as connection:
            with pytest.raises(RuntimeError):
                execute(connection, seed=TINY_SEED + 3, case_count=TINY_CASES, commit=True)

        assert persisted_counts(db_engine) == before

    def test_the_live_database_is_checked_after_connecting(self, db_engine: Engine) -> None:
        """The DSN is a claim; `current_database()` is the authority."""
        with db_engine.connect() as connection:
            live = connection.execute(text("SELECT current_database()")).scalar_one()
        assert guard_database_name(live) == live
        assert live != FORBIDDEN_DATABASE


# -- the script writes no rows of its own ----------------------------------


class TestItDelegatesToTheBridge:
    def test_it_hand_writes_no_row(self) -> None:
        """No model is constructed here; `materialise` is the only writer.

        Hand-written rows would bypass `assign_risk`, `open_window` and
        `seal_due`, which is the ordering that keeps the arm independent of the
        outcome.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("run_materialise.py").read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "materialise" in called
        for model in ("RevenueRisk", "CaseAssignment", "CaseOutcome", "Order", "Customer"):
            assert model not in called, f"{model} is constructed directly"

    def test_it_adds_nothing_to_a_session(self) -> None:
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("run_materialise.py").read_text(encoding="utf-8"))
        attrs = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "add" not in attrs
        assert "add_all" not in attrs
        assert "bulk_save_objects" not in attrs

    def test_the_watched_tables_cover_what_the_bridge_writes(self) -> None:
        for table in (
            "merchants",
            "experiments",
            "customers",
            "orders",
            "payment_attempts",
            "revenue_risks",
            "case_assignments",
            "case_outcomes",
        ):
            assert table in WATCHED_TABLES

    def test_the_salt_is_the_benchmark_one(self) -> None:
        """A different salt would re-randomise every arm."""
        from tests.benchmark.bridge import BENCHMARK_SALT

        assert run_materialise.BENCHMARK_SALT == BENCHMARK_SALT
