# RevTrace

**Razorpay Buildathon — Track 03: AI Revenue Recovery**

> RevTrace traces where revenue is leaking, determines why, recovers what is
> recoverable, and proves what happened.

The full engineering specification lives in [CLAUDE.md](CLAUDE.md). This README
covers local setup only.

---

## Status

**Phase 3 complete — deterministic revenue-risk detection.**

The backend ingests an event stream, reconstructs order and subscription
timelines that survive duplicate, delayed, out-of-order, and missing delivery,
computes money and confidence deterministically, runs four detectors, resolves
risks that stop being real, and serves it all over six versioned API endpoints.

No AI yet, no Razorpay integration yet, no recovery execution, and no frontend
dashboard. See [docs/architecture.md](docs/architecture.md) for what was
actually built and [docs/contracts/detection-api.md](docs/contracts/detection-api.md)
for the API contract.

> **The API is unauthenticated and must not be exposed.** No key, no token, no
> tenant isolation. Bind to localhost only. Authentication was outside the
> approved Phase 3 scope and is a prerequisite for any deployment beyond a local
> demo.

Measured on the 17 synthetic scenarios at `seed=42`: 10 true positives, 0 false
positives, 0 false negatives. **Synthetic/demo measurement over generated data**
— not a held-out set, and not evidence of real-world accuracy. Phase 11 builds
the held-out benchmark.

## Architecture rule (non-negotiable)

The LLM is **not** the authority over money. It may diagnose, interpret
evidence, reason, explain, and recommend among permitted actions. All revenue
math, risk math, policy enforcement, limits, execution authorization, and
verification are deterministic code.

Every financial action follows:

```
AI recommendation -> deterministic validation -> policy engine -> approved action
    -> Razorpay adapter -> verification -> audit log
```

## Prerequisites

| Tool | Required | Verified on this machine |
|---|---|---|
| macOS | — | 15.7.9 (x86_64) |
| Python | **3.13.x** | 3.13.7 |
| Node | 20+ | 24.19.0 |
| npm | 10+ | 11.17.0 |
| PostgreSQL | 16 | 16.15 (Homebrew, running on :5432) |
| Git | 2.x | 2.39.5 |

Python 3.14 is also installed on this machine and is the default `python3`.
**Do not use it** — the backend pins 3.13 (see `backend/.python-version`).
Always create the venv with `python3.13` explicitly.

Docker is not installed and is not required.

## Repository layout

```
.
├── CLAUDE.md              Master engineering specification
├── .env.example           Environment variable template (no values)
├── docs/                  Environment report, architecture notes, ADRs,
│                          API and fixture contracts
├── backend/               Python 3.13 / FastAPI modular monolith
│   ├── app/
│   │   ├── api/           Routes, dependencies, router wiring
│   │   ├── core/          Config, logging, security
│   │   ├── models/        SQLAlchemy models
│   │   ├── schemas/       Pydantic schemas
│   │   ├── repositories/  Data access
│   │   ├── services/      ingestion, detection, tracing, recovery,
│   │   │                  verification, audit
│   │   ├── engine/        Deterministic risk / recovery / policy engines
│   │   ├── agents/        LLM-facing agents (diagnosis, intervention,
│   │   │                  explanation) — advisory only
│   │   ├── integrations/  razorpay/, notifications/ — adapter boundary
│   │   └── db/            Session and engine setup
│   ├── alembic/           Migrations (Phase 1)
│   └── tests/
├── frontend/              React + Vite + TS + Tailwind + Recharts (Phase 10)
└── simulator/             Synthetic event generator (Phase 2)
```

## Setup

```bash
# 1. Secrets (never committed)
cp .env.example .env
# then fill in values locally

# 2. Backend virtualenv — note python3.13, NOT python3
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
python -V                      # expect 3.13.x

# 3. Dependencies
pip install -e ".[dev]"
pip install -e ../simulator

# 4. Databases
createdb revtrace_dev
createdb revtrace_test         # integration and API tests only
.venv/bin/alembic upgrade head

# 5. Run
.venv/bin/uvicorn app.main:app --reload    # http://127.0.0.1:8000/docs
```

Load a scenario and detect against it:

```bash
.venv/bin/python -m simulator generate S04 --seed 42
# POST simulator/output/S04_seed42/fixture.json (minus ground_truth)
#   to /api/v1/ingest/simulation, then POST /api/v1/detection/runs
```

`revtrace_dev` is the development database and is never used by tests.
`revtrace_test` is the only database the test suite touches, and every test runs
in a transaction that is rolled back.

## Milestones

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository and development environment inspection | ✅ Complete |
| 1 | Backend foundation and database | ✅ Complete |
| 2 | Synthetic event simulator | ✅ Complete |
| 3 | Deterministic revenue-risk detection | ✅ Complete |
| 4 | Revenue Leak Graph and timelines | Partly done in Phase 3 — per-order timelines built; graph outstanding |
| 5 | AI diagnosis | Not started |
| 6 | Counterfactual recovery engine | Not started |
| 7 | Policy and safety engine | Not started |
| 8 | Razorpay Test Mode integration | Not started |
| 9 | Execution + webhook verification | Not started |
| 10 | Frontend dashboard | Not started |
| 11 | Evaluation benchmark | Not started |
| 12 | Failure injection and resilience testing | Not started |
| 13 | Demo hardening, documentation and final polish | Not started |

## Security

- **The API is unauthenticated as of Phase 3.** No key, no token, no session, no
  per-merchant authorization. Localhost only; authentication is a prerequisite
  for any deployment. It is a documented gap, not an oversight — see the
  [API contract](docs/contracts/detection-api.md#security-posture).
- No endpoint moves money. Nothing approves, executes, refunds, retries,
  contacts a customer, or calls Razorpay. Detection writes to `revenue_risks`
  and nothing else, verified by test.
- Razorpay runs in **Test Mode** only.
- Secrets live in `.env` (gitignored). Only `.env.example` is tracked.
- No Razorpay secret is ever exposed to the frontend.
- Webhooks are signature-validated, idempotent, and tolerant of duplicate,
  delayed, and out-of-order delivery.

## Measurement honesty

Metrics produced against simulator data are **synthetic/demo** measurements and
are labeled as such wherever reported. Scoring formulas are documented and
tested; they are not presented as scientifically validated predictions.
