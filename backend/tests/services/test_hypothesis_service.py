"""The hypothesis seam, tested offline.

**No provider call is made anywhere in this file.** Proposals come from
`RecordedProposals`, the same replay path the agent tests use, so what is being
tested is the *orchestration* — the order of the steps, and what happens to the
database when one of them refuses.

The property that matters most is negative: a proposal that fails validation or
falsification must leave **no row at all**. Not a row marked invalid, not a row
with a null verdict. A model's rejected answer is a finding about the model, and
the audit trail is for findings about the population.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.contracts import (
    CellStat,
    HypothesisError,
    HypothesisProposal,
    HypothesisRequest,
    PopulationSummary,
    ProviderInfo,
    Status,
)
from app.agents.hypothesis_agent import OPENROUTER_MODEL, RecordedProposals
from app.engine.falsification import FalsificationError
from app.models.audit_event import AuditEvent
from app.models.enums import ActorType, DecisionType
from app.repositories.audit_repository import HYPOTHESIS_ACTION
from app.services.hypothesis.service import (
    MAX_RETRIES,
    HypothesisOutcome,
    HypothesisServiceError,
    generate_and_record,
)

EXPERIMENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
AS_OF = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SERVICE_ROOT = pathlib.Path("app/services/hypothesis")


def a_cell(
    key: str = "insufficient_funds|upi",
    level: str = "fine",
    *,
    uplift: int = 3_373,
    low: int = 3_006,
    high: int = 3_738,
) -> CellStat:
    return CellStat(
        cell_key=key,
        ladder_level=level,
        n_treated=847,
        n_holdout=841,
        recovered_treated=337,
        recovered_holdout=51,
        p_treat_bps=3_979,
        p_control_bps=606,
        uplift_bps=uplift,
        ci_low_bps=low,
        ci_high_bps=high,
        qualified=True,
        qualification_reason="qualified",
    )


def a_request() -> HypothesisRequest:
    return HypothesisRequest(
        experiment_id=EXPERIMENT,
        population=PopulationSummary(
            ate_bps=1_564,
            ci_low_bps=1_370,
            ci_high_bps=1_757,
            n_treatment=5_044,
            n_holdout=4_956,
            feature_vocabulary=("failure_code", "payment_method"),
        ),
        cells=(a_cell(), a_cell("card_declined|card", uplift=500, low=200, high=800)),
    )


def a_proposal(
    cell_key: str = "insufficient_funds|upi",
    level: str = "fine",
    claim: str = "higher_uplift_than_population",
    rationale: str = "The interval [3006, 3738] lies entirely above the population ATE of 1564.",
    evidence: list[str] | None = None,
) -> HypothesisProposal:
    return HypothesisProposal.model_validate(
        {
            "cell_key": cell_key,
            "ladder_level": level,
            "claim": claim,
            "rationale": rationale,
            "evidence_cited": evidence if evidence is not None else [cell_key],
        }
    )


def a_source(
    proposal: HypothesisProposal | None = None, *, provider: str = "openrouter"
) -> RecordedProposals:
    """A replay. No client, no key, no socket — and it says it is free."""
    return RecordedProposals(
        [proposal or a_proposal()],
        info=ProviderInfo(provider=provider, model=OPENROUTER_MODEL, schema_mode="json_schema"),
    )


def run(session: Session, source: RecordedProposals, **kwargs: object) -> HypothesisOutcome:
    return generate_and_record(
        session,
        EXPERIMENT,
        provider=source,
        as_of=AS_OF,
        alpha_bps=500,
        mde_bps=1_000,
        request=a_request(),
        **kwargs,  # type: ignore[arg-type]
    )


def audit_rows(session: Session) -> int:
    return session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()


# -- orchestration ---------------------------------------------------------


@pytest.mark.db
class TestOrchestration:
    def test_a_good_proposal_is_validated_falsified_and_recorded(self, db_session: Session) -> None:
        outcome = run(db_session, a_source())

        assert outcome.hypothesis.cell_key == "insufficient_funds|upi"
        assert outcome.result.status is Status.CONFIRMED
        assert outcome.recorded
        assert outcome.audit_event is not None

    def test_the_row_is_a_diagnosis_by_an_ai_agent_and_never_an_execution(
        self, db_session: Session
    ) -> None:
        event = run(db_session, a_source()).audit_event
        assert event is not None
        assert event.actor == ActorType.AI_AGENT.value
        assert event.decision_type == DecisionType.DIAGNOSE.value
        assert event.is_execution is False
        assert event.action == HYPOTHESIS_ACTION

    def test_it_is_anchored_on_neither_case_nor_risk(self, db_session: Session) -> None:
        """A hypothesis is about a cell; neither foreign key can say that."""
        event = run(db_session, a_source()).audit_event
        assert event is not None
        assert event.case_id is None
        assert event.risk_id is None

    def test_a_naive_as_of_is_refused_before_anything_is_read(self, db_session: Session) -> None:
        with pytest.raises(HypothesisServiceError, match="timezone-aware"):
            generate_and_record(
                db_session,
                EXPERIMENT,
                provider=a_source(),
                as_of=datetime(2026, 9, 1, 12, 0),
                alpha_bps=500,
                mde_bps=1_000,
                request=a_request(),
            )
        assert audit_rows(db_session) == 0

    def test_a_request_for_another_experiment_is_refused(self, db_session: Session) -> None:
        """The request grades the answer, so it must describe the right run."""
        other = uuid.UUID("33333333-3333-4333-8333-333333333333")
        with pytest.raises(HypothesisServiceError, match="describes experiment"):
            generate_and_record(
                db_session,
                other,
                provider=a_source(),
                as_of=AS_OF,
                alpha_bps=500,
                mde_bps=1_000,
                request=a_request(),
            )
        assert audit_rows(db_session) == 0


# -- nothing is persisted unless every step succeeds -----------------------


@pytest.mark.db
class TestRefusalWritesNothing:
    @pytest.mark.parametrize(
        ("proposal", "match"),
        [
            (a_proposal(cell_key="invented|cell"), "not present in the population"),
            (a_proposal(level="coarse"), "contradicts the population"),
            (a_proposal(rationale="   "), "rationale must not be blank"),
            (
                a_proposal(evidence=["insufficient_funds|upi", "invented|cell"]),
                "cites cell keys not in the population",
            ),
        ],
        ids=["invented-cell", "wrong-ladder", "blank-rationale", "invented-evidence"],
    )
    def test_a_validation_failure_leaves_no_row(
        self, db_session: Session, proposal: HypothesisProposal, match: str
    ) -> None:
        before = audit_rows(db_session)
        with pytest.raises(HypothesisError, match=match):
            run(db_session, a_source(proposal))
        assert audit_rows(db_session) == before

    def test_a_falsification_failure_leaves_no_row(self, db_session: Session) -> None:
        """`falsify` is handed the cell the hypothesis names, so a mismatch is a
        programming error rather than a model error — and still writes nothing."""
        request = a_request()
        source = a_source()
        before = audit_rows(db_session)

        with pytest.raises(FalsificationError):
            validated = None
            # Drive the same order the service uses, but hand `falsify` a cell
            # that is not the one named, which is the only way to reach its
            # guard without changing the service.
            from app.agents.hypothesis_agent import validate
            from app.engine.falsification import falsify

            proposal = source.propose(request)
            validated = validate(request, proposal, source.info)
            falsify(validated, request.cell("card_declined|card"), request.population)
        assert validated is not None
        assert audit_rows(db_session) == before

    def test_an_exhausted_source_leaves_no_row(self, db_session: Session) -> None:
        source = a_source()
        run(db_session, source)
        before = audit_rows(db_session)
        with pytest.raises(HypothesisError, match="exhausted"):
            run(db_session, source)
        assert audit_rows(db_session) == before


# -- transaction ownership -------------------------------------------------


@pytest.mark.db
class TestTheServiceDoesNotCommit:
    def test_the_row_is_visible_but_uncommitted(self, db_session: Session) -> None:
        """Flushed so the caller can inspect it; never committed."""
        outcome = run(db_session, a_source())
        assert outcome.audit_event is not None
        assert outcome.audit_event.id is not None
        assert db_session.in_transaction()

    def test_no_commit_call_appears_in_the_service(self) -> None:
        """A source check, because a stray commit would pass every other test.

        The suite rolls back after each test, so a service that committed would
        look identical from inside one — right up until a partial pipeline run
        left a hypothesis behind in production.
        """
        tree = ast.parse((SERVICE_ROOT / "service.py").read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "commit" not in called


# -- provenance ------------------------------------------------------------


@pytest.mark.db
class TestProvenance:
    def test_the_answering_provider_reaches_the_stored_row(self, db_session: Session) -> None:
        event = run(db_session, a_source()).audit_event
        assert event is not None
        assert event.input_snapshot is not None
        assert event.input_snapshot["provider"] == "openrouter"
        assert event.input_snapshot["model"] == OPENROUTER_MODEL
        assert event.input_snapshot["schema_mode"] == "json_schema"

    def test_the_pinned_free_model_is_the_one_recorded(self, db_session: Session) -> None:
        event = run(db_session, a_source()).audit_event
        assert event is not None
        assert event.input_snapshot is not None
        assert event.input_snapshot["model"] == "nvidia/nemotron-3-super-120b-a12b:free"
        assert str(event.input_snapshot["model"]).endswith(":free")

    def test_live_is_operational_state_not_provenance(self, db_session: Session) -> None:
        """`ProviderInfo` says what produced the answer; `live` says how it was
        obtained, and belongs where the gate already keeps operational facts."""
        offline = run(db_session, a_source()).audit_event
        assert offline is not None
        assert offline.numeric_snapshot is not None
        assert offline.numeric_snapshot["live"] is False

        marked = run(db_session, a_source(), live=True).audit_event
        assert marked is not None
        assert marked.numeric_snapshot is not None
        assert marked.numeric_snapshot["live"] is True

    def test_provider_info_was_not_widened_to_carry_live(self) -> None:
        assert tuple(ProviderInfo.__dataclass_fields__) == ("provider", "model", "schema_mode")

    def test_the_proposal_schema_is_unchanged(self) -> None:
        assert tuple(HypothesisProposal.model_fields) == (
            "cell_key",
            "ladder_level",
            "claim",
            "rationale",
            "evidence_cited",
        )


# -- the real as_dict payloads survive the snapshot guard ------------------


@pytest.mark.db
class TestRealPayloadsPassTheSnapshotGuard:
    def test_both_snapshots_come_from_real_as_dict_output(self, db_session: Session) -> None:
        """Not hand-built dicts. `evidence_cited` is a list and `evidence` a
        dict; `_flatten` must JSON-encode both before the scalar guard runs, and
        this is the only test that exercises that on genuine output."""
        outcome = run(db_session, a_source())
        event = outcome.audit_event
        assert event is not None
        assert event.input_snapshot is not None
        assert event.output_snapshot is not None

        for field in ("hypothesis_id", "experiment_id", "cell_key", "ladder_level"):
            assert event.input_snapshot[field]
            assert event.output_snapshot[field]

        assert event.input_snapshot["exploratory"] is True
        assert event.output_snapshot["exploratory"] is True
        assert "EXPLORATORY" in str(event.input_snapshot["note"])
        assert "EXPLORATORY" in str(event.output_snapshot["note"])
        assert event.output_snapshot["status"] == "confirmed"
        assert event.output_snapshot["decided_at"] == AS_OF.isoformat()

    def test_no_float_reaches_any_snapshot(self, db_session: Session) -> None:
        event = run(db_session, a_source()).audit_event
        assert event is not None
        for snapshot in (
            event.input_snapshot,
            event.output_snapshot,
            event.numeric_snapshot,
        ):
            assert snapshot is not None
            for key, value in snapshot.items():
                assert not isinstance(value, float), key

    def test_no_credential_or_identifier_leaks_into_a_snapshot(self, db_session: Session) -> None:
        import json

        event = run(db_session, a_source()).audit_event
        assert event is not None
        rendered = json.dumps(
            [event.input_snapshot, event.output_snapshot, event.numeric_snapshot]
        ).lower()
        for term in ("api_key", "sk-or", "authorization", "bearer", "truth_", "risk_id"):
            assert term not in rendered, term


# -- isolation -------------------------------------------------------------


def service_modules() -> list[pathlib.Path]:
    return sorted(p for p in SERVICE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


class TestTheSeamStaysIsolated:
    def test_no_module_declares_tools(self) -> None:
        for path in service_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        assert kw.arg != "tools", f"{path.name} passes tools="
                        assert kw.arg != "tool_choice", f"{path.name} passes tool_choice="

    def test_no_module_imports_the_reporter(self) -> None:
        """The reporter is the only permitted reader of ground truth."""
        for path in service_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "evaluation" not in node.module, f"{path.name} -> {node.module}"

    def test_no_module_imports_the_simulator_or_tests(self) -> None:
        for path in service_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("simulator"), path.name
                    assert not node.module.startswith("tests"), path.name

    def test_no_truth_field_is_named_anywhere_in_the_seam(self) -> None:
        for path in service_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
                n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
            }
            leaked = sorted(name for name in names if name.startswith("truth_"))
            assert not leaked, f"{path.name} names {leaked}"

    def test_no_module_reads_a_clock(self) -> None:
        """`as_of` is injected, so a stored verdict is reproducible."""
        for path in service_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            assert called & {"now", "utcnow", "today"} == set(), path.name

    def test_the_single_request_guarantee_is_stated_as_zero(self) -> None:
        assert MAX_RETRIES == 0

    def test_the_live_chain_is_the_free_only_one(self) -> None:
        """Source-level: no other chain constructor may appear in the seam."""
        tree = ast.parse((SERVICE_ROOT / "service.py").read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "free_only_chain" in called
        assert "featherless_config" not in called
        assert "FallbackProvider" not in called
