"""Materialise the canonical benchmark population into revtrace_test.

Every other caller of `materialise` rolls back — `run_acceptance.py` says so in
its own docstring, and that is why `revtrace_test` is empty. This script exists
because `run_hypothesis.py` opens its own session in its own process and must
read an experiment it did not create, which needs the population to survive a
commit.

That makes this **the only script in the project that deliberately persists
synthetic data**, so it is built to refuse rather than to proceed:

* `TEST_DATABASE_URL` is required; there is no fall back to `DATABASE_URL`.
* The resolved database name must be `revtrace_test` or carry `_test`, and the
  literal name `revtrace_dev` is refused by name before a connection is opened.
* `SELECT current_database()` is checked again *after* connecting, because a
  DSN is a claim about where it points and the server is the authority.
* `bridge.guard_test_database` checks a third time, inside `materialise`.
* Without `--commit` the transaction is rolled back however well it went.
* Any exception rolls back and re-raises.

**Nothing here writes a row itself.** `tests.benchmark.bridge.materialise` is
the canonical path — it drives `assign_risk`, `open_window` and `seal_due` in
the order a real experiment runs, so the arm cannot depend on the outcome.
Hand-writing rows would bypass that ordering and require inventing the ground
truth the generator produces from planted parameters.

Usage::

    TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/revtrace_test \\
        python run_materialise.py [--seed 42] [--cases 10000] [--commit]

**`case_outcomes` carries `truth_*` ground truth.** Committing means simulator
truth sits in a database for the first time. No production reader can reach it —
`app/reporting/evaluation.py` is the only permitted reader, and the hypothesis
loader's contract has no field for one — but it is a real consequence of running
this with `--commit` and is stated here rather than discovered later.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.benchmark.bridge import (
    BENCHMARK_SALT,
    PERMITTED_DATABASE_MARKERS,
    BenchmarkRun,
    benchmark_experiment_id,
    benchmark_experiment_name,
    benchmark_merchant_id,
    materialise,
)

#: Never, under any flag.
FORBIDDEN_DATABASE = "revtrace_dev"

DEFAULT_SEED = 42
DEFAULT_CASE_COUNT = 10_000

#: Counted before and after so the report states what changed rather than
#: asserting it. Ordered as the bridge writes them.
WATCHED_TABLES = (
    "merchants",
    "experiments",
    "customers",
    "orders",
    "payment_attempts",
    "revenue_risks",
    "case_assignments",
    "case_outcomes",
    "uplift_scores",
    "experiment_results",
    "recovery_cases",
    "audit_events",
)


class MaterialiseRefused(SystemExit):
    """A guard said no. Nothing was written."""


def database_name(dsn: str) -> str:
    """The database a DSN names, without its credentials."""
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def guard_database_name(name: str) -> str:
    """Refuse anything that is not a test database. Returns the name.

    Two checks rather than one: the marker test is the same rule the bridge
    applies, and the explicit `revtrace_dev` refusal is a named guard so the
    failure says which database was refused rather than which pattern missed.
    """
    if name == FORBIDDEN_DATABASE:
        raise MaterialiseRefused(
            f"refusing to materialise into {FORBIDDEN_DATABASE}: it must never "
            "hold synthetic benchmark data"
        )
    if not any(marker in name for marker in PERMITTED_DATABASE_MARKERS):
        raise MaterialiseRefused(
            f"refusing to materialise into {name!r}: the database name must "
            f"contain one of {PERMITTED_DATABASE_MARKERS}"
        )
    return name


def resolve_dsn() -> str:
    """The DSN this run will use. Requires an explicit choice."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise MaterialiseRefused(
            "TEST_DATABASE_URL is not set. This script will not fall back to "
            "DATABASE_URL: a script that persists rows must be told where, and "
            "DATABASE_URL points at the development database."
        )
    guard_database_name(database_name(dsn))
    return dsn


def canonical_experiment_id(seed: int, case_count: int) -> uuid.UUID:
    """The derived identity of this population.

    Derived rather than chosen, by the same function the bridge uses, so the id
    a later run reads is a function of the inputs and not of what someone typed.
    """
    return benchmark_experiment_id(seed, case_count)


def row_counts(connection: Connection) -> dict[str, int]:
    return {
        table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in WATCHED_TABLES
    }


def execute(
    connection: Connection,
    *,
    seed: int,
    case_count: int,
    commit: bool,
) -> tuple[BenchmarkRun, dict[str, int], dict[str, int]]:
    """Materialise inside one transaction this function owns.

    Returns the run and the before/after counts. Rolls back and re-raises on any
    exception, and rolls back on success too unless `commit` was asked for.
    """
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()
    try:
        # The server is the authority on where this connection points; the DSN
        # is only a claim about it.
        live = session.execute(text("SELECT current_database()")).scalar_one()
        guard_database_name(live)

        before = row_counts(connection)
        run = materialise(session, seed=seed, case_count=case_count, salt=BENCHMARK_SALT)
        session.flush()
        after = row_counts(connection)

        if commit:
            transaction.commit()
        else:
            transaction.rollback()
        return run, before, after
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialise the canonical benchmark population.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cases", type=int, default=DEFAULT_CASE_COUNT)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="persist the population; without it the transaction is rolled back",
    )
    args = parser.parse_args()

    dsn = resolve_dsn()
    experiment_id = canonical_experiment_id(args.seed, args.cases)

    print(f"database   : {database_name(dsn)}")
    print(f"seed/cases : {args.seed} / {args.cases:,}")
    print(f"experiment : {experiment_id}")
    print(f"name       : {benchmark_experiment_name(args.seed, args.cases)}")
    print(f"merchant   : {benchmark_merchant_id(args.seed, args.cases)}")
    print(f"mode       : {'COMMIT' if args.commit else 'dry run (will roll back)'}")
    print()

    engine = create_engine(dsn, future=True)
    connection = engine.connect()
    try:
        run, before, after = execute(
            connection, seed=args.seed, case_count=args.cases, commit=args.commit
        )
    except Exception:
        print("rolled back: the run failed, nothing persisted", file=sys.stderr)
        raise
    finally:
        connection.close()
        engine.dispose()

    print(f"{'table':<22}{'before':>10}{'after':>10}{'written':>10}")
    for table in WATCHED_TABLES:
        written = after[table] - before[table]
        print(f"{table:<22}{before[table]:>10,}{after[table]:>10,}{written:>10,}")
    print()
    print(f"enrolled   : {run.enrolled:,}  ({run.treatment:,} treatment / {run.holdout:,} holdout)")
    print(f"windows    : {run.windows_opened:,} opened, {run.outcomes_sealed:,} sealed")
    print(f"captures   : {run.captures_planted:,}")
    print()
    if args.commit:
        print(f"COMMITTED to {run.database}. run_hypothesis.py can now read {run.experiment_id}.")
    else:
        print("rolled back: nothing persisted. Pass --commit to keep it.")


if __name__ == "__main__":
    main()
