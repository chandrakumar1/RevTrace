# RevTrace

**Razorpay Buildathon — Track 03: AI Revenue Recovery**

> RevTrace is a revenue recovery system that can prove how much of the money it
> recovered it actually caused — and that refuses to act when it cannot.

---

## 1. What RevTrace is, in plain terms

When a payment fails, most recovery tools ask *"can we get this money back?"* and
then take credit for every rupee that arrives afterwards. Many of those customers
would have retried on their own. The tool did nothing, and billed for it.

RevTrace asks a harder question: **is recovery worth attempting on this payment,
and can we prove the attempt is what made the difference?**

Three ideas carry the whole system.

**Deterministic code owns the money.** Every revenue figure, risk score, uplift
estimate, policy decision and execution authorisation is computed by ordinary,
tested, integer arithmetic. No language model is anywhere near those numbers.

**Recovery is measured against a randomised holdout.** Every case is assigned to
treatment or control by `sha256(risk_id : experiment_id : salt)`, so an auditor
can recompute any unit's arm from stored values alone. That holdout is what makes
"we caused this" a measurable claim instead of a marketing one.

**Not acting is a valid answer.** When the estimated uplift interval contains
zero, or the customer was likely to self-recover, RevTrace **abstains** and
records why. Contacting a customer who would have paid anyway costs money and
goodwill, and the system treats that as a real cost rather than a free upside.

Around this sits a payment-provider boundary — the Razorpay adapter, webhook
verification, and merchant attribution — built so that a verified webhook can
move a payment's state and nothing else can.

## 2. DEMO / SYNTHETIC / OFFLINE

**Read this before interpreting any number in this repository.**

- **No real Razorpay transaction has ever been processed by this system.**
- **No real customer data. No real payment. No real money.**
- **No external provider dependency is required to run the demo** — it works
  with an empty `.env`, no credentials, and no network access.
- The payment-provider responses in the demo come from a **deterministic
  synthetic provider** that implements the adapter's interface offline.
- Every metric in this repository is measured against **simulator-generated
  data with planted effects**. Recovering a planted effect validates the
  estimator; it says nothing about the world.

Every synthetic artifact carries a `DEMO` marker, and the provenance string
`DEMO / SYNTHETIC / OFFLINE — not a Razorpay transaction` travels with the data
so a screenshot cannot be mistaken for a capture.

What *is* real, and worth being precise about: the HMAC-SHA256 signature
verification, the merchant derivation, the idempotency constraint, the ordering
rules and the payment state machine are **production code**, exercised by the
demo rather than simulated by it. Only the bytes and the secret are synthetic.

## 3. Architecture

A clean modular monolith. No microservices.

```
┌───────────────────────────────────────────────────────────────────┐
│  frontend/           React 19 · Vite 7 · TypeScript · Tailwind 4  │
│  Page 1 Ledger  ·  Page 2 Evaluation  ─── committed fixtures      │
│  Page 3 Live demo ──────────────────────── /api (dev proxy)       │
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

### The causal / decision layer

`causal/` and `engine/` own every number. A five-fold cross-fitted T-learner over
empirical cell rates produces per-cell uplift with confidence intervals; the
policy engine turns those into one of a small set of decisions — act, abstain
with a named reason, or escalate. Money is carried as **integer minor units** and
rates as **integer basis points** throughout; no floating-point arithmetic
touches a money or probability path on either side of the wire (ADR 0001).

### The hypothesis / AI layer

`agents/hypothesis_agent.py` is the only component that talks to a language
model, and it holds no authority. It reads a structured evidence bundle,
proposes a falsifiable hypothesis about a cell, and cites integers. Deterministic
code then **checks every cited integer against the computed values** and marks
the hypothesis confirmed, refuted, or unsupported. A model that invents a number
is caught by arithmetic, not by trust.

### The Razorpay adapter

All provider code lives in `app/integrations/razorpay/` and nothing outside that
package sees a Razorpay request or response shape. `mapper.py` is the single
place a provider event name becomes a RevTrace `EventType` — an unmapped event is
**refused, never guessed**, because a timeline is evidence.

### Webhook verification

Raw bytes in, signature checked, *then* parsed. The owning merchant is derived
from the signed payment id, never from the caller. See [Security](#5-security).

### Database separation

Three databases with three different jobs — see [Databases](#7-databases).

## 4. The demo flow, exactly

One click runs this end to end. Every arrow is a real call into the code named.

| # | Step | What actually runs |
|---|---|---|
| 1 | Synthetic failed payment | A `DEMO`-marked merchant, customer, order and **failed** payment attempt — the recovery opportunity |
| 2 | Recovery decision → payment-link adapter interface | `payment_links.build_request` + `create_payment_link` — **production code** |
| 3 | Synthetic provider response | `DemoPaymentLinkClient`, implementing the SDK's `payment_link.create` surface **offline** |
| 4 | Signed synthetic webhooks | Three deliveries, each signed with a genuine **HMAC-SHA256 over the exact bytes to be delivered** |
| 5 | HMAC verification | `verify_signature` over raw bytes, before any parsing |
| 6 | Merchant derivation | `payment_attempts → orders → merchant_id`, reached from the **signed** payment id |
| 7 | Event mapping + persistence | `payment.failed → payment.failed`, `payment.captured → payment.captured`, **`payment_link.paid → order.paid`** |
| 8 | Payment state update | The attempt advances `failed → captured`, forward only |
| 9 | Duplicate webhook rejection | The same three delivered again — `duplicate detected`, row count unchanged |
| 10 | Tampered webhook rejection | The amount altered after signing — **REFUSED** |
| 11 | Foreign merchant rejection | Another merchant claims the payment — **REFUSED** |
| 12 | Rollback | `Rolled back — nothing persisted.` |

Steps 10 and 11 are **successes**, and the UI renders them that way. They are
security controls doing their job, and showing them as errors would invert what
the demo exists to prove.

## 5. Security

**Raw bytes are verified before JSON parsing.** Razorpay signs the exact octets
it sent; a parsed-then-re-serialised body is different octets. The route reads
`await request.body()`, verifies, and only then calls `json.loads`.

**HMAC-SHA256, timing-safe.** Verification goes through the official SDK's
`verify_webhook_signature`, which compares with `hmac.compare_digest`. The
verifier returns `None` on success and **raises on every failure**, so a caller
cannot mistake a falsy return for a pass.

**Merchant identity is derived from signed data.** The owning merchant is reached
through `PaymentAttempt → Order → merchant_id` from the signed payment id.

**`merchant_id` is an assertion, not a selection.** The optional query parameter
is checked *against* the derivation and can only cause a rejection. This was a
real, demonstrated defect: when the parameter was authoritative, a validly-signed
webhook about one merchant's payment could be filed under another, pointing at
the victim's order and advancing the victim's payment attempt. Existence-checking
the merchant caught none of it, because the merchant existed — it simply was not
the one that owned the payment. Fixed, and regression-tested.

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
authentication in the system. Bind to localhost. This is a documented boundary,
not an oversight — see [Limitations](#11-limitations).

## 6. AI, and what it is not allowed to do

RevTrace uses exactly one model, on a **free-only provider lock**:

| | |
|---|---|
| Provider | OpenRouter — the only provider ever constructed |
| Model | `nvidia/nemotron-3-super-120b-a12b:free` |
| Fallback | **None.** Zero paid-model fallback, by construction |

The lock is fail-closed. Whether a provider is free is an explicit declaration
that **defaults to false**; the `:free` suffix only corroborates it and never
infers it. `free_only_chain()` is the only production constructor, and it refuses
at construction time rather than at call time — a 429 on a free model is exactly
when a naive chain would silently fail over to a paid one.

**The model's live result, stated accurately.** One live call succeeded. It
proposed a hypothesis about the cell `card_declined|card` — claim
`higher_uplift_than_population` — and cited eleven integers. Deterministic code
then checked all eleven against the computed cell statistics: **all eleven
matched, status `confirmed`.** No value was fabricated.

Three subsequent live calls failed, and are reported as failures rather than
retried into a success: one returned empty content, one returned a 200 whose body
carried an error and no choices, and one was a `502 Upstream error`. Each
produced a diagnostic improvement. **Whether the full 45-cell payload is viable
within the 2,048-token budget remains unknown** — the 502 never reached the
model.

**None of this is required for the demo.** The browser demo makes no model call.

## 7. Databases

Three databases, three jobs. The separation is enforced in code, not by
convention.

| Database | Role | Rule |
|---|---|---|
| `revtrace_test` | Ephemeral test **and demo** database | Every test and the demo run inside a transaction that is **always rolled back**. No row survives. The harness never drops, truncates or recreates anything. |
| `revtrace_hypothesis_test` | Persistent **canonical benchmark** population | The materialised N=10,000 experiment every measured claim rests on. Read-only for everything else; the demo refuses it **by name**. |
| `revtrace_dev` | The application database | What `DATABASE_URL` names and what `uvicorn` serves. The test suite and the demo both refuse it by name; no test has ever written to it. |

`DEMO_DATABASE_URL` is a **separate setting from `DATABASE_URL`**, and it is
empty by default — the demo endpoint reports itself disabled rather than choosing
a database on its own. `revtrace_dev` and `revtrace_hypothesis_test` are refused
whatever it holds, and there is **no HTTP equivalent of `run_demo.py --commit`**:
the endpoint takes no such parameter and rolls back in a `finally`.

No connection string, username or password appears anywhere in this repository.
Every documented DSN uses placeholders.

## 8. Verified test results

Measured on the current tree:

| Check | Result |
|---|---|
| **Full test suite** | **4,573 passed · 0 failed · 0 skipped** (11m 43s) |
| Integration-adapter suite (`tests/integrations`) | 208 passed |
| `ruff check` | clean |
| `ruff format --check` | clean, 223 files |
| `mypy app` | Success — 114 source files |
| Frontend `npm run typecheck` | clean |
| Frontend `npm run build` | clean |

Nothing is skipped, and no test is marked expected-to-fail.

## 9. Running it locally

Replace `USER` with your PostgreSQL role. Nothing below needs a credential of any
kind — no Razorpay key, no AI key.

### Prerequisites

| Tool | Required |
|---|---|
| Python | **3.13.x** (pinned in `backend/.python-version`) |
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

### Run the backend

```bash
cd backend
DEMO_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
```

Without `DEMO_DATABASE_URL` everything still runs; only the demo endpoint reports
itself disabled.

### Run the frontend

```bash
cd frontend
npm run dev                                       # http://localhost:5173
```

### The offline demo — terminal

No server needed, no credentials, nothing kept:

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/python run_demo.py
```

### The offline demo — browser

Start the backend and frontend as above, open **http://localhost:5173**, and
click **Live demo → ▶ Run Demo**.

The Vite dev server proxies `/api` to `localhost:8000`, so the browser sees a
single origin and the backend needs no CORS middleware.

### The evaluation benchmark

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
    .venv/bin/python run_acceptance.py
```

Materialises ~70,000 rows inside one transaction and rolls back in a `finally`.
It writes exactly two files — `docs/EVALUATION.md` and `docs/evaluation.json` —
and touches nothing else.

### Tests

```bash
cd backend
.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest -m "not db"  # hermetic only; no PostgreSQL needed
```

## 10. Deploying it

The demo runs entirely on a laptop and needs nothing hosted. If you want it
reachable, the buildathon-shaped deployment is:

| Component | Host |
|---|---|
| Frontend | **Vercel** |
| Backend | **Render** |
| PostgreSQL | **Supabase**, Neon, or Render's managed PostgreSQL |

**A cloud backend cannot reach your laptop's PostgreSQL.** `localhost:5432` on
Render means *Render's* localhost, where nothing is listening. The database must
be a hosted instance with a public connection string, supplied to the backend as
`DATABASE_URL`. This is the single most common way this deployment fails.

Three more things that are easy to get wrong:

**The Vite proxy is development-only.** `server.proxy` exists in the dev server
and is absent from a production build. Deployed, the frontend's `/api` requests
must be routed some other way — a Vercel rewrite from `/api/*` to the Render URL
is the smallest change and keeps the single-origin property, so no CORS
middleware is needed. Pointing the frontend at an absolute backend URL instead
*would* require adding CORS to the backend.

**Run the migrations against the hosted database** before first boot:
`alembic upgrade head` with `DATABASE_URL` set to the hosted DSN. The current
head is `9c41e07b2d58`.

**Decide deliberately whether the demo endpoint is enabled.** Leaving
`DEMO_DATABASE_URL` unset disables it. If you do enable it, point it at a
throwaway database — never at the one `DATABASE_URL` names.

Set `APP_ENV=production` to disable `/docs` and `/redoc`.

## 11. Limitations

Stated plainly, because a system that claims to prove things has to be honest
about what it has not proven.

- **Razorpay has never processed a transaction for this system.** The adapter is
  written against the official SDK and the official documentation, and it has
  been exercised only against a synthetic offline provider. Test Mode
  integration is prepared, not performed.
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
- `revtrace_dev` is currently one migration behind head. It is not used by tests
  or the demo, so nothing depends on it.

## 12. Judge's demo script

Two minutes, four clicks.

**Before you start** — two terminals, as in [§9](#9-running-it-locally):
`uvicorn` with `DEMO_DATABASE_URL` set, and `npm run dev`. Then open
**http://localhost:5173**.

| # | Click | What to look for |
|---|---|---|
| 1 | **Incrementality ledger** (opens here) | Three figures. Gross recovered, incremental recovered, and **credited-not-earned** — the gap, which is the money a conventional dashboard would have claimed. That gap is the pitch. |
| 2 | **Evaluation** | Cross-fitted uplift, the Qini curve, quadrant labels — and a limitations section carrying equal weight. Note that an undefined Qini coefficient renders as `undefined`, never as `0`. |
| 3 | **Live demo → ▶ Run Demo** | Six steps appear. Watch step 4: the payment attempt advances `failed → captured`. Watch step 5: the same webhooks delivered again change nothing. |
| 4 | **Scroll to step 6** | Two attacks, both **REFUSED**, both green. A tampered body and a cross-tenant webhook. The second was a real defect that was found and fixed. |

**The closing line is on screen:** *Rolled back — nothing persisted.*

Three things worth asking about:

- *"Is any of this real?"* — No, and the page says so before you press anything.
  The provider response and the webhook secret are synthetic. The signature
  verification, merchant derivation, idempotency and state machine are production
  code.
- *"What happens when it isn't sure?"* — It abstains, and records which of twelve
  named reasons applied. Not acting is a first-class outcome.
- *"What does the AI decide?"* — Nothing. It proposes a hypothesis and cites
  integers; deterministic code checks every one against the computed values.

## Repository layout

```
.
├── README.md              This file
├── CLAUDE.md              Working agreement for coding agents
├── .env.example           Environment template — no values, ever
├── docs/
│   ├── architecture.md    What was actually built, and why
│   ├── EVALUATION.md      Generated evaluation (synthetic/demo)
│   ├── evaluation.json    The same report as structured data
│   ├── gate_comparison.json   Gate/spend figures, kept separate from causal
│   ├── EXPERIMENT_DESIGN.md   Pre-registration — not amended after the fact
│   ├── BREAKAGE.md        What actually broke, in order, with costs
│   ├── contracts/         API and fixture contracts
│   └── decisions/         ADRs
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
│   ├── alembic/           Migrations (head: 9c41e07b2d58)
│   └── tests/             4,573 tests, plus the benchmark harness
├── frontend/              React 19 · Vite 7 · TypeScript · Tailwind 4
└── simulator/             Synthetic event generator, potential outcomes
```

The frontend has **no charting library**. Its visualisations are laid out with
CSS from the report payload; every visible number comes from the backend through
a formatter, and nothing on screen is derived from layout geometry.

## Measurement honesty

`docs/EVALUATION.md` carries its own limitations section and is the authoritative
source for every figure. **This README deliberately quotes no result from it** —
one number in two places is one number that will drift.

Scoring formulas are documented and tested. None is presented as a scientifically
validated prediction, and two tests enforce that the synthetic/demo labelling
stays in place.
