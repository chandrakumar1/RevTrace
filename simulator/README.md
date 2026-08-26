# RevTrace Simulator

Synthetic event generator for development and evaluation.

**Status: Phase 0 placeholder.** Empty. Built in **Phase 2**.

## Why this is a top-level directory

The simulator is a development and evaluation tool, not a runtime service. It
lives outside `backend/app/` so that test-data generation can never be imported
by production code paths. It may read the backend's schemas; the backend must
never depend on it.

Generated output goes to `simulator/output/`, which is gitignored — generated
data is not source.

## Purpose

Build the simulator *before* relying heavily on live Razorpay activity. It must
eventually generate thousands of events for evaluation against a held-out set.

## Scenarios to support

**Payment outcomes**
- successful payments
- repeated failures
- UPI-like timeout scenarios
- bank declines

**Revenue-loss shapes**
- checkout abandonment
- subscription failures
- high-value customers
- false positives

**Recovery outcomes**
- successful recovery
- failed recovery

**Delivery pathologies** — the system must tolerate all of these
- delayed webhook
- duplicate webhook
- out-of-order webhook

**Safety**
- unsafe AI recommendation (must be caught by the policy engine, not obeyed)

## Ground truth

The simulator knows the true cause of every generated case. That ground truth
is what makes real measurement possible in Phase 11: true positives, false
positives, detection precision, expected vs. actual recovery, false
interventions.

Metrics derived from this data are **synthetic/demo** measurements and must be
labeled as such wherever they are reported. Do not cherry-pick examples.
