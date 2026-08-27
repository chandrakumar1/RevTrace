# RevTrace Backend

Python 3.13 · FastAPI · SQLAlchemy · Pydantic · PostgreSQL 16 · Alembic

**Status: Phase 3 complete.** Ingestion, timeline reconstruction, the
deterministic risk engine, four detectors, risk resolution, persistence, and six
API endpoints. No AI, no Razorpay integration, no recovery execution.

## Python version — read this first

This machine's default `python3` is **3.14.3**. The backend targets **3.13**
(pinned in `.python-version` and `requires-python`). Creating the venv with a
bare `python3` will produce a 3.14 environment and likely fail on dependency
wheels.

Always be explicit:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -V          # must print 3.13.x
```

## Setup

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ../simulator      # editable; zero third-party dependencies

createdb revtrace_dev
createdb revtrace_test
.venv/bin/alembic upgrade head   # applies to DATABASE_URL

.venv/bin/uvicorn app.main:app --reload
```

Environment variables come from the repository-root `.env`, created from
`.env.example`. `.env` is gitignored and must never be committed.

### `TEST_DATABASE_URL`

Optional. Overrides the DSN used by the integration and API tests. It is
**deliberately not in `.env.example`** — it is a test-harness setting, not an
application setting, and `app/core/config.py` does not read it.

```bash
TEST_DATABASE_URL=postgresql+psycopg://sancha@localhost:5432/revtrace_test
```

That is also the default, so it normally needs no setting at all. A guard in
`tests/conftest_db.py` refuses any DSN whose database name does not contain
`revtrace_test` or `_test`, so a stray value cannot point the suite at
development data.

## Layout

```
app/
├── main.py           FastAPI entrypoint
├── api/
│   ├── router.py     /api/v1 for features; /health unversioned at the root
│   └── routes/       health, ingestion, detection, risks, timeline
├── core/             config.py, logging.py, security.py, money.py
├── models/           SQLAlchemy ORM models (9 entities)
├── schemas/          Pydantic request/response; common.py holds UtcDatetime
├── repositories/     entity, event, risk — keeps queries out of services
├── services/
│   ├── ingestion/    Event intake, reference validation, idempotency
│   ├── detection/    Run orchestration: the one engine/database seam
│   ├── tracing/      state.py + reconstruction.py — timelines
│   ├── recovery/     empty until Phase 6
│   ├── verification/ empty until Phase 9
│   └── audit/        empty until Phase 9
├── engine/           Deterministic, pure
│   ├── risk_engine.py    money, in integer minor units
│   ├── scoring.py        confidence_bps (synthetic heuristic)
│   ├── resolution.py     reconcile findings against stored risks
│   └── detectors/        four pure detect(timeline, as_of, config)
├── agents/           empty until Phase 5 — advisory only, never executes
├── integrations/     empty until Phase 8
└── db/               Engine, session, base
alembic/              Migrations: 343997466c88 (Phase 1), bbc0a1ffda2c (Phase 3)
tests/
├── integration/      the only tests that touch PostgreSQL
├── evaluation/       TP/FP/FN harness + 17 regression fixtures
└── ...               everything else is hermetic
```

## Boundaries that must hold

**`agents/` never executes anything.** Agents return structured
recommendations. `engine/` validates and applies policy; `services/` execute.
An agent module importing a Razorpay client is a bug.

**Razorpay code stays in `integrations/razorpay/`.** The rest of the
application depends on our own service interfaces, never on Razorpay's
request/response shapes. `mapper.py` is the only place provider payloads become
domain objects.

**`engine/` is deterministic and testable.** No LLM calls, no network, no
clock-dependence that tests can't control. Every scoring formula is documented
and unit-tested.

**The simulator is a consumer, never a dependency.** `simulator/` may import
`app.models.enums` and `app.core.money`; `app/` must never import `simulator`.
A test enforces the direction. Simulated data reaches the database only through
the ingestion layer, never by direct write.

**Detection never authorises.** It writes to `revenue_risks` and nothing else.
`recovery_cases`, `recovery_actions`, and `audit_events` belong to Phases 6–9;
integration tests assert they stay at zero rows. Identifying a risk is not
authorising a response to it.

**`as_of` is injected, never read from a clock.** Detectors, resolution, and the
detection-run endpoint all take it as a required input. A run that read the
system clock would not be reproducible, and reproducibility is the basis of the
audit trail.

## API

Six feature routes under `/api/v1`; `/health` and `/health/db` stay at the root.
Interactive docs at `/docs`, schema at `/openapi.json`.

Full contract with real captured examples and TypeScript types:
[docs/contracts/detection-api.md](../docs/contracts/detection-api.md).

**Unauthenticated.** No key, no token, no per-merchant authorization —
`merchant_id` is a filter, not a tenant boundary. Localhost only. This is a
documented Phase 3 gap; authentication was outside the approved scope.

## Testing

```bash
pytest                    # everything
pytest -m "not db"        # hermetic only; no PostgreSQL needed
pytest -m db              # integration + API, against revtrace_test
```

The `db` marker gates the only tests that touch PostgreSQL. They run against
`revtrace_test` — never `revtrace_dev` — inside a transaction that is always
rolled back, so no row survives a test and the harness never drops, truncates,
or recreates anything. If PostgreSQL is unreachable the DB suite skips rather
than fails, so the hermetic majority still runs.

```bash
ruff check app tests ../simulator
ruff format --check app tests ../simulator
mypy app
```

`alembic/` is deliberately excluded from lint paths: applied migrations are
history and must not be reformatted.

Deterministic engine functions have direct unit tests with the formulas
documented alongside. Synthetic/demo scoring is labeled as such — it is not
presented as a validated prediction, and two tests enforce that the labelling
stays in place.
