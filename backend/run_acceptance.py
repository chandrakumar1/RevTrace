"""Acceptance run — 10,000 cases, full bootstrap, against revtrace_test.

Deliberate entry point. Materialises the seed-42 population, evaluates it with
the uplift model fitted, and writes `docs/EVALUATION.md` and
`docs/evaluation.json`. Rolls the transaction back afterwards: the benchmark is
reproducible from the seed, so there is no reason to leave 70,000 rows behind in
the test database.

**Uplift is not optional here.** `build_report` omits the model by default, and
a report without it renders a strictly smaller document — no quadrants, no Qini,
no confusion matrix, no uplift limitations. Writing that over the artifact would
delete sections while looking like a refresh, so the run requests uplift and
then refuses to write unless it is actually present.

Run from `backend/`:

    TEST_DATABASE_URL=postgresql+psycopg://localhost:5432/revtrace_test \\
        .venv/bin/python run_acceptance.py
"""

from __future__ import annotations

import os
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.benchmark.gate_benchmark import run_gate_comparison
from tests.benchmark.gate_benchmark import summarise as summarise_gate
from tests.benchmark.report import (
    ACCEPTANCE_CASE_COUNT,
    BenchmarkOutcome,
    run_benchmark,
    summarise,
    write_evaluation,
)
from tests.conftest_db import resolve_test_dsn

#: The database this must never touch, whatever the environment says.
FORBIDDEN_DATABASE = "revtrace_dev"


def resolve_dsn() -> str:
    """The test DSN, or a message explaining exactly what to set.

    `resolve_test_dsn` falls back to a hard-coded personal DSN when the variable
    is unset, which is right for the test suite on this machine and wrong for a
    deliberate run on any other. Requiring it explicitly turns a confusing
    connection failure into an instruction.
    """
    if not os.environ.get("TEST_DATABASE_URL"):
        raise SystemExit(
            "TEST_DATABASE_URL is not set.\n\n"
            "This run writes the evaluation artifact and must name its database "
            "explicitly rather than inherit a fallback. Create and migrate the "
            "test database, then name it:\n\n"
            "    createdb revtrace_test\n"
            "    DATABASE_URL=postgresql+psycopg://localhost:5432/revtrace_test \\\n"
            "        .venv/bin/alembic upgrade head\n"
            "    TEST_DATABASE_URL=postgresql+psycopg://localhost:5432/revtrace_test \\\n"
            "        .venv/bin/python run_acceptance.py\n\n"
            f"The database name must mark itself as a test database, and {FORBIDDEN_DATABASE} "
            "is never acceptable."
        )
    # Keeps the marker guard: a DSN naming a non-test database is refused here.
    return resolve_test_dsn()


def guard_uplift(outcome: BenchmarkOutcome) -> None:
    """Refuse to write an artifact that would be missing its uplift half."""
    if outcome.report.uplift is None:
        raise RuntimeError(
            "refusing to write the evaluation artifact: the report carries no "
            "uplift model, so writing it would delete the quadrant, Qini, "
            "confusion-matrix and uplift-limitation sections rather than "
            "refresh them. This means `include_uplift=True` did not reach "
            "`build_report`."
        )


def main() -> None:
    engine = create_engine(resolve_dsn(), future=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    try:
        name = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"database: {name}")
        if name == FORBIDDEN_DATABASE:
            raise RuntimeError(f"refusing to run against {FORBIDDEN_DATABASE}")

        started = time.perf_counter()
        outcome = run_benchmark(session, case_count=ACCEPTANCE_CASE_COUNT, include_uplift=True)
        elapsed = time.perf_counter() - started

        print()
        print(summarise(outcome))
        print()
        print(f"elapsed: {elapsed:.1f}s")

        guard_uplift(outcome)

        # Gate-off vs gate-on, over the population already materialised above.
        # Reads only, so it runs inside the same transaction and cannot move the
        # estimate that was just measured. Deliberately not written into the
        # evaluation artifact: this is a spend comparison, not a causal result.
        gate_started = time.perf_counter()
        comparison = run_gate_comparison(
            session,
            outcome.run.experiment_id,
            report=outcome.report,
        )
        print()
        print(summarise_gate(comparison))
        print()
        print(f"gate comparison elapsed: {time.perf_counter() - gate_started:.1f}s")

        path = write_evaluation(outcome)
        print(f"written: {path}")
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
        print("rolled back; revtrace_test left as it was")


if __name__ == "__main__":
    main()
