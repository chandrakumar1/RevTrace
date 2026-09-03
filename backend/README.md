# RevTrace Backend

Python 3.13 · FastAPI · SQLAlchemy · Pydantic · PostgreSQL 16 · Alembic

**Status: the ledger, the gate, the provider boundary and the offline demo are
built.** Ingestion, timeline reconstruction, the deterministic risk engine, four
detectors, deterministic assignment and ITT measurement, cross-fitted uplift, the
policy gate, the advisory hypothesis agent, the Razorpay adapter, webhook
verification, and a browser-facing offline demo — **11 API operations**.

**No real Razorpay transaction has ever been processed.** The adapter is
exercised against a deterministic synthetic offline provider. See the root
[README](../README.md#2-demo--synthetic--offline).

## Deployed

The API runs at **<https://revtrace-backend.onrender.com>**, with interactive
docs at [`/docs`](https://revtrace-backend.onrender.com/docs) and the schema at
`/openapi.json`. It is on Render's free tier and sleeps when idle, so the first
request after a quiet period can take up to a minute.

**Everything below is for local development.** You do not need any of it to use
the deployed application — see the root [README](../README.md#live).

## Python version — read this first

The backend targets **Python 3.13** (pinned in `.python-version` and
`requires-python`). If your default `python3` is newer, creating the venv with a
bare `python3` will produce the wrong environment and may fail on dependency
wheels.

Always be explicit:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -V          # must print 3.13.x
```

## Setup

Replace `USER` with your PostgreSQL role.

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # the [dev] extra is REQUIRED
pip install -e ../simulator      # editable; zero third-party dependencies

createdb revtrace_dev
createdb revtrace_test
.venv/bin/alembic upgrade head   # applies to DATABASE_URL

# With the browser demo enabled (optional)
DEMO_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/uvicorn app.main:app --reload
```

Environment variables come from the repository-root `.env`, created from
`.env.example`. `.env` is gitignored and must never be committed. No Razorpay or
AI credential is needed to run anything here; all of them default to empty.

### `TEST_DATABASE_URL`

Optional. Overrides the DSN used by the integration and API tests. It is
**deliberately not in `.env.example`** — it is a test-harness setting, not an
application setting, and `app/core/config.py` does not read it.

```bash
TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test
```

A guard in `tests/conftest_db.py` refuses any DSN whose database name does not
contain `revtrace_test` or `_test`, so a stray value cannot point the suite at
development data. Its built-in default names a local role; set this variable
explicitly if that role is not yours.

### `DEMO_DATABASE_URL`

Where the browser demo runs. **Empty by default, and empty means the demo is
off** — `/api/v1/demo/run` reports itself unavailable rather than choosing a
database on its own.

Deliberately separate from `DATABASE_URL`, because the demo writes rows and
`DATABASE_URL` names the development database. `revtrace_dev` and
`revtrace_hypothesis_test` are refused **by name** whatever this holds, and the
demo transaction is always rolled back — there is no HTTP equivalent of
`run_demo.py --commit`.

## Running it hosted

What a deployment needs to set. No credential appears in this repository, and
none of these values is recorded here.

| Variable | What it must be |
|---|---|
| `DATABASE_URL` | The hosted PostgreSQL DSN. A bare `postgres://` or `postgresql://` URL is accepted and normalised to the psycopg 3 driver, so a provider-issued URL works as given. |
| `DEMO_DATABASE_URL` | A **throwaway** database for the browser demo, never the one `DATABASE_URL` names. Leaving it unset disables the demo endpoint. |
| `FRONTEND_ORIGIN` | The application's origin, when the frontend is built with an absolute `VITE_API_BASE_URL`. One origin, scheme and host, no trailing slash. Empty means no CORS middleware is installed at all. |
| `APP_ENV` | `production` disables `/docs` and `/redoc`. |

Run `alembic upgrade head` against the hosted DSN before first boot.

The demo endpoint rolls its transaction back on every call, so a hosted demo
database accumulates nothing — but it still must not be a database anything else
depends on. `revtrace_dev` and `revtrace_hypothesis_test` are refused by name
regardless of what is configured.

## Layout

```
app/
├── main.py           FastAPI entrypoint
├── api/
│   ├── router.py     /api/v1 for features; /health unversioned at the root
│   └── routes/       health, ingestion, detection, risks, timeline,
│                     webhooks, demo
├── core/             config.py, logging.py, security.py, money.py
├── models/           SQLAlchemy ORM models
├── schemas/          Pydantic request/response; common.py holds UtcDatetime
├── repositories/     entity, event, risk, uplift, experiment results
├── services/
│   ├── ingestion/    Event intake, reference validation, idempotency
│   ├── detection/    Run orchestration: the one engine/database seam
│   ├── tracing/      state.py + reconstruction.py — timelines
│   ├── recovery/     gate.py — the policy seam
│   ├── hypothesis/   loader + service for the advisory agent
│   ├── verification/ HMAC → merchant → persist → advance; demo_scenario.py
│   └── demo/         runner.py — the browser-facing offline demo
├── engine/           Deterministic, pure
│   ├── risk_engine.py    money, in integer minor units
│   ├── scoring.py        confidence_bps (synthetic heuristic)
│   ├── resolution.py     reconcile findings against stored risks
│   ├── policy_engine.py  act / abstain / escalate — never a silent override
│   ├── falsification.py  confirm, refute, or refuse to conclude
│   └── detectors/        four pure detect(timeline, as_of, config)
├── causal/           Cross-fitted uplift, Qini, quadrants, power
├── experiments/      Deterministic assignment, ITT lifecycle
├── reporting/        evaluation.py — the sole reader of ground truth
├── agents/           hypothesis_agent.py — advisory only, never executes
├── integrations/
│   └── razorpay/     client · payment_links · webhooks · mapper · demo
└── db/               Engine, session, base
alembic/              Migrations — head: 9c41e07b2d58
tests/                4,604 tests
├── integration/      DB-backed: ingestion, assignment, persistence
├── integrations/     Adapter boundary: client, links, webhooks, demo
├── evaluation/       TP/FP/FN harness + 17 regression fixtures
├── benchmark/        The N=10,000 harness
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
Identifying a risk is not authorising a response to it, and integration tests
assert the separation holds.

**Webhook verification never executes.** It writes `payment_attempts` and
`events` and nothing else — no `audit_events` row, no `recovery_actions` flag.
There is no actor for an AI to become on that path.

**The demo never commits.** `services/demo/runner.py` rolls back in a `finally`
and `execute()` takes no parameter that could change that. `revtrace_dev` and
`revtrace_hypothesis_test` are refused by name. It reuses the production
functions rather than reimplementing them — a second implementation of the
recovery path would be a second thing to keep correct.

**`as_of` is injected, never read from a clock.** Detectors, resolution, and the
detection-run endpoint all take it as a required input. A run that read the
system clock would not be reproducible, and reproducibility is the basis of the
audit trail.

## API

Eleven operations. `/health` and `/health/db` stay at the root; everything else
is under `/api/v1`. Interactive docs at `/docs`, schema at `/openapi.json`.

| Method | Path |
|---|---|
| `GET` | `/health` · `/health/db` |
| `POST` | `/api/v1/ingest/simulation` |
| `POST` | `/api/v1/detection/runs` |
| `GET` | `/api/v1/risks` · `/{risk_id}` · `/{risk_id}/evidence` |
| `GET` | `/api/v1/orders/{order_id}/timeline` |
| `POST` | `/api/v1/webhooks/razorpay` |
| `POST` | `/api/v1/demo/run` |
| `GET` | `/api/v1/demo/status` |

Full detection contract with captured examples and TypeScript types:
[docs/contracts/detection-api.md](../docs/contracts/detection-api.md).

**Unauthenticated, except the webhook.** No key, no token, no per-merchant
authorization — `merchant_id` is a filter, not a tenant boundary. Localhost only.
This is a documented demo boundary, not an oversight.

The webhook route is the exception, and is the only authenticated endpoint: its
HMAC signature *is* the security boundary, checked over raw bytes before parsing.
Its tenancy is derived from the signed payment id, never from a query parameter.

**No endpoint moves money.** Nothing approves, executes, refunds, retries or
contacts a customer over HTTP. Execution stays behind the policy gate and an
operator script.

## Operator scripts

Four entry points at `backend/`. Each requires `TEST_DATABASE_URL` explicitly,
refuses `revtrace_dev` by name, and defaults to rolling back — none of them
falls back to a developer's personal DSN.

| Script | What it does |
|---|---|
| `run_demo.py` | The offline demo, in a terminal. `--commit` to keep rows |
| `run_acceptance.py` | The N=10,000 evaluation benchmark; writes `docs/EVALUATION.md` |
| `run_materialise.py` | Materialises the canonical benchmark population |
| `run_hypothesis.py` | One controlled live model call, one request, no retry |

## Testing

```bash
pytest                    # everything — 4,604 passed, 0 failed, 0 skipped
pytest -m "not db"        # hermetic only; no PostgreSQL needed
pytest -m db              # integration + API, against revtrace_test
```

The `db` marker gates the only tests that touch PostgreSQL. They run against
`revtrace_test` — never `revtrace_dev` — inside a transaction that is always
rolled back, so no row survives a test and the harness never drops, truncates,
or recreates anything. If PostgreSQL is unreachable the DB suite skips rather
than fails, so the hermetic majority still runs.

`revtrace_hypothesis_test` holds the persistent canonical benchmark population
and is **not** written by the suite. Materialising it is a deliberate operator
step (`run_materialise.py`), never a side effect of running tests.

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
