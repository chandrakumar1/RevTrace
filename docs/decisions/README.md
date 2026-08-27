# Architecture Decision Records

One file per decision, named `NNNN-short-title.md`, numbered sequentially.

The specification requires explaining architecture changes after each
milestone. ADRs are how that requirement is satisfied — a decision that alters
structure, a dependency, a boundary, or a safety property gets a record here.

Routine implementation choices do not need one.

## Template

```markdown
# NNNN — Title

- **Status:** Proposed | Accepted | Superseded by NNNN
- **Date:** YYYY-MM-DD
- **Phase:** N

## Context
What forced a decision. Constraints, and what was actually observed.

## Decision
What was chosen, stated plainly.

## Consequences
What this makes easy, what it makes hard, and what it rules out.

## Alternatives considered
What else was on the table and why it lost.
```

## Records

| # | Title | Status | Phase |
|---|---|---|---|
| [0001](0001-money-as-integer-minor-units.md) | Money as integer minor units | Accepted | 1 |
| [0002](0002-uuid-primary-keys.md) | UUID primary keys | Accepted | 1 |
| [0003](0003-status-columns-as-varchar-with-check-constraints.md) | Status columns as VARCHAR with CHECK constraints | Accepted | 1 |
| [0004](0004-simulator-emits-event-stream-not-database-writes.md) | The simulator emits an event stream, not database writes | Accepted | 2 |
| [0005](0005-simulator-does-not-generate-recovery-or-risk-entities.md) | The simulator generates no risk or recovery entities | Accepted | 2 |

Phase 0's decisions are recorded in
[../phase-0-environment.md](../phase-0-environment.md) §7 rather than as
separate ADRs, since they are environment findings rather than architecture
changes.
