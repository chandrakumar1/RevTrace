# RevTrace Frontend

React · Vite · TypeScript · Tailwind CSS · Recharts

**Status: Phase 0 placeholder.** Empty. Scaffolded in **Phase 10**; nothing has
been installed and no `package.json` exists yet.

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
