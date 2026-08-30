"""Running the benchmark end to end and writing the evaluation report.

Orchestration only. This module materialises a population, asks the reporter to
build the report, and writes it out. It never touches a `truth_*` column itself
— the answer key reaches the report through `app.reporting.evaluation`, which is
the one permitted reader, and that boundary is the whole point of splitting the
two apart.

`write_evaluation()` is a deliberate entry point, run on purpose rather than
from a test, in the same way the generator's committed fixture is regenerated.
Tests render to a string and assert; they do not write into `docs/`.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.causal.estimators import BOOTSTRAP_RESAMPLES
from app.reporting.evaluation import (
    EvaluationReport,
    build_report,
    render_json,
    render_markdown,
)
from tests.benchmark.bridge import BENCHMARK_SEED, BenchmarkRun, materialise

#: The full run, as the plan specifies: 8,000-12,000 cases.
ACCEPTANCE_CASE_COUNT = 10_000

#: Where a deliberate run writes its output.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
EVALUATION_PATH = REPO_ROOT / "docs" / "EVALUATION.md"
EVALUATION_JSON_PATH = REPO_ROOT / "docs" / "evaluation.json"


@dataclass(frozen=True, slots=True)
class BenchmarkOutcome:
    """One complete run: what was materialised, and what it measured."""

    run: BenchmarkRun
    report: EvaluationReport

    @property
    def headline(self) -> str:
        recovery = self.report.recovery
        return (
            f"ATE {recovery.ate_bps}bps "
            f"[{recovery.interval.low}, {recovery.interval.high}] "
            f"vs true {self.report.truth.true_ate_bps}bps"
        )


def run_benchmark(
    session: Session,
    *,
    seed: int = BENCHMARK_SEED,
    case_count: int = ACCEPTANCE_CASE_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> BenchmarkOutcome:
    """Materialise a population and evaluate it.

    The two halves stay separate on purpose: `materialise` writes rows and knows
    the generator, `build_report` reads rows and does not.
    """
    run = materialise(session, seed=seed, case_count=case_count)
    report = build_report(session, run.experiment_id, resamples=resamples)
    return BenchmarkOutcome(run=run, report=report)


def summarise(outcome: BenchmarkOutcome) -> str:
    """A short console summary of a run, for the operator who started it."""
    report = outcome.report
    ledger = report.ledger
    return "\n".join(
        [
            f"experiment        {report.experiment_id}",
            f"seed / cases      {outcome.run.seed} / {outcome.run.case_count:,}",
            f"action            {outcome.run.action}",
            f"arms              {outcome.run.treatment:,} treated, {outcome.run.holdout:,} holdout",
            f"estimated ATE     {report.recovery.ate_bps} bps",
            f"  95% CI          [{report.recovery.interval.low}, "
            f"{report.recovery.interval.high}] bps",
            f"  p-value         {report.recovery.p_value_micros} micros",
            f"true ATE          {report.truth.true_ate_bps} bps",
            f"  error           {report.ate_error_bps} bps",
            f"  CI covers truth {report.interval_covers_the_truth}",
            f"gross recovered   {ledger.gross_recovered:,} minor units",
            f"incremental       {ledger.incremental_recovered:,} minor units",
            f"  95% CI          [{ledger.interval.low:,}, {ledger.interval.high:,}]",
            f"credited-not-earned {ledger.credited_not_earned:,} minor units "
            f"({ledger.credited_share_bps} bps of gross)",
            f"harm ATE          {report.harm.ate_bps} bps "
            f"[{report.harm.interval.low}, {report.harm.interval.high}]",
            f"balance           {'balanced' if report.balance.is_balanced else 'FLAGGED'}",
            f"underpowered      {report.is_underpowered}",
            f"bootstrap         seed {report.bootstrap_seed}, "
            f"{report.bootstrap_resamples:,} resamples",
        ]
    )


def write_evaluation(
    outcome: BenchmarkOutcome,
    *,
    markdown_path: pathlib.Path = EVALUATION_PATH,
    json_path: pathlib.Path | None = EVALUATION_JSON_PATH,
) -> pathlib.Path:
    """Write the report. Run deliberately, never from a test."""
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(outcome.report), encoding="utf-8")

    if json_path is not None:
        json_path.write_text(
            json.dumps(render_json(outcome.report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return markdown_path


def experiment_id_for(seed: int, case_count: int) -> uuid.UUID:
    """The derived identity of a run, without materialising it."""
    from tests.benchmark.bridge import benchmark_experiment_id

    return benchmark_experiment_id(seed, case_count)


__all__ = [
    "ACCEPTANCE_CASE_COUNT",
    "EVALUATION_JSON_PATH",
    "EVALUATION_PATH",
    "BenchmarkOutcome",
    "experiment_id_for",
    "run_benchmark",
    "summarise",
    "write_evaluation",
]
