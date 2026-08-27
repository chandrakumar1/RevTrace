# RevTrace Frontend

React · Vite · TypeScript · Tailwind CSS · Recharts

**Status: not yet scaffolded.** No `package.json`, nothing installed. The app
itself is built in **Phase 10** — but the data contract it consumes is settled
now, so components can be written against real fixtures immediately.

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

Node 24.19.0 and npm 11.17.0 are available on this machine. npm is the package
manager — pnpm, yarn, and bun are not installed and are not being added.

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
