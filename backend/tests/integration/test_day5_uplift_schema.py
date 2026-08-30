"""Day 5 schema: uplift scores are scoped to an experiment.

The guarantee under test is that storage — not the writer's diligence — is what
stops two different experiments' estimates for the same risk from becoming
indistinguishable. So these run against the real database, inside the
rolled-back transaction fixture. No row survives, and revtrace_dev is never
involved.

The persistence writer is a later gate. Nothing here is a writer; every row is
built by hand precisely so that the constraints are tested rather than the
convenience layer that will eventually sit on top of them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.experiments.registry import create_draft
from app.models import UpliftScore
from app.models.enums import Quadrant
from tests.integration.test_day1_schema import CLOSED_AT, a_draft, a_risk

pytestmark = pytest.mark.db


def a_score(risk_id: uuid.UUID, experiment_id: uuid.UUID, **overrides: object) -> UpliftScore:
    fields: dict[str, object] = {
        "risk_id": risk_id,
        "experiment_id": experiment_id,
        "model_version": "cellrate-1/k5/fc+pm",
        "p_treat_bps": 5_500,
        "p_control_bps": 3_500,
        "uplift_bps": 2_000,
        "uplift_ci_low_bps": 1_200,
        "uplift_ci_high_bps": 2_800,
        "quadrant": Quadrant.PERSUADABLE.value,
        "scored_at": CLOSED_AT,
    }
    fields.update(overrides)
    return UpliftScore(**fields)  # type: ignore[arg-type]


class TestExperimentScope:
    def test_the_column_exists_and_is_not_null(self, db_session: Session) -> None:
        row = db_session.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'uplift_scores' AND column_name = 'experiment_id'"
            )
        ).one()
        assert row.data_type == "uuid"
        assert row.is_nullable == "NO"

    def test_a_score_without_an_experiment_is_rejected(self, db_session: Session) -> None:
        """The NOT NULL is the point: an unattributed score is not storable."""
        risk_id = a_risk(db_session)
        with pytest.raises(IntegrityError):
            db_session.execute(
                text(
                    "INSERT INTO uplift_scores (id, risk_id, model_version, p_treat_bps, "
                    "p_control_bps, uplift_bps, uplift_ci_low_bps, uplift_ci_high_bps, "
                    "quadrant, scored_at, created_at) VALUES (:id, :r, 'v1', 5500, 3500, "
                    "2000, 1200, 2800, 'persuadable', now(), now())"
                ),
                {"id": uuid.uuid4(), "r": risk_id},
            )

    def test_an_unknown_experiment_is_rejected(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        db_session.add(a_score(risk_id, uuid.uuid4()))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_deleting_an_experiment_removes_its_scores(self, db_session: Session) -> None:
        """CASCADE, matching case_assignments.

        An estimate means nothing without the design that produced it, so the
        two are removed together rather than leaving orphans behind.
        """
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(a_score(risk_id, experiment.id))
        db_session.flush()

        db_session.execute(text("DELETE FROM experiments WHERE id = :e"), {"e": experiment.id})
        remaining = db_session.execute(
            text("SELECT count(*) FROM uplift_scores WHERE risk_id = :r"), {"r": risk_id}
        ).scalar()
        assert remaining == 0


class TestUniqueness:
    def test_the_same_model_cannot_score_a_risk_twice_in_one_experiment(
        self, db_session: Session
    ) -> None:
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(a_score(risk_id, experiment.id))
        db_session.flush()

        db_session.add(a_score(risk_id, experiment.id, uplift_bps=1_900))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_the_same_model_may_score_a_risk_in_two_experiments(self, db_session: Session) -> None:
        """The reason the column exists.

        Cross-fitting derives every cell rate from one experiment's population,
        so the same risk under the same model version legitimately carries two
        different numbers. Before this constraint they were indistinguishable
        once stored; the old uniqueness shape would now reject the second.
        """
        risk_id = a_risk(db_session)
        first = create_draft(db_session, a_draft("EXP-001"))
        second = create_draft(db_session, a_draft("EXP-002"))

        db_session.add(a_score(risk_id, first.id, uplift_bps=2_000))
        db_session.add(
            a_score(
                risk_id,
                second.id,
                p_treat_bps=3_200,
                p_control_bps=3_500,
                uplift_bps=-300,
                uplift_ci_low_bps=-900,
                uplift_ci_high_bps=400,
                quadrant=Quadrant.GRAY_ZONE.value,
            )
        )
        db_session.flush()

        count = db_session.execute(
            text("SELECT count(*) FROM uplift_scores WHERE risk_id = :r"), {"r": risk_id}
        ).scalar()
        assert count == 2

    def test_rescoring_under_a_new_model_version_still_appends(self, db_session: Session) -> None:
        """Append-only survives the tightened constraint."""
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(a_score(risk_id, experiment.id, model_version="cellrate-1/k5/fc+pm"))
        db_session.add(a_score(risk_id, experiment.id, model_version="cellrate-2/k5/fc+pm"))
        db_session.flush()

        count = db_session.execute(
            text("SELECT count(*) FROM uplift_scores WHERE risk_id = :r"), {"r": risk_id}
        ).scalar()
        assert count == 2

    def test_two_risks_may_share_an_experiment_and_model(self, db_session: Session) -> None:
        """The constraint scopes by risk; it does not cap an experiment at one."""
        experiment = create_draft(db_session, a_draft())
        db_session.add(a_score(a_risk(db_session), experiment.id))
        db_session.add(a_score(a_risk(db_session), experiment.id))
        db_session.flush()


class TestSurvivingConstraints:
    """The migration is additive. What the table already promised still holds."""

    def test_the_estimate_must_still_lie_inside_its_interval(self, db_session: Session) -> None:
        """The CHECK the application writer will mirror as a pre-insert guard.

        Kept in the database as well as in the writer: a guard that lives only
        in application code is a guard that a psql session walks straight past.
        """
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(a_score(risk_id, experiment.id, uplift_bps=9_000))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_negative_uplift_is_still_storable(self, db_session: Session) -> None:
        risk_id = a_risk(db_session)
        experiment = create_draft(db_session, a_draft())
        db_session.add(
            a_score(
                risk_id,
                experiment.id,
                p_treat_bps=5_800,
                p_control_bps=6_500,
                uplift_bps=-700,
                uplift_ci_low_bps=-1_400,
                uplift_ci_high_bps=-100,
                quadrant=Quadrant.SLEEPING_DOG.value,
            )
        )
        db_session.flush()

    def test_every_pre_existing_check_survived_the_migration(self, db_session: Session) -> None:
        """Named explicitly, so a dropped CHECK fails here rather than silently."""
        names = set(
            db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'uplift_scores'::regclass AND contype = 'c'"
                )
            ).scalars()
        )
        assert {
            "ck_uplift_scores_quadrant_valid",
            "ck_uplift_scores_p_treat_bps_in_range",
            "ck_uplift_scores_p_control_bps_in_range",
            "ck_uplift_scores_uplift_bps_in_range",
            "ck_uplift_scores_uplift_ci_ordered",
            "ck_uplift_scores_uplift_within_ci",
        } <= names


class TestNaming:
    """The constraint and index names the migration's downgrade refers to.

    A downgrade drops objects by name, so a name that drifted from the
    convention would leave a migration that upgrades but cannot reverse —
    which is how BREAKAGE entry 12 happened.
    """

    def test_the_unique_constraint_is_named_and_shaped_as_agreed(self, db_session: Session) -> None:
        columns = (
            db_session.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                    "WHERE c.conname = 'uq_uplift_scores_experiment_risk_model' "
                    "AND c.conrelid = 'uplift_scores'::regclass ORDER BY k.ord"
                )
            )
            .scalars()
            .all()
        )
        assert columns == ["experiment_id", "risk_id", "model_version"]

    def test_the_foreign_key_is_named_by_convention_and_cascades(self, db_session: Session) -> None:
        row = db_session.execute(
            text(
                "SELECT confdeltype, confrelid::regclass::text AS target "
                "FROM pg_constraint "
                "WHERE conname = 'fk_uplift_scores_experiment_id_experiments' "
                "AND conrelid = 'uplift_scores'::regclass"
            )
        ).one()
        assert row.target == "experiments"
        assert row.confdeltype == "c"

    def test_the_experiment_id_index_exists(self, db_session: Session) -> None:
        names = set(
            db_session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'uplift_scores'")
            ).scalars()
        )
        assert "ix_uplift_scores_experiment_id" in names
        assert "uq_uplift_scores_experiment_risk_model" in names


class TestModelMatchesDatabase:
    def test_the_orm_and_the_migration_agree(self, db_session: Session) -> None:
        """Autogenerate finds nothing left to do for uplift_scores.

        Cheaper than asserting each column twice, and it catches the failure
        mode that matters: a model edited without a migration, or the reverse.
        """
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        from app.db.base import Base

        context = MigrationContext.configure(db_session.connection())
        diffs = [
            diff
            for diff in compare_metadata(context, Base.metadata)
            if "uplift_scores" in repr(diff)
        ]
        assert diffs == []
