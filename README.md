# RevTrace

**Razorpay Buildathon — Track 03: AI Revenue Recovery**

> A payment fails. We intervene. The customer pays.
> **Did our intervention actually cause the recovery?**

Most recovery tools answer that question by not asking it. They count every
rupee that arrives after an action and call it recovered. Many of those
customers would have paid anyway.

RevTrace measures the difference — and refuses to act when it cannot tell.

---

## Live

| | |
|---|---|
| **Start here** — the 90-second explanation | <https://revtrace-overview.onrender.com> |
| **The application** — the real product | <https://revtrace-frontend.onrender.com> |
| **The API** | <https://revtrace-backend.onrender.com> |
| **Interactive API docs** | <https://revtrace-backend.onrender.com/docs> |
| **Source** | <https://github.com/chandrakumar1/RevTrace> |

**Start with the Overview** if you want the idea before the machinery.
**Open the Application** to inspect the real product and run the synthetic demo.

> All services are on Render's free tier and sleep when idle — the first request
> after a quiet period can take up to a minute to wake. The overview and the
> application's first two pages are static and unaffected; only the live demo
> waits on the API.

## Explore the project

Five steps, nothing to install.

| # | Where | What to look for |
|---|---|---|
| 1 | [**Overview**](https://revtrace-overview.onrender.com) | The problem, the experiment, and why abstention matters — in about ninety seconds. |
| 2 | [**Application**](https://revtrace-frontend.onrender.com) | The product itself. Three pages. |
| 3 | **Incrementality Ledger** | Gross recovered, incremental recovered, and **credited-not-earned** — the gap, which is the money a conventional dashboard would have claimed. That gap is the pitch. |
| 4 | **Evaluation** | Cross-fitted uplift, Qini, quadrant labels — beside a limitations section carrying equal weight. |
| 5 | **Live demo → ▶ Run Demo** | Six steps. Watch the payment advance `failed → captured`, the replay change nothing, and two attacks get refused. |


Localhost is **not** required to review this project. Local setup is documented
under [Local development](#local-development) for contributors.

---

## What RevTrace is

When a payment fails and you intervene, and the customer then pays, you have not
learned that you caused the payment. You have learned that both things happened.

RevTrace splits recovered revenue into two very different quantities:

- **Incremental** — caused by the intervention. Defensible.
- **Credited, not earned** — would have arrived anyway. The part a conventional
  recovery dashboard silently keeps.

That split is only possible because a **randomised holdout** is kept. Every case
is assigned to treatment or holdout by `sha256(risk_id : experiment_id : salt)`,
so an auditor can recompute any unit's arm from stored values alone. Nothing is
drawn at run time. The holdout is never contacted, so what happens to it is what
would have happened anyway.

## Why it is different

**1. Deterministic code owns the numbers.**
Every revenue figure, risk score, uplift estimate, policy decision and execution
authorisation is ordinary, tested, integer arithmetic. Money is carried as
integer minor units and rates as integer basis points; no floating-point
arithmetic touches a money or probability path on either side of the wire.

**2. A holdout provides the counterfactual.**
Intention-to-treat: a case stays in the arm it was assigned to even when
execution failed. The denominator is fixed at randomisation.

**3. Abstention is a valid outcome.**
When the estimated uplift interval contains zero, or the customer was likely to
self-recover, RevTrace **declines to act** and records which named reason
applied. Contacting someone who would have paid anyway costs money and goodwill,
and the system treats that as a real cost rather than a free upside.

**4. AI is advisory, and holds no authority.**

> **AI explains the evidence. It does not rewrite the evidence.**

The model proposes falsifiable hypotheses and cites the integers it relies on.
Deterministic code then checks every cited integer against the computed values
before anything is believed. A model that invents a number is caught by
arithmetic, not by trust.

---

## Synthetic / offline — read this before interpreting any number

- **No real Razorpay transaction has ever been processed by this system.**
- **No real customer data, no real payment, no real money.**
- The demo's provider responses come from a **deterministic synthetic offline
  provider**. No credential is read and no socket is opened.
- Every metric here is a **synthetic/demo measurement** against a generated
  population with **planted effects**. Recovering a planted effect validates the
  estimator, not the world.
- The **live browser demo makes no model call.**
- **This should not be read as production-ready.** See
  [Limitations](#limitations).

What *is* real, and worth being precise about: the HMAC-SHA256 signature
verification, merchant derivation, idempotency constraint, ordering rules and
payment state machine are **production code**, exercised by the demo rather than
simulated by it. Only the bytes and the secret are synthetic.

Every synthetic artifact carries a `DEMO` marker, and the provenance string
`DEMO / SYNTHETIC / OFFLINE — not a Razorpay transaction` travels with the data
so a screenshot cannot be mistaken for a capture.

---

## The demo, exactly

One click runs this end to end. Every step is a real call into the code named.

| # | Step | What runs |
|---|---|---|
| 1 | Synthetic failed payment | A `DEMO`-marked merchant, customer, order and **failed** payment attempt — the recovery opportunity |
| 2 | Recovery link | `payment_links.build_request` + `create_payment_link` — **production code** |
| 3 | Synthetic provider response | `DemoPaymentLinkClient`, implementing the SDK's own surface, **offline** |
| 4 | Signed synthetic webhooks | Three deliveries, each signed with a genuine **HMAC-SHA256 over the exact bytes to be delivered** |
| 5 | HMAC verification | Raw bytes verified before anything parses them |
| 6 | Merchant derivation | `payment_attempts → orders → merchant_id`, from the **signed** payment id |
| 7 | Event mapping | `payment.failed → payment.failed`, `payment.captured → payment.captured`, **`payment_link.paid → order.paid`** |
| 8 | Payment state | The attempt advances **`failed → captured`**, forward only |
| 9 | Replay | The same three delivered again — duplicate detected, row count unchanged |
| 10 | Tampered webhook | **REFUSED** |
| 11 | Foreign-merchant webhook | **REFUSED** |
| 12 | Rollback | *Rolled back — nothing persisted.* |

Steps 10 and 11 are **successes**, and the UI renders them that way. They are
security controls doing their job; showing them as errors would invert what the
demo exists to prove.

---

## Architecture

A clean modular monolith. No microservices.

```
┌───────────────────────────────────────────────────────────────────┐
│  overview/    the public showcase — deploys on its own            │
│               static throughout · no API call, ever               │
└───────────────────────────────────────────────────────────────────┘
        (no dependency in either direction — see below)

┌───────────────────────────────────────────────────────────────────┐
│  frontend/           React 19 · Vite 7 · TypeScript · Tailwind 4  │
│  Page 1 Ledger  ·  Page 2 Evaluation  ─── committed fixtures      │
│  Page 3 Live demo ──────────────────────── /api                   │
└────────────────────────────────┬──────────────────────────────────┘
                                 │
┌────────────────────────────────▼──────────────────────────────────┐
│  backend/  FastAPI · SQLAlchemy 2 · Pydantic 2 · Alembic          │
│                                                                    │
│  api/routes/       health · ingest · detection · risks · timeline  │
│                    webhooks · demo            (11 operations)      │
│                                                                    │
│  engine/           risk, scoring, detectors, policy_engine,        │
│                    falsification — deterministic, pure             │
│  causal/           cross-fitted uplift, Qini, quadrants, power     │
│  experiments/      deterministic assignment, ITT lifecycle         │
│  reporting/        evaluation.py — the sole reader of ground truth │
│                                                                    │
│  agents/           hypothesis_agent — advisory only, never acts    │
│                                                                    │
│  integrations/razorpay/                                            │
│      client · payment_links · webhooks · mapper · demo            │
│  services/verification/   HMAC → merchant → persist → advance      │
│  services/demo/           the browser-facing offline demo          │
└────────────────────────────────┬──────────────────────────────────┘
                                 │
     revtrace_dev  ·  revtrace_test  ·  revtrace_hypothesis_test
```

### The application — `frontend/`

Three pages, and only three:

| Page | Reads |
|---|---|
| **Incrementality Ledger** | Committed fixture, build time. No request. |
| **Evaluation** | Committed fixture, build time. No request. |
| **Live Demo** | Calls the API — but only when you press **Run Demo**. |

No charting library. The visualisations are laid out with CSS from the report
payload; every visible number comes from the backend through a formatter, and
nothing on screen is derived from layout geometry.

### The showcase — `overview/`, and why it is separate

`overview/` is a **public project-introduction site, not part of the application
UI.** It exists to explain the idea before a reviewer opens the real product,
and then to hand them the link.

It has its own React/Vite app, its own design system, and its own build. It
shares **no components** with `frontend/`, has **no imports in either
direction**, makes **no API calls**, and **does not depend on the backend** —
every figure it shows is compiled in from its own local evidence module,
transcribed from the generated artifacts with the artifact path recorded beside
each value.

That independence is deliberate: the showcase must render when the API is
asleep, and the application must not acquire a dependency on explanatory copy.
The two live in this repository and **deploy as two separate Render sites**.

### The causal / decision layer

`causal/` and `engine/` own every number. A five-fold cross-fitted T-learner over
empirical cell rates produces per-cell uplift with confidence intervals; the
policy engine turns those into one of a small set of decisions — act, abstain
with a named reason, or escalate.

### The Razorpay adapter

All provider code lives in `app/integrations/razorpay/`, and nothing outside that
package sees a Razorpay request or response shape. `mapper.py` is the single
place a provider event name becomes a RevTrace `EventType` — an unmapped event is
**refused, never guessed**, because a timeline is evidence.

---

## Security

**Raw bytes are verified before JSON parsing.** Razorpay signs the exact octets
it sent; a parsed-then-re-serialised body is different octets. The route reads
the raw body, verifies, and only then parses.

**HMAC-SHA256, timing-safe.** Verification goes through the official SDK's
`verify_webhook_signature`, which compares with `hmac.compare_digest`. The
verifier returns `None` on success and **raises on every failure**, so a caller
cannot mistake a falsy return for a pass.

**Merchant ownership is derived from signed data.** The owning merchant is
reached through `PaymentAttempt → Order → merchant_id`, from the signed payment
id.

**`merchant_id` is an assertion, not a selector.** The optional query parameter
is checked *against* the derivation and can only cause a rejection. This was a
real, demonstrated defect: when the parameter was authoritative, a
validly-signed webhook about one merchant's payment could be filed under
another, pointing at the victim's order and advancing the victim's payment
attempt. Existence-checking the merchant caught none of it, because the merchant
existed — it simply was not the one that owned the payment. **Found, fixed, and
regression-tested**; the attack is now part of the suite and is visible in the
live demo.

**Idempotency is a database constraint, not a code path.**
`UNIQUE(merchant_id, external_event_id)` means a duplicate delivery is an insert
that writes nothing, rather than a case someone has to remember to detect.

**Ordering comes from `occurred_at`,** the provider's own timestamp — never from
arrival order. A delayed or out-of-order delivery lands correctly, and a payment
attempt moves forward only, so a late `payment.authorized` cannot undo a
`payment.captured` that already arrived.

**No AI execution authority.** The verification path writes `payment_attempts`
and `events` and nothing else — no `audit_events` row, no `recovery_actions`
flag. There is no actor for an AI to become on that path, and the database
independently refuses an `ai_agent` actor on an execution entry.

**No secret reaches the frontend.** Secret-bearing settings are typed
`SecretStr`, so an accidental `repr()`, log line or traceback cannot leak them —
and they are empty in this repository regardless.

**Authentication is out of scope for this demo.** There is no key, token, or
session on the feature endpoints; the webhook route's HMAC is the only
authentication in the system. This is a documented boundary, not an oversight.

---

## AI, and what it is not allowed to do

RevTrace uses exactly one model, on a **free-only provider lock**:

| | |
|---|---|
| Provider | **OpenRouter** — the only provider ever constructed |
| Model | `nvidia/nemotron-3-super-120b-a12b:free` |
| Fallback | **None.** Zero paid-model fallback, by construction |

The lock is fail-closed: whether a provider is free is an explicit declaration
that **defaults to false**, and the `:free` suffix only corroborates it, never
infers it. The free-only chain refuses at construction time rather than at call
time — a 429 on a free model is exactly when a naive chain would silently fail
over to a paid one.

**The live result, stated accurately.** One live call succeeded. It proposed a
hypothesis about the cell `card_declined|card` — claim
`higher_uplift_than_population`, rule
`interval_lies_entirely_above_the_population_effect` — and deterministic code
returned a verdict of `confirmed`.

**That run was rolled back and never persisted.** No audit row exists in any
database, so the numeric detail of the capture — including how many cited
integers were checked — is **not available as evidence** and is not reconstructed
anywhere in this project. The classification above is what survives.

Three subsequent live calls failed and are reported as failures rather than
retried into a success: one returned empty content, one returned a 200 whose body
carried an error and no choices, and one was a `502 Upstream error`. Whether the
full payload is viable within the token budget **remains unknown** — the 502
never reached the model.

**None of this is required for the demo.** The browser demo makes no model call.

---

## Databases

Three databases, three jobs. The separation is enforced in code, not by
convention.

| Database | Role |
|---|---|
| `revtrace_test` | **Ephemeral** test and demo database. Every test and the demo run inside a transaction that is always rolled back; no row survives. |
| `revtrace_hypothesis_test` | The **persistent canonical benchmark** population that measured claims rest on. Read-only for everything else; the demo refuses it by name. |
| `revtrace_dev` | The **application database** — what `DATABASE_URL` names locally. The test suite and the demo both refuse it by name. |

`DEMO_DATABASE_URL` is a **separate setting from `DATABASE_URL`**, empty by
default — the demo endpoint reports itself disabled rather than choosing a
database on its own. There is **no HTTP equivalent of `run_demo.py --commit`**:
the endpoint takes no such parameter and rolls back in a `finally`.

---

## Verified test results

Measured on the current tree:

| Check | Result |
|---|---|
| **Full test suite** | **4,604 passed · 0 failed · 0 skipped** |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy app` | clean |
| Frontend `npm run typecheck` | clean |
| Frontend `npm run build` | clean |

Nothing is skipped, and no test is marked expected-to-fail.

---

## Local development

**Not required to review this project** — the deployed links above are the
intended path. This section is for contributors.

Replace `USER` with your PostgreSQL role. Nothing below needs a credential of any
kind — no Razorpay key, no AI key.

### Prerequisites

| Tool | Required |
|---|---|
| Python | **3.13.x** (pinned in `backend/.python-version`; `pyproject.toml` requires `>=3.13,<3.14`) |
| Node | 20.19+ |
| npm | 10+ |
| PostgreSQL | 16 |

If your default `python3` is newer than 3.13, create the venv with `python3.13`
explicitly — the backend pins 3.13 and newer wheels may not resolve.

### Setup

```bash
# 1. Backend environment — the [dev] extra is REQUIRED
cd backend
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pip install -e ../simulator

# 2. Databases
createdb revtrace_dev
createdb revtrace_test

# 3. Schema — Alembic reads DATABASE_URL and must run from backend/
DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_dev \
    .venv/bin/alembic upgrade head
DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/alembic upgrade head

# 4. Frontend
cd ../frontend && npm install
```

### Run the application

```bash
# terminal 1 — backend, with the demo enabled
cd backend
DEMO_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs

# terminal 2 — the application
cd frontend && npm run dev                        # http://localhost:5173
```

Without `DEMO_DATABASE_URL` everything still runs; only the demo endpoint
reports itself disabled. The Vite dev server proxies `/api` to `localhost:8000`,
so the browser sees a single origin and the backend needs no CORS middleware.

### Run the showcase site

```bash
cd overview
npm install
npm run dev                                       # http://localhost:5173
```

It has its own `package.json` and lockfile; installing here does not touch
`frontend/`.

### The offline demo, in a terminal

No server, no credentials, nothing kept:

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/python run_demo.py
```

### The evaluation benchmark

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/python run_acceptance.py
```

Materialises the population inside one transaction and rolls back in a
`finally`. It writes exactly two files — `docs/EVALUATION.md` and
`docs/evaluation.json` — and touches nothing else.

### Tests and checks

```bash
cd backend
.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest -m "not db"  # hermetic only; no PostgreSQL needed
.venv/bin/ruff check app tests ../simulator
.venv/bin/mypy app

cd ../frontend && npm run typecheck && npm run build
```

---

## Deployment

Three separate Render services, plus a managed PostgreSQL instance:

| Component | Source | URL |
|---|---|---|
| Overview — the public showcase | `overview/` | <https://revtrace-overview.onrender.com> |
| Application | `frontend/` | <https://revtrace-frontend.onrender.com> |
| API | `backend/` | <https://revtrace-backend.onrender.com> |
| PostgreSQL | — | managed, not public |

`overview/` and `frontend/` are independent static builds from this repository
and deploy as separate sites. The overview never calls the API, so it is
unaffected by the backend's state.

**A cloud backend cannot reach a laptop's PostgreSQL.** `localhost:5432` on
Render means *Render's* localhost, where nothing is listening. The database must
be a hosted instance with a connection string supplied as `DATABASE_URL`. This
is the single most common way this deployment fails.

**The Vite proxy is development-only.** `server.proxy` exists in the dev server
and is absent from a production build, so a deployed frontend must be told where
the API is. Two supported ways:

| | Frontend build | Backend |
|---|---|---|
| **Absolute API URL** | `VITE_API_BASE_URL=https://revtrace-backend.onrender.com` | `FRONTEND_ORIGIN=https://revtrace-frontend.onrender.com` |
| **Same-origin rewrite** | leave `VITE_API_BASE_URL` unset; add a host rewrite from `/api/*` to the backend | leave `FRONTEND_ORIGIN` unset |

**Do not do both** — a rewrite plus an absolute URL sends requests somewhere
neither was configured for.

`VITE_API_BASE_URL` is read at **build** time, not run time: changing it means
rebuilding. It is inlined into readable JavaScript, so it must never hold a
secret.

**`FRONTEND_ORIGIN` is one origin, and empty means no CORS middleware at all.**
Scheme and host, no path and no trailing slash — the backend refuses a malformed
value at startup rather than letting it fail as an opaque browser error.
Credentials are never enabled; there is no cookie or token to send.

**Run the migrations against the hosted database** before first boot:
`alembic upgrade head` with `DATABASE_URL` set to the hosted DSN.

**Decide deliberately whether the demo endpoint is enabled.** Leaving
`DEMO_DATABASE_URL` unset disables it. If enabled, point it at a throwaway
database — never at the one `DATABASE_URL` names.

`APP_ENV=production` disables `/docs` and `/redoc`.

---

## Limitations

Stated plainly, because a system that claims to prove things has to be honest
about what it has not proven.

- **Razorpay has never processed a transaction for this system.** The adapter is
  written against the official SDK and documentation, and has been exercised only
  against a synthetic offline provider. Test Mode integration is prepared, not
  performed.
- **All provider responses in the demo are synthetic**, produced by
  `DemoPaymentLinkClient`. They are shaped like real ones and are not real ones.
- **Every metric is a synthetic/demo measurement.** The planted effects are
  assumptions someone wrote down. Recovering them validates the estimator, not
  the world. No figure here is a validated prediction about real customers.
- **The live AI path is not required, and is not fully proven.** The demo makes
  no model call. One live call succeeded and was verified; three failed. Whether
  the full payload fits the token budget is unknown.
- **There is no authentication** on the feature endpoints. `merchant_id` is a
  filter, not a tenant boundary, except on the webhook route where tenancy is
  derived from signed data.
- **Production deployment needs work this repository does not contain**:
  authentication and authorisation, rate limiting, secret management and
  rotation, backups, monitoring and alerting, and a real Test Mode integration
  before any Live Mode conversation.

---

## Repository layout

```
RevTrace/
├── README.md              This file
├── CLAUDE.md              Working agreement for coding agents
├── .env.example           Environment template — no values, ever
├── overview/              The public showcase — separate site, no API calls
├── frontend/              The application — React 19 · Vite 7 · Tailwind 4
├── backend/
│   ├── run_acceptance.py  The evaluation benchmark
│   ├── run_demo.py        The offline demo, in a terminal
│   ├── run_hypothesis.py  One controlled live model call
│   ├── run_materialise.py Canonical population materialisation
│   ├── app/
│   │   ├── api/           Routes, dependencies, router wiring
│   │   ├── core/          Config, logging, security, money
│   │   ├── models/        SQLAlchemy models
│   │   ├── schemas/       Pydantic schemas
│   │   ├── repositories/  Data access
│   │   ├── services/      ingestion · detection · tracing · recovery
│   │   │                  verification · demo
│   │   ├── engine/        Deterministic risk, policy, falsification
│   │   ├── experiments/   Deterministic assignment, ITT lifecycle
│   │   ├── causal/        Uplift, Qini, quadrants, power
│   │   ├── reporting/     evaluation.py — the sole truth reader
│   │   ├── agents/        hypothesis_agent — advisory only
│   │   ├── integrations/  razorpay/ — the provider boundary
│   │   └── db/            Session and engine setup
│   ├── alembic/           Migrations
│   └── tests/             4,604 tests, plus the benchmark harness
├── simulator/             Synthetic event generator, potential outcomes
└── docs/
    ├── architecture.md    What was actually built, and why
    ├── EVALUATION.md      Generated evaluation (synthetic/demo)
    ├── evaluation.json    The same report as structured data
    ├── gate_comparison.json   Gate/spend figures, kept separate from causal
    ├── EXPERIMENT_DESIGN.md   Pre-registration — not amended after the fact
    ├── BREAKAGE.md        What actually broke, in order, with costs
    ├── contracts/         API and fixture contracts
    └── decisions/         ADRs
```

---

## Measurement honesty

[`docs/EVALUATION.md`](docs/EVALUATION.md) carries its own limitations section
and is the authoritative source for every figure. **This README deliberately
quotes no result from it** — one number in two places is one number that will
drift.

Scoring formulas are documented and tested. None is presented as a
scientifically validated prediction, and tests enforce that the synthetic/demo
labelling stays in place.

Further reading:

| Document | What it is |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | What was actually built, and why it diverged where it did |
| [`docs/EXPERIMENT_DESIGN.md`](docs/EXPERIMENT_DESIGN.md) | The pre-registration, frozen before the run and never amended |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | The generated evaluation, with its own limitations |
| [`docs/BREAKAGE.md`](docs/BREAKAGE.md) | What broke during the build, in order, with what it cost |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |
