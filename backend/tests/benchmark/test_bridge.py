"""The benchmark bridge against revtrace_test.

What is checked here: that the run is reproducible from the seed, that identity
survives from the generator into `revenue_risks`, that recovery is genuinely
read off the timeline rather than copied from the answer key, that the window
boundary excludes a late capture, that truth and observation stay on separate
sides, and that materialising creates no recovery case and no recovery action.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from datetime import timedelta

import pytest
from simulator.benchmark_fixture import read_fixture
from simulator.potential_outcomes import generate
from simulator.segments import Action
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.causal.analysis import load_population
from app.experiments.assignment import decide
from app.models import (
    CaseAssignment,
    CaseOutcome,
    Experiment,
    Order,
    PaymentAttempt,
    RecoveryAction,
    RecoveryCase,
    RevenueRisk,
)
from app.models.enums import Arm, ExperimentStatus, PaymentStatus, RiskType
from tests.benchmark.bridge import (
    BENCHMARK_ACTION,
    BENCHMARK_HOLDOUT_BPS,
    BENCHMARK_SALT,
    BENCHMARK_SEED,
    BridgeError,
    action_for,
    benchmark_experiment_id,
    benchmark_merchant_id,
    capture_instant,
    captures_within,
    materialise,
    observed_recovery,
    observed_recovery_for_risk,
)

pytestmark = pytest.mark.db

#: Small enough to keep the suite quick, large enough that both arms fill.
SIZE = 120


# -- structural helpers ---------------------------------------------------
#
# Every guard below reads the syntax tree, never the source text. Modules in
# this project document the rules they follow, so a substring scan reliably
# flags the explanation as the violation — it has happened often enough to be
# worth doing properly once.


def _tree_of(module: object) -> ast.Module:
    return ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))


def _identifiers_of(module: object) -> set[str]:
    """Names actually referenced: variables, attributes, and definitions."""
    found: set[str] = set()
    for node in ast.walk(_tree_of(module)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.ClassDef):
            found.add(node.name)
    return found


def _imports_of(module: object) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree_of(module)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


class TestObservedRecoveryIsPure:
    """The derivation, exercised without a database."""

    @staticmethod
    def an_attempt(status: str, offset_hours: int, amount: int = 1_000) -> PaymentAttempt:
        from simulator.clock import SIMULATION_EPOCH

        return PaymentAttempt(
            order_id=uuid.uuid4(),
            amount=amount,
            currency="INR",
            payment_method="card",
            provider="benchmark",
            status=status,
            attempt_number=1,
            attempted_at=SIMULATION_EPOCH + timedelta(hours=offset_hours),
        )

    def window(self) -> tuple:  # noqa: ANN201
        from simulator.clock import SIMULATION_EPOCH

        return SIMULATION_EPOCH, SIMULATION_EPOCH + timedelta(hours=72)

    def test_no_capture_is_no_recovery(self) -> None:
        opens, closes = self.window()
        result = observed_recovery([self.an_attempt("failed", 1)], opens, closes)
        assert result.recovered is False
        assert result.amount == 0
        assert result.at is None

    def test_a_capture_inside_the_window_is_a_recovery(self) -> None:
        opens, closes = self.window()
        result = observed_recovery([self.an_attempt("captured", 10, 5_000)], opens, closes)
        assert result.recovered is True
        assert result.amount == 5_000
        assert result.at == opens + timedelta(hours=10)

    def test_a_capture_after_the_window_does_not_count(self) -> None:
        """The boundary is what makes the observation window mean anything.
        Money that arrived on day five is real, and it is not this experiment's
        outcome."""
        opens, closes = self.window()
        result = observed_recovery([self.an_attempt("captured", 100, 5_000)], opens, closes)
        assert result.recovered is False
        assert result.amount == 0

    def test_a_capture_before_the_window_does_not_count(self) -> None:
        opens, closes = self.window()
        result = observed_recovery([self.an_attempt("captured", -5)], opens, closes)
        assert result.recovered is False

    def test_the_window_is_half_open_at_the_close(self) -> None:
        opens, closes = self.window()
        assert observed_recovery([self.an_attempt("captured", 71)], opens, closes).recovered
        assert not observed_recovery([self.an_attempt("captured", 72)], opens, closes).recovered

    def test_it_counts_from_the_opening_instant(self) -> None:
        opens, closes = self.window()
        assert observed_recovery([self.an_attempt("captured", 0)], opens, closes).recovered

    def test_an_unstamped_attempt_is_ignored(self) -> None:
        opens, closes = self.window()
        attempt = self.an_attempt("captured", 1)
        attempt.attempted_at = None
        assert not observed_recovery([attempt], opens, closes).recovered

    def test_the_earliest_capture_supplies_the_timestamp(self) -> None:
        opens, closes = self.window()
        attempts = [self.an_attempt("captured", 40), self.an_attempt("captured", 5)]
        assert observed_recovery(attempts, opens, closes).at == opens + timedelta(hours=5)

    def test_only_captures_are_counted(self) -> None:
        opens, closes = self.window()
        attempts = [
            self.an_attempt("failed", 1),
            self.an_attempt("authorized", 2),
            self.an_attempt("timeout", 3),
        ]
        assert captures_within(attempts, opens, closes) == []


class TestTheDatabaseGuard:
    def test_it_accepts_the_test_database(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=4)
        assert "test" in run.database

    def test_it_would_refuse_a_development_database(self, db_session: Session) -> None:
        """The bridge's own guard, behind the harness's DSN check. Simulated by
        overriding what the database reports its name to be."""
        from tests.benchmark import bridge

        original = bridge.guard_test_database

        def pretend_dev(session: Session) -> str:
            name = "revtrace_dev"
            if not any(marker in name for marker in bridge.PERMITTED_DATABASE_MARKERS):
                raise BridgeError(f"refusing to materialise the benchmark into {name!r}")
            return name  # pragma: no cover

        bridge.guard_test_database = pretend_dev
        try:
            with pytest.raises(BridgeError, match="refusing to materialise"):
                bridge.materialise(db_session, case_count=2)
        finally:
            bridge.guard_test_database = original

    def test_the_permitted_markers_exclude_the_development_name(self) -> None:
        from tests.benchmark.bridge import PERMITTED_DATABASE_MARKERS

        assert not any(marker in "revtrace_dev" for marker in PERMITTED_DATABASE_MARKERS)


class TestIdentityAndDeterminism:
    def test_risk_ids_are_the_generator_case_ids(self, db_session: Session) -> None:
        population = generate(seed=BENCHMARK_SEED, case_count=25)
        run = materialise(db_session, case_count=25)
        assert run.risk_ids == tuple(case.case_id for case in population.cases)

    def test_they_match_the_committed_fixture_sample(self, db_session: Session) -> None:
        """The fixture's 25-case sample is the committed record of this seed."""
        sample = read_fixture()["sample"]
        run = materialise(db_session, case_count=len(sample))
        assert [str(risk_id) for risk_id in run.risk_ids] == [case["case_id"] for case in sample]

    def test_the_stored_risks_carry_those_ids(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=20)
        stored = {
            risk.id
            for risk in db_session.execute(
                select(RevenueRisk).where(RevenueRisk.merchant_id == run.merchant_id)
            ).scalars()
        }
        assert stored == set(run.risk_ids)

    def test_the_experiment_id_is_derived_not_drawn(self) -> None:
        """The arm is `sha256(risk_id : experiment_id : salt)`. A randomly
        drawn experiment id would re-randomise every unit on every run, and the
        benchmark would report a different estimate each time it executed."""
        assert benchmark_experiment_id(42, 120) == benchmark_experiment_id(42, 120)
        assert benchmark_experiment_id(42, 120) != benchmark_experiment_id(43, 120)
        assert benchmark_experiment_id(42, 120) != benchmark_experiment_id(42, 240)

    def test_the_run_uses_that_derived_id(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=30)
        assert run.experiment_id == benchmark_experiment_id(BENCHMARK_SEED, 30)
        assert run.merchant_id == benchmark_merchant_id(BENCHMARK_SEED, 30)

    def test_only_one_experiment_row_results(self, db_session: Session) -> None:
        """The derived id replaces the one `create_draft` allocated, while the
        row is still DRAFT. That must leave one row, not two."""
        run = materialise(db_session, case_count=10)
        count = db_session.execute(
            select(func.count()).select_from(Experiment).where(Experiment.id == run.experiment_id)
        ).scalar()
        assert count == 1

    def test_every_arm_is_recomputable_from_stored_inputs(self, db_session: Session) -> None:
        """The reproducibility that actually matters: an auditor recomputes the
        arm from the risk id, the experiment id and the salt, and gets what is
        stored. Nothing has to be trusted."""
        run = materialise(db_session, case_count=SIZE)
        assignments = db_session.execute(
            select(CaseAssignment).where(CaseAssignment.experiment_id == run.experiment_id)
        ).scalars()

        for assignment in assignments:
            risk = db_session.get(RevenueRisk, assignment.risk_id)
            assert risk is not None
            expected = decide(
                risk.id,
                run.experiment_id,
                risk_type=risk.risk_type,
                amount_minor=risk.amount_at_risk,
                holdout_bps=BENCHMARK_HOLDOUT_BPS,
                salt=BENCHMARK_SALT,
            )
            assert assignment.arm == expected.arm.value
            assert assignment.assignment_hash == expected.assignment_hash

    def test_a_second_run_of_the_same_benchmark_is_refused(self, db_session: Session) -> None:
        """Identity is preserved from the generator, so the same seed cannot
        coexist with itself. Refused with a reason rather than an opaque
        primary-key violation."""
        materialise(db_session, case_count=10)
        with pytest.raises(BridgeError, match="already materialised"):
            materialise(db_session, case_count=10)

    def test_a_different_size_of_the_same_seed_is_also_refused(self, db_session: Session) -> None:
        """A seed's populations are prefix-stable — the first ten cases of a
        twelve-case run are the first ten of a ten-case run — so a different
        size is not a different set of risk ids."""
        materialise(db_session, case_count=10)
        with pytest.raises(BridgeError, match="different case count"):
            materialise(db_session, case_count=12)

    def test_the_prefix_really_is_stable(self) -> None:
        ten = generate(seed=BENCHMARK_SEED, case_count=10).cases
        twelve = generate(seed=BENCHMARK_SEED, case_count=12).cases
        assert [case.case_id for case in ten] == [case.case_id for case in twelve[:10]]

    def test_the_capture_instant_is_derived_not_drawn(self, db_session: Session) -> None:
        population = generate(seed=BENCHMARK_SEED, case_count=5)
        case = population.cases[0]
        opens = case.detected_at
        closes = opens + timedelta(hours=72)
        assert capture_instant(case, opens, closes) == capture_instant(case, opens, closes)

    def test_the_capture_instant_lands_inside_the_window(self, db_session: Session) -> None:
        for case in generate(seed=BENCHMARK_SEED, case_count=50).cases:
            opens = case.detected_at
            closes = opens + timedelta(hours=72)
            assert opens <= capture_instant(case, opens, closes) < closes


class TestTheExperiment:
    def test_it_is_running(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=10)
        experiment = db_session.get(Experiment, run.experiment_id)
        assert experiment is not None
        assert experiment.status == ExperimentStatus.RUNNING.value
        assert experiment.locked_at is not None

    def test_it_is_benchmark_scoped_not_the_registered_one(self, db_session: Session) -> None:
        """The pre-registration in docs/ stays DRAFT. This is synthetic."""
        run = materialise(db_session, case_count=10)
        experiment = db_session.get(Experiment, run.experiment_id)
        assert experiment is not None
        assert experiment.name.startswith("BENCH-")
        assert "EXP-001" not in experiment.name

    def test_the_split_is_even(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=10)
        experiment = db_session.get(Experiment, run.experiment_id)
        assert experiment is not None
        assert experiment.holdout_bps == BENCHMARK_HOLDOUT_BPS

    def test_both_arms_fill(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        assert run.treatment > 0
        assert run.holdout > 0
        assert run.treatment + run.holdout == SIZE

    def test_the_split_is_near_even_at_scale(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=400)
        assert abs(run.treatment - run.holdout) < 80

    def test_every_case_is_enrolled(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        stored = db_session.execute(
            select(func.count())
            .select_from(CaseAssignment)
            .where(CaseAssignment.experiment_id == run.experiment_id)
        ).scalar()
        assert stored == SIZE


class TestArmToAction:
    def test_the_holdout_gets_nothing(self) -> None:
        assert action_for(Arm.HOLDOUT.value) is Action.NO_ACTION

    def test_the_treated_arm_gets_a_payment_link(self) -> None:
        assert action_for(Arm.TREATMENT.value) is Action.CREATE_PAYMENT_LINK
        assert BENCHMARK_ACTION is Action.CREATE_PAYMENT_LINK

    def test_the_fixture_was_built_for_the_same_action(self) -> None:
        """Otherwise the known effect would answer a different question."""
        assert read_fixture()["action"] == BENCHMARK_ACTION.value

    def test_only_the_treated_arm_records_an_action(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        rows = db_session.execute(
            select(CaseAssignment.arm, CaseOutcome.actions_executed, CaseOutcome.contacts_made)
            .join(CaseOutcome, CaseOutcome.risk_id == CaseAssignment.risk_id)
            .where(CaseAssignment.experiment_id == run.experiment_id)
        ).all()

        for arm, actions, contacts in rows:
            expected = 0 if arm == Arm.HOLDOUT.value else 1
            assert actions == expected
            assert contacts == expected


class TestRecoveryIsDerived:
    def test_the_stored_outcome_matches_the_timeline(self, db_session: Session) -> None:
        """Read the captures back independently and confirm they agree. If the
        write path had simply copied the answer key this would still pass —
        which is why the late-capture test below exists as well."""
        run = materialise(db_session, case_count=SIZE)
        risks = db_session.execute(
            select(RevenueRisk).where(RevenueRisk.merchant_id == run.merchant_id)
        ).scalars()

        for risk in risks:
            outcome = (
                db_session.execute(select(CaseOutcome).where(CaseOutcome.risk_id == risk.id))
                .scalars()
                .one()
            )
            derived = observed_recovery_for_risk(db_session, risk)
            assert outcome.recovered == derived.recovered
            assert outcome.recovered_amount == derived.amount
            assert outcome.recovered_at == derived.at

    def test_a_capture_planted_after_the_window_is_not_a_recovery(
        self, db_session: Session
    ) -> None:
        """The boundary is real, not decorative."""
        run = materialise(db_session, case_count=10)
        risk = (
            db_session.execute(
                select(RevenueRisk)
                .where(RevenueRisk.merchant_id == run.merchant_id)
                .order_by(RevenueRisk.id)
            )
            .scalars()
            .first()
        )
        assert risk is not None

        before = observed_recovery_for_risk(db_session, risk)
        db_session.add(
            PaymentAttempt(
                order_id=risk.order_id,
                amount=999_999,
                currency="INR",
                payment_method="card",
                provider="benchmark",
                status=PaymentStatus.CAPTURED.value,
                attempt_number=9,
                attempted_at=risk.detected_at + timedelta(hours=200),
            )
        )
        db_session.flush()

        after = observed_recovery_for_risk(db_session, risk)
        assert after.recovered == before.recovered
        assert after.amount == before.amount

    def test_a_recovery_carries_its_instant(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        outcomes = db_session.execute(
            select(CaseOutcome).where(CaseOutcome.risk_id.in_(run.risk_ids))
        ).scalars()

        for outcome in outcomes:
            if outcome.recovered:
                assert outcome.recovered_at is not None
                assert outcome.recovered_amount > 0
                assert outcome.window_opens_at <= outcome.recovered_at
                assert outcome.recovered_at < outcome.window_closes_at
            else:
                assert outcome.recovered_at is None
                assert outcome.recovered_amount == 0

    def test_the_recovered_count_matches_the_captures_planted(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        recovered = db_session.execute(
            select(func.count())
            .select_from(CaseOutcome)
            .where(CaseOutcome.risk_id.in_(run.risk_ids), CaseOutcome.recovered.is_(True))
        ).scalar()
        assert recovered == run.captures_planted

    def test_some_but_not_all_units_recover(self, db_session: Session) -> None:
        """A run where nobody or everybody recovers would make the estimator
        untestable, and would mean the generator was misconfigured."""
        run = materialise(db_session, case_count=SIZE)
        assert 0 < run.captures_planted < SIZE

    def test_the_bridge_never_calls_the_recovery_amount_helper(self) -> None:
        """`risk_engine.recovered_amount` requires a recovery action, which a
        held-out unit can never have. Using it would score every control zero.

        Checked on the syntax tree rather than the source text: the module
        docstring explains why the helper is avoided, and a substring scan
        would flag that explanation as the violation it describes."""
        from tests.benchmark import bridge

        tree = _tree_of(bridge)
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)

        assert "recovered_amount" not in called
        assert not any("risk_engine" in module for module in _imports_of(bridge))


class TestTruthStaysSeparate:
    def test_both_potential_outcomes_are_recorded(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        outcomes = list(
            db_session.execute(
                select(CaseOutcome).where(CaseOutcome.risk_id.in_(run.risk_ids))
            ).scalars()
        )
        assert all(o.truth_y0 is not None and o.truth_y1 is not None for o in outcomes)
        assert all(o.truth_segment is not None for o in outcomes)

    def test_truth_holds_what_no_system_could_observe(self, db_session: Session) -> None:
        """Both arms' outcomes for the same unit. That is the answer key, and
        the reason nothing under app/causal may read it."""
        run = materialise(db_session, case_count=200)
        outcomes = list(
            db_session.execute(
                select(CaseOutcome).where(CaseOutcome.risk_id.in_(run.risk_ids))
            ).scalars()
        )
        assert any(o.truth_y0 != o.truth_y1 for o in outcomes)

    def test_the_observed_value_is_the_assigned_arm_only(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        rows = db_session.execute(
            select(CaseAssignment.arm, CaseOutcome)
            .join(CaseOutcome, CaseOutcome.risk_id == CaseAssignment.risk_id)
            .where(CaseAssignment.experiment_id == run.experiment_id)
        ).all()

        for arm, outcome in rows:
            expected = outcome.truth_y0 if arm == Arm.HOLDOUT.value else outcome.truth_y1
            assert outcome.recovered == expected

    def test_more_than_one_planted_stratum_appears(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=200)
        labels = {
            outcome.truth_segment
            for outcome in db_session.execute(
                select(CaseOutcome).where(CaseOutcome.risk_id.in_(run.risk_ids))
            ).scalars()
        }
        assert len(labels) >= 5


class TestSealingAndHandoff:
    def test_every_window_is_sealed(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        unsealed = db_session.execute(
            select(func.count())
            .select_from(CaseOutcome)
            .where(CaseOutcome.risk_id.in_(run.risk_ids), CaseOutcome.sealed.is_(False))
        ).scalar()
        assert unsealed == 0
        assert run.outcomes_sealed == SIZE

    def test_the_analysis_layer_accepts_the_result(self, db_session: Session) -> None:
        """The point of the whole gate: a population the estimators can load
        without the loader refusing it."""
        run = materialise(db_session, case_count=SIZE)
        population = load_population(db_session, run.experiment_id)

        assert population.n_enrolled == SIZE
        assert population.itt.treatment.n == run.treatment
        assert population.itt.holdout.n == run.holdout
        assert population.itt.excluded_total == 0

    def test_per_protocol_matches_itt_with_no_execution_layer(self, db_session: Session) -> None:
        """Nothing executes on Day 4, so nothing is non-compliant."""
        run = materialise(db_session, case_count=SIZE)
        population = load_population(db_session, run.experiment_id)
        assert population.per_protocol.n_total == population.itt.n_total
        assert population.per_protocol.non_compliance_bps == 0

    def test_the_windows_are_seventy_two_hours(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=20)
        outcomes = db_session.execute(
            select(CaseOutcome).where(CaseOutcome.risk_id.in_(run.risk_ids))
        ).scalars()
        for outcome in outcomes:
            assert outcome.window_closes_at - outcome.window_opens_at == timedelta(hours=72)


class TestItCreatesNoRecoveryState:
    def test_no_recovery_case_is_created(self, db_session: Session) -> None:
        materialise(db_session, case_count=SIZE)
        assert db_session.execute(select(func.count()).select_from(RecoveryCase)).scalar() == 0

    def test_no_recovery_action_is_created(self, db_session: Session) -> None:
        materialise(db_session, case_count=SIZE)
        assert db_session.execute(select(func.count()).select_from(RecoveryAction)).scalar() == 0

    def test_the_bridge_names_no_recovery_model(self) -> None:
        """`Experiment` is read-only here, for the duplicate-run guard. Neither
        recovery model is reachable at all."""
        from tests.benchmark import bridge

        imported: set[str] = set()
        for node in ast.walk(_tree_of(bridge)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                imported.update(alias.name for alias in node.names)

        assert imported == {
            "Customer",
            "Experiment",
            "Order",
            "PaymentAttempt",
            "RevenueRisk",
        }
        assert "RecoveryCase" not in imported
        assert "RecoveryAction" not in imported

    def test_no_action_is_approved_or_executed(self) -> None:
        from tests.benchmark import bridge

        identifiers = _identifiers_of(bridge)
        for banned in ("approve", "approved", "policy_status", "execute_action", "recommend"):
            assert banned not in identifiers, banned


class TestTheApplicationStaysClean:
    def test_no_application_file_imports_the_generator(self) -> None:
        """The reason the bridge lives under tests/ at all."""
        app_root = pathlib.Path(inspect.getfile(load_population)).resolve().parents[2] / "app"
        for path in app_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "import simulator" not in source, path
            assert "from simulator" not in source, path

    def test_the_bridge_is_not_inside_the_application(self) -> None:
        from tests.benchmark import bridge

        assert "/app/" not in inspect.getfile(bridge).replace("\\", "/")

    def test_the_analysis_module_is_untouched_by_this_gate(self) -> None:
        """Gate 4's contract has to keep holding: the loader reads rows and has
        never heard of a planted stratum. On the syntax tree, because the
        module documents the rule it obeys and a text scan would trip on it."""
        from app.causal import analysis

        assert not any(module.startswith("simulator") for module in _imports_of(analysis))
        assert not any(name.startswith("truth_") for name in _identifiers_of(analysis))


class TestOrdersAndAttempts:
    def test_each_case_gets_an_order(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=30)
        orders = db_session.execute(
            select(func.count()).select_from(Order).where(Order.merchant_id == run.merchant_id)
        ).scalar()
        assert orders == 30

    def test_each_case_has_two_failed_attempts(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=30)
        failed = db_session.execute(
            select(func.count())
            .select_from(PaymentAttempt)
            .join(Order, Order.id == PaymentAttempt.order_id)
            .where(
                Order.merchant_id == run.merchant_id,
                PaymentAttempt.status == PaymentStatus.FAILED.value,
            )
        ).scalar()
        assert failed == 60

    def test_the_payment_method_is_a_usable_covariate(self, db_session: Session) -> None:
        """It carries the generator's own draw, which is what makes
        `payment_method` usable in the balance table."""
        run = materialise(db_session, case_count=60)
        methods = {
            method
            for (method,) in db_session.execute(
                select(PaymentAttempt.payment_method)
                .join(Order, Order.id == PaymentAttempt.order_id)
                .where(Order.merchant_id == run.merchant_id)
            ).all()
        }
        assert methods <= {"card", "upi", "netbanking"}
        assert len(methods) > 1

    def test_every_risk_is_a_repeated_payment_failure(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=20)
        types = {
            risk.risk_type
            for risk in db_session.execute(
                select(RevenueRisk).where(RevenueRisk.merchant_id == run.merchant_id)
            ).scalars()
        }
        assert types == {RiskType.REPEATED_PAYMENT_FAILURE.value}


class TestValidation:
    def test_a_zero_case_count_is_refused(self, db_session: Session) -> None:
        with pytest.raises(BridgeError, match="at least 1"):
            materialise(db_session, case_count=0)

    def test_the_run_summary_serialises_without_a_float(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=10)
        for value in run.as_dict().values():
            assert not isinstance(value, float), value

    def test_the_run_summary_is_frozen(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=4)
        with pytest.raises(AttributeError):
            run.seed = 1  # type: ignore[misc]

    def test_nothing_leaks_into_the_development_database(self, db_session: Session) -> None:
        """Belt and braces: the harness DSN guard, the bridge guard, and this."""
        name = db_session.execute(text("SELECT current_database()")).scalar_one()
        assert name != "revtrace_dev"
