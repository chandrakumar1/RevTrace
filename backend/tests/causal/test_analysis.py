"""Analysis populations — the pure half.

Building ITT and per-protocol samples from already-loaded rows needs no
database. The loading half — the join, the refusals, deterministic ordering —
is in `tests/integration/test_day4_analysis.py`.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid

import pytest

from app.causal.analysis import (
    ITT,
    PER_PROTOCOL,
    AnalysisError,
    AnalysisRefused,
    ArmSample,
    OutcomeRow,
    build_population,
    itt_sample,
    per_protocol_sample,
)
from app.models.enums import Arm

EXPERIMENT_ID = uuid.UUID("eeeeeeee-0000-4000-8000-000000000001")


def row(
    arm: str = Arm.TREATMENT.value,
    *,
    recovered: bool = False,
    amount: int = 0,
    execution_failed: bool = False,
    actions_executed: int = 1,
    mandate_cancelled: bool = False,
    opted_out: bool = False,
    complaint: bool = False,
    risk_id: uuid.UUID | None = None,
) -> OutcomeRow:
    return OutcomeRow(
        risk_id=risk_id or uuid.uuid4(),
        arm=arm,
        stratum_key="repeated_payment_failure|2000-5000",
        recovered=recovered,
        recovered_amount=amount,
        execution_failed=execution_failed,
        actions_executed=0 if arm == Arm.HOLDOUT.value else actions_executed,
        contacts_made=0,
        harm_mandate_cancelled=mandate_cancelled,
        harm_opted_out=opted_out,
        harm_complaint=complaint,
    )


def treated(**kwargs: object) -> OutcomeRow:
    return row(Arm.TREATMENT.value, **kwargs)  # type: ignore[arg-type]


def held_out(**kwargs: object) -> OutcomeRow:
    return row(Arm.HOLDOUT.value, **kwargs)  # type: ignore[arg-type]


class TestArmSample:
    def test_it_counts_and_sums(self) -> None:
        rows = [treated(recovered=True, amount=500), treated(), treated(recovered=True, amount=300)]
        sample = itt_sample(rows, EXPERIMENT_ID).treatment
        assert sample.n == 3
        assert sample.recoveries == 2
        assert sample.gross == 800

    def test_indicators_are_integers_not_booleans(self) -> None:
        """The estimators take sequences of integers; a bool would still sum
        correctly but would not survive a strict indicator check."""
        sample = itt_sample([treated(recovered=True), treated()], EXPERIMENT_ID).treatment
        assert sample.recovered == (1, 0)
        assert all(value in (0, 1) for value in sample.recovered)

    def test_columns_stay_aligned_by_index(self) -> None:
        rows = [treated(recovered=True, amount=700, mandate_cancelled=True), treated()]
        sample = itt_sample(rows, EXPERIMENT_ID).treatment
        assert sample.recovered[0] == 1 and sample.amounts[0] == 700
        assert sample.harm_mandate_cancelled == (1, 0)

    def test_misaligned_columns_are_rejected(self) -> None:
        with pytest.raises(AnalysisError, match="misaligned"):
            ArmSample(
                arm=Arm.TREATMENT.value,
                risk_ids=(uuid.uuid4(),),
                recovered=(1, 0),
                amounts=(0,),
                harm_mandate_cancelled=(0,),
                harm_opted_out=(0,),
                harm_complaint=(0,),
            )

    def test_an_empty_arm_reports_itself(self) -> None:
        sample = itt_sample([treated()], EXPERIMENT_ID).holdout
        assert sample.is_empty
        assert sample.n == 0
        assert sample.recoveries == 0
        assert sample.gross == 0


class TestZeroRecovery:
    def test_a_population_that_recovered_nothing_is_valid(self) -> None:
        """Zero is a measurement. Every unit is still counted, and the arms are
        still comparable — this must not be mistaken for missing data."""
        rows = [treated() for _ in range(5)] + [held_out() for _ in range(5)]
        population = build_population(rows, EXPERIMENT_ID)

        assert population.itt.treatment.n == 5
        assert population.itt.holdout.n == 5
        assert population.itt.treatment.recoveries == 0
        assert population.itt.treatment.gross == 0
        assert population.itt.holdout.gross == 0

    def test_non_recoveries_are_kept_as_zeros_not_dropped(self) -> None:
        """Dropping them would turn the mean into "how much did payers pay"."""
        rows = [treated(recovered=True, amount=1_000)] + [treated() for _ in range(3)]
        sample = itt_sample(rows, EXPERIMENT_ID).treatment
        assert sample.n == 4
        assert sample.amounts == (1_000, 0, 0, 0)

    def test_an_unrecovered_unit_carries_no_amount(self) -> None:
        sample = itt_sample([treated(recovered=False, amount=0)], EXPERIMENT_ID).treatment
        assert sample.recovered == (0,)
        assert sample.amounts == (0,)


class TestIntentionToTreat:
    def test_it_is_named(self) -> None:
        assert itt_sample([treated(), held_out()], EXPERIMENT_ID).analysis == ITT

    def test_the_arm_comes_from_the_assignment(self) -> None:
        rows = [treated() for _ in range(7)] + [held_out() for _ in range(3)]
        sample = itt_sample(rows, EXPERIMENT_ID)
        assert (sample.treatment.n, sample.holdout.n) == (7, 3)

    def test_a_failed_execution_stays_in_treatment(self) -> None:
        """The whole content of the phrase. Moving it to the control arm would
        let the treatment look best exactly where it worked least."""
        rows = [treated(execution_failed=True) for _ in range(4)] + [held_out()]
        sample = itt_sample(rows, EXPERIMENT_ID)
        assert sample.treatment.n == 4
        assert sample.holdout.n == 1

    def test_a_contaminated_holdout_stays_in_holdout(self) -> None:
        contaminated = OutcomeRow(
            risk_id=uuid.uuid4(),
            arm=Arm.HOLDOUT.value,
            stratum_key="repeated_payment_failure|2000-5000",
            recovered=True,
            recovered_amount=500,
            execution_failed=False,
            actions_executed=2,
            contacts_made=1,
            harm_mandate_cancelled=False,
            harm_opted_out=False,
            harm_complaint=False,
        )
        sample = itt_sample([treated(), contaminated], EXPERIMENT_ID)
        assert sample.holdout.n == 1
        assert sample.holdout.gross == 500

    def test_nothing_is_ever_excluded(self) -> None:
        rows = [treated(execution_failed=True), treated(), held_out()]
        sample = itt_sample(rows, EXPERIMENT_ID)
        assert sample.excluded_total == 0
        assert sample.non_compliance_bps == 0
        assert sample.n_total == 3


class TestPerProtocol:
    def test_it_is_named(self) -> None:
        assert per_protocol_sample([treated(), held_out()], EXPERIMENT_ID).analysis == PER_PROTOCOL

    def test_a_failed_execution_is_dropped(self) -> None:
        rows = [treated(execution_failed=True), treated(), treated(), held_out()]
        sample = per_protocol_sample(rows, EXPERIMENT_ID)
        assert sample.treatment.n == 2
        assert sample.excluded_treatment == 1

    def test_an_acted_on_holdout_is_dropped(self) -> None:
        contaminated = OutcomeRow(
            risk_id=uuid.uuid4(),
            arm=Arm.HOLDOUT.value,
            stratum_key="repeated_payment_failure|2000-5000",
            recovered=False,
            recovered_amount=0,
            execution_failed=False,
            actions_executed=1,
            contacts_made=1,
            harm_mandate_cancelled=False,
            harm_opted_out=False,
            harm_complaint=False,
        )
        sample = per_protocol_sample([treated(), held_out(), contaminated], EXPERIMENT_ID)
        assert sample.holdout.n == 1
        assert sample.excluded_holdout == 1

    def test_no_action_yet_is_not_non_compliance(self) -> None:
        """The pre-registration names `execution_failed` as the marker.
        `actions_executed == 0` cannot be told apart from an action that has
        simply not been attempted, so it must not silently empty the arm."""
        rows = [treated(actions_executed=0) for _ in range(5)] + [held_out()]
        sample = per_protocol_sample(rows, EXPERIMENT_ID)
        assert sample.treatment.n == 5
        assert sample.excluded_treatment == 0

    def test_the_non_compliance_rate_is_reported(self) -> None:
        rows = [treated(execution_failed=True) for _ in range(2)]
        rows += [treated() for _ in range(8)]
        rows += [held_out() for _ in range(10)]
        sample = per_protocol_sample(rows, EXPERIMENT_ID)
        assert sample.excluded_total == 2
        assert sample.non_compliance_bps == 1_000  # 2 of 20

    def test_it_can_differ_from_itt(self) -> None:
        """The difference is the thing that has to be stated, so it must be
        visible rather than smoothed away."""
        rows = [treated(execution_failed=True) for _ in range(5)]
        rows += [treated(recovered=True, amount=100) for _ in range(5)]
        rows += [held_out() for _ in range(10)]

        itt = itt_sample(rows, EXPERIMENT_ID)
        per_protocol = per_protocol_sample(rows, EXPERIMENT_ID)
        assert itt.treatment.n == 10
        assert per_protocol.treatment.n == 5
        assert itt.treatment.recoveries == per_protocol.treatment.recoveries

    def test_a_fully_compliant_population_matches_itt(self) -> None:
        rows = [treated(recovered=True, amount=200) for _ in range(4)]
        rows += [held_out() for _ in range(4)]
        itt = itt_sample(rows, EXPERIMENT_ID)
        per_protocol = per_protocol_sample(rows, EXPERIMENT_ID)
        assert itt.treatment.n == per_protocol.treatment.n
        assert itt.holdout.n == per_protocol.holdout.n
        assert per_protocol.non_compliance_bps == 0

    def test_an_empty_population_has_no_compliance_rate(self) -> None:
        assert per_protocol_sample([], EXPERIMENT_ID).non_compliance_bps == 0


class TestPopulation:
    def test_it_builds_both(self) -> None:
        rows = [treated(), held_out()]
        population = build_population(rows, EXPERIMENT_ID)
        assert population.itt.analysis == ITT
        assert population.per_protocol.analysis == PER_PROTOCOL
        assert population.n_enrolled == 2

    def test_it_keeps_the_rows_it_was_given(self) -> None:
        rows = [treated(), held_out(), treated()]
        assert build_population(rows, EXPERIMENT_ID).rows == tuple(rows)

    def test_an_empty_arm_is_refused(self) -> None:
        with pytest.raises(AnalysisRefused, match="not an experiment"):
            build_population([treated(), treated()], EXPERIMENT_ID)

    def test_an_empty_arm_can_be_allowed_deliberately(self) -> None:
        population = build_population([treated()], EXPERIMENT_ID, require_both_arms=False)
        assert population.itt.holdout.is_empty

    def test_the_refusal_names_the_experiment(self) -> None:
        with pytest.raises(AnalysisRefused) as caught:
            build_population([held_out()], EXPERIMENT_ID)
        assert caught.value.experiment_id == EXPERIMENT_ID

    def test_it_is_deterministic(self) -> None:
        rows = [treated(recovered=True, amount=50), held_out(), treated()]
        assert build_population(rows, EXPERIMENT_ID).itt.as_dict() == (
            build_population(list(rows), EXPERIMENT_ID).itt.as_dict()
        )

    def test_the_row_order_is_preserved_into_the_arms(self) -> None:
        """The bootstrap draws from these sequences, so their order has to be
        a function of the input rather than of anything incidental."""
        first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        rows = [
            treated(risk_id=first),
            held_out(risk_id=second),
            treated(risk_id=third),
        ]
        sample = itt_sample(rows, EXPERIMENT_ID)
        assert sample.treatment.risk_ids == (first, third)
        assert sample.holdout.risk_ids == (second,)


class TestSerialisation:
    def test_the_payload_carries_no_float(self) -> None:
        rows = [treated(recovered=True, amount=100), held_out()]
        for sample in (
            itt_sample(rows, EXPERIMENT_ID),
            per_protocol_sample(rows, EXPERIMENT_ID),
        ):
            for value in sample.as_dict().values():
                assert not isinstance(value, float), value

    def test_the_payload_states_the_analysis(self) -> None:
        rows = [treated(), held_out()]
        assert itt_sample(rows, EXPERIMENT_ID).as_dict()["analysis"] == ITT
        assert per_protocol_sample(rows, EXPERIMENT_ID).as_dict()["analysis"] == PER_PROTOCOL

    def test_the_samples_are_frozen(self) -> None:
        sample = itt_sample([treated(), held_out()], EXPERIMENT_ID)
        with pytest.raises(AttributeError):
            sample.analysis = "other"  # type: ignore[misc]


class TestPurity:
    """The loader reads. It never writes, and never sees the answer key."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import analysis as module

        return ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))

    @classmethod
    def _identifiers(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                found.add(node.name)
        return found

    @classmethod
    def _imports(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    def test_it_writes_nothing(self) -> None:
        identifiers = self._identifiers()
        for banned in ("add", "add_all", "commit", "flush", "merge", "delete", "update"):
            assert banned not in identifiers, banned

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name

    def test_it_imports_no_generator_module(self) -> None:
        for module in self._imports():
            assert not module.startswith("simulator"), module

    def test_it_reads_no_clock(self) -> None:
        for name in ("now", "utcnow", "today"):
            assert name not in self._identifiers(), name

    def test_it_reads_only_assignments_and_outcomes(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                imported.update(alias.name for alias in node.names)
        assert imported == {"CaseAssignment", "CaseOutcome"}

    def test_it_does_not_reach_into_the_estimators(self) -> None:
        """The seam runs one way: analysis hands integers upward, and the
        estimators never learn what a session is."""
        for module in self._imports():
            assert "estimators" not in module, module
            assert "power" not in module, module

    def test_it_creates_no_recovery_or_policy_concept(self) -> None:
        identifiers = self._identifiers()
        for banned in (
            "RecoveryCase",
            "RecoveryAction",
            "ExperimentResult",
            "approve",
            "approved",
            "policy_status",
            "execute_action",
            "recommend",
        ):
            assert banned not in identifiers, banned

    def test_it_invents_no_economic_input(self) -> None:
        for banned in ("gross_margin", "avg_customer_ltv", "harm_cost", "net_incremental_value"):
            assert banned not in self._identifiers(), banned

    def test_it_uses_an_outer_join(self) -> None:
        """An inner join would hide a missing outcome by returning fewer rows —
        exactly the silent drop the refusal exists to prevent."""
        source = pathlib.Path(
            inspect.getfile(__import__("app.causal.analysis", fromlist=["x"]))
        ).read_text(encoding="utf-8")
        assert "outerjoin" in source
