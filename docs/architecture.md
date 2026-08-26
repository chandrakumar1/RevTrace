# RevTrace — Architecture

**Status: placeholder.** Filled in from Phase 1 onward, one section per
milestone as the corresponding component is actually built. Nothing below is
implemented yet.

The authoritative specification is [../CLAUDE.md](../CLAUDE.md). This document
records what was *actually built* and why it diverged, if it did.

---

## Shape

A clean modular monolith. No microservices.

- `backend/` — Python 3.13, FastAPI, SQLAlchemy, Pydantic, PostgreSQL, Alembic
- `frontend/` — React, Vite, TypeScript, Tailwind, Recharts (Phase 10)
- `simulator/` — synthetic event generator, dev/eval only (Phase 2)

## Core pipeline

```
EVENT INGESTION
  -> DETECTION
  -> REVENUE RISK
  -> REVENUE LEAK GRAPH
  -> AI DIAGNOSIS
  -> INTERVENTION SIMULATION
  -> POLICY GATE
  -> EXECUTION
  -> VERIFICATION
  -> AUDIT
  -> METRICS
```

## The authority boundary

The single most important rule in this system: **the LLM is not the authority
over money.**

| LLM may do | Deterministic code must own |
|---|---|
| Diagnosis | Revenue calculations |
| Evidence interpretation | Risk calculations |
| Reasoning | Expected-recovery calculations |
| Explanation | Policy enforcement |
| Recommendation among *permitted* actions | Limits, retry counts, spend/discount caps |
| Communication drafting | Stopping rules |
| | Execution authorization |
| | Verification |
| | Metrics |

The LLM never directly executes a Razorpay operation. Every financial action
passes through:

```
AI recommendation -> deterministic validation -> policy engine -> approved action
    -> Razorpay adapter -> verification -> audit log
```

Structurally this means `app/agents/` may only ever *return structured
recommendations*. `app/engine/` and `app/services/` decide and act.

## Sections to be written

- [ ] Data model and entity relationships (Phase 1)
- [ ] Simulator design and scenario catalogue (Phase 2)
- [ ] Detection rules and thresholds (Phase 3)
- [ ] Revenue Leak Graph construction and timeline reconstruction (Phase 4)
- [ ] AI evidence contract and structured output schemas (Phase 5)
- [ ] Counterfactual recovery engine and scoring formulas (Phase 6)
- [ ] Policy engine rules and escalation paths (Phase 7)
- [ ] Razorpay adapter boundary (Phase 8)
- [ ] Execution and webhook verification, idempotency strategy (Phase 9)
- [ ] Frontend view model (Phase 10)
- [ ] Evaluation methodology and held-out set (Phase 11)
- [ ] Failure injection matrix and observed behaviour (Phase 12)
