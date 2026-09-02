# RevTrace Frontend

React 19 · Vite 7 · TypeScript · Tailwind CSS 4

**No charting library.** The visualisations are laid out with CSS from the
report payload. Every visible number comes from the backend through a formatter,
and nothing on screen is derived from layout geometry.

**Status: three pages.** The incrementality ledger and the evaluation view read
committed fixtures; the live demo calls the backend. The remaining dashboard
views listed at the bottom of this file are still to come.

## Local development

```sh
cd frontend
npm install     # required once — nothing is installed yet
npm run dev     # Vite dev server
npm run build   # tsc -b && vite build
npm run typecheck
```

Requires **Node 20.19+** and **npm 10+**; written against Node 24 and npm 11.
**npm is the package manager** — no pnpm, yarn or bun lockfile is maintained.

Tailwind v4 is configured through `@tailwindcss/vite` and a CSS-first
`@import "tailwindcss"` in `src/index.css`. That is why there is deliberately no
`tailwind.config.js` and no `postcss.config.js`.

`@/` resolves to `src/`, in both `vite.config.ts` and `tsconfig.app.json`.

## Evaluation report fixtures

`src/fixtures/` holds four backend-generated reports, verified during the Day 5
closeout. **Do not hand-edit them** — regenerate from the backend, or they stop
being evidence of anything.

| Fixture | What it exercises |
|---|---|
| `report.10k.json` | The accepted N=10,000 run. Byte-identical to `docs/evaluation.json`. |
| `report.underpowered.json` | `is_underpowered = true` (298 per arm against a planned 384). |
| `report.qini_undefined.json` | `Q(N) = 0`, so the Qini coefficient is `null` — undefined, not zero. |
| `report.empty.json` | No sealed outcomes, so the backend refuses to estimate. |

`report.empty.json` has a **different shape** from the other three: there is no
report, only the refusal. The payload is a discriminated union on `available`,
and `isAvailable()` in `src/types/report.ts` is the narrowing helper. Branch
before reading any report field.

Two conventions run through the payload. Rates and effects are **integer basis
points** out of 10,000 (`1564` is 15.64%), and money is an **integer count of
minor units** (`447880605` is ₹4,478,806.05). `src/lib/format.ts` turns both
into strings by slicing the decimal representation, never by dividing — no
float touches a money or probability path, on either side of the wire.

`null` is never interchangeable with `0`. An undefined Qini coefficient means
there was no incremental recovery to apportion, which is a different statement
from a ranking that did no better than chance.

**The fixture pages still make no request.** Pages 1 and 2 read these files at
build time, as they always have. Page 3 — the live demo — is the only part of
the app that calls the backend; see below.

## Page 3 — the live demo

`src/pages/DemoPage.tsx` calls `POST /api/v1/demo/run` and renders the six steps
the backend returns. It is the only network code in the frontend, and it lives
in `src/lib/api.ts`.

```sh
# terminal 1 — backend, with the demo enabled
cd backend
DEMO_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \
  .venv/bin/uvicorn app.main:app --reload

# terminal 2 — frontend
cd frontend && npm run dev     # http://localhost:5173
```

Without `DEMO_DATABASE_URL` the endpoint reports itself disabled and the page
explains why instead of offering a button that would fail. The demo always rolls
back; there is no HTTP equivalent of `run_demo.py --commit`.

**Use relative `/api` paths only.** The Vite dev server proxies `/api` to
`localhost:8000` so the browser sees one origin. That is why the backend has no
CORS middleware — an absolute `http://localhost:8000` URL would bypass the proxy
and be refused by the browser.

**Every string on that page comes from the backend.** The page makes claims
about which code path is production and which response is synthetic, and a
claim composed in the browser would be one nobody verified.

## Phase 2 simulator contract

The app itself is built in **Phase 10** — but the simulator data contract was
settled in Phase 2, so components can be written against real fixtures.

## Build against this now

- **Contract:** [`docs/contracts/simulation-fixture.md`](../docs/contracts/simulation-fixture.md)
  — full field reference plus a suggested TypeScript shape.
- **Sample data:** [`src/fixtures/sample_S04_seed42.json`](src/fixtures/sample_S04_seed42.json)
  — a real, deterministic `frontend.json` from the repeated-payment-failure
  scenario.
- **Generate more:** `cd backend && .venv/bin/python -m simulator generate S05 --seed 7`
  then read `simulator/output/S05_seed7/frontend.json`. `python -m simulator list`
  shows all 17 scenarios.

Two fields are present and explicitly `null` in Phase 2 — `revenue_risk`
(filled by Phase 3) and `recovery_state` (Phases 6–9). Build the components and
leave them empty; their position in the shape will not change.

Money arrives as **integer minor units** under `*_minor` keys. Formatting is the
frontend's job — never do arithmetic on a formatted string, and never introduce
a float into a money path.

npm is the package manager — no pnpm, yarn or bun lockfile is maintained.

## Planned views

| View | Shows |
|---|---|
| **Overview** | Revenue at risk, potentially recoverable revenue, recovered revenue, recovery rate, active cases, leakage by cause |
| **Leak Explorer** | Major revenue leak categories and affected revenue |
| **Investigation** | One case: customer, order, amount, timeline, payment attempts, evidence, root-cause explanation, confidence |
| **Recovery Simulator** | Candidate interventions with expected recovery, estimated cost, risk, confidence, net expected recovery |
| **Execution Gate** | Action, parameters, policy checks, confidence, approval status — requires an explicit human action before execution |
| **Audit Trail** | Detection, diagnosis, recommendation, policy decision, execution, verification, final result |

## Hard rule

**No Razorpay secret ever reaches the frontend.** `RAZORPAY_KEY_SECRET` and
`RAZORPAY_WEBHOOK_SECRET` are backend-only. The frontend talks exclusively to
the RevTrace backend API.

Anything displayed as a metric from simulator data must be labeled
synthetic/demo in the UI itself, not only in documentation.
