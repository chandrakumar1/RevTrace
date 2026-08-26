# RevTrace — Master Engineering Specification

## Project

Name: RevTrace

Track: Razorpay Buildathon — Track 03: AI Revenue Recovery

## Mission

Build an AI-powered revenue recovery system that:

1. Detects revenue at risk.
2. Traces the events and evidence behind the loss.
3. Determines the likely root cause.
4. Evaluates multiple bounded recovery interventions.
5. Selects the safest/highest-value intervention.
6. Executes approved recovery actions using Razorpay Test Mode.
7. Verifies whether revenue was actually recovered.
8. Records a complete audit trail.
9. Measures recovery performance on a held-out dataset.
10. Handles failures safely.

Core product statement:

"RevTrace traces where revenue is leaking, determines why, recovers what is recoverable, and proves what happened."

## Core Product Concepts

### 1. Revenue Leak Graph

RevTrace must connect related:

* customers
* orders
* checkout events
* payment attempts
* payment failures
* successful payments
* subscription events
* recovery actions
* recovery outcomes

The system must be able to reconstruct a timeline showing how a revenue opportunity was lost.

### 2. Counterfactual Recovery Engine

For every significant revenue-risk case, RevTrace should evaluate multiple possible recovery strategies.

For each strategy calculate:

* expected recovery
* estimated action cost
* risk
* net expected recovery
* confidence
* applicable policy constraints

The system should prefer the strategy with the strongest expected value subject to safety and policy constraints.

## Critical Architecture Rule

The LLM is NOT the authority over money.

### The LLM may perform:

* diagnosis
* evidence interpretation
* reasoning
* explanation
* recommendation among permitted actions
* communication drafting

### Deterministic code must control:

* revenue calculations
* risk calculations
* expected-recovery calculations
* policy enforcement
* limits
* retry counts
* spending/discount limits
* stopping rules
* execution authorization
* verification
* metrics

The LLM MUST NEVER directly execute arbitrary Razorpay operations.

Every financial action must pass through:

AI recommendation
→ deterministic validation
→ policy engine
→ approved action
→ Razorpay adapter
→ verification
→ audit log

## Initial Recovery Scenarios

The first implementation should support:

### Scenario A — Repeated payment failure

Example:

Customer attempts payment twice within a defined window and fails.

RevTrace should:

* identify the case
* calculate revenue at risk
* investigate history
* compare recovery options
* apply policy
* execute an allowed recovery workflow
* verify outcome

### Scenario B — Checkout abandonment

A customer starts checkout but does not complete payment.

RevTrace should identify the opportunity and determine whether a recovery action is appropriate.

### Scenario C — Subscription payment failure

A recurring payment fails and revenue is at risk.

RevTrace should determine whether retry, notification, payment-link recovery, or escalation is appropriate.

### Scenario D — Payment degradation

Detect an unusual increase in payment failures relative to a historical baseline.

RevTrace should identify affected payment segments and estimate the revenue impact.

## Razorpay Integration

Use Razorpay Test Mode.

The integration must be isolated behind an adapter layer.

Recommended structure:

backend/app/integrations/razorpay/

* client.py
* orders.py
* payments.py
* payment_links.py
* subscriptions.py
* webhooks.py
* mapper.py

Do not spread Razorpay-specific API code throughout business logic.

The rest of the application should depend on our own service interfaces.

## Webhook Requirements

Webhook processing must be:

* signature-validated
* idempotent
* persistence-backed
* tolerant of duplicate delivery
* tolerant of delayed events
* tolerant of out-of-order events

Never assume a webhook is delivered exactly once.

Where immediate critical verification is required, perform API-side verification as appropriate.

## Security Requirements

Never expose Razorpay secrets to the frontend.

Secrets belong only in environment variables.

Required environment variables will eventually include:

RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET
RAZORPAY_WEBHOOK_SECRET
GEMINI_API_KEY
DATABASE_URL

Never commit .env.

Commit only .env.example.

## Recommended Architecture

Frontend:

* React
* Vite
* TypeScript
* Tailwind CSS
* Recharts

Backend:

* Python
* FastAPI
* SQLAlchemy
* Pydantic
* PostgreSQL
* Alembic

AI:

* Gemini through a dedicated AI service layer

Core pipeline:

EVENT INGESTION
→ DETECTION
→ REVENUE RISK
→ REVENUE LEAK GRAPH
→ AI DIAGNOSIS
→ INTERVENTION SIMULATION
→ POLICY GATE
→ EXECUTION
→ VERIFICATION
→ AUDIT
→ METRICS

## Recommended Backend Structure

backend/

app/
main.py

```
api/
    routes/
    dependencies.py
    router.py

core/
    config.py
    logging.py
    security.py

models/

schemas/

repositories/

services/
    ingestion/
    detection/
    tracing/
    recovery/
    verification/
    audit/

engine/
    risk_engine.py
    recovery_engine.py
    policy_engine.py
    scoring.py

agents/
    diagnosis_agent.py
    intervention_agent.py
    explanation_agent.py

integrations/
    razorpay/
    notifications/

db/
```

tests/

alembic/

## Initial Database Entities

At minimum plan for:

### merchants

* id
* name
* currency
* timezone
* created_at

### customers

* id
* merchant_id
* external_customer_id
* name
* email
* phone
* lifetime_value
* created_at

### orders

* id
* merchant_id
* customer_id
* external_order_id
* amount
* currency
* status
* created_at

### payment_attempts

* id
* order_id
* customer_id
* amount
* payment_method
* provider
* status
* failure_code
* failure_reason
* attempt_number
* created_at

### events

* id
* merchant_id
* customer_id
* order_id
* event_type
* payload
* occurred_at

### revenue_risks

* id
* merchant_id
* customer_id
* order_id
* risk_type
* amount_at_risk
* confidence
* detected_at
* status

### recovery_cases

* id
* risk_id
* strategy
* expected_recovery
* max_cost
* policy_status
* execution_status
* actual_recovery
* created_at
* completed_at

### recovery_actions

* id
* case_id
* action_type
* parameters
* approved
* executed
* result
* created_at

### audit_events

* id
* case_id
* actor
* action
* reason
* input_snapshot
* output_snapshot
* created_at

## Deterministic Risk Engine

The first implementation should NOT depend on an LLM for numerical risk calculations.

Build deterministic functions for:

* amount at risk
* recovery probability
* expected recovery
* intervention cost
* net expected recovery
* confidence thresholds
* policy eligibility

The exact scoring formulas should be documented and tested.

Do not pretend that an arbitrary score is a scientifically validated prediction. Clearly label synthetic/demo scoring as such.

## AI Output Requirements

AI responses must use structured schemas.

AI should receive structured evidence instead of unrestricted database access.

Example information:

* order amount
* customer history
* failure count
* failure reasons
* timeline
* candidate actions
* calculated risk
* policy constraints

AI should return structured information such as:

* diagnosis
* evidence
* recommended action
* reasoning
* confidence
* escalation requirement

The AI must be capable of responding with insufficient evidence / escalation instead of fabricating certainty.

## Policy Engine

Every action must pass explicit policy checks.

Examples:

* maximum retries
* maximum discount
* maximum customer contacts
* minimum confidence
* maximum action cost
* customer opt-out
* merchant preferences
* maximum total recovery attempts
* escalation requirements

Policy violations must result in rejection or escalation, not silent override.

## Simulator

Build a synthetic event generator before relying heavily on live Razorpay activity.

The simulator should support realistic scenarios such as:

* successful payments
* repeated failures
* UPI-like timeout scenarios
* bank declines
* checkout abandonment
* subscription failures
* high-value customers
* false positives
* successful recovery
* failed recovery
* delayed webhook
* duplicate webhook
* out-of-order webhook
* unsafe AI recommendation

The simulator should eventually generate thousands of events for evaluation.

## Evaluation

RevTrace must report real measurements.

At minimum track:

* number of cases
* detected cases
* true positives
* false positives
* detection precision
* amount at risk
* amount recovered
* recovery rate
* expected vs actual recovery
* intervention cost
* false intervention count
* escalation count
* policy violations prevented

Use a held-out evaluation set.

Do not cherry-pick examples.

Synthetic/demo metrics must be explicitly labeled as synthetic/demo evaluation.

## Failure Engineering

The project must intentionally test failures.

Examples:

* Razorpay API failure
* network timeout
* delayed webhook
* duplicate webhook
* missing webhook
* out-of-order webhook
* malformed webhook
* invalid AI recommendation
* policy violation
* repeated retry condition

Expected principle:

When uncertain or unsafe, RevTrace should stop, escalate, or choose a bounded fallback rather than continue indefinitely.

## Frontend

The frontend should eventually include:

### Overview

* revenue at risk
* potentially recoverable revenue
* recovered revenue
* recovery rate
* active cases
* revenue leakage by cause

### Leak Explorer

Show major revenue leak categories and affected revenue.

### Investigation View

For one case show:

* customer
* order
* amount
* timeline
* payment attempts
* evidence
* root-cause explanation
* confidence

### Recovery Simulator

Show candidate interventions:

* expected recovery
* estimated cost
* risk
* confidence
* net expected recovery

### Execution Gate

Show:

* action
* parameters
* policy checks
* confidence
* approval status

Require an explicit action before execution.

### Audit Trail

Show:

* detection
* diagnosis
* recommendation
* policy decision
* execution
* verification
* final result

## Development Rules

Do NOT build the entire project in one pass.

Work in milestones.

After each milestone:

1. inspect the existing code
2. implement only the agreed scope
3. run tests
4. fix failures
5. update documentation
6. explain architecture changes
7. verify that existing functionality still works

Do not make unrelated architectural changes.

Do not invent external API behavior.

Use official API documentation whenever external API details are required.

Do not introduce unnecessary microservices.

Prefer a clean modular monolith for the hackathon unless a strong technical reason requires otherwise.

## Milestones

### Phase 0

Repository and development environment inspection.

### Phase 1

Backend foundation and database.

### Phase 2

Synthetic event simulator.

### Phase 3

Deterministic revenue-risk detection.

### Phase 4

Revenue Leak Graph and timelines.

### Phase 5

AI diagnosis.

### Phase 6

Counterfactual recovery engine.

### Phase 7

Policy and safety engine.

### Phase 8

Razorpay Test Mode integration.

### Phase 9

Execution + webhook verification.

### Phase 10

Frontend dashboard.

### Phase 11

Evaluation benchmark.

### Phase 12

Failure injection and resilience testing.

### Phase 13

Demo hardening, documentation and final polish.

## First Task — IMPORTANT

Do NOT create the full application yet.

First inspect the current environment and repository.

Report:

1. Operating system
2. Python version
3. Node version
4. npm/pnpm/yarn availability
5. Git version
6. PostgreSQL availability
7. Docker availability
8. Existing files
9. Existing VS Code/project configuration
10. Available package managers
11. Existing environment variables, without exposing secret values
12. Any potential conflicts
13. Recommended initial setup

Then propose the Phase 0 repository structure.

DO NOT install large dependencies or implement product functionality until the environment inspection and Phase 0 plan are complete.

Wait for the user to approve Phase 0 before proceeding.
