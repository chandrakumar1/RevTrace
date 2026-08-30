"""Materialising the synthetic population into revtrace_test.

The generator produces cases in memory. The estimators read sealed outcomes out
of a database. This is the only thing that connects them, and it lives under
`tests/` for a reason that is not organisational: **`app/` may not import the
generator**, enforced by a scan over every application file. Putting the bridge
in the application package would put the answer key one import away from the
estimator, and no amount of discipline afterwards would restore the guarantee.

So the direction is one-way. The bridge reads the generator and writes rows.
`app/causal/analysis.py` reads rows and has never heard of a segment.

**Recovery is derived, not copied.** The revealed outcome decides whether a
capture is planted on the timeline; a separate pure function then reads captures
back out of `[window_opens_at, window_closes_at)` and that is what lands in
`case_outcomes`. Two things follow. The window boundary is real — a capture
placed after the window closes does not count, and a test plants one to prove
it. And the definition is *money that arrived, regardless of cause*, which is
the only definition an experiment can use: a held-out unit never has a recovery
action, so anything keyed on one would score every control at zero and turn the
measured effect into the gross treated rate.

That is also why `risk_engine.recovered_amount()` is not used here. It returns
money captured **after a recovery action succeeded**, which is the right answer
for a recovery case and exactly the wrong one for a holdout.

**Truth stays on its own side.** `reveal()` collapses each case to the single
outcome its assigned arm produces and drops everything else; that is what feeds
the observed columns. The `truth_*` columns are written separately from the
case's own potential outcomes, and nothing in `app/causal/` may read them.

Nothing here creates a `recovery_case` or a `recovery_action`. No money moves,
nothing is approved, and the treated arm's "action" is a counter on the outcome
row rather than an execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from simulator.clock import SIMULATION_EPOCH
from simulator.potential_outcomes import PotentialOutcomeCase, generate
from simulator.segments import SEGMENTS, SEGMENTS_BY_ID, Action
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.experiments.assignment import assign_risk
from app.experiments.registry import (
    ExperimentDraft,
    create_draft,
    lock_experiment,
    start_experiment,
)
from app.experiments.windows import open_window, seal_due, window_for
from app.models import Customer, Experiment, Order, PaymentAttempt, RevenueRisk
from app.models.enums import Arm, OrderStatus, PaymentStatus, RiskStatus, RiskType

#: The committed benchmark seed.
BENCHMARK_SEED = 42

#: Benchmark policy for the treated arm. **Not** a recommender: Day 4 has no
#: model, so every treated unit gets the same action and the effect measured is
#: the effect of that one action.
BENCHMARK_ACTION = Action.CREATE_PAYMENT_LINK

#: An even split, as the pre-registration fixes for the benchmark.
BENCHMARK_HOLDOUT_BPS = 5_000
BENCHMARK_PLANNED_N_PER_ARM = 384
BENCHMARK_MDE_BPS = 1_000
BENCHMARK_SALT = "revtrace-demo-salt-v1"

#: Every case is a repeated payment failure, whose window is 72 hours.
BENCHMARK_RISK_TYPE = RiskType.REPEATED_PAYMENT_FAILURE.value

#: Lifecycle timestamps, derived from the simulation epoch rather than a clock.
#: Both precede the earliest case, because a running experiment cannot enrol a
#: unit detected before it started.
LOCKED_AT = SIMULATION_EPOCH - timedelta(hours=2)
STARTED_AT = SIMULATION_EPOCH - timedelta(hours=1)

#: Confidence the deterministic engine would have assigned. Constant here so it
#: contributes no spurious imbalance; the balance table reports it either way.
BENCHMARK_CONFIDENCE_BPS = 7_000

#: Database names the bridge will write to. A second guard behind the test
#: harness's own, so the bridge cannot be pointed at development data even when
#: called outside the fixture.
PERMITTED_DATABASE_MARKERS = ("revtrace_test", "_test")

#: Namespace for the run's derived identities.
BENCHMARK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "revtrace.benchmark")


def benchmark_experiment_id(seed: int, case_count: int) -> uuid.UUID:
    """The experiment's identity, derived from the run rather than drawn.

    This matters more than it looks. The arm is
    `sha256(risk_id : experiment_id : salt)`, so a randomly generated
    experiment id would re-randomise every unit on every run and the benchmark
    would produce a different estimate each time it was executed. Deriving the
    id from the seed and the case count makes the whole run — population,
    assignment, outcomes — reproducible end to end.
    """
    return uuid.uuid5(BENCHMARK_NAMESPACE, f"experiment:{seed}:{case_count}")


def benchmark_merchant_id(seed: int, case_count: int) -> uuid.UUID:
    return uuid.uuid5(BENCHMARK_NAMESPACE, f"merchant:{seed}:{case_count}")


def benchmark_experiment_name(seed: int, case_count: int) -> str:
    return f"BENCH-seed{seed}-n{case_count}"


# -- observable features --------------------------------------------------
#
# The generator draws a dozen covariates per case; the database only holds a
# feature that has a column to live in. Everything below exists to carry a
# *legitimately observable* field across that boundary faithfully — never a
# potential outcome, and never the stratum label.

#: The failure codes a payment can carry, sorted so the noise draw below is
#: reproducible. Read off the generator's declared strata, which is where the
#: vocabulary lives; the mapping from stratum to code is not itself persisted.
FAILURE_CODES: tuple[str, ...] = tuple(sorted({spec.failure_code for spec in SEGMENTS}))

#: Share of cases showing their characteristic failure code, in percent. The
#: rest draw uniformly from the whole vocabulary.
#:
#: Deliberately not 100. A code that identified the stratum outright would make
#: "the model rediscovers the heterogeneity" a lookup rather than a finding —
#: the same reason the generator only makes `payment_method` 70% characteristic.
CHARACTERISTIC_CODE_PERCENT = 70


def observed_failure_code(case: PotentialOutcomeCase) -> str:
    """The failure code a payment processor would have reported.

    Derived from the case id rather than drawn, so a rerun reproduces it. Uses
    a different slice of the digest from `capture_instant`, so the two are not
    correlated through a shared draw.

    A failure code is a genuinely observable field — it is on every real payment
    attempt. What must not leak is the *stratum*, and adding noise is what keeps
    the two apart: after this, no single code identifies a stratum and no
    stratum is identified by a single code.
    """
    draw = int(case.case_id.hex[8:12], 16) % 100
    if draw < CHARACTERISTIC_CODE_PERCENT:
        return SEGMENTS_BY_ID[case.segment_id].failure_code
    return FAILURE_CODES[int(case.case_id.hex[12:16], 16) % len(FAILURE_CODES)]


def detection_instant(case: PotentialOutcomeCase, case_index: int) -> datetime:
    """When the risk was detected, carrying the generator's own clock features.

    The generator draws `day_of_month` and `hour_of_day` as covariates — the
    salary-cycle mechanism depends on the first — but the earlier bridge stamped
    `detected_at` from a sequential counter, so both were lost on the way into
    the database and anything derived from the column described row order rather
    than the case.

    Rebuilt here so the two are genuinely recoverable. The minute component
    keeps cases from piling onto identical instants; it carries no signal.
    """
    covariates = case.covariates
    return (
        SIMULATION_EPOCH
        + timedelta(days=covariates.day_of_month - 1)
        + timedelta(hours=covariates.hour_of_day)
        + timedelta(minutes=case_index % 60)
    )


def customer_created_at(case: PotentialOutcomeCase, detected_at: datetime) -> datetime:
    """When the customer first transacted, from the generator's tenure draw."""
    return detected_at - timedelta(days=case.covariates.tenure_days)


class BridgeError(RuntimeError):
    """The population could not be materialised."""


def guard_test_database(session: Session) -> str:
    """Refuse to write anywhere but a test database. Returns its name."""
    name = session.execute(text("SELECT current_database()")).scalar_one()
    if not any(marker in name for marker in PERMITTED_DATABASE_MARKERS):
        raise BridgeError(
            f"refusing to materialise the benchmark into database {name!r}: "
            f"the name must contain one of {PERMITTED_DATABASE_MARKERS}. "
            "revtrace_dev must never hold synthetic benchmark data."
        )
    return name


# -- observed recovery ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedRecovery:
    """What the timeline says arrived inside the window."""

    recovered: bool
    amount: int
    at: datetime | None

    def __post_init__(self) -> None:
        # The same pairing `case_outcomes` enforces with CHECK constraints.
        if self.recovered and self.at is None:
            raise BridgeError("a recovery must carry the instant it arrived")
        if not self.recovered and self.amount:
            raise BridgeError("a non-recovery cannot carry an amount")


def captures_within(
    attempts: list[PaymentAttempt],
    opens_at: datetime,
    closes_at: datetime,
) -> list[PaymentAttempt]:
    """Captured attempts falling in the half-open observation window.

    Half-open on purpose: a capture exactly at `closes_at` is outside. The
    window covers up to its close, not through it, and `windows.is_late` draws
    the boundary in the same place.
    """
    return sorted(
        (
            attempt
            for attempt in attempts
            if attempt.status == PaymentStatus.CAPTURED.value
            and attempt.attempted_at is not None
            and opens_at <= attempt.attempted_at < closes_at
        ),
        key=lambda attempt: (attempt.attempted_at, attempt.attempt_number),
    )


def observed_recovery(
    attempts: list[PaymentAttempt],
    opens_at: datetime,
    closes_at: datetime,
) -> ObservedRecovery:
    """Money that arrived inside the window, **regardless of cause**.

    No reference to a recovery action, deliberately. A held-out unit never has
    one, so a definition that required it would score every control at zero and
    report the gross treated rate as the effect.
    """
    captured = captures_within(attempts, opens_at, closes_at)
    if not captured:
        return ObservedRecovery(recovered=False, amount=0, at=None)

    return ObservedRecovery(
        recovered=True,
        amount=sum(attempt.amount for attempt in captured),
        at=captured[0].attempted_at,
    )


def observed_recovery_for_risk(session: Session, risk: RevenueRisk) -> ObservedRecovery:
    """The same derivation, read back out of the database.

    Used by the tests to prove that what was stored matches what the timeline
    independently says, rather than trusting the write path to agree with
    itself.
    """
    opens_at, closes_at = window_for(risk.risk_type, risk.detected_at)
    attempts = list(
        session.execute(
            select(PaymentAttempt).where(PaymentAttempt.order_id == risk.order_id)
        ).scalars()
    )
    return observed_recovery(attempts, opens_at, closes_at)


def capture_instant(
    case: PotentialOutcomeCase,
    opens_at: datetime,
    closes_at: datetime,
) -> datetime:
    """A deterministic moment inside the window, derived from the case id.

    Not drawn at runtime: the whole run has to be reproducible from the seed,
    and a capture time that moved between runs would move the sealed outcome
    with it.
    """
    span = int((closes_at - opens_at).total_seconds())
    offset = int(case.case_id.hex[:8], 16) % span
    return opens_at + timedelta(seconds=offset)


# -- materialisation ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    """What one materialisation produced."""

    experiment_id: uuid.UUID
    merchant_id: uuid.UUID
    database: str
    seed: int
    case_count: int
    action: str
    treatment: int
    holdout: int
    windows_opened: int
    outcomes_sealed: int
    captures_planted: int
    risk_ids: tuple[uuid.UUID, ...]

    @property
    def enrolled(self) -> int:
        return self.treatment + self.holdout

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": str(self.experiment_id),
            "database": self.database,
            "seed": self.seed,
            "case_count": self.case_count,
            "action": self.action,
            "treatment": self.treatment,
            "holdout": self.holdout,
            "enrolled": self.enrolled,
            "windows_opened": self.windows_opened,
            "outcomes_sealed": self.outcomes_sealed,
            "captures_planted": self.captures_planted,
        }


def _merchant(session: Session, seed: int, case_count: int) -> uuid.UUID:
    merchant_id = benchmark_merchant_id(seed, case_count)
    session.execute(
        text(
            "INSERT INTO merchants (id, name, currency, timezone, created_at, updated_at) "
            "VALUES (:id, :name, 'INR', 'Asia/Kolkata', now(), now())"
        ),
        {"id": merchant_id, "name": f"Benchmark Merchant seed{seed} n{case_count}"},
    )
    session.flush()
    return merchant_id


def _experiment(session: Session, seed: int, case_count: int) -> Experiment:
    """A benchmark-scoped experiment, locked and started.

    Its own row, never EXP-001. The pre-registration in `docs/` stays DRAFT;
    this is a synthetic run against synthetic data and must not be mistaken for
    the registered experiment.

    The id is replaced with the derived one immediately after creation, while
    the row is still DRAFT and nothing references it. `create_draft` allocates
    its own, and re-implementing it here to pass one in would duplicate the
    registry's validation and let the two drift.
    """
    experiment = create_draft(
        session,
        ExperimentDraft(
            name=benchmark_experiment_name(seed, case_count),
            hypothesis=(
                "Creating a payment link on a repeated payment failure increases the "
                "probability of payment within 72 hours, relative to an untreated holdout."
            ),
            primary_metric="recovery_rate",
            holdout_bps=BENCHMARK_HOLDOUT_BPS,
            planned_n_per_arm=BENCHMARK_PLANNED_N_PER_ARM,
            mde_bps=BENCHMARK_MDE_BPS,
        ),
    )
    experiment.id = benchmark_experiment_id(seed, case_count)
    session.flush()

    lock_experiment(session, experiment, LOCKED_AT)
    start_experiment(session, experiment, STARTED_AT)
    return experiment


def _order_and_failures(
    session: Session,
    merchant_id: uuid.UUID,
    case: PotentialOutcomeCase,
    detected_at: datetime,
    customer_id: uuid.UUID,
) -> tuple[Order, list[PaymentAttempt]]:
    """The order and the two failed attempts that make it a repeated failure."""
    order = Order(
        merchant_id=merchant_id,
        customer_id=customer_id,
        external_order_id=f"bench_order_{case.case_id.hex[:16]}",
        amount=case.amount_minor,
        currency="INR",
        status=OrderStatus.ATTEMPTED.value,
    )
    session.add(order)
    session.flush()

    failure_code = observed_failure_code(case)
    attempts = [
        PaymentAttempt(
            order_id=order.id,
            customer_id=customer_id,
            external_payment_id=f"bench_pay_{case.case_id.hex[:12]}_{number}",
            amount=case.amount_minor,
            currency="INR",
            payment_method=case.covariates.payment_method,
            provider="benchmark",
            status=PaymentStatus.FAILED.value,
            failure_code=failure_code,
            attempt_number=number,
            attempted_at=detected_at - timedelta(hours=3 - number),
        )
        for number in (1, 2)
    ]
    session.add_all(attempts)
    session.flush()
    return order, attempts


def _customer(
    session: Session,
    merchant_id: uuid.UUID,
    case: PotentialOutcomeCase,
    detected_at: datetime,
) -> uuid.UUID:
    """The customer, carrying tenure through `created_at`.

    `lifetime_value` is `NOT NULL` and the generator models no such quantity, so
    it is written as zero and **must never be used as a feature** — a test
    asserts that. A zero here is a placeholder the schema demands, not a
    measurement, and treating it as one would be inventing an input.
    """
    customer = Customer(
        merchant_id=merchant_id,
        external_customer_id=f"bench_cust_{case.case_id.hex[:16]}",
        lifetime_value=0,
        created_at=customer_created_at(case, detected_at),
    )
    session.add(customer)
    session.flush()
    return customer.id


def _risk(  # noqa: ANN202
    session: Session,
    merchant_id: uuid.UUID,
    order: Order,
    case: PotentialOutcomeCase,
    detected_at: datetime,
    customer_id: uuid.UUID,
):
    """The detected risk, carrying the case's own identity.

    `risk.id = case.case_id` is what makes the entire run reproducible from the
    seed: the arm is a hash of the risk id, so a rerun assigns every unit the
    same way without storing anything.
    """
    risk = RevenueRisk(
        id=case.case_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        order_id=order.id,
        risk_type=BENCHMARK_RISK_TYPE,
        amount_at_risk=case.amount_minor,
        currency="INR",
        confidence_bps=BENCHMARK_CONFIDENCE_BPS,
        detection_rule="repeated_payment_failure.v1",
        detected_at=detected_at,
        status=RiskStatus.DETECTED.value,
    )
    session.add(risk)
    session.flush()
    return risk


def action_for(arm: str) -> Action:
    """Benchmark policy: the holdout gets nothing, the treated arm gets a link."""
    return Action.NO_ACTION if arm == Arm.HOLDOUT.value else BENCHMARK_ACTION


def materialise(
    session: Session,
    *,
    seed: int = BENCHMARK_SEED,
    case_count: int,
    salt: str = BENCHMARK_SALT,
) -> BenchmarkRun:
    """Generate, persist, assign, observe, and seal. Returns what it did.

    The order matters and is the order an experiment actually runs in:
    randomise before revealing anything, open the window from detection, let
    the timeline produce a capture or not, read the outcome off the timeline,
    then seal. Doing any of it out of order would let the arm depend on the
    outcome, which is the one thing randomisation exists to prevent.
    """
    if case_count < 1:
        raise BridgeError(f"case_count must be at least 1, got {case_count}")

    database = guard_test_database(session)

    population = generate(seed=seed, case_count=case_count)
    experiment_id = benchmark_experiment_id(seed, case_count)

    # Two overlap checks, because a seed's populations are prefix-stable: the
    # first N cases of a 12-case run are the first N of a 10-case run, so a
    # different `case_count` is not a different set of risk ids. Checking the
    # first case is enough to detect any overlap, and costs one lookup rather
    # than an IN over ten thousand identifiers.
    #
    # These guard a **sequential** rerun and nothing more. `session.get` cannot
    # see another transaction's uncommitted rows, so two overlapping
    # materialisations running concurrently — a module-scoped fixture holding
    # one open while a second starts on another connection — sail past both
    # checks and then block on a unique index until one side is killed. Do not
    # materialise overlapping seeds concurrently; there is no cheap way to make
    # the guard catch it, and BREAKAGE 18 records what it looks like when it
    # happens.
    if session.get(Experiment, experiment_id) is not None:
        raise BridgeError(
            f"seed {seed} at {case_count} cases is already materialised in {database!r} "
            f"as experiment {experiment_id}. A rerun would collide on every identity this "
            "bridge preserves — the risk ids are the generator's case ids, so they cannot "
            "coexist with themselves."
        )

    if session.get(RevenueRisk, population.cases[0].case_id) is not None:
        raise BridgeError(
            f"seed {seed} is already materialised in {database!r} at a different case "
            "count. Populations from one seed share a prefix, so their risk ids overlap "
            "and the two runs cannot coexist."
        )
    merchant_id = _merchant(session, seed, case_count)
    experiment = _experiment(session, seed, case_count)

    treatment = holdout = opened = captures = 0
    risk_ids: list[uuid.UUID] = []
    latest_close = STARTED_AT

    for case_index, case in enumerate(population.cases):
        detected_at = detection_instant(case, case_index)
        customer_id = _customer(session, merchant_id, case, detected_at)
        order, attempts = _order_and_failures(session, merchant_id, case, detected_at, customer_id)
        risk = _risk(session, merchant_id, order, case, detected_at, customer_id)
        risk_ids.append(risk.id)

        assignment = assign_risk(session, experiment, risk, detected_at, salt=salt)
        if assignment is None:  # pragma: no cover - benchmark risks are all eligible
            raise BridgeError(f"risk {risk.id} was excluded from assignment")

        outcome = open_window(session, risk)
        if outcome is None:  # pragma: no cover - guarded by the risk type
            raise BridgeError(f"risk {risk.id} has no observation window")
        opened += 1
        latest_close = max(latest_close, outcome.window_closes_at)

        revealed = case.reveal(action_for(assignment.arm))

        if revealed.recovered:
            captures += 1
            capture = PaymentAttempt(
                order_id=order.id,
                customer_id=customer_id,
                external_payment_id=f"bench_cap_{case.case_id.hex[:12]}",
                amount=case.amount_minor,
                currency="INR",
                payment_method=case.covariates.payment_method,
                provider="benchmark",
                status=PaymentStatus.CAPTURED.value,
                attempt_number=3,
                attempted_at=capture_instant(
                    case, outcome.window_opens_at, outcome.window_closes_at
                ),
            )
            session.add(capture)
            attempts.append(capture)
            order.status = OrderStatus.PAID.value

        observed = observed_recovery(attempts, outcome.window_opens_at, outcome.window_closes_at)
        outcome.recovered = observed.recovered
        outcome.recovered_amount = observed.amount
        outcome.recovered_at = observed.at

        is_treated = assignment.arm == Arm.TREATMENT.value
        outcome.actions_executed = 1 if is_treated else 0
        outcome.contacts_made = 1 if is_treated else 0
        outcome.execution_failed = False
        outcome.harm_mandate_cancelled = revealed.harmed

        # Ground truth: both potential outcomes, which no real system observes.
        outcome.truth_y0 = case.y0
        outcome.truth_y1 = case.y1[BENCHMARK_ACTION]
        outcome.truth_harm_0 = case.harm0
        outcome.truth_harm_1 = case.harm1[BENCHMARK_ACTION]
        outcome.truth_segment = case.segment_id.value

        if is_treated:
            treatment += 1
        else:
            holdout += 1

        session.flush()

    sealed = seal_due(session, latest_close + timedelta(hours=1))

    return BenchmarkRun(
        experiment_id=experiment.id,
        merchant_id=merchant_id,
        database=database,
        seed=seed,
        case_count=case_count,
        action=BENCHMARK_ACTION.value,
        treatment=treatment,
        holdout=holdout,
        windows_opened=opened,
        outcomes_sealed=sealed.sealed,
        captures_planted=captures,
        risk_ids=tuple(risk_ids),
    )


__all__ = [
    "BENCHMARK_ACTION",
    "BENCHMARK_NAMESPACE",
    "CHARACTERISTIC_CODE_PERCENT",
    "FAILURE_CODES",
    "BENCHMARK_HOLDOUT_BPS",
    "BENCHMARK_MDE_BPS",
    "BENCHMARK_PLANNED_N_PER_ARM",
    "BENCHMARK_RISK_TYPE",
    "BENCHMARK_SALT",
    "BENCHMARK_SEED",
    "LOCKED_AT",
    "PERMITTED_DATABASE_MARKERS",
    "STARTED_AT",
    "BenchmarkRun",
    "BridgeError",
    "ObservedRecovery",
    "action_for",
    "benchmark_experiment_id",
    "benchmark_experiment_name",
    "benchmark_merchant_id",
    "capture_instant",
    "captures_within",
    "customer_created_at",
    "detection_instant",
    "guard_test_database",
    "materialise",
    "observed_failure_code",
    "observed_recovery",
    "observed_recovery_for_risk",
]
