"""The evaluation reporter, and the run that produces it.

Checked here: that the report is labelled synthetic everywhere a reader could
look, that the ground-truth section reads the answer key correctly, that the
deferred sections are listed rather than omitted, that no `experiment_results`
row is written, and that the reporter is the only file in the application that
touches a truth column.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ExperimentResult, RecoveryAction, RecoveryCase
from app.models.case_outcome import TRUTH_COLUMNS
from app.reporting.evaluation import (
    DEFERRED_SECTIONS,
    LIMITATIONS,
    SYNTHETIC_LABEL,
    EvaluationError,
    ground_truth,
    ground_truth_by_stratum,
    render_json,
    render_markdown,
    sections_of,
)
from tests.benchmark.bridge import materialise
from tests.benchmark.report import (
    ACCEPTANCE_CASE_COUNT,
    run_benchmark,
    summarise,
    write_evaluation,
)

pytestmark = pytest.mark.db

#: Big enough for a stable estimate, small enough to stay in the fast suite.
#: The fixture is function-scoped — `db_session` rolls back per test — so this
#: population is rebuilt for every test that uses it, and its size is the main
#: cost of this module.
SIZE = 300

#: Above the pre-registered plan of 384 *per arm*, so this run is not flagged
#: underpowered. `SIZE` is 150 per arm and is.
POWERED_SIZE = 800

#: Fewer resamples than the pre-registered 10,000, for speed. The acceptance
#: run uses the full count.
FAST_RESAMPLES = 150


@pytest.fixture
def outcome(db_session: Session):  # noqa: ANN201
    return run_benchmark(db_session, case_count=SIZE, resamples=FAST_RESAMPLES)


class TestGroundTruth:
    def test_it_reads_both_potential_outcomes(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        truth = ground_truth(db_session, run.experiment_id)

        assert truth.n == SIZE
        assert truth.true_ate_bps == truth.y1_rate_bps - truth.y0_rate_bps
        assert truth.true_harm_ate_bps == truth.harm1_rate_bps - truth.harm0_rate_bps

    def test_the_self_recovery_share_is_the_untreated_rate(self, db_session: Session) -> None:
        """The number the pitch turns on: money that arrives with no help."""
        run = materialise(db_session, case_count=SIZE)
        truth = ground_truth(db_session, run.experiment_id)
        assert truth.self_recovery_share_bps == truth.y0_rate_bps
        assert truth.self_recovery_share_bps > 0

    def test_it_reports_a_positive_planted_effect(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        assert ground_truth(db_session, run.experiment_id).true_ate_bps > 0

    def test_it_breaks_down_by_planted_stratum(self, db_session: Session) -> None:
        run = materialise(db_session, case_count=SIZE)
        strata = ground_truth_by_stratum(db_session, run.experiment_id)

        assert len(strata) >= 5
        assert sum(stratum.n for stratum in strata) == SIZE
        assert [s.label for s in strata] == sorted(s.label for s in strata)

    def test_a_planted_negative_or_harmful_stratum_shows_up(self, db_session: Session) -> None:
        """One stratum is planted to be harmed by contact. The report has to be
        able to show it, or the headline claim has nothing behind it."""
        run = materialise(db_session, case_count=1_200)
        strata = ground_truth_by_stratum(db_session, run.experiment_id)
        assert any(stratum.true_harm_ate_bps > 0 for stratum in strata)

    def test_an_experiment_with_no_outcomes_is_refused(self, db_session: Session) -> None:
        import uuid

        with pytest.raises(EvaluationError, match="no outcomes"):
            ground_truth(db_session, uuid.uuid4())


class TestTheReport:
    def test_it_builds(self, outcome) -> None:  # noqa: ANN001
        assert outcome.report.experiment_id == outcome.run.experiment_id
        assert outcome.report.population.n_enrolled == SIZE

    def test_the_arms_match_the_run(self, outcome) -> None:  # noqa: ANN001
        assert outcome.report.recovery.n_treatment == outcome.run.treatment
        assert outcome.report.recovery.n_holdout == outcome.run.holdout

    def test_the_ledger_is_internally_consistent(self, outcome) -> None:  # noqa: ANN001
        ledger = outcome.report.ledger
        assert ledger.credited_not_earned == ledger.gross_recovered - ledger.incremental_recovered

    def test_the_estimate_lands_near_the_planted_effect(self, outcome) -> None:  # noqa: ANN001
        """Reported as a fact. The pre-registration sets no acceptance
        threshold, and this suite does not invent one — but an estimator that
        missed by tens of percentage points would be broken, not merely noisy."""
        error = abs(outcome.report.ate_error_bps)
        assert error < 1_500, f"estimated {outcome.report.recovery.ate_bps}bps"

    def test_every_headline_number_has_an_interval(self, outcome) -> None:  # noqa: ANN001
        report = outcome.report
        for effect in (report.recovery, report.harm, report.per_protocol_recovery):
            assert effect.interval.low <= effect.ate_bps <= effect.interval.high
        assert report.ledger.interval.contains(report.ledger.incremental_recovered)

    def test_the_interval_carries_its_seed(self, outcome) -> None:  # noqa: ANN001
        assert outcome.report.recovery.interval.seed == outcome.report.bootstrap_seed
        assert outcome.report.bootstrap_resamples == FAST_RESAMPLES

    def test_power_is_reported_against_the_plan(self, outcome) -> None:  # noqa: ANN001
        report = outcome.report
        assert report.achieved_n_per_arm == min(
            report.population.itt.treatment.n, report.population.itt.holdout.n
        )
        assert report.planned_n_per_arm == 384
        assert report.detectable_mde_bps > 0

    def test_a_small_run_is_flagged_underpowered(self, db_session: Session) -> None:
        small = run_benchmark(db_session, case_count=100, resamples=FAST_RESAMPLES)
        assert small.report.is_underpowered
        assert small.report.achieved_n_per_arm < small.report.planned_n_per_arm

    def test_the_default_size_is_underpowered_because_of_the_split(self, outcome) -> None:  # noqa: ANN001
        """300 cases is 150 per arm, and the plan asks for 384 *per arm*. The
        label keys on the per-arm figure, not the total."""
        assert outcome.report.achieved_n_per_arm < 384
        assert outcome.report.is_underpowered

    def test_a_run_above_the_plan_is_not_flagged(self, db_session: Session) -> None:
        powered = run_benchmark(db_session, case_count=POWERED_SIZE, resamples=FAST_RESAMPLES)
        assert powered.report.achieved_n_per_arm >= 384
        assert not powered.report.is_underpowered

    def test_it_writes_no_experiment_result(self, outcome, db_session: Session) -> None:  # noqa: ANN001
        """Building a report is a read. Persisting is a separate, explicit call.

        `experiment_result_repository.persist_result` exists and is tested in
        `tests/integration/test_day6_result_persistence.py`; the point here is
        that the reporter does not invoke it as a side effect. A read that wrote
        rows would make every report a mutation.
        """
        db_session.flush()
        assert db_session.execute(select(func.count()).select_from(ExperimentResult)).scalar() == 0

    def test_it_creates_no_recovery_state(self, outcome, db_session: Session) -> None:  # noqa: ANN001
        db_session.flush()
        assert db_session.execute(select(func.count()).select_from(RecoveryCase)).scalar() == 0
        assert db_session.execute(select(func.count()).select_from(RecoveryAction)).scalar() == 0


class TestTheMarkdown:
    def test_the_header_is_the_synthetic_label(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        assert markdown.startswith(f"# {SYNTHETIC_LABEL}")
        assert SYNTHETIC_LABEL == "SYNTHETIC / DEMO EVALUATION"

    def test_it_says_so_again_in_prose(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        assert "generated population" in markdown
        assert "None of it is evidence about real customers." in markdown

    def test_the_three_headline_numbers_appear(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        assert "Gross recovered" in markdown
        assert "Incremental recovered" in markdown
        assert "Credited-not-earned" in markdown

    def test_the_required_sections_are_present(self, outcome) -> None:  # noqa: ANN001
        headings = " ".join(sections_of(render_markdown(outcome.report)))
        for expected in (
            "Experiment",
            "Power",
            "Covariate balance",
            "headline numbers",
            "recovery rate",
            "Harm",
            "per-protocol",
            "known effect",
            "Reproduction",
            "Deferred",
            "Limitations",
        ):
            assert expected in headings, expected

    def test_deferred_sections_are_listed_with_reasons(self, outcome) -> None:  # noqa: ANN001
        """A gap a reader can see is a gap; a gap they cannot see is a claim."""
        markdown = render_markdown(outcome.report)
        for name, reason in DEFERRED_SECTIONS:
            assert name in markdown
            assert reason.split(",")[0] in markdown

    def test_the_missing_economics_are_named_not_zeroed(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        assert "gross margin" in markdown
        assert "lifetime value" in markdown
        assert "Net incremental value P&L" in markdown

    def test_the_ground_truth_section_says_why_it_exists(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        assert "Only possible because the data is synthetic" in markdown
        assert "no production system can produce this section" in markdown.lower()

    def test_the_limitations_are_all_stated(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        for limitation in LIMITATIONS:
            assert limitation in markdown

    def test_the_reproduction_section_names_the_seed(self, outcome) -> None:  # noqa: ANN001
        markdown = render_markdown(outcome.report)
        assert str(outcome.report.bootstrap_seed) in markdown
        assert "Percentile method" in markdown

    def test_a_zero_p_value_renders_as_a_bound(self, outcome) -> None:  # noqa: ANN001
        """Below the stored resolution is not "impossible"."""
        from app.reporting.evaluation import _p_value

        assert _p_value(0) == "< 0.000001"
        assert "0.000000" not in render_markdown(outcome.report).split("p-value")[-1][:40]

    def test_it_renders_deterministically(self, outcome) -> None:  # noqa: ANN001
        assert render_markdown(outcome.report) == render_markdown(outcome.report)

    def test_no_float_appears_in_the_formatting_helpers(self) -> None:
        from app.reporting.evaluation import _bps, _money

        assert _bps(1_409) == "14.09%"
        assert _bps(-64) == "-0.64%"
        assert _money(451_200) == "Rs 4,512.00"
        assert _money(-100) == "-Rs 1.00"


class TestTheJson:
    def test_it_carries_the_label(self, outcome) -> None:  # noqa: ANN001
        assert render_json(outcome.report)["label"] == SYNTHETIC_LABEL

    def test_it_is_serialisable(self, outcome) -> None:  # noqa: ANN001
        assert json.loads(json.dumps(render_json(outcome.report)))

    def test_it_carries_no_float(self, outcome) -> None:  # noqa: ANN001
        def walk(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(render_json(outcome.report))

    def test_the_accuracy_block_states_both_numbers(self, outcome) -> None:  # noqa: ANN001
        accuracy = render_json(outcome.report)["accuracy"]
        assert accuracy["estimated_ate_bps"] == outcome.report.recovery.ate_bps
        assert accuracy["true_ate_bps"] == outcome.report.truth.true_ate_bps
        assert accuracy["error_bps"] == accuracy["estimated_ate_bps"] - accuracy["true_ate_bps"]

    def test_the_deferred_list_is_carried(self, outcome) -> None:  # noqa: ANN001
        deferred = render_json(outcome.report)["deferred"]
        assert len(deferred) == len(DEFERRED_SECTIONS)
        assert all(entry["reason"] for entry in deferred)


class TestTheRunner:
    def test_it_summarises(self, outcome) -> None:  # noqa: ANN001
        text = summarise(outcome)
        assert "estimated ATE" in text
        assert "true ATE" in text
        assert "credited-not-earned" in text

    def test_the_headline_names_both_numbers(self, outcome) -> None:  # noqa: ANN001
        assert "vs true" in outcome.headline

    def test_the_acceptance_size_is_within_the_planned_range(self) -> None:
        assert 8_000 <= ACCEPTANCE_CASE_COUNT <= 12_000

    def test_writing_is_a_deliberate_entry_point(self, outcome, tmp_path) -> None:  # noqa: ANN001
        """Tests render to a string; only an explicit run writes into docs/."""
        target = tmp_path / "EVALUATION.md"
        written = write_evaluation(outcome, markdown_path=target, json_path=None)
        assert written == target
        assert target.read_text(encoding="utf-8").startswith(f"# {SYNTHETIC_LABEL}")

    def test_the_orchestrator_never_touches_the_answer_key(self) -> None:
        """Truth reaches the report through the permitted reader, not around it."""
        from tests.benchmark import report as module

        tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        assert not any(name.startswith("truth_") for name in names)
        assert not any(name in names for name in TRUTH_COLUMNS)


class TestTheTruthBoundary:
    def test_the_reporter_is_the_only_application_reader(self) -> None:
        from app.reporting import evaluation

        app_root = pathlib.Path(inspect.getfile(evaluation)).resolve().parents[1]
        readers: set[str] = set()

        for path in sorted(app_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                named = (
                    node.id
                    if isinstance(node, ast.Name)
                    else node.attr
                    if isinstance(node, ast.Attribute)
                    else node.value
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    else None
                )
                if named in TRUTH_COLUMNS:
                    readers.add(path.relative_to(app_root).as_posix())

        assert readers == {"models/case_outcome.py", "reporting/evaluation.py"}

    def test_the_reporter_does_not_import_the_generator(self) -> None:
        from app.reporting import evaluation

        tree = ast.parse(pathlib.Path(inspect.getfile(evaluation)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("simulator"), node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("simulator"), alias.name

    def test_the_estimators_still_cannot_see_it(self) -> None:
        """Widening the allowlist by one file must not have loosened anything
        else. The estimator layer stays blind."""
        from app.causal import analysis, balance, estimators

        for module in (analysis, balance, estimators):
            tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    assert not node.id.startswith("truth_")
                elif isinstance(node, ast.Attribute):
                    assert not node.attr.startswith("truth_")

    def test_the_reporter_writes_nothing(self) -> None:
        from app.reporting import evaluation

        tree = ast.parse(pathlib.Path(inspect.getfile(evaluation)).read_text(encoding="utf-8"))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)

        for banned in ("add", "add_all", "commit", "flush", "merge", "delete"):
            assert banned not in called, banned
