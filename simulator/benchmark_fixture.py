"""The committed benchmark snapshot.

Ten thousand cases is far too much to commit verbatim, and committing it would
be the wrong guarantee anyway: what needs to be reproducible is the
*generator*, not a copy of its output. So the fixture records the seed, the
generator version, a checksum over every case, the planted parameters, and the
ground-truth aggregates — enough that a mismatch is caught immediately and the
full population is regenerable from the seed alone.

A small verbatim sample is included so the shape can be read without running
anything.

The checksum covers each case's identity, segment, both potential outcomes, and
both harm outcomes. Any change to the segment parameters, the draw order, or
the sub-stream derivation moves it, which is what makes an accidental change
loud rather than silent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from simulator.potential_outcomes import (
    DEFAULT_CASE_COUNT,
    POTENTIAL_OUTCOMES_VERSION,
    PotentialOutcomeCase,
    PotentialOutcomeSet,
    generate,
    overall_truth,
    self_recovery_share_bps,
    truth_by_segment,
)
from simulator.segments import SEGMENTS, Action, SegmentId

#: The committed benchmark seed. Changing it invalidates the fixture, which is
#: the intended friction.
BENCHMARK_SEED = 42

#: Cases rendered verbatim, so the shape is readable without a run.
SAMPLE_SIZE = 25

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "benchmark_seed42.json"


def _case_digest_line(case: PotentialOutcomeCase) -> str:
    """One case, reduced to the fields that must never drift."""
    y1 = "".join("1" if case.y1[a] else "0" for a in sorted(case.y1, key=lambda x: x.value))
    harm1 = "".join(
        "1" if case.harm1[a] else "0" for a in sorted(case.harm1, key=lambda x: x.value)
    )
    return (
        f"{case.case_id}|{case.segment_id.value}|{int(case.y0)}|{y1}"
        f"|{int(case.harm0)}|{harm1}|{case.amount_minor}"
    )


def population_checksum(population: PotentialOutcomeSet) -> str:
    digest = hashlib.sha256()
    for case in population.cases:
        digest.update(_case_digest_line(case).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _sample(case: PotentialOutcomeCase) -> dict[str, Any]:
    return {
        "case_id": str(case.case_id),
        "segment_id": case.segment_id.value,
        "amount_minor": case.amount_minor,
        "y0": case.y0,
        "y1": {action.value: value for action, value in sorted(case.y1.items())},
        "harm0": case.harm0,
        "harm1": {action.value: value for action, value in sorted(case.harm1.items())},
        "covariates": {
            "salary_window": case.covariates.salary_window,
            "downtime_ended": case.covariates.downtime_ended,
            "day_of_month": case.covariates.day_of_month,
            "payment_method": case.covariates.payment_method,
            "issuer": case.covariates.issuer,
            "tenure_days": case.covariates.tenure_days,
            "prior_recovery_count": case.covariates.prior_recovery_count,
            "prior_contact_count_30d": case.covariates.prior_contact_count_30d,
            "hour_of_day": case.covariates.hour_of_day,
        },
    }


def build_snapshot(
    seed: int = BENCHMARK_SEED,
    case_count: int = DEFAULT_CASE_COUNT,
    action: Action = Action.CREATE_PAYMENT_LINK,
) -> dict[str, Any]:
    """Everything needed to verify a regeneration, and nothing more."""
    population = generate(seed=seed, case_count=case_count)
    ate, harm_ate = overall_truth(population, action)

    return {
        "generator_version": POTENTIAL_OUTCOMES_VERSION,
        "seed": seed,
        "case_count": case_count,
        "action": action.value,
        "checksum_sha256": population_checksum(population),
        "planted_parameters": {
            spec.id.value: {
                "weight_bps": spec.weight_bps,
                "expected_quadrant": spec.expected_quadrant.value,
                "failure_code": spec.failure_code,
                "preferred_method": spec.preferred_method,
            }
            for spec in SEGMENTS
        },
        "ground_truth": {
            "overall_true_ate_bps": ate,
            "overall_true_harm_ate_bps": harm_ate,
            "self_recovery_share_bps": self_recovery_share_bps(population),
            "by_segment": {
                truth.segment_id.value: {
                    "n": truth.n,
                    "y0_rate_bps": truth.y0_rate_bps,
                    "y1_rate_bps": truth.y1_rate_bps,
                    "true_ate_bps": truth.true_ate_bps,
                    "harm0_rate_bps": truth.harm0_rate_bps,
                    "harm1_rate_bps": truth.harm1_rate_bps,
                    "true_harm_ate_bps": truth.true_harm_ate_bps,
                    "expected_quadrant": truth.expected_quadrant,
                    "is_sleeping_dog": truth.is_sleeping_dog,
                }
                for truth in truth_by_segment(population, action)
            },
        },
        "sample": [_sample(case) for case in population.cases[:SAMPLE_SIZE]],
    }


def write_fixture(path: Path = FIXTURE_PATH) -> Path:
    """Regenerate the committed snapshot. Run deliberately, never from a test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def segment_ids() -> tuple[SegmentId, ...]:
    return tuple(spec.id for spec in SEGMENTS)


__all__ = [
    "BENCHMARK_SEED",
    "FIXTURE_PATH",
    "build_snapshot",
    "population_checksum",
    "read_fixture",
    "write_fixture",
]
