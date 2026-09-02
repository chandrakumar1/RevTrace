"""Gate-off versus gate-on, over one materialised population.

Two passes, no writes. **Gate-off** is the benchmark's current policy: every
treatment unit receives the intervention, so its action count is exactly
`n_treatment`. **Gate-on** puts each treatment unit through the deterministic
policy gate and counts only `ACT`.

Orchestration and arithmetic only, in the same shape as `coverage.py`: it reads
a materialised population, asks the existing public causal functions for the
per-unit labels, calls the pure gate, and tallies. It computes no causal
statistic of its own, and it writes nothing at all.

**Why one transaction is enough.** The gate is pure — `decide()` takes no
session — and every causal input was materialised before either pass began. The
two passes therefore cannot influence each other or the estimate, which is why
the report is built *once* and reused rather than recomputed and compared. The
proof is structural: there is no write to isolate.

**Why not `evaluate_risk`.** The seam loads evidence from `uplift_scores`, which
this benchmark never persists, and it writes one `audit_events` row per
abstention. Both are correct for the production path and wrong for a comparison
harness. The integration path is tested separately in
`tests/integration/test_day2_abstention.py`.

What the numbers are not
------------------------
`cost_avoided` is money **not spent**. `incremental_recovered` is money
**earned**. They are not comparable quantities and subtracting one from the
other does not produce a profit figure — that would need a gross margin, which
does not exist anywhere in this codebase. `expected_recovery` here is
`revenue_risks.amount_at_risk`, the full order amount at risk: a gross proxy,
not a contribution figure. The summary says all of this in the output, because
a reader who sees two rupee figures side by side will subtract them unless told
why they must not.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.causal.analysis import load_population
from app.causal.estimators import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED
from app.causal.quadrants import QuadrantAssignment, assign_quadrants
from app.causal.uplift import DEFAULT_FOLD_COUNT, load_units
from app.engine.policy_engine import (
    EXPLORATION_BUDGET_BPS,
    GateDecision,
    GrayZonePolicy,
    UpliftEvidence,
    decide,
    expected_incremental_recovery,
)
from app.models import CaseAssignment, CaseOutcome, RecoveryCase, RevenueRisk
from app.models.audit_event import AuditEvent
from app.models.enums import AbstainReason, Arm, CaseDecision
from app.reporting.evaluation import EvaluationReport
from app.services.recovery.gate import load_intervention
from tests.benchmark.bridge import BENCHMARK_ACTION

#: Full basis-point scale.
BPS_SCALE = 10_000

#: Tables that must be untouched by either pass. Counted before and after.
WATCHED_TABLES = ("case_assignments", "case_outcomes", "audit_events", "recovery_cases")

#: The instant the comparison decides at. A fixed constant rather than a clock:
#: an acceptance run must be reproducible, and reading `now()` would make the
#: inputs differ between runs even where the output does not.
#:
#: It reaches only the cooldown rule, and the benchmark passes no
#: `last_contacted_at` — no contact history is materialised — so this value
#: cannot change a single decision today. It is fixed anyway, because a
#: benchmark that happens not to depend on the clock is not the same thing as
#: one that cannot.
GATE_AS_OF = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)


class GateBenchmarkError(RuntimeError):
    """The comparison could not be run."""


def _round_half_up(numerator: int, denominator: int) -> int:
    """`round(numerator / denominator)`, halves away from zero, no float."""
    if denominator <= 0:
        raise GateBenchmarkError(f"denominator must be positive, got {denominator}")
    if numerator < 0:
        return -((-numerator * 2 + denominator) // (2 * denominator))
    return (numerator * 2 + denominator) // (2 * denominator)


# -- the causal snapshot --------------------------------------------------


@dataclass(frozen=True, slots=True)
class CausalSnapshot:
    """Everything that must be identical before and after the two passes.

    The report is captured as a digest rather than a dict so a comparison is a
    single exact equality rather than a deep one that could quietly skip a
    nested field.
    """

    ate_bps: int
    ci_low_bps: int
    ci_high_bps: int
    incremental_recovered: int
    report_digest: str
    row_counts: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ate_bps": self.ate_bps,
            "ci_low_bps": self.ci_low_bps,
            "ci_high_bps": self.ci_high_bps,
            "incremental_recovered": self.incremental_recovered,
            "report_digest": self.report_digest,
            "row_counts": dict(self.row_counts),
        }


def report_digest(report: EvaluationReport) -> str:
    """A stable digest of the whole report payload."""
    return hashlib.sha256(
        json.dumps(report.as_dict(), sort_keys=True, default=str).encode()
    ).hexdigest()


def row_counts(session: Session) -> tuple[tuple[str, int], ...]:
    """Row counts for every table a gate pass must not touch."""
    models = {
        "case_assignments": CaseAssignment,
        "case_outcomes": CaseOutcome,
        "audit_events": AuditEvent,
        "recovery_cases": RecoveryCase,
    }
    return tuple(
        (
            name,
            session.execute(select(func.count()).select_from(models[name])).scalar_one(),
        )
        for name in WATCHED_TABLES
    )


def capture_snapshot(session: Session, report: EvaluationReport) -> CausalSnapshot:
    """Freeze the causal estimate and the row counts behind it."""
    return CausalSnapshot(
        ate_bps=report.recovery.ate_bps,
        ci_low_bps=report.recovery.interval.low,
        ci_high_bps=report.recovery.interval.high,
        incremental_recovered=report.ledger.incremental_recovered,
        report_digest=report_digest(report),
        row_counts=row_counts(session),
    )


# -- the comparison -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateComparison:
    """What the two passes counted, and the arithmetic over them."""

    n_treatment: int
    gate_off_actions: int
    gate_on_actions: int
    intervention_code: str
    unit_cost: int
    abstentions: tuple[tuple[str, int], ...]
    explored: int
    exploration_budget_bps: int
    ate_bps: int
    incremental_recovered: int

    def __post_init__(self) -> None:
        if self.gate_on_actions > self.gate_off_actions:
            raise GateBenchmarkError(
                f"gate-on acted {self.gate_on_actions} times against gate-off's "
                f"{self.gate_off_actions}; the gate can only ever decline"
            )
        decided = self.gate_on_actions + sum(count for _, count in self.abstentions)
        if decided != self.n_treatment:
            raise GateBenchmarkError(
                f"{decided} decisions recorded for {self.n_treatment} treatment units; "
                "every unit must reach exactly one decision"
            )

    @property
    def actions_avoided(self) -> int:
        return self.gate_off_actions - self.gate_on_actions

    @property
    def cost_avoided(self) -> int:
        """Money not spent. **Not** a profit figure — see the module docstring."""
        return self.actions_avoided * self.unit_cost

    @property
    def gate_off_cost(self) -> int:
        return self.gate_off_actions * self.unit_cost

    @property
    def gate_on_cost(self) -> int:
        return self.gate_on_actions * self.unit_cost

    @property
    def gate_on_fraction_bps(self) -> int | None:
        """`gate_on / gate_off`. None when gate-off acted zero times.

        Undefined, not zero: a zero here would read as "the gate declined
        everything" when in fact there was nothing to decline.
        """
        if self.gate_off_actions == 0:
            return None
        return _round_half_up(self.gate_on_actions * BPS_SCALE, self.gate_off_actions)

    @property
    def avoided_fraction_bps(self) -> int | None:
        if self.gate_off_actions == 0:
            return None
        return _round_half_up(self.actions_avoided * BPS_SCALE, self.gate_off_actions)

    @property
    def explored_fraction_bps(self) -> int | None:
        if self.n_treatment == 0:
            return None
        return _round_half_up(self.explored * BPS_SCALE, self.n_treatment)

    def as_dict(self) -> dict[str, object]:
        return {
            "n_treatment": self.n_treatment,
            "gate_off_actions": self.gate_off_actions,
            "gate_on_actions": self.gate_on_actions,
            "actions_avoided": self.actions_avoided,
            "intervention_code": self.intervention_code,
            "unit_cost": self.unit_cost,
            "gate_off_cost": self.gate_off_cost,
            "gate_on_cost": self.gate_on_cost,
            "cost_avoided": self.cost_avoided,
            "gate_on_fraction_bps": self.gate_on_fraction_bps,
            "avoided_fraction_bps": self.avoided_fraction_bps,
            "abstentions": dict(self.abstentions),
            "explored": self.explored,
            "exploration_budget_bps": self.exploration_budget_bps,
            "explored_fraction_bps": self.explored_fraction_bps,
            "ate_bps": self.ate_bps,
            "incremental_recovered": self.incremental_recovered,
        }


def tally_abstentions(decisions: Sequence[GateDecision]) -> tuple[tuple[str, int], ...]:
    """Count every `AbstainReason`, including the ones that never fired.

    All eleven are always present. A reason that is absent from the output would
    be indistinguishable from a reason that was measured at zero, and those are
    different findings.
    """
    counts = dict.fromkeys((reason.value for reason in AbstainReason), 0)
    for decision in decisions:
        if decision.reason is not None:
            counts[decision.reason.value] += 1
    return tuple(counts.items())


def evidence_for(assignment: QuadrantAssignment) -> UpliftEvidence:
    """A causal per-unit label, as the gate's input type."""
    score = assignment.uplift
    return UpliftEvidence(
        uplift_bps=score.uplift_bps,
        uplift_ci_low_bps=score.ci_low_bps,
        uplift_ci_high_bps=score.ci_high_bps,
        quadrant=assignment.quadrant,
        qualified=score.qualified,
    )


def amounts_at_risk(session: Session, risk_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """`revenue_risks.amount_at_risk` for each risk.

    The measured amount, used as `expected_recovery`. It is the **gross order
    amount at risk**, not a margin or a contribution figure; see the module
    docstring for why that distinction has to travel with the output.
    """
    if not risk_ids:
        return {}
    rows = session.execute(
        select(RevenueRisk.id, RevenueRisk.amount_at_risk).where(RevenueRisk.id.in_(risk_ids))
    ).all()
    return {risk_id: amount for risk_id, amount in rows}


def gate_pass(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    assignments: Sequence[QuadrantAssignment],
    as_of: datetime = GATE_AS_OF,
    intervention_code: str = BENCHMARK_ACTION.value,
    gray_zone_policy: GrayZonePolicy = GrayZonePolicy.CURRENT_BASELINE,
    budget_bps: int = EXPLORATION_BUDGET_BPS,
) -> tuple[tuple[GateDecision, ...], str, int]:
    """Decide for every treatment unit. Reads only; writes nothing.

    Returns the decisions in `risk_id` order, plus the intervention's code and
    unit cost as they are actually stored.

    `gray_zone_policy` and `budget_bps` default to the measured baseline, so a
    caller that omits them reproduces the accepted N=10,000 run exactly.
    """
    population = load_population(session, experiment_id)
    treatment = sorted(
        (row for row in population.rows if row.is_treatment), key=lambda row: row.risk_id
    )
    labels = {assignment.risk_id: assignment for assignment in assignments}
    amounts = amounts_at_risk(session, [row.risk_id for row in treatment])
    terms = load_intervention(session, intervention_code)

    decisions: list[GateDecision] = []
    for row in treatment:
        amount = amounts.get(row.risk_id)
        if amount is None:
            raise GateBenchmarkError(f"risk {row.risk_id} has no amount_at_risk")
        assignment = labels.get(row.risk_id)
        decisions.append(
            decide(
                row.risk_id,
                experiment_id,
                arm=Arm.TREATMENT,
                uplift=evidence_for(assignment) if assignment else None,
                intervention=terms,
                expected_recovery=amount,
                # No recovery case exists, so no case maximum is stored. The
                # amount at risk is the most permissive defensible ceiling,
                # which means the max-cost rule never fires here and the
                # cost-recovery comparison is the rule actually doing the work.
                max_cost=amount,
                as_of=as_of,
                budget_bps=budget_bps,
                gray_zone_policy=gray_zone_policy,
            )
        )
    return tuple(decisions), terms.code, terms.unit_cost


def run_gate_comparison(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    report: EvaluationReport,
    as_of: datetime = GATE_AS_OF,
    assignments: Sequence[QuadrantAssignment] | None = None,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> GateComparison:
    """Both passes over one population. Nothing is written.

    `assignments` may be supplied by a caller that already computed them;
    otherwise they are derived here with `assign_quadrants`, using the same
    parameters the report used. They must match — labels computed at a
    different resample count would not be the labels the report describes.
    """
    if assignments is None:
        units = load_units(session, experiment_id)
        assignments = assign_quadrants(
            units,
            experiment_id,
            alpha_bps=report.alpha_bps,
            mde_bps=report.mde_bps,
            folds=folds,
            resamples=resamples,
            seed=seed,
        ).assignments

    decisions, code, unit_cost = gate_pass(
        session, experiment_id, assignments=assignments, as_of=as_of
    )

    n_treatment = len(decisions)
    if n_treatment != report.recovery.n_treatment:
        raise GateBenchmarkError(
            f"decided for {n_treatment} treatment units but the report measured "
            f"{report.recovery.n_treatment}; the two passes are reading different populations"
        )

    return GateComparison(
        n_treatment=n_treatment,
        # Gate-off is the benchmark's current policy: every treated unit acts.
        gate_off_actions=n_treatment,
        gate_on_actions=sum(1 for d in decisions if d.decision is CaseDecision.ACT),
        intervention_code=code,
        unit_cost=unit_cost,
        abstentions=tally_abstentions(decisions),
        explored=sum(1 for d in decisions if d.explored),
        exploration_budget_bps=EXPLORATION_BUDGET_BPS,
        ate_bps=report.recovery.ate_bps,
        incremental_recovered=report.ledger.incremental_recovered,
    )


# -- rendering ------------------------------------------------------------


def _money(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    magnitude = abs(minor)
    return f"{sign}Rs {magnitude // 100:,}.{magnitude % 100:02d}"


def _fraction(bps: int | None) -> str:
    if bps is None:
        return "undefined"
    return f"{bps // 100}.{bps % 100:02d}%"


def summarise(comparison: GateComparison) -> str:
    """A console summary for whoever ran the comparison."""
    lines = [
        "GATE-OFF vs GATE-ON  (synthetic/demo, one materialised population)",
        "",
        f"treatment units      {comparison.n_treatment:,}",
        f"gate-off actions     {comparison.gate_off_actions:,}  (every treated unit acts)",
        f"gate-on actions      {comparison.gate_on_actions:,}",
        f"actions avoided      {comparison.actions_avoided:,}",
        "",
        f"gate-on / gate-off   {_fraction(comparison.gate_on_fraction_bps)}",
        f"avoided fraction     {_fraction(comparison.avoided_fraction_bps)}",
        "",
        f"intervention         {comparison.intervention_code} "
        f"at {_money(comparison.unit_cost)} per action",
        f"gate-off spend       {_money(comparison.gate_off_cost)}",
        f"gate-on spend        {_money(comparison.gate_on_cost)}",
        f"cost avoided         {_money(comparison.cost_avoided)}",
        "",
        f"exploration budget   {_fraction(comparison.exploration_budget_bps)}",
        f"exploration selected {comparison.explored:,} "
        f"({_fraction(comparison.explored_fraction_bps)} of treatment)",
        "",
        "abstentions by reason:",
    ]
    for reason, count in comparison.abstentions:
        lines.append(f"  {reason:<28} {count:>8,}")

    lines.extend(
        [
            "",
            "unchanged by the gate (measured before both passes):",
            f"  ate_bps               {comparison.ate_bps:,}",
            f"  incremental_recovered {_money(comparison.incremental_recovered)}",
            "",
            "READ THIS BEFORE QUOTING THE NUMBERS ABOVE.",
            "",
            "`cost avoided` is money NOT SPENT. `incremental_recovered` is money",
            "EARNED. They are not comparable quantities and subtracting one from",
            "the other does not yield a profit figure — that needs a gross margin,",
            "which does not exist anywhere in this codebase.",
            "",
            "Expected recovery is `revenue_risks.amount_at_risk`: the full order",
            "amount at risk, a gross proxy rather than a contribution figure. It is",
            "large against a per-action cost, so the cost-recovery test clears for",
            "most qualifying units and the gate-on count is driven mainly by the",
            "evidence rules. That is a property of this population, not a claim",
            "that the cost test is doing work here.",
            "",
            "Avoiding an action is not free: an avoided action is also a recovery",
            "that may not happen. This comparison counts spend, not the revenue",
            "consequence of declining.",
        ]
    )
    return "\n".join(lines)


# -- scenario sensitivity -------------------------------------------------

#: The exploration budgets swept, in basis points. 500 is the constant the gate
#: actually uses, so that column reproduces the measured run; the others are
#: **declared scenario assumptions**, not measurements.
BUDGET_BPS_GRID: tuple[int, ...] = (0, 250, 500, 1_000)

#: The policies swept. Both act on a significant lift in a high-baseline cell;
#: they differ only on whether exploration may rescue a null-effect unit.
POLICY_GRID: tuple[GrayZonePolicy, ...] = (
    GrayZonePolicy.CURRENT_BASELINE,
    GrayZonePolicy.NULL_ONLY,
)

#: Percentiles reported for the expected-incremental distribution. Present so a
#: reader can see where an intervention cost would have to sit before the
#: cost-recovery test began to bind — using measured amounts rather than an
#: invented cost axis.
PERCENTILES: tuple[int, ...] = (0, 25, 50, 75, 100)

#: Stated with every scenario. Two rupee figures side by side invite a
#: subtraction that this repository cannot honestly support.
HONESTY: tuple[str, ...] = (
    "These are policy-scenario measurements, not causal estimates.",
    "Every scenario shares one ATE, one confidence interval and one "
    "incremental-recovery measurement: the gate is pure and runs after "
    "materialisation, so no policy can change what was measured.",
    "Spend and spend avoided are counts of money moved and money not moved. Neither is P&L.",
    "`amount_at_risk` is a gross expected-recovery proxy — the full order "
    "amount — not a contribution figure.",
    "Converting incremental recovery into profit requires economic inputs "
    "absent from this repository: no gross margin, take rate, MDR, commission, "
    "lifetime value or monetary harm valuation exists here.",
)


def _percentile(ordered: Sequence[int], percent: int) -> int:
    """Nearest-rank percentile over a sorted sequence. Integer, no float.

    Index is `(percent * (n - 1)) // 100`, so 0 gives the minimum and 100 the
    maximum exactly. An even-length median returns the lower of the two middle
    values rather than averaging them — averaging would introduce the one float
    this module exists to avoid.
    """
    if not ordered:
        raise GateBenchmarkError("a percentile over zero values is undefined")
    if not 0 <= percent <= 100:
        raise GateBenchmarkError(f"percent must be within 0..100, got {percent}")
    return ordered[(percent * (len(ordered) - 1)) // 100]


@dataclass(frozen=True, slots=True)
class AmountDistribution:
    """Where `expected_incremental_recovery` sits across the treatment arm.

    Policy-independent: it is a property of the population and the estimates,
    not of any decision, so it is computed once and shared by every scenario.
    """

    n: int
    minimum: int
    p25: int
    median: int
    p75: int
    maximum: int

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "min": self.minimum,
            "p25": self.p25,
            "median": self.median,
            "p75": self.p75,
            "max": self.maximum,
        }


def amount_distribution(
    session: Session,
    experiment_id: uuid.UUID,
    assignments: Sequence[QuadrantAssignment],
) -> AmountDistribution:
    """`amount_at_risk x uplift` for every treatment unit that has an estimate.

    Computed independently of any decision, so an abstained unit still
    contributes. Reading it off the decisions instead would fill the
    distribution with the zeros that early abstentions carry.
    """
    population = load_population(session, experiment_id)
    treatment = sorted(
        (row for row in population.rows if row.is_treatment), key=lambda row: row.risk_id
    )
    labels = {assignment.risk_id: assignment for assignment in assignments}
    amounts = amounts_at_risk(session, [row.risk_id for row in treatment])

    values = sorted(
        expected_incremental_recovery(amounts[row.risk_id], labels[row.risk_id].uplift.uplift_bps)
        for row in treatment
        if row.risk_id in labels and row.risk_id in amounts
    )
    if not values:
        raise GateBenchmarkError("no treatment unit carried both an amount and an estimate")
    return AmountDistribution(
        n=len(values),
        minimum=_percentile(values, 0),
        p25=_percentile(values, 25),
        median=_percentile(values, 50),
        p75=_percentile(values, 75),
        maximum=_percentile(values, 100),
    )


@dataclass(frozen=True, slots=True)
class Scenario:
    """One point on the grid. Both fields are declared, not measured."""

    gray_zone_policy: GrayZonePolicy
    exploration_budget_bps: int

    @property
    def name(self) -> str:
        return f"{self.gray_zone_policy.value}@{self.exploration_budget_bps}bps"


#: The eight scenarios, policy-major so a reader scanning the output sees one
#: policy's budget sweep before the next begins.
SCENARIO_GRID: tuple[Scenario, ...] = tuple(
    Scenario(gray_zone_policy=policy, exploration_budget_bps=budget)
    for policy in POLICY_GRID
    for budget in BUDGET_BPS_GRID
)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """What one scenario decided, and the measured context it decided within."""

    scenario: Scenario
    comparison: GateComparison
    amounts: AmountDistribution

    @property
    def act_count(self) -> int:
        return self.comparison.gate_on_actions

    @property
    def abstain_count(self) -> int:
        return sum(count for _, count in self.comparison.abstentions)

    def as_dict(self) -> dict[str, object]:
        payload = dict(self.comparison.as_dict())
        payload.update(
            {
                "scenario": self.scenario.name,
                "gray_zone_policy": self.scenario.gray_zone_policy.value,
                "exploration_budget_bps": self.scenario.exploration_budget_bps,
                "act_count": self.act_count,
                "abstain_count": self.abstain_count,
                "expected_incremental_recovery": self.amounts.as_dict(),
                "honesty": list(HONESTY),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class SensitivityRun:
    """Every scenario, plus the single causal snapshot they all share."""

    results: tuple[ScenarioResult, ...]
    snapshot: CausalSnapshot
    amounts: AmountDistribution

    def __post_init__(self) -> None:
        shared = {
            (r.comparison.ate_bps, r.comparison.incremental_recovered, r.comparison.n_treatment)
            for r in self.results
        }
        if len(shared) > 1:
            raise GateBenchmarkError(
                f"scenarios disagree on the measured population: {sorted(shared)}; "
                "every scenario must decide over one materialisation"
            )

    def result_for(self, scenario: Scenario) -> ScenarioResult:
        for result in self.results:
            if result.scenario == scenario:
                return result
        raise GateBenchmarkError(f"no result for {scenario.name}")

    def as_dict(self) -> dict[str, object]:
        return {
            "scenarios": [result.as_dict() for result in self.results],
            "snapshot": self.snapshot.as_dict(),
            "expected_incremental_recovery": self.amounts.as_dict(),
            "honesty": list(HONESTY),
        }


def run_sensitivity(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    report: EvaluationReport,
    as_of: datetime = GATE_AS_OF,
    assignments: Sequence[QuadrantAssignment] | None = None,
    grid: Sequence[Scenario] = SCENARIO_GRID,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> SensitivityRun:
    """Every scenario over one population. Nothing is written.

    **`assign_quadrants` is called exactly once**, and the resulting labels are
    reused by all eight scenarios. Labels are a property of the population and
    the estimator, not of the policy, so recomputing them per scenario would
    cost eight cross-fits to produce eight identical answers.
    """
    if assignments is None:
        units = load_units(session, experiment_id)
        assignments = assign_quadrants(
            units,
            experiment_id,
            alpha_bps=report.alpha_bps,
            mde_bps=report.mde_bps,
            folds=folds,
            resamples=resamples,
            seed=seed,
        ).assignments

    amounts = amount_distribution(session, experiment_id, assignments)

    results: list[ScenarioResult] = []
    for scenario in grid:
        decisions, code, unit_cost = gate_pass(
            session,
            experiment_id,
            assignments=assignments,
            as_of=as_of,
            gray_zone_policy=scenario.gray_zone_policy,
            budget_bps=scenario.exploration_budget_bps,
        )
        n_treatment = len(decisions)
        if n_treatment != report.recovery.n_treatment:
            raise GateBenchmarkError(
                f"{scenario.name}: decided for {n_treatment} treatment units but the "
                f"report measured {report.recovery.n_treatment}"
            )
        results.append(
            ScenarioResult(
                scenario=scenario,
                comparison=GateComparison(
                    n_treatment=n_treatment,
                    gate_off_actions=n_treatment,
                    gate_on_actions=sum(1 for d in decisions if d.decision is CaseDecision.ACT),
                    intervention_code=code,
                    unit_cost=unit_cost,
                    abstentions=tally_abstentions(decisions),
                    explored=sum(1 for d in decisions if d.explored),
                    exploration_budget_bps=scenario.exploration_budget_bps,
                    ate_bps=report.recovery.ate_bps,
                    incremental_recovered=report.ledger.incremental_recovered,
                ),
                amounts=amounts,
            )
        )

    return SensitivityRun(
        results=tuple(results),
        snapshot=capture_snapshot(session, report),
        amounts=amounts,
    )


def summarise_sensitivity(run: SensitivityRun) -> str:
    """A console table over the grid, followed by the honesty block."""
    lines = [
        "SCENARIO SENSITIVITY  (synthetic/demo, one materialised population)",
        "",
        f"{'scenario':<30} {'ACT':>7} {'ABSTAIN':>8} {'avoided':>8} "
        f"{'avoided%':>9} {'spend':>14} {'explored':>9}",
        "-" * 90,
    ]
    for result in run.results:
        c = result.comparison
        lines.append(
            f"{result.scenario.name:<30} {c.gate_on_actions:>7,} "
            f"{result.abstain_count:>8,} {c.actions_avoided:>8,} "
            f"{_fraction(c.avoided_fraction_bps):>9} {_money(c.gate_on_cost):>14} "
            f"{c.explored:>9,}"
        )

    lines.extend(["", "abstentions by reason, per scenario:", ""])
    reasons = [name for name, _ in run.results[0].comparison.abstentions]
    header = f"{'reason':<28}" + "".join(f"{r.scenario.name.split('@')[1]:>9}" for r in run.results)
    lines.append(
        f"{'policy':<28}"
        + "".join(f"{r.scenario.gray_zone_policy.value[:8]:>9}" for r in run.results)
    )
    lines.append(header)
    lines.append("-" * (28 + 9 * len(run.results)))
    for reason in reasons:
        row = f"{reason:<28}"
        for result in run.results:
            row += f"{dict(result.comparison.abstentions)[reason]:>9,}"
        lines.append(row)

    d = run.amounts
    lines.extend(
        [
            "",
            f"expected incremental recovery across {d.n:,} scored treatment units:",
            f"  min {_money(d.minimum)}   p25 {_money(d.p25)}   median {_money(d.median)}"
            f"   p75 {_money(d.p75)}   max {_money(d.maximum)}",
            "",
            "  An intervention cost would only begin to change decisions once it",
            "  approached these amounts. At the measured catalogue cost it does not,",
            "  which is why cost is not a scenario axis.",
            "",
            "shared by every scenario (measured once, before any policy ran):",
            f"  ate_bps               {run.snapshot.ate_bps:,}",
            f"  95% CI                [{run.snapshot.ci_low_bps:,}, {run.snapshot.ci_high_bps:,}]",
            f"  incremental_recovered {_money(run.snapshot.incremental_recovered)}",
            f"  report digest         {run.snapshot.report_digest[:16]}...",
            "",
            "READ THIS BEFORE QUOTING THE NUMBERS ABOVE.",
            "",
        ]
    )
    lines.extend(f"* {line}" for line in HONESTY)
    return "\n".join(lines)


__all__ = [
    "BPS_SCALE",
    "BUDGET_BPS_GRID",
    "HONESTY",
    "PERCENTILES",
    "POLICY_GRID",
    "SCENARIO_GRID",
    "WATCHED_TABLES",
    "AmountDistribution",
    "Scenario",
    "ScenarioResult",
    "SensitivityRun",
    "amount_distribution",
    "run_sensitivity",
    "summarise_sensitivity",
    "CausalSnapshot",
    "GateBenchmarkError",
    "GateComparison",
    "amounts_at_risk",
    "capture_snapshot",
    "evidence_for",
    "gate_pass",
    "report_digest",
    "row_counts",
    "run_gate_comparison",
    "summarise",
    "tally_abstentions",
]
