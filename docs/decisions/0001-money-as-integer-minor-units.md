# 0001 — Money as integer minor units

- **Status:** Accepted
- **Date:** 2026-08-26
- **Phase:** 1

## Context

RevTrace's central claim is that revenue calculations are deterministic and
reproducible — the specification puts revenue math, risk math, and
expected-recovery math firmly on the deterministic side of the authority
boundary, explicitly out of the LLM's reach.

That claim only holds if the arithmetic itself is exact. IEEE-754 floats are
not: `0.1 + 0.2 != 0.3`, and the error is silent. A float that enters a money
path early produces figures that cannot be reproduced from the same inputs,
which would undermine both the audit trail and the Phase 11 evaluation.

Razorpay's own API works in minor units (paise), so storing major units would
also mean converting on every provider boundary crossing.

## Decision

All monetary values are stored and computed as **integer counts of minor
units** — paise for INR.

- Database columns are `BigInteger`, never `Float` or `Numeric`.
  `BigInteger` rather than `Integer` because INR paise overflow a 32-bit
  column at roughly ₹21 crore, which is not a safe ceiling for aggregates.
- `app/core/money.py` is the only place conversion happens. `to_minor()`
  **raises `TypeError` on a float argument** rather than converting it.
- Parsing goes through `Decimal`, never `float`.
- Rounding mode is an explicit parameter. There is no implicit default at the
  call site.
- Rates and probabilities are integers in **basis points** (0–10000), not
  floats, so that a policy threshold comparison is exact.

`tests/test_models_metadata.py::test_no_float_or_numeric_anywhere` asserts that
no float-typed column exists anywhere in the schema. It will fail the build if
one is ever added.

## Consequences

**Easy:** exact arithmetic; reproducible expected-recovery figures; direct
mapping to Razorpay's representation; no rounding surprises in the audit trail.

**Harder:** every display path must convert for humans — `format_money()`
exists for this. Percentages must be expressed in basis points, which reads
less naturally than `0.375` until you are used to it.

**Ruled out:** passing floats into any money function. This is enforced, not
merely documented.

**Deferred:** zero-decimal currencies (JPY, KRW) need a scale of 1 rather than
100. `MINOR_UNITS_PER_MAJOR` is structured to accommodate them, but none are
supported yet and adding one requires revisiting `format_money()`.

## Alternatives considered

**`Decimal` columns (`NUMERIC`).** Exact, and PostgreSQL supports it well. Lost
because it still admits a float at the Python boundary through implicit
conversion, and because Razorpay speaks minor units — we would convert on every
call anyway. Integer minor units make the invalid state unrepresentable rather
than merely discouraged.

**Floats with careful rounding.** Rejected outright. "Careful" is not a property
a schema can enforce, and the failure mode is silent.
