"""The Day 5 reporting additions: Qini, quadrants, and the confusion matrix.

The property that matters most here is the direction of information flow. The
answer key may be read *after* every label has been decided, to say how well the
labels line up — and never before, by anything that produces one. These tests
check both halves: that the matrix really is built from `truth_segment`, and
that nothing the model consumed could have carried it.

Everything runs on revtrace_test inside the rolled-back transaction, at a size
small enough for the fast suite. The acceptance run is a separate, deliberate
step; nothing here writes to `docs/`.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.causal import cells as cells_module
from app.causal.analysis import load_population
from app.causal.cells import Features, Unit
from app.causal.qini import round_half_up
from app.causal.uplift import MODEL_VERSION, load_units
from app.models import ExperimentResult, UpliftScore
from app.models.case_outcome import TRUTH_COLUMNS
from app.models.enums import Quadrant
from app.reporting import evaluation as evaluation_module
from app.reporting.evaluation import (
    QUADRANT_ORDER,
    SEGMENT_PERSUADABLE,
    SEGMENT_SELF_RECOVERING,
    SEGMENT_SLEEPING_DOG,
    SYNTHETIC_LABEL,
    build_report,
    build_uplift_report,
    render_json,
    render_markdown,
    sections_of,
)
from tests.benchmark.bridge import materialise

pytestmark = pytest.mark.db

#: Large enough for several cells to qualify and for all seven planted strata to
#: appear, small enough for the fast suite. Every test rebuilds this population
#: because `db_session` rolls back per test, so the size is this module's whole
#: cost — and a module-scoped fixture is not the fix, for the reason recorded in
#: BREAKAGE entry 18.
SIZE = 400

#: Far below the pre-registered 10,000. The acceptance run uses the full count.
FAST_RESAMPLES = 40

EVALUATION_SOURCE = pathlib.Path(evaluation_module.__file__)
CAUSAL_ROOT = pathlib.Path(cells_module.__file__).parent


@pytest.fixture
def report(db_session: Session):  # noqa: ANN201
    run = materialise(db_session, case_count=SIZE)
    return build_uplift_report(db_session, run.experiment_id, resamples=FAST_RESAMPLES)


@pytest.fixture
def experiment_id(db_session: Session):  # noqa: ANN201
    return materialise(db_session, case_count=SIZE).experiment_id


# -- the answer key, and where it may not go ------------------------------


class TestTruthReachesOnlyTheReport:
    def test_the_confusion_matrix_is_built_from_truth_segment(self) -> None:
        """Stated positively: the matrix genuinely reads the column."""
        source = inspect.getsource(evaluation_module.confusion_matrix)
        assert "truth_segment" in source

    def test_no_causal_module_names_a_truth_column(self) -> None:
        """The guard that makes every number non-circular.

        If the estimator could read the answer, its accuracy would be a
        tautology rather than a measurement.
        """
        offenders: dict[str, set[str]] = {}
        for path in sorted(CAUSAL_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in TRUTH_COLUMNS:
                    found.add(node.id)
                elif isinstance(node, ast.Attribute) and node.attr in TRUTH_COLUMNS:
                    found.add(node.attr)
                elif isinstance(node, ast.Constant) and node.value in TRUTH_COLUMNS:
                    found.add(str(node.value))
            if found:
                offenders[path.name] = found
        assert offenders == {}

    def test_the_model_input_type_cannot_carry_truth(self, db_session: Session) -> None:
        """Structural, not incidental: there is no field for it to arrive in."""
        unit_fields = {field.name for field in dataclasses.fields(Unit)}
        feature_fields = {field.name for field in dataclasses.fields(Features)}
        assert not any(name.startswith("truth") for name in unit_fields | feature_fields)
        assert set(TRUTH_COLUMNS) & (unit_fields | feature_fields) == set()

    def test_the_loaded_units_carry_no_truth_values(
        self, db_session: Session, experiment_id
    ) -> None:
        """And at runtime the loaded objects hold none either."""
        units = load_units(db_session, experiment_id)
        assert units
        for unit in units[:50]:
            assert not any(hasattr(unit.features, column) for column in TRUTH_COLUMNS)


# -- the confusion matrix -------------------------------------------------


class TestConfusionMatrix:
    def test_every_segment_row_lists_all_five_quadrants(self, report) -> None:
        """Explicitly, including the ones nothing landed in.

        A quadrant that silently vanished from the table would be
        indistinguishable from one that was never evaluated.
        """
        assert len(QUADRANT_ORDER) == 5
        for segment in report.confusion.strata:
            assert set(segment.counts) == set(QUADRANT_ORDER)

    def test_empty_quadrants_are_named(self, report) -> None:
        empty = set(report.confusion.empty_quadrants)
        for label in QUADRANT_ORDER:
            total = sum(row.counts[label] for row in report.confusion.strata)
            assert (label in empty) == (total == 0)

    def test_each_row_sums_to_its_segment_size(self, report) -> None:
        for segment in report.confusion.strata:
            assert sum(segment.counts.values()) == segment.n

    def test_the_matrix_covers_every_scored_unit(self, report) -> None:
        assert sum(segment.n for segment in report.confusion.strata) == report.n_scored

    def test_segments_are_ordered(self, report) -> None:
        labels = [segment.label for segment in report.confusion.strata]
        assert labels == sorted(labels)

    def test_the_planted_strata_are_present(self, report) -> None:
        labels = {segment.label for segment in report.confusion.strata}
        assert SEGMENT_SLEEPING_DOG in labels
        assert SEGMENT_SELF_RECOVERING in labels
        assert SEGMENT_PERSUADABLE in labels

    def test_the_modal_quadrant_is_the_most_common_one(self, report) -> None:
        for segment in report.confusion.strata:
            assert segment.counts[segment.modal_quadrant] == max(segment.counts.values())


class TestAcceptanceCriterion:
    def test_all_three_clauses_are_evaluated(self, report) -> None:
        assert len(report.acceptance) == 3
        assert all(isinstance(criterion.passed, bool) for criterion in report.acceptance)

    def test_the_clauses_name_the_right_strata(self, report) -> None:
        names = " ".join(criterion.name for criterion in report.acceptance)
        assert SEGMENT_SLEEPING_DOG in names
        assert SEGMENT_SELF_RECOVERING in names
        assert SEGMENT_PERSUADABLE in names

    def test_the_self_recovering_clause_is_an_exclusion_not_a_label(self, report) -> None:
        """It must not require SURE_THING.

        At a size where the planted 3pp lift is detectable, that stratum's
        interval excludes zero and the null-effect rules cannot fire. Demanding
        Sure Thing would make the criterion pass only while the study was too
        small to work.
        """
        clause = next(c for c in report.acceptance if SEGMENT_SELF_RECOVERING in c.name)
        assert Quadrant.PERSUADABLE.value in clause.expected
        assert "anything but" in clause.expected

        segment = report.confusion.segment(SEGMENT_SELF_RECOVERING)
        assert segment is not None
        assert clause.passed == (segment.modal_quadrant != Quadrant.PERSUADABLE.value)

    def test_the_ranking_clause_compares_mean_uplift(self, report) -> None:
        clause = next(c for c in report.acceptance if "mean uplift" in c.name)
        low = report.confusion.segment(SEGMENT_SELF_RECOVERING)
        high = report.confusion.segment(SEGMENT_PERSUADABLE)
        assert low is not None and high is not None
        assert clause.passed == (low.mean_uplift_bps < high.mean_uplift_bps)

    def test_acceptance_is_reported_not_enforced(self, report) -> None:
        """Building a report never raises on a failing clause.

        The result is a measurement. A reporter that refused to produce output
        unless the numbers came out right would be manufacturing the outcome.
        """
        assert isinstance(report.accepted, bool)
        assert report.accepted == all(c.passed for c in report.acceptance)


# -- observed data only ---------------------------------------------------


class TestQiniComesFromObservedOutcomes:
    def test_the_curve_covers_the_whole_enrolled_population(self, report) -> None:
        assert report.qini.curve.n == report.n_scored
        assert report.qini.curve.n_treated + report.qini.curve.n_holdout == report.n_scored

    def test_the_total_matches_an_independent_recount(
        self, db_session: Session, experiment_id
    ) -> None:
        """Q(N) recomputed by hand from the stored arms and outcomes.

        If the curve were reading anything but observed data, these would differ.
        """
        report = build_uplift_report(db_session, experiment_id, resamples=FAST_RESAMPLES)
        rows = load_population(db_session, experiment_id).rows

        treated = [row for row in rows if row.is_treatment]
        holdout = [row for row in rows if not row.is_treatment]
        expected = sum(1 for row in treated if row.recovered) - round_half_up(
            sum(1 for row in holdout if row.recovered) * len(treated), len(holdout)
        )
        assert report.qini.curve.total == expected

    def test_the_amount_capture_totals_the_recovered_money(
        self, db_session: Session, experiment_id
    ) -> None:
        report = build_uplift_report(db_session, experiment_id, resamples=FAST_RESAMPLES)
        rows = load_population(db_session, experiment_id).rows

        treated = [row for row in rows if row.is_treatment]
        holdout = [row for row in rows if not row.is_treatment]
        expected = sum(row.recovered_amount for row in treated) - round_half_up(
            sum(row.recovered_amount for row in holdout) * len(treated), len(holdout)
        )
        assert report.top_amount_capture.total == expected

    def test_the_two_weightings_are_reported_separately(self, report) -> None:
        """Count and money answer different questions and must not be merged."""
        assert report.top_capture.share_bps == report.top_amount_capture.share_bps
        assert report.top_capture.k == report.top_amount_capture.k
        assert report.top_capture.total != report.top_amount_capture.total

    def test_an_undefined_capture_is_none_not_zero(self, report) -> None:
        for capture in (report.top_capture, report.top_amount_capture):
            assert (capture.capture_bps is None) == (capture.total == 0)


# -- the breakdowns -------------------------------------------------------


class TestGrayZoneReasons:
    KNOWN_REASONS = {
        cells_module.QUALIFIED,
        cells_module.EMPTY_ARM,
        cells_module.NO_ROOM_FOR_EFFECT,
        cells_module.DEGENERATE_RATIO,
        cells_module.UNDERPOWERED,
        "global_fallback",
    }

    def test_the_total_matches_the_quadrant_count(self, report) -> None:
        assert report.gray_zone.total == report.quadrant_counts[Quadrant.GRAY_ZONE.value]

    def test_reasons_are_preserved_not_collapsed(self, report) -> None:
        assert set(report.gray_zone.by_reason) <= self.KNOWN_REASONS
        assert sum(report.gray_zone.by_reason.values()) == report.gray_zone.total

    def test_rules_are_preserved_alongside_reasons(self, report) -> None:
        """Two different situations, counted apart."""
        assert sum(report.gray_zone.by_rule.values()) == report.gray_zone.total
        assert set(report.gray_zone.by_rule) <= {"not_qualifying", "undecided"}


class TestQuadrantCounts:
    def test_all_five_are_present_even_at_zero(self, report) -> None:
        assert set(report.quadrant_counts) == set(QUADRANT_ORDER)

    def test_they_sum_to_the_scored_population(self, report) -> None:
        assert sum(report.quadrant_counts.values()) == report.n_scored

    def test_rule_counts_sum_to_the_same_population(self, report) -> None:
        assert sum(report.rule_counts.values()) == report.n_scored


class TestLadderUsage:
    def test_levels_sum_to_the_scored_population(self, report) -> None:
        assert sum(report.ladder.by_level.values()) == report.n_scored

    def test_cells_sum_to_the_scored_population(self, report) -> None:
        assert sum(n for _, n in report.ladder.cells) == report.n_scored
        assert report.ladder.distinct_cells == len(report.ladder.cells)

    def test_cells_are_ordered_by_size_then_name(self, report) -> None:
        assert list(report.ladder.cells) == sorted(
            report.ladder.cells, key=lambda item: (-item[1], item[0])
        )


class TestHarmSummary:
    def test_it_covers_every_scored_unit(self, report) -> None:
        assert report.harm.n == report.n_scored

    def test_the_mean_lies_within_the_range(self, report) -> None:
        assert report.harm.min_bps <= report.harm.mean_bps <= report.harm.max_bps


class TestFoldThresholds:
    def test_one_set_per_fold(self, report) -> None:
        assert len(report.thresholds) == report.folds
        assert [t.fold for t in report.thresholds] == sorted(t.fold for t in report.thresholds)

    def test_each_is_derived_from_its_training_folds(self, report) -> None:
        """Training size is the population minus the held-out fold, not all of it."""
        for threshold in report.thresholds:
            assert 0 < threshold.training_size < report.n_scored


class TestRunMetadata:
    def test_the_model_version_and_settings_are_recorded(self, report) -> None:
        assert report.model_version == MODEL_VERSION
        assert report.resamples == FAST_RESAMPLES
        assert report.folds == 5
        assert report.n_scored == SIZE


# -- determinism and purity ----------------------------------------------


class TestDeterminism:
    def test_two_builds_of_the_same_population_agree(
        self, db_session: Session, experiment_id
    ) -> None:
        first = build_uplift_report(db_session, experiment_id, resamples=FAST_RESAMPLES)
        second = build_uplift_report(db_session, experiment_id, resamples=FAST_RESAMPLES)
        assert first.as_dict() == second.as_dict()

    def test_the_rendered_report_is_byte_identical(
        self, db_session: Session, experiment_id
    ) -> None:
        first = build_report(
            db_session, experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )
        second = build_report(
            db_session, experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )
        assert render_markdown(first) == render_markdown(second)

    def test_the_reporter_reads_no_clock(self) -> None:
        tree = ast.parse(EVALUATION_SOURCE.read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called & {"now", "utcnow", "today"} == set()

    def test_the_reporter_introduces_no_randomness(self) -> None:
        """Every draw comes from the seeded bootstrap in the estimator layer."""
        tree = ast.parse(EVALUATION_SOURCE.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "random" not in modules
        assert "secrets" not in modules


class TestNothingIsWritten:
    def test_building_the_report_persists_nothing(self, db_session: Session, experiment_id) -> None:
        """A reporter that wrote rows as a side effect would make reading a mutation."""
        build_uplift_report(db_session, experiment_id, resamples=FAST_RESAMPLES)
        db_session.flush()

        assert db_session.execute(select(func.count()).select_from(UpliftScore)).scalar() == 0
        assert db_session.execute(select(func.count()).select_from(ExperimentResult)).scalar() == 0


# -- rendering ------------------------------------------------------------


class TestRendering:
    @pytest.fixture
    def full(self, db_session: Session, experiment_id):  # noqa: ANN201
        return build_report(
            db_session, experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )

    def test_the_uplift_sections_appear(self, full) -> None:
        headings = " ".join(sections_of(render_markdown(full)))
        for fragment in (
            "Uplift model",
            "Qini",
            "Quadrant counts",
            "Gray Zone, by reason",
            "Ladder level and cell usage",
            "Harm uplift",
            "Fold-local thresholds",
            "confusion matrix",
        ):
            assert fragment in headings

    def test_the_synthetic_label_still_leads(self, full) -> None:
        assert render_markdown(full).startswith(f"# {SYNTHETIC_LABEL}")

    def test_the_uplift_payload_is_in_the_json(self, full) -> None:
        payload = render_json(full)
        uplift = payload["uplift"]
        assert uplift is not None
        for key in (
            "qini",
            "top_capture",
            "top_amount_capture",
            "quadrant_counts",
            "gray_zone",
            "ladder",
            "harm_uplift",
            "fold_thresholds",
            "confusion_matrix",
            "acceptance",
        ):
            assert key in uplift

    def test_a_day_four_report_omits_the_uplift_section(
        self, db_session: Session, experiment_id
    ) -> None:
        """Cross-fitting is expensive; a caller that wants the ledger does not pay for it."""
        plain = build_report(db_session, experiment_id, resamples=FAST_RESAMPLES)
        assert plain.uplift is None
        assert render_json(plain)["uplift"] is None
        assert "Uplift model" not in " ".join(sections_of(render_markdown(plain)))

    def test_the_qini_and_confusion_sections_are_no_longer_deferred(self, full) -> None:
        deferred = " ".join(name for name, _ in full.deferred)
        assert "Qini" not in deferred
        assert "confusion matrix" not in deferred
