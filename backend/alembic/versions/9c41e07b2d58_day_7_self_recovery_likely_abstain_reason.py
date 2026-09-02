"""day 7: self_recovery_likely is a valid abstain reason

Adds one value to the `recovery_cases.abstain_reason` vocabulary and rewrites
the single CHECK constraint that enumerates it. Nothing else changes: no
column, no index, no default, no other table.

**Why the vocabulary was short by one (DR-4).** The gate had no branch for
`Quadrant.GRAY_ZONE`. `QUADRANT_ABSTENTIONS` names Sleeping Dog, Sure Thing and
Lost Cause, so a Gray Zone unit passed the quadrant rule, and the only
gray-zone-aware branch after it tested `interval_contains_zero` — true of one
gray-zone population and false of the other. The other one fell through every
remaining rule and acted.

On the accepted seed=42 N=10,000 population that was **1,757 of 4,124 actions**,
and they are a single homogeneous population, measured rather than assumed:

    ci_low > 0                          True   for all 1,757
    contains_zero                       False  for all 1,757
    qualified                           True   for all 1,757
    p_control >= self_recovery_ceiling  True   for all 1,757

So the effect is real, positive, and statistically distinguishable from zero.
What is wrong with acting is not the evidence but the value: the control arm
recovers at or above the fold's self-recovery ceiling (~3,646-3,779 bps), so
most of what the action would be credited with was going to happen anyway.

**Why a new value rather than an existing one.** Every existing reason would be
a false statement about these units. `uplift_not_significant` is untrue — the
interval excludes zero. `insufficient_sample` is untrue — the cell qualified.
`negative_net_value` is untrue — the cost-recovery rule passes. `sure_thing`
is the closest in spirit but is a quadrant label these units do not carry, and
reusing it would make the reason disagree with the stored quadrant. A reason
code that misdescribes its own decision is worse than a migration.

**The downgrade refuses once any such row exists.** PostgreSQL validates a CHECK
on creation, so restoring the eleven-value constraint fails outright against a
stored `self_recovery_likely`. There is no honest value to rewrite those rows
to — every candidate is one of the false statements above — so failing is the
correct behaviour rather than a limitation. Safe while no such row exists.

Revision ID: 9c41e07b2d58
Revises: adf84244709a
Create Date: 2026-09-01 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c41e07b2d58"
down_revision: str | Sequence[str] | None = "adf84244709a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "abstain_reason_valid"
TABLE = "recovery_cases"

#: Written out rather than derived from `AbstainReason`. A migration records
#: what the schema looked like at a point in time; importing the enum would make
#: this file silently follow future edits and stop describing its own revision.
REASONS_BEFORE = (
    "negative_uplift",
    "uplift_not_significant",
    "negative_net_value",
    "sleeping_dog",
    "sure_thing",
    "lost_cause",
    "insufficient_sample",
    "contact_budget_exhausted",
    "customer_opted_out",
    "regulatory_block",
    "holdout_arm",
)

REASONS_AFTER = (
    "negative_uplift",
    "uplift_not_significant",
    "negative_net_value",
    "sleeping_dog",
    "sure_thing",
    "lost_cause",
    "self_recovery_likely",
    "insufficient_sample",
    "contact_budget_exhausted",
    "customer_opted_out",
    "regulatory_block",
    "holdout_arm",
)


def _condition(reasons: Sequence[str]) -> str:
    values = ", ".join(repr(reason) for reason in reasons)
    return f"abstain_reason IS NULL OR abstain_reason IN ({values})"


def upgrade() -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_{CONSTRAINT}"), TABLE, type_="check")
    op.create_check_constraint(op.f(f"ck_{TABLE}_{CONSTRAINT}"), TABLE, _condition(REASONS_AFTER))


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_{CONSTRAINT}"), TABLE, type_="check")
    # Fails with a check violation if any row stores 'self_recovery_likely'.
    # Deliberate: see the module docstring.
    op.create_check_constraint(op.f(f"ck_{TABLE}_{CONSTRAINT}"), TABLE, _condition(REASONS_BEFORE))
