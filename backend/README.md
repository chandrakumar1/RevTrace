# RevTrace Backend

Python 3.13 · FastAPI · SQLAlchemy · Pydantic · PostgreSQL 16 · Alembic

**Status: Phase 0 scaffold.** Directories and package markers only. No
dependencies installed, no virtualenv, no database, no application code.

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

## Setup (Phase 1 — do not run during Phase 0)

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Environment variables come from the repository-root `.env`, created from
`.env.example`. `.env` is gitignored and must never be committed.

## Layout

```
app/
├── main.py           FastAPI entrypoint (placeholder)
├── api/routes/       HTTP routes; dependencies.py and router.py land in Phase 1
├── core/             config.py, logging.py, security.py
├── models/           SQLAlchemy ORM models
├── schemas/          Pydantic request/response and AI output schemas
├── repositories/     Data access; keeps queries out of services
├── services/
│   ├── ingestion/    Event intake, normalization, idempotency
│   ├── detection/    Revenue-risk detection
│   ├── tracing/      Revenue Leak Graph, timeline reconstruction
│   ├── recovery/     Recovery case orchestration
│   ├── verification/ Did the money actually arrive?
│   └── audit/        Immutable audit trail
├── engine/           Deterministic: risk, recovery, policy, scoring
├── agents/           LLM-facing: diagnosis, intervention, explanation
├── integrations/
│   ├── razorpay/     client, orders, payments, payment_links,
│   │                 subscriptions, webhooks, mapper
│   └── notifications/
└── db/               Engine, session, base
alembic/              Migrations (Phase 1)
tests/
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

## Testing

```bash
pytest
```

Deterministic engine functions must have direct unit tests with the formulas
documented alongside. Synthetic/demo scoring is labeled as such — it is not
presented as a validated prediction.
