# RevTrace

**Razorpay Buildathon — Track 03: AI Revenue Recovery**

> RevTrace traces where revenue is leaking, determines why, recovers what is
> recoverable, and proves what happened.

The full engineering specification lives in [CLAUDE.md](CLAUDE.md). This README
covers local setup only.

---

## Status

**Phase 0 complete — repository scaffolding only.**

No application code, no installed dependencies, no database. Directories are
placeholders awaiting their milestone. See
[docs/phase-0-environment.md](docs/phase-0-environment.md) for the environment
inspection this scaffold was built against.

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
├── docs/                  Environment report, architecture notes, ADRs
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

Phase 0 does not install anything. The steps below are the **Phase 1** plan,
recorded here so setup is reproducible; do not run them until Phase 1 begins.

```bash
# 1. Secrets (never committed)
cp .env.example .env
# then fill in values locally

# 2. Backend virtualenv — note python3.13, NOT python3
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
python -V                      # expect 3.13.x

# 3. Dependencies (Phase 1)
pip install -e ".[dev]"

# 4. Database (Phase 1)
createdb revtrace_dev
```

## Milestones

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository and development environment inspection | ✅ Complete |
| 1 | Backend foundation and database | 🔄 Built; migration pending review |
| 2 | Synthetic event simulator | Not started |
| 3 | Deterministic revenue-risk detection | Not started |
| 4 | Revenue Leak Graph and timelines | Not started |
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

- Razorpay runs in **Test Mode** only.
- Secrets live in `.env` (gitignored). Only `.env.example` is tracked.
- No Razorpay secret is ever exposed to the frontend.
- Webhooks are signature-validated, idempotent, and tolerant of duplicate,
  delayed, and out-of-order delivery.

## Measurement honesty

Metrics produced against simulator data are **synthetic/demo** measurements and
are labeled as such wherever reported. Scoring formulas are documented and
tested; they are not presented as scientifically validated predictions.
