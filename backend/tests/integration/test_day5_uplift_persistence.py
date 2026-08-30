"""The uplift-score writer, against the real database.

Runs on revtrace_test inside a rolled-back transaction. revtrace_dev is never
touched, and no row survives a test.

Two properties carry this file. The batch must be atomic — a refusal leaves
nothing behind, so a later analysis can never read half a scoring run and not
know it. And a bootstrap interval must reach storage exactly as computed or not
at all: the writer is given several intervals it cannot legally store, and the
only acceptable behaviour is refusal, never a bound nudged to fit the CHECK.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.causal.estimators import Interval
from app.causal.quadrants import QuadrantAssignment
from app.causal.uplift import MODEL_VERSION, UpliftScore
from app.experiments.registry import create_draft
from app.models import CaseAssignment
from app.models import UpliftScore as UpliftScoreRow
from app.models.enums import Arm, Quadrant
from app.repositories import uplift_repository
from app.repositories.uplift_repository import UpliftPersistenceError, persist_scores
from tests.integration.test_day1_schema import a_draft, a_risk

pytestmark = pytest.mark.db

SCORED_AT = datetime(2026, 9, 3, 13, 0, tzinfo=UTC)
ALPHA = 500
RESAMPLES = 1_000

WRITER_SOURCE = Path(uplift_repository.__file__)


# -- builders -------------------------------------------------------------


def a_causal_score(
    risk_id: uuid.UUID,
    *,
    uplift_bps: int = 2_000,
    ci_low: int = 1_200,
    ci_high: int = 2_800,
    p_control_bps: int = 3_500,
    model_version: str = MODEL_VERSION,
) -> UpliftScore:
    return UpliftScore(
        risk_id=risk_id,
        model_version=model_version,
        fold=0,
        level=0,
        level_name="failure_code|payment_method",
        cell_key="gateway_timeout|upi",
        p_treat_bps=p_control_bps + uplift_bps,
        p_control_bps=p_control_bps,
        uplift_bps=uplift_bps,
        interval=Interval(low=ci_low, high=ci_high, alpha_bps=ALPHA, resamples=RESAMPLES, seed=1),
        harm_uplift_bps=50,
        qualified=True,
        reason="qualified",
        n_treated=800,
        n_holdout=800,
    )


def an_assignment(
    risk_id: uuid.UUID,
    *,
    quadrant: Quadrant = Quadrant.PERSUADABLE,
    **score_fields: object,
) -> QuadrantAssignment:
    return QuadrantAssignment(
        risk_id=risk_id,
        quadrant=quadrant,
        rule="significant_uplift_below_ceiling",
        fold=0,
        uplift=a_causal_score(risk_id, **score_fields),  # type: ignore[arg-type]
    )


def force(obj: object, **fields: object) -> object:
    """Overwrite a frozen dataclass field, bypassing validation.

    Needed to build values the causal layer will not produce — an inverted
    interval, a rate above 10000. Those can only arrive through a bug upstream,
    which is precisely what the writer's guards exist to stop at the boundary,
    so the tests have to manufacture them.
    """
    for name, value in fields.items():
        object.__setattr__(obj, name, value)
    return obj


def enrol(session: Session, experiment_id: uuid.UUID, count: int = 3) -> list[uuid.UUID]:
    """`count` risks randomised into the experiment, newest-first by nothing.

    Enrolment lives in `case_assignments`, which is what the writer consults to
    decide whether a risk may be scored at all.
    """
    risk_ids = []
    for index in range(count):
        risk_id = a_risk(session)
        session.add(
            CaseAssignment(
                risk_id=risk_id,
                experiment_id=experiment_id,
                arm=Arm.TREATMENT.value if index % 2 == 0 else Arm.HOLDOUT.value,
                stratum_key="repeated_payment_failure|mid",
                assignment_hash=f"{index:064d}",
                assigned_at=SCORED_AT,
            )
        )
        risk_ids.append(risk_id)
    session.flush()
    return risk_ids


@pytest.fixture
def experiment_id(db_session: Session) -> uuid.UUID:
    return create_draft(db_session, a_draft()).id


def stored(session: Session, experiment_id: uuid.UUID) -> list[UpliftScoreRow]:
    return list(
        session.execute(
            select(UpliftScoreRow)
            .where(UpliftScoreRow.experiment_id == experiment_id)
            .order_by(UpliftScoreRow.risk_id)
        ).scalars()
    )


# -- the happy path -------------------------------------------------------


class TestValidInsert:
    def test_a_batch_is_persisted(self, db_session: Session, experiment_id: uuid.UUID) -> None:
        risk_ids = enrol(db_session, experiment_id)
        rows = persist_scores(
            db_session,
            experiment_id,
            [an_assignment(risk_id) for risk_id in risk_ids],
            as_of=SCORED_AT,
        )

        assert len(rows) == 3
        assert len(stored(db_session, experiment_id)) == 3

    def test_the_persisted_values_are_exactly_what_was_computed(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Every number round-trips unchanged, the interval above all."""
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(
            risk_id,
            quadrant=Quadrant.SLEEPING_DOG,
            uplift_bps=-700,
            ci_low=-1_400,
            ci_high=-100,
            p_control_bps=6_500,
        )

        persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)
        row = stored(db_session, experiment_id)[0]

        assert row.risk_id == risk_id
        assert row.experiment_id == experiment_id
        assert row.model_version == MODEL_VERSION
        assert row.p_treat_bps == 5_800
        assert row.p_control_bps == 6_500
        assert row.uplift_bps == -700
        assert row.uplift_ci_low_bps == -1_400
        assert row.uplift_ci_high_bps == -100
        assert row.quadrant == Quadrant.SLEEPING_DOG.value

    def test_scored_at_comes_from_the_injected_as_of(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Not from a clock, and not from a server default — the column has none."""
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        as_of = datetime(2019, 4, 2, 7, 30, tzinfo=UTC)

        persist_scores(db_session, experiment_id, [an_assignment(risk_id)], as_of=as_of)
        row = stored(db_session, experiment_id)[0]

        assert row.scored_at == as_of

    def test_a_naive_as_of_is_refused(self, db_session: Session, experiment_id: uuid.UUID) -> None:
        """`scored_at` is timestamptz; a naive value would shift silently."""
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        with pytest.raises(UpliftPersistenceError, match="timezone-aware"):
            persist_scores(
                db_session,
                experiment_id,
                [an_assignment(risk_id)],
                as_of=datetime(2026, 9, 3, 13, 0),
            )

    def test_an_empty_batch_writes_nothing_and_does_not_raise(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        assert persist_scores(db_session, experiment_id, [], as_of=SCORED_AT) == ()
        assert stored(db_session, experiment_id) == []

    def test_rows_are_inserted_in_risk_id_order(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Shuffled in, sorted out — a re-run repeats the same INSERT sequence."""
        risk_ids = enrol(db_session, experiment_id, count=5)
        shuffled = sorted(risk_ids, reverse=True)

        rows = persist_scores(
            db_session,
            experiment_id,
            [an_assignment(risk_id) for risk_id in shuffled],
            as_of=SCORED_AT,
        )

        assert [row.risk_id for row in rows] == sorted(risk_ids)


# -- refusals -------------------------------------------------------------


class TestIntervalGuard:
    def test_an_uplift_outside_its_interval_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """The case the DB CHECK cannot be relied on to explain.

        A percentile bootstrap does not guarantee the point estimate lies inside
        its own bounds. When it does not, the batch is refused and the numbers
        stand as computed.
        """
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id, uplift_bps=2_000, ci_low=2_100, ci_high=2_800)

        with pytest.raises(UpliftPersistenceError, match="lies outside its interval"):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)

        assert stored(db_session, experiment_id) == []

    def test_an_inverted_interval_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id)
        force(assignment.uplift.interval, low=2_800, high=1_200)

        with pytest.raises(UpliftPersistenceError, match="inverted"):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)

    def test_the_interval_is_never_adjusted_to_fit(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """The refusal must not be a clip in disguise.

        After the failure the caller's own object must be untouched, so a retry
        or a report sees the interval the bootstrap actually produced.
        """
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id, uplift_bps=2_000, ci_low=2_100, ci_high=2_800)

        with pytest.raises(UpliftPersistenceError):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)

        assert assignment.uplift.ci_low_bps == 2_100
        assert assignment.uplift.ci_high_bps == 2_800
        assert assignment.uplift.uplift_bps == 2_000


class TestRangeGuards:
    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("p_treat_bps", 10_001, "p_treat_bps"),
            ("p_treat_bps", -1, "p_treat_bps"),
            ("p_control_bps", 10_001, "p_control_bps"),
            ("p_control_bps", -1, "p_control_bps"),
        ],
    )
    def test_a_rate_outside_the_scale_is_refused(
        self,
        db_session: Session,
        experiment_id: uuid.UUID,
        field: str,
        value: int,
        message: str,
    ) -> None:
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id)
        force(assignment.uplift, **{field: value})

        with pytest.raises(UpliftPersistenceError, match=message):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)

        assert stored(db_session, experiment_id) == []

    def test_an_uplift_beyond_the_scale_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id, uplift_bps=10_001, ci_low=10_001, ci_high=10_002)

        with pytest.raises(UpliftPersistenceError, match="uplift_bps"):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)

    def test_an_unknown_quadrant_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id)
        force(assignment, quadrant="probably_fine")

        with pytest.raises(UpliftPersistenceError, match="not a quadrant"):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)

    def test_a_blank_model_version_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        assignment = an_assignment(risk_id, model_version="")

        with pytest.raises(UpliftPersistenceError, match="model_version"):
            persist_scores(db_session, experiment_id, [assignment], as_of=SCORED_AT)


class TestOwnership:
    def test_a_risk_not_enrolled_in_the_experiment_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """A score with no assignment behind it has no arm and no population."""
        enrol(db_session, experiment_id, count=1)
        stranger = a_risk(db_session)

        with pytest.raises(UpliftPersistenceError, match="not enrolled"):
            persist_scores(db_session, experiment_id, [an_assignment(stranger)], as_of=SCORED_AT)

        assert stored(db_session, experiment_id) == []

    def test_a_risk_enrolled_in_a_different_experiment_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Enrolment is per experiment, not global."""
        other = create_draft(db_session, a_draft("EXP-OTHER")).id
        elsewhere = enrol(db_session, other, count=1)[0]

        with pytest.raises(UpliftPersistenceError, match="not enrolled"):
            persist_scores(db_session, experiment_id, [an_assignment(elsewhere)], as_of=SCORED_AT)

    def test_a_missing_experiment_id_is_refused(self, db_session: Session) -> None:
        with pytest.raises(UpliftPersistenceError, match="experiment_id is required"):
            persist_scores(
                db_session,
                None,  # type: ignore[arg-type]
                [an_assignment(uuid.uuid4())],
                as_of=SCORED_AT,
            )


class TestDuplicates:
    def test_a_risk_appearing_twice_in_one_batch_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_id = enrol(db_session, experiment_id, count=1)[0]

        with pytest.raises(UpliftPersistenceError, match="more than once"):
            persist_scores(
                db_session,
                experiment_id,
                [an_assignment(risk_id), an_assignment(risk_id, uplift_bps=1_900)],
                as_of=SCORED_AT,
            )

        assert stored(db_session, experiment_id) == []

    def test_rerunning_the_same_model_over_the_same_experiment_is_refused(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_ids = enrol(db_session, experiment_id)
        batch = [an_assignment(risk_id) for risk_id in risk_ids]
        persist_scores(db_session, experiment_id, batch, as_of=SCORED_AT)

        with pytest.raises(UpliftPersistenceError, match="already holds"):
            persist_scores(db_session, experiment_id, batch, as_of=SCORED_AT)

        assert len(stored(db_session, experiment_id)) == 3

    def test_a_new_model_version_may_still_be_stored(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Append-only survives: rescoring under a new version is not a duplicate."""
        risk_ids = enrol(db_session, experiment_id)
        persist_scores(
            db_session,
            experiment_id,
            [an_assignment(risk_id) for risk_id in risk_ids],
            as_of=SCORED_AT,
        )
        persist_scores(
            db_session,
            experiment_id,
            [an_assignment(risk_id, model_version="cellrate-2/k5/fc+pm") for risk_id in risk_ids],
            as_of=SCORED_AT,
        )

        assert len(stored(db_session, experiment_id)) == 6

    def test_the_same_model_may_score_the_same_risk_in_another_experiment(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """The reason experiment_id exists. Not a duplicate, and not refused."""
        risk_id = enrol(db_session, experiment_id, count=1)[0]
        persist_scores(db_session, experiment_id, [an_assignment(risk_id)], as_of=SCORED_AT)

        other = create_draft(db_session, a_draft("EXP-OTHER")).id
        db_session.add(
            CaseAssignment(
                risk_id=risk_id,
                experiment_id=other,
                arm=Arm.HOLDOUT.value,
                stratum_key="repeated_payment_failure|mid",
                assignment_hash="9" * 64,
                assigned_at=SCORED_AT,
            )
        )
        db_session.flush()

        persist_scores(db_session, other, [an_assignment(risk_id)], as_of=SCORED_AT)

        assert len(stored(db_session, experiment_id)) == 1
        assert len(stored(db_session, other)) == 1


class TestAtomicity:
    def test_one_bad_row_refuses_the_whole_batch(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Not one valid row is written. A half-run is invisible and worse than none."""
        risk_ids = enrol(db_session, experiment_id, count=4)
        batch = [an_assignment(risk_id) for risk_id in risk_ids]
        force(batch[2].uplift, p_treat_bps=99_999)

        with pytest.raises(UpliftPersistenceError):
            persist_scores(db_session, experiment_id, batch, as_of=SCORED_AT)

        count = db_session.execute(select(func.count()).select_from(UpliftScoreRow)).scalar()
        assert count == 0

    def test_a_refusal_leaves_the_session_usable(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        """Refusal happens before any `session.add`, so no rollback is needed.

        If the writer had staged rows and let the database reject them, the
        transaction would be poisoned and this second call would fail.
        """
        risk_ids = enrol(db_session, experiment_id, count=2)
        bad = [an_assignment(risk_ids[0], uplift_bps=2_000, ci_low=2_100, ci_high=2_800)]

        with pytest.raises(UpliftPersistenceError):
            persist_scores(db_session, experiment_id, bad, as_of=SCORED_AT)

        rows = persist_scores(
            db_session,
            experiment_id,
            [an_assignment(risk_id) for risk_id in risk_ids],
            as_of=SCORED_AT,
        )
        assert len(rows) == 2

    def test_every_problem_is_reported_not_just_the_first(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_ids = enrol(db_session, experiment_id, count=2)
        batch = [an_assignment(risk_id) for risk_id in risk_ids]
        force(batch[0].uplift, p_treat_bps=-5)
        force(batch[1].uplift, p_control_bps=20_000)

        with pytest.raises(UpliftPersistenceError) as raised:
            persist_scores(db_session, experiment_id, batch, as_of=SCORED_AT)

        assert "p_treat_bps" in str(raised.value)
        assert "p_control_bps" in str(raised.value)


# -- authority ------------------------------------------------------------


class TestWritesOnlyUpliftScores:
    OTHER_TABLES = (
        "experiment_results",
        "recovery_cases",
        "recovery_actions",
        "audit_events",
        "case_outcomes",
        "interventions",
    )

    def test_no_other_table_gains_a_row(
        self, db_session: Session, experiment_id: uuid.UUID
    ) -> None:
        risk_ids = enrol(db_session, experiment_id)
        before = {
            table: db_session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in self.OTHER_TABLES
        }

        persist_scores(
            db_session,
            experiment_id,
            [an_assignment(risk_id) for risk_id in risk_ids],
            as_of=SCORED_AT,
        )

        after = {
            table: db_session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in self.OTHER_TABLES
        }
        assert before == after

    def test_the_module_imports_no_other_writable_model(self) -> None:
        """`CaseAssignment` is read for enrolment; nothing else is even in scope."""
        tree = ast.parse(WRITER_SOURCE.read_text())
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        forbidden = {
            "ExperimentResult",
            "RecoveryCase",
            "RecoveryAction",
            "AuditEvent",
            "CaseOutcome",
            "Intervention",
        }
        assert imported & forbidden == set()


class TestWriterDiscipline:
    def _tree(self) -> ast.Module:
        return ast.parse(WRITER_SOURCE.read_text())

    def test_the_writer_never_commits(self) -> None:
        """Transaction ownership stays with the caller."""
        called = {
            node.func.attr
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "commit" not in called
        assert "rollback" not in called
        assert {"add", "flush"} <= called

    def test_the_writer_never_reads_a_clock(self) -> None:
        """`scored_at` may only come from the injected `as_of`."""
        called = {
            node.func.attr
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called & {"now", "utcnow", "today"} == set()

    def test_the_writer_imports_no_truth_or_simulator(self) -> None:
        tree = self._tree()
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(module.startswith("simulator") for module in modules)
        assert not any(module.startswith("tests") for module in modules)

        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert not any(name.startswith("truth_") for name in names)

    def test_the_two_uplift_score_classes_are_imported_under_distinct_names(self) -> None:
        """They share a name; conflating them would persist the wrong object."""
        aliases = {
            (node.module, alias.name, alias.asname)
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "UpliftScore"
        }
        assert aliases == {
            ("app.causal.uplift", "UpliftScore", "CausalUpliftScore"),
            ("app.models.uplift_score", "UpliftScore", "UpliftScoreRow"),
        }
