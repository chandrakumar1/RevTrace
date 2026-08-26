# 0003 — Status columns as VARCHAR with CHECK constraints

- **Status:** Accepted
- **Date:** 2026-08-26
- **Phase:** 1

## Context

RevTrace has eleven controlled vocabularies — order status, payment status,
payment method, event type, risk type, risk status, recovery strategy, policy
status, execution status, action type, and actor.

Almost all of them will grow. Event types expand as the simulator gains
scenarios (Phase 2) and as Razorpay webhook types are mapped (Phase 8).
Recovery strategies expand with the counterfactual engine (Phase 6). Action
types expand as execution capabilities land (Phase 9).

The vocabularies also need to be enforced. Several of them gate money: a
`policy_status` outside the known set, or an `actor` the audit trail does not
recognise, is a correctness failure on a financial path — not a cosmetic one.

## Decision

Status and type columns are **`VARCHAR` with a named `CHECK` constraint**,
backed by a Python `StrEnum` in `app/models/enums.py`.

`enum_check()` in `app/models/mixins.py` renders the constraint from the enum's
own values, so the constraint text and the Python vocabulary have a single
source. `tests/test_enums.py` asserts, per column, that every enum member
appears in that column's CHECK constraint — Python and the database cannot
drift apart silently.

Two constraints go further than vocabulary enforcement and encode the
architecture rule itself:

- `ck_audit_events_execution_actor_never_ai` — an execution-class audit entry
  may not have `ai_agent` as its actor.
- `ck_recovery_cases_execution_requires_policy_approval` and
  `ck_recovery_actions_executed_requires_approved` — nothing reaches an
  executed state without policy approval.

`PolicyStatus` deliberately has no value meaning "overridden". The
specification requires that policy violations reject or escalate; silent
override must not be representable.

## Consequences

**Easy:** adding a vocabulary value is a one-line enum change plus a migration
that drops and recreates one CHECK constraint. The naming convention in
`app/db/base.py` gives every constraint a stable, referenceable name, which is
what makes that drop-and-recreate possible.

**Harder:** the database stores strings rather than a compact enum ordinal —
irrelevant at this scale. Application code must compare against
`SomeEnum.VALUE.value` rather than relying on the driver to coerce.

**Guaranteed:** an invalid status cannot be stored, by any caller, including a
future agent or a malformed webhook.

## Alternatives considered

**Native PostgreSQL `ENUM`.** The obvious choice, and the one Alembic
autogenerates by default. Lost on migration ergonomics: `ALTER TYPE ... ADD
VALUE` cannot run inside a transaction block in older PostgreSQL versions,
cannot remove or rename values without recreating the type and every dependent
column, and produces migrations that are awkward to reverse. With eleven
vocabularies expected to change across twelve remaining phases, that cost
compounds badly.

**Application-level validation only.** Rejected. The database is the last line
of defence on money paths, and the constraints above are precisely the ones we
least want to depend on caller discipline for.
