"""Day 4 acceptance run — 10,000 cases, full bootstrap, against revtrace_test.

Deliberate entry point. Materialises the seed-42 population, evaluates it, and
writes docs/EVALUATION.md. Rolls the transaction back afterwards: the benchmark
is reproducible from the seed, so there is no reason to leave 70,000 rows behind
in the test database.
"""

from __future__ import annotations

import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.benchmark.report import (
    ACCEPTANCE_CASE_COUNT,
    run_benchmark,
    summarise,
    write_evaluation,
)
from tests.conftest_db import resolve_test_dsn

engine = create_engine(resolve_test_dsn(), future=True)
connection = engine.connect()
transaction = connection.begin()
session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

name = session.execute(text("SELECT current_database()")).scalar_one()
print(f"database: {name}")
assert name != "revtrace_dev"

started = time.perf_counter()
outcome = run_benchmark(session, case_count=ACCEPTANCE_CASE_COUNT)
elapsed = time.perf_counter() - started

print()
print(summarise(outcome))
print()
print(f"elapsed: {elapsed:.1f}s")

path = write_evaluation(outcome)
print(f"written: {path}")

session.close()
transaction.rollback()
connection.close()
print("rolled back; revtrace_test left as it was")
