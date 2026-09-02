"""The hypothesis agent, tested entirely offline.

**No provider call is made anywhere in this file.** Proposals come from
`tests/agents/fixtures/proposals.json`, which is **hand-authored synthetic test
data, not captured model output** — the fixture says so itself, and a test below
asserts that disclaimer stays there. Nothing in this suite is evidence of what
the model actually returns; it is evidence of what the code does with a response
of that shape.

The split that matters: a model response is **recorded input**, and everything
downstream of it — validation, identity, falsification — is deterministic. The
tests assert the second half and deliberately do not claim the first.

Isolation guards walk the AST rather than scanning text. Several of these
modules *document* the constraints in prose — "no tools", "truth_*",
"policy_engine" all appear in docstrings explaining why they are absent — so a
substring scan would fail on the very sentences that record the rule.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from app.agents.contracts import (
    EXPLORATORY_NOTE,
    CellStat,
    Claim,
    HypothesisError,
    HypothesisProposal,
    HypothesisRequest,
    PopulationSummary,
    ProviderInfo,
)
from app.agents.hypothesis_agent import (
    FEATHERLESS_BASE_URL,
    FEATHERLESS_MODEL,
    FREE_MODEL_SUFFIX,
    JSON_ONLY_INSTRUCTION,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    REASONING_FIELDS,
    SCHEMA_NAME,
    SHAPE_PREFIX_LIMIT,
    SYSTEM_PROMPT,
    FallbackProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    RecordedProposals,
    SchemaMode,
    _body_error_code,
    _empty_content_diagnosis,
    _is_client_error,
    _no_choices_diagnosis,
    _parse_failure_diagnosis,
    _shape_prefix,
    featherless_config,
    free_only_chain,
    hypothesis_id_for,
    openrouter_config,
    propose_hypothesis,
    strict_schema,
    validate,
)
from app.core.config import Settings

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "proposals.json"
AGENT_ROOT = pathlib.Path("app/agents")
EXPERIMENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
AS_OF = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def fixtures() -> dict[str, dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def proposal(name: str) -> HypothesisProposal:
    """One synthetic fixture, parsed through the real output schema."""
    return HypothesisProposal.model_validate(fixtures()[name])


def a_cell(
    key: str,
    level: str = "fine",
    *,
    uplift: int = 1_350,
    low: int = 900,
    high: int = 1_800,
    qualified: bool = True,
    reason: str = "qualified",
) -> CellStat:
    return CellStat(
        cell_key=key,
        ladder_level=level,
        n_treated=4_000,
        n_holdout=4_000,
        recovered_treated=1_180,
        recovered_holdout=640,
        p_treat_bps=2_950,
        p_control_bps=1_600,
        uplift_bps=uplift,
        ci_low_bps=low,
        ci_high_bps=high,
        qualified=qualified,
        qualification_reason=reason,
    )


def a_population() -> PopulationSummary:
    return PopulationSummary(
        ate_bps=1_564,
        ci_low_bps=1_370,
        ci_high_bps=1_757,
        n_treatment=5_044,
        n_holdout=4_956,
        feature_vocabulary=(
            "failure_code",
            "payment_method",
            "amount_band",
            "hour_bucket",
            "tenure_bucket",
            "salary_window",
        ),
    )


def a_request() -> HypothesisRequest:
    """A population whose keys match the fixture's cell keys."""
    return HypothesisRequest(
        experiment_id=EXPERIMENT,
        population=a_population(),
        cells=(
            a_cell("insufficient_funds|upi"),
            a_cell("card_declined|card", uplift=500, low=200, high=800),
            a_cell("expired_card|card", uplift=50, low=-300, high=400),
            a_cell("insufficient_funds", "coarse", uplift=1_200, low=800, high=1_600),
        ),
    )


#: A synthetic key. A literal in this file — it reads no environment variable,
#: no `.env`, and no `Settings`. Providers below are constructed with it and
#: never asked to `propose()`, which is the only method that opens a socket.
TEST_KEY = SecretStr("test-placeholder-not-a-real-key-0000000000")


def a_provider(mode: SchemaMode) -> OpenAICompatibleProvider:
    """A provider wired to a synthetic key. Constructed, never called.

    Constructing an `OpenAI` client issues no request — it configures a
    transport and returns. `_request_kwargs`, which the request-shape tests
    call, never touches `self._client` at all, so it cannot reach the network
    even in principle.
    """
    json_schema = mode is SchemaMode.JSON_SCHEMA
    return OpenAICompatibleProvider(
        ProviderConfig(
            name="openrouter" if json_schema else "featherless",
            base_url=OPENROUTER_BASE_URL if json_schema else FEATHERLESS_BASE_URL,
            model=OPENROUTER_MODEL if json_schema else "openai/gpt-oss-120b",
            api_key=TEST_KEY,
            schema_mode=mode,
            extra_body={"provider": {"require_parameters": True}} if json_schema else {},
        )
    )


# -- the fixture is synthetic, and says so --------------------------------


class TestTheFixtureIsHonestAboutItself:
    def test_it_declares_itself_synthetic(self) -> None:
        """A fixture that claimed captured provenance would be a false record."""
        note = fixtures()["_note"]
        assert "not captured model outputs" in note
        assert "test fixtures" in note
        assert "Synthetic" in note

    def test_it_claims_no_capture_timestamp_or_provenance(self) -> None:
        data = fixtures()
        assert data["_provenance"] == "hand-authored"
        assert "_recorded_at" not in data
        assert "no response from it has been captured" in data["_model_target"]


# -- parsing the structured output ----------------------------------------


class TestProposalParsing:
    def test_a_valid_proposal_parses(self) -> None:
        parsed = proposal("higher_uplift")
        assert parsed.cell_key == "insufficient_funds|upi"
        assert parsed.ladder_level == "fine"
        assert parsed.claim is Claim.HIGHER
        assert parsed.rationale.strip()

    @pytest.mark.parametrize(
        ("name", "claim"),
        [
            ("higher_uplift", Claim.HIGHER),
            ("lower_uplift", Claim.LOWER),
            ("no_effect", Claim.NO_EFFECT),
        ],
    )
    def test_all_three_claims_parse(self, name: str, claim: Claim) -> None:
        assert proposal(name).claim is claim

    def test_the_claim_vocabulary_is_exactly_three_values(self) -> None:
        assert {c.value for c in Claim} == {
            "higher_uplift_than_population",
            "lower_uplift_than_population",
            "no_effect",
        }

    def test_an_invalid_claim_is_rejected(self) -> None:
        payload = dict(fixtures()["higher_uplift"])
        payload["claim"] = "increase_discount_by_10_percent"
        with pytest.raises(ValidationError):
            HypothesisProposal.model_validate(payload)

    def test_an_extra_field_is_rejected(self) -> None:
        """The model cannot smuggle a threshold or an action into the schema."""
        payload = dict(fixtures()["higher_uplift"])
        payload["recommended_discount_bps"] = 500
        with pytest.raises(ValidationError):
            HypothesisProposal.model_validate(payload)

    def test_the_proposal_is_frozen(self) -> None:
        parsed = proposal("higher_uplift")
        with pytest.raises(ValidationError):
            parsed.cell_key = "something_else"  # type: ignore[misc]


class TestCellStatValidation:
    def test_an_invalid_ladder_level_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown ladder level"):
            a_cell("insufficient_funds|upi", "molecular")

    def test_an_inverted_interval_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="interval inverted"):
            a_cell("insufficient_funds|upi", low=900, high=100)

    def test_duplicate_cell_keys_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            HypothesisRequest(
                experiment_id=EXPERIMENT,
                population=a_population(),
                cells=(a_cell("dup|upi"), a_cell("dup|upi")),
            )

    def test_an_empty_population_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one cell"):
            HypothesisRequest(experiment_id=EXPERIMENT, population=a_population(), cells=())


# -- deterministic validation of what the model said -----------------------


class TestValidation:
    def test_a_real_key_validates(self) -> None:
        validated = validate(a_request(), proposal("higher_uplift"))
        assert validated.cell_key == "insufficient_funds|upi"
        assert validated.claim is Claim.HIGHER
        assert validated.experiment_id == EXPERIMENT

    def test_a_coarse_level_proposal_validates(self) -> None:
        validated = validate(a_request(), proposal("coarse_level"))
        assert validated.ladder_level == "coarse"

    def test_an_invented_cell_key_is_rejected(self) -> None:
        """The single most important guard: the model may only point, never invent."""
        with pytest.raises(HypothesisError, match="not present in the population"):
            validate(a_request(), proposal("invented_cell_key"))

    def test_a_wrong_ladder_level_for_a_real_key_is_rejected(self) -> None:
        with pytest.raises(HypothesisError, match="contradicts the population"):
            validate(a_request(), proposal("wrong_ladder_level"))

    def test_invented_evidence_is_rejected(self) -> None:
        with pytest.raises(HypothesisError, match="not in the population"):
            validate(a_request(), proposal("invented_evidence"))

    def test_a_blank_rationale_is_rejected(self) -> None:
        with pytest.raises(HypothesisError, match="rationale"):
            validate(a_request(), proposal("blank_rationale"))

    def test_evidence_must_be_a_subset_of_supplied_cells(self) -> None:
        validated = validate(a_request(), proposal("higher_uplift"))
        assert set(validated.evidence_cited) <= a_request().cell_keys

    def test_the_identity_is_derived_not_drawn(self) -> None:
        """A drawn id would make an otherwise deterministic replay differ."""
        first = validate(a_request(), proposal("higher_uplift"))
        second = validate(a_request(), proposal("higher_uplift"))
        assert first.hypothesis_id == second.hypothesis_id
        assert first.hypothesis_id == hypothesis_id_for(
            EXPERIMENT, "insufficient_funds|upi", Claim.HIGHER
        )

    def test_different_claims_get_different_identities(self) -> None:
        assert hypothesis_id_for(EXPERIMENT, "a|b", Claim.HIGHER) != hypothesis_id_for(
            EXPERIMENT, "a|b", Claim.LOWER
        )

    def test_the_validated_payload_is_marked_exploratory(self) -> None:
        payload = validate(a_request(), proposal("higher_uplift")).as_dict()
        assert payload["exploratory"] is True
        assert payload["note"] == EXPLORATORY_NOTE
        assert "not a pre-registered confirmatory result" in str(payload["note"])


# -- replay through the injected source ------------------------------------


class TestRecordedReplay:
    def test_a_synthetic_response_replays_through_the_protocol(self) -> None:
        source = RecordedProposals([proposal("higher_uplift")])
        validated = propose_hypothesis(source, a_request())
        assert validated.cell_key == "insufficient_funds|upi"

    def test_proposals_replay_in_order(self) -> None:
        source = RecordedProposals([proposal("higher_uplift"), proposal("lower_uplift")])
        request = a_request()
        assert propose_hypothesis(source, request).claim is Claim.HIGHER
        assert propose_hypothesis(source, request).claim is Claim.LOWER

    def test_an_exhausted_source_is_refused(self) -> None:
        source = RecordedProposals([proposal("higher_uplift")])
        propose_hypothesis(source, a_request())
        with pytest.raises(HypothesisError, match="exhausted"):
            source.propose(a_request())

    def test_an_empty_source_is_refused(self) -> None:
        with pytest.raises(HypothesisError, match="at least one proposal"):
            RecordedProposals([])

    def test_a_recorded_rejection_still_gets_rejected(self) -> None:
        """Replay does not launder a bad proposal past validation."""
        source = RecordedProposals([proposal("invented_cell_key")])
        with pytest.raises(HypothesisError):
            propose_hypothesis(source, a_request())


class TestDownstreamDeterminism:
    """The model is not deterministic; everything after it is.

    These assert the second half only. No test here claims a given input
    produces a given model response — that would be a claim this suite has no
    evidence for, since every proposal it uses was hand-authored.
    """

    def test_identical_recorded_response_yields_identical_output(self) -> None:
        ids = set()
        for _ in range(50):
            source = RecordedProposals([proposal("higher_uplift")])
            ids.add(propose_hypothesis(source, a_request()).as_dict()["hypothesis_id"])
        assert len(ids) == 1

    def test_the_whole_payload_is_stable_across_replays(self) -> None:
        first = propose_hypothesis(RecordedProposals([proposal("lower_uplift")]), a_request())
        second = propose_hypothesis(RecordedProposals([proposal("lower_uplift")]), a_request())
        assert first == second
        assert first.as_dict() == second.as_dict()

    def test_a_different_response_yields_a_different_result(self) -> None:
        """Determinism downstream is not sameness: the input still decides."""
        high = propose_hypothesis(RecordedProposals([proposal("higher_uplift")]), a_request())
        low = propose_hypothesis(RecordedProposals([proposal("lower_uplift")]), a_request())
        assert high.hypothesis_id != low.hypothesis_id

    def test_the_module_does_not_claim_the_model_is_deterministic(self) -> None:
        """Guard against a future docstring overclaiming reproducibility.

        The docstring is the target here, so reading its text is the point —
        this is not a substring scan standing in for a structural check.
        Markdown emphasis is stripped first so `**not**` still reads as `not`.
        """
        from app.agents import hypothesis_agent

        # Collapse whitespace: the assertion is about what the docstring says,
        # not about where its lines happen to wrap.
        raw = (hypothesis_agent.__doc__ or "").lower().replace("*", "")
        text = " ".join(raw.split())
        assert "the model is not deterministic" in text
        assert "identical inputs may still yield different proposals" in text
        for overclaim in (
            "the model is deterministic",
            "reproducible model output",
            "the same prompt always returns",
        ):
            assert overclaim not in text, overclaim


# -- no network -----------------------------------------------------------


class TestNoNetwork:
    def test_the_recorded_path_runs_with_sockets_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prove it rather than assert it: forbid sockets, then replay."""
        import socket

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("the offline test suite opened a socket")

        monkeypatch.setattr(socket, "socket", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)

        validated = propose_hypothesis(RecordedProposals([proposal("higher_uplift")]), a_request())
        assert validated.cell_key == "insufficient_funds|upi"

    @pytest.mark.parametrize("factory", [openrouter_config, featherless_config])
    def test_no_live_provider_is_constructed_without_a_key(self, factory) -> None:  # noqa: ANN001
        """No key, no client — and no ambient-credential fallback, per provider.

        Both keys are passed empty rather than left to the default: `Settings`
        reads the repository `.env`, so a developer who has a real key on disk
        would otherwise turn this into a test of their own machine — and, worse,
        into one that constructs a live client. The field defaults are asserted
        separately, where no client is built.
        """
        settings = Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://localhost/x",
            openrouter_api_key="",
            featherless_api_key="",
        )
        assert not settings.openrouter_configured
        assert not settings.featherless_configured
        with pytest.raises(HypothesisError, match="_API_KEY is not configured"):
            OpenAICompatibleProvider(factory(settings))

    def test_no_live_client_in_this_file_is_ever_asked_to_propose(self) -> None:
        """The property that matters, rather than a count of constructions.

        Several tests construct `OpenAICompatibleProvider` — to prove it refuses
        an empty key, and to prove a placeholder parses. None of them calls
        `.propose()` on it, which is the only method that opens a socket. A
        count would go stale every time a credential test is added; this does
        not.
        """
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))

        live: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id == "OpenAICompatibleProvider":
                live.update(t.id for t in node.targets if isinstance(t, ast.Name))

        proposed_on = {
            node.func.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "propose"
            and isinstance(node.func.value, ast.Name)
        }
        assert live, "expected at least one construction to guard"
        assert not (live & proposed_on), sorted(live & proposed_on)


# -- isolation, by AST ----------------------------------------------------


def agent_modules() -> list[pathlib.Path]:
    return sorted(p for p in AGENT_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: pathlib.Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def identifiers(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.ClassDef):
            found.add(node.name)
    return found


class TestIsolation:
    """AST-based, not substring — these modules document the constraints in
    prose, so a text scan would fail on the sentences recording the rule."""

    def test_the_agent_package_is_not_empty(self) -> None:
        assert len(agent_modules()) >= 2

    def test_no_agent_module_imports_the_reporter(self) -> None:
        for path in agent_modules():
            for module in imported_modules(path):
                assert "reporting" not in module, f"{path.name} -> {module}"
                assert "evaluation" not in module, f"{path.name} -> {module}"

    def test_no_agent_module_imports_the_policy_engine(self) -> None:
        """A hypothesis is an observation, never a policy input."""
        for path in agent_modules():
            for module in imported_modules(path):
                assert "policy_engine" not in module, f"{path.name} -> {module}"

    def test_no_agent_module_imports_the_causal_package_or_a_model(self) -> None:
        for path in agent_modules():
            for module in imported_modules(path):
                assert not module.startswith("app.causal"), f"{path.name} -> {module}"
                assert not module.startswith("app.models"), f"{path.name} -> {module}"

    def test_no_agent_module_imports_the_simulator_or_tests(self) -> None:
        for path in agent_modules():
            for module in imported_modules(path):
                assert not module.startswith("simulator"), f"{path.name} -> {module}"
                assert not module.startswith("tests"), f"{path.name} -> {module}"

    def test_no_agent_module_names_a_truth_column(self) -> None:
        truth = {"truth_y0", "truth_y1", "truth_harm_0", "truth_harm_1", "truth_segment"}
        for path in agent_modules():
            offenders = identifiers(path) & truth
            assert offenders == set(), f"{path.name}: {offenders}"

    def test_no_agent_module_names_a_forbidden_row_field(self) -> None:
        """No per-unit identity, no money, no intervention catalogue."""
        banned = {"risk_id", "customer_id", "order_id", "amount_at_risk", "unit_cost"}
        for path in agent_modules():
            offenders = identifiers(path) & banned
            assert offenders == set(), f"{path.name}: {offenders}"

    def test_the_agent_declares_no_tools(self) -> None:
        """Not an empty tool list — no `tools` keyword anywhere in the package."""
        for path in agent_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        assert kw.arg != "tools", f"{path.name} passes tools="
                        assert kw.arg != "tool_choice", f"{path.name} passes tool_choice="

    def test_the_agent_never_reads_a_clock_or_draws_randomness(self) -> None:
        banned = {"now", "utcnow", "today", "uuid4", "uuid1", "random", "choice", "shuffle"}
        for path in agent_modules():
            offenders = identifiers(path) & banned
            assert offenders == set(), f"{path.name}: {offenders}"

    def test_the_claude_agent_sdk_is_not_imported(self) -> None:
        """A coding agent with built-in file and bash tools is the wrong tool."""
        for path in agent_modules():
            for module in imported_modules(path):
                assert "claude_agent_sdk" not in module, f"{path.name} -> {module}"

    def test_no_httpx_timeout_reaches_the_client(self) -> None:
        """`httpx` and `httpx2` are different distributions with different types."""
        for path in agent_modules():
            for module in imported_modules(path):
                assert module != "httpx", f"{path.name} imports httpx"


class TestPromptSurface:
    def test_the_system_prompt_forbids_invention(self) -> None:
        lowered = SYSTEM_PROMPT.lower()
        for phrase in ("invent", "threshold", "monetary value", "intervention"):
            assert phrase in lowered

    def test_the_system_prompt_says_nothing_is_executed(self) -> None:
        assert "nothing you propose will be executed" in SYSTEM_PROMPT

    def test_the_primary_model_and_base_urls_are_the_approved_ones(self) -> None:
        assert OPENROUTER_MODEL == "nvidia/nemotron-3-super-120b-a12b:free"
        assert OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"
        assert FEATHERLESS_BASE_URL == "https://api.featherless.ai/v1"

    def test_the_payload_carries_only_aggregates(self) -> None:
        payload = a_request().as_prompt_payload()
        rendered = json.dumps(payload)
        for banned in (
            "truth_y0",
            "truth_y1",
            "truth_harm_0",
            "truth_harm_1",
            "truth_segment",
            "risk_id",
            "customer",
            "order_id",
            "amount_at_risk",
            "unit_cost",
            "create_payment_link",
        ):
            assert banned not in rendered, banned

    def test_the_payload_omits_the_experiment_id(self) -> None:
        assert "experiment_id" not in json.dumps(a_request().as_prompt_payload())

    def test_the_payload_carries_the_exploratory_note(self) -> None:
        assert a_request().as_prompt_payload()["note"] == EXPLORATORY_NOTE

    def test_the_payload_lists_the_allowed_keys_and_claims(self) -> None:
        payload = a_request().as_prompt_payload()
        assert payload["allowed_cell_keys"] == sorted(a_request().cell_keys)
        assert payload["allowed_claims"] == [c.value for c in Claim]

    def test_the_payload_is_json_serialisable_without_floats(self) -> None:
        rendered = json.loads(json.dumps(a_request().as_prompt_payload()))
        for cell in rendered["cells"]:
            for key, value in cell.items():
                assert not isinstance(value, float), key


# -- audit persistence, against the real database --------------------------


@pytest.mark.db
class TestRecordHypothesis:
    """`record_hypothesis` against revtrace_test.

    What is tested here rather than in the pure suite: the storage-layer
    guarantees. That the row is never an execution, that the identifiers land in
    JSONB rather than in a new column, and — the one that matters most — that
    the frozen pre-registration is never touched.
    """

    def _payloads(self) -> tuple[dict, dict]:
        from app.engine.falsification import falsify

        request = a_request()
        hypothesis = propose_hypothesis(RecordedProposals([proposal("higher_uplift")]), request)
        result = falsify(hypothesis, request.cell(hypothesis.cell_key), request.population)
        return hypothesis.as_dict(), result.as_dict()

    def test_it_records_with_the_ai_agent_actor(self, db_session) -> None:  # noqa: ANN001
        from app.models.enums import ActorType, DecisionType
        from app.repositories.audit_repository import HYPOTHESIS_ACTION, record_hypothesis

        hypothesis, result = self._payloads()
        event = record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)
        assert event.actor == ActorType.AI_AGENT.value
        assert event.decision_type == DecisionType.DIAGNOSE.value
        assert event.action == HYPOTHESIS_ACTION

    def test_it_is_never_an_execution(self, db_session) -> None:  # noqa: ANN001
        from app.repositories.audit_repository import record_hypothesis

        hypothesis, result = self._payloads()
        event = record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)
        assert event.is_execution is False

    def test_the_database_refuses_an_ai_execution_entry(self, db_session) -> None:  # noqa: ANN001
        """`execution_actor_never_ai`, independent of the repository."""
        from sqlalchemy.exc import IntegrityError

        from app.models.audit_event import AuditEvent
        from app.models.enums import ActorType

        db_session.add(
            AuditEvent(actor=ActorType.AI_AGENT.value, action="FORCED", is_execution=True)
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_the_identifiers_land_in_jsonb(self, db_session) -> None:  # noqa: ANN001
        from app.repositories.audit_repository import record_hypothesis

        hypothesis, result = self._payloads()
        event = record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)
        snapshot = event.numeric_snapshot
        assert snapshot is not None
        for field in ("hypothesis_id", "experiment_id", "cell_key", "ladder_level"):
            assert snapshot[field], field
        assert snapshot["experiment_id"] == str(EXPERIMENT)
        assert snapshot["cell_key"] == "insufficient_funds|upi"
        assert snapshot["ladder_level"] == "fine"
        assert snapshot["exploratory"] is True

    def test_it_has_no_case_or_risk_anchor(self, db_session) -> None:  # noqa: ANN001
        """A hypothesis is about a cell; neither foreign key can express that."""
        from app.repositories.audit_repository import record_hypothesis

        hypothesis, result = self._payloads()
        event = record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)
        assert event.case_id is None
        assert event.risk_id is None

    def test_both_snapshots_are_stored_and_exploratory(self, db_session) -> None:  # noqa: ANN001
        from app.repositories.audit_repository import record_hypothesis

        hypothesis, result = self._payloads()
        event = record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)
        assert event.input_snapshot is not None
        assert event.output_snapshot is not None
        assert event.input_snapshot["exploratory"] is True
        assert event.output_snapshot["exploratory"] is True
        assert event.output_snapshot["decided_at"] == AS_OF.isoformat()

    def test_it_never_writes_experiments_hypothesis(self) -> None:
        """The frozen pre-registration is not amended by an AI proposal.

        Two structural checks, neither satisfiable by a docstring: the module
        imports no experiment model, and nothing in it assigns to a
        `.hypothesis` attribute. `hypothesis` is also a *parameter* name here,
        so a bare identifier scan would be useless.
        """
        tree = ast.parse(
            pathlib.Path("app/repositories/audit_repository.py").read_text(encoding="utf-8")
        )

        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not [m for m in imported if "experiment" in m.lower()], imported

        assigned = [
            target
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign | ast.AugAssign)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Attribute)
        ]
        assert not [t for t in assigned if t.attr == "hypothesis"], [t.attr for t in assigned]

    def test_the_experiments_hypothesis_column_stays_frozen(self) -> None:
        """Belt and braces: the model's own immutability list still names it."""
        from app.models.experiment import FROZEN_AFTER_LOCK

        assert "hypothesis" in FROZEN_AFTER_LOCK

    def test_a_missing_identifier_is_refused(self, db_session) -> None:  # noqa: ANN001
        from app.repositories.audit_repository import (
            AuditPersistenceError,
            record_hypothesis,
        )

        hypothesis, result = self._payloads()
        del hypothesis["cell_key"]
        with pytest.raises(AuditPersistenceError, match="cell_key"):
            record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)

    def test_an_unmarked_payload_is_refused(self, db_session) -> None:  # noqa: ANN001
        """A result that forgot it was exploratory must not be storable."""
        from app.repositories.audit_repository import (
            AuditPersistenceError,
            record_hypothesis,
        )

        hypothesis, result = self._payloads()
        result["exploratory"] = False
        with pytest.raises(AuditPersistenceError, match="exploratory"):
            record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)

    def test_a_naive_as_of_is_refused(self, db_session) -> None:  # noqa: ANN001
        from datetime import datetime

        from app.repositories.audit_repository import (
            AuditPersistenceError,
            record_hypothesis,
        )

        hypothesis, result = self._payloads()
        with pytest.raises(AuditPersistenceError, match="timezone-aware"):
            record_hypothesis(
                db_session,
                hypothesis=hypothesis,
                result=result,
                as_of=datetime(2026, 8, 31, 12, 0),
            )

    def test_it_does_not_commit(self) -> None:
        import inspect

        from app.repositories import audit_repository

        assert "commit()" not in inspect.getsource(audit_repository)

    def test_no_experiment_result_row_is_written(self, db_session) -> None:  # noqa: ANN001
        from sqlalchemy import func, select

        from app.models.experiment_result import ExperimentResult
        from app.repositories.audit_repository import record_hypothesis

        before = db_session.execute(select(func.count()).select_from(ExperimentResult)).scalar_one()
        hypothesis, result = self._payloads()
        record_hypothesis(db_session, hypothesis=hypothesis, result=result, as_of=AS_OF)
        after = db_session.execute(select(func.count()).select_from(ExperimentResult)).scalar_one()
        assert after == before


# -- credential handling ---------------------------------------------------


class TestCredentialHandling:
    """How the key is read, refused, and kept out of everything else.

    No real credential is involved. The placeholder below is a literal in this
    file and reaches no network — the client is constructed and then discarded,
    and `TestNoNetwork` above proves the recorded path opens no socket.
    """

    #: Obviously not keys. Long enough to be realistic, invalid by construction.
    OPENROUTER_PLACEHOLDER = "sk-or-v1-placeholder-not-a-real-key-0000000000"
    FEATHERLESS_PLACEHOLDER = "rc_placeholder-not-a-real-key-00000000000000"

    def a_settings(self, *, openrouter: str = "", featherless: str = "") -> Settings:
        return Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://localhost/x",
            openrouter_api_key=openrouter,
            featherless_api_key=featherless,
        )

    def configured(self, factory) -> Settings:  # noqa: ANN001
        """Settings with a placeholder for whichever provider is under test."""
        if factory is openrouter_config:
            return self.a_settings(openrouter=self.OPENROUTER_PLACEHOLDER)
        return self.a_settings(featherless=self.FEATHERLESS_PLACEHOLDER)

    @pytest.mark.parametrize("name", ["openrouter_api_key", "featherless_api_key"])
    def test_the_env_var_maps_to_the_setting(self, name: str) -> None:
        """Both provider keys map from their env names, case-insensitively."""
        field = Settings.model_fields[name]
        assert field.default.get_secret_value() == ""
        assert Settings.model_config["case_sensitive"] is False

    def test_the_removed_provider_leaves_no_configuration(self) -> None:
        assert "anthropic_api_key" not in Settings.model_fields
        assert not hasattr(Settings, "anthropic_configured")

    @pytest.mark.parametrize("factory", [openrouter_config, featherless_config])
    def test_an_empty_key_refuses_construction(self, factory) -> None:  # noqa: ANN001
        settings = self.a_settings()
        assert not settings.openrouter_configured
        assert not settings.featherless_configured
        with pytest.raises(HypothesisError, match="_API_KEY is not configured"):
            OpenAICompatibleProvider(factory(settings))

    @pytest.mark.parametrize("factory", [openrouter_config, featherless_config])
    def test_the_refusal_names_no_fallback(self, factory) -> None:  # noqa: ANN001
        """No ambient credential, no environment sniffing, no CLI profile."""
        with pytest.raises(HypothesisError) as excinfo:
            OpenAICompatibleProvider(factory(self.a_settings()))
        message = str(excinfo.value)
        assert "will not fall back to an ambient credential" in message
        assert "must name its own key" in message

    @pytest.mark.parametrize("factory", [openrouter_config, featherless_config])
    def test_a_placeholder_parses_and_configures(self, factory) -> None:  # noqa: ANN001
        """Configuration succeeds; nothing is transmitted by constructing it."""
        config = factory(self.configured(factory))
        assert config.api_key.get_secret_value()
        client = OpenAICompatibleProvider(config)
        assert client is not None
        assert client.info.provider == config.name

    def test_the_keys_are_secrets_and_never_repr(self) -> None:
        """SecretStr keeps them out of an accidental log line or traceback."""
        settings = self.a_settings(
            openrouter=self.OPENROUTER_PLACEHOLDER,
            featherless=self.FEATHERLESS_PLACEHOLDER,
        )
        for text in (
            repr(settings),
            str(settings),
            repr(settings.openrouter_api_key),
            repr(settings.featherless_api_key),
        ):
            assert self.OPENROUTER_PLACEHOLDER not in text
            assert self.FEATHERLESS_PLACEHOLDER not in text
        assert "**********" in repr(settings.openrouter_api_key)
        assert "**********" in repr(settings.featherless_api_key)

    def test_the_keys_never_enter_the_payload(self) -> None:
        """The prompt is built from statistics; settings never reach it."""
        settings = self.a_settings(
            openrouter=self.OPENROUTER_PLACEHOLDER,
            featherless=self.FEATHERLESS_PLACEHOLDER,
        )
        assert settings.openrouter_configured
        assert settings.featherless_configured
        rendered = json.dumps(a_request().as_prompt_payload())
        assert self.OPENROUTER_PLACEHOLDER not in rendered
        assert self.FEATHERLESS_PLACEHOLDER not in rendered
        for term in (
            "sk-or",
            "api_key",
            "openrouter_api_key",
            "featherless_api_key",
            "secret",
            "password",
        ):
            assert term not in rendered, term

    def test_the_payload_builder_cannot_see_settings(self) -> None:
        """`contracts.py` has no import path to configuration at all."""
        modules = imported_modules(pathlib.Path("app/agents/contracts.py"))
        assert not [m for m in modules if "config" in m or "settings" in m], modules

    def test_the_key_reaches_only_the_client_constructor(self) -> None:
        """`api_key=` reaches the client constructor; the request body never sees it."""
        tree = ast.parse(pathlib.Path("app/agents/hypothesis_agent.py").read_text())
        constructor_kwargs: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "OpenAI"
            ):
                constructor_kwargs = {k.arg for k in node.keywords if k.arg}
        assert constructor_kwargs == {"api_key", "base_url", "timeout"}

        for mode in SchemaMode:
            body = a_provider(mode)._request_kwargs(a_request())
            assert "api_key" not in body
            rendered = json.dumps(body).lower()
            assert "authorization" not in rendered
            assert "test-placeholder" not in rendered

    def test_no_credential_is_logged(self) -> None:
        """No print, log, or warn call anywhere in the agent package."""
        for path in agent_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id != "print", f"{path.name} prints"
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"info", "debug", "warning", "error"}, (
                        f"{path.name} logs"
                    )

    @pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "FEATHERLESS_API_KEY"])
    def test_the_template_documents_the_variable_without_a_value(self, name: str) -> None:
        template = pathlib.Path("../.env.example").read_text(encoding="utf-8")
        assert f"{name}=" in template
        for line in template.splitlines():
            if line.startswith(name):
                assert line.strip() == f"{name}=", line

    def test_the_template_drops_the_removed_provider_and_keeps_gemini(self) -> None:
        template = pathlib.Path("../.env.example").read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY" not in template
        assert "GEMINI_API_KEY=" in template


# -- the strict schema sent to a JSON_SCHEMA provider ----------------------


class TestStrictSchema:
    """`strict_schema()` exists because Pydantic's output is not strict-valid.

    A field with a default is omitted from `required`, but an OpenAI-style
    strict schema demands every property. Sending the raw schema would be
    rejected — or worse, silently accepted with enforcement dropped.
    """

    def test_every_property_is_required(self) -> None:
        schema = strict_schema()
        assert set(schema["required"]) == set(schema["properties"])
        assert len(schema["required"]) == 5

    def test_evidence_cited_is_required_in_the_copy(self) -> None:
        """The field Pydantic omits, because it carries a default."""
        assert "evidence_cited" in strict_schema()["required"]

    def test_additional_properties_stays_false(self) -> None:
        assert strict_schema()["additionalProperties"] is False

    def test_the_contract_itself_is_not_mutated(self) -> None:
        """`HypothesisProposal` is untouched — the copy is what changes."""
        raw = HypothesisProposal.model_json_schema()
        assert "evidence_cited" not in raw["required"]
        assert set(raw["required"]) == {"cell_key", "ladder_level", "claim", "rationale"}

    def test_it_returns_a_fresh_copy_each_time(self) -> None:
        first = strict_schema()
        first["required"] = []
        assert strict_schema()["required"] != []

    def test_the_claim_enum_survives_the_copy(self) -> None:
        schema = strict_schema()
        assert "$defs" in schema
        assert schema["$defs"]["Claim"]["enum"] == [c.value for c in Claim]

    def test_it_is_json_serialisable(self) -> None:
        assert json.loads(json.dumps(strict_schema())) == strict_schema()


# -- what each provider actually sends ------------------------------------


class TestRequestShape:
    """Built offline. `_request_kwargs` never touches the client."""

    def test_openrouter_sends_a_strict_json_schema(self) -> None:
        body = a_provider(SchemaMode.JSON_SCHEMA)._request_kwargs(a_request())
        fmt = body["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["name"] == SCHEMA_NAME
        assert fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["schema"] == strict_schema()

    def test_openrouter_requires_the_parameter_to_be_honoured(self) -> None:
        """Without this, routing may pick a backend that ignores the schema —
        an unenforced schema that looks enforced is worse than none."""
        body = a_provider(SchemaMode.JSON_SCHEMA)._request_kwargs(a_request())
        assert body["extra_body"]["provider"]["require_parameters"] is True

    def test_openrouter_uses_the_approved_model(self) -> None:
        body = a_provider(SchemaMode.JSON_SCHEMA)._request_kwargs(a_request())
        assert body["model"] == OPENROUTER_MODEL

    def test_featherless_sends_no_response_format(self) -> None:
        """Featherless documents none, so asking for one would be a fiction."""
        body = a_provider(SchemaMode.PROMPT_ONLY)._request_kwargs(a_request())
        assert "response_format" not in body
        assert "extra_body" not in body

    def test_featherless_asks_for_json_in_the_prompt(self) -> None:
        body = a_provider(SchemaMode.PROMPT_ONLY)._request_kwargs(a_request())
        system = body["messages"][0]["content"]
        assert JSON_ONLY_INSTRUCTION in system
        assert "single JSON object" in system

    def test_only_prompt_only_carries_the_json_instruction(self) -> None:
        strict = a_provider(SchemaMode.JSON_SCHEMA)._request_kwargs(a_request())
        assert JSON_ONLY_INSTRUCTION not in strict["messages"][0]["content"]

    @pytest.mark.parametrize("mode", list(SchemaMode))
    def test_neither_provider_declares_tools(self, mode: SchemaMode) -> None:
        """The property the whole isolation argument rests on."""
        body = a_provider(mode)._request_kwargs(a_request())
        assert "tools" not in body
        assert "tool_choice" not in body
        assert "functions" not in body
        assert "function_call" not in body

    @pytest.mark.parametrize("mode", list(SchemaMode))
    def test_both_carry_the_same_aggregate_payload(self, mode: SchemaMode) -> None:
        body = a_provider(mode)._request_kwargs(a_request())
        user = json.loads(body["messages"][1]["content"])
        assert user == a_request().as_prompt_payload()
        assert user["note"] == EXPLORATORY_NOTE

    @pytest.mark.parametrize("mode", list(SchemaMode))
    def test_no_forbidden_field_reaches_either_provider(self, mode: SchemaMode) -> None:
        rendered = json.dumps(a_provider(mode)._request_kwargs(a_request()))
        for banned in (
            "truth_y0",
            "truth_y1",
            "truth_harm_0",
            "truth_harm_1",
            "truth_segment",
            "risk_id",
            "customer",
            "order_id",
            "amount_at_risk",
            "unit_cost",
            "experiment_id",
        ):
            assert banned not in rendered, banned

    @pytest.mark.parametrize("mode", list(SchemaMode))
    def test_the_request_is_stateless(self, mode: SchemaMode) -> None:
        """Two messages, system then user. No history is retained."""
        body = a_provider(mode)._request_kwargs(a_request())
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user"]
        assert a_provider(mode)._request_kwargs(a_request()) == body


# -- fallback: what may be retried elsewhere, and what may not -------------


class _Boom:
    """A provider that always raises. Never opens a socket."""

    def __init__(self, exc: Exception, name: str = "boom") -> None:
        self._exc = exc
        self._info = ProviderInfo(provider=name, model="none", schema_mode="none")

    @property
    def info(self) -> ProviderInfo:
        return self._info

    def propose(self, request: HypothesisRequest) -> HypothesisProposal:
        raise self._exc


class TestFallback:
    def a_good(self, name: str = "secondary") -> RecordedProposals:
        return RecordedProposals(
            [proposal("higher_uplift")],
            info=ProviderInfo(provider=name, model="m", schema_mode="prompt_only"),
        )

    @pytest.mark.parametrize(
        "exc",
        [
            ProviderError("connection reset"),
            ProviderError("APITimeoutError"),
            ProviderError("RateLimitError"),
            ProviderError("HTTP 503"),
        ],
        ids=["connection", "timeout", "429", "5xx"],
    )
    def test_it_falls_over_on_a_retryable_failure(self, exc: Exception) -> None:
        chain = FallbackProvider([_Boom(exc, "primary"), self.a_good()])
        assert chain.propose(a_request()).cell_key == "insufficient_funds|upi"
        assert chain.info.provider == "secondary"

    @pytest.mark.parametrize(
        "exc",
        [
            HypothesisError("returned JSON that does not match the contract"),
            HypothesisError("cell_key 'invented' is not present in the population"),
            ValidationError.from_exception_data("HypothesisProposal", []),
            RuntimeError("HTTP 400"),
        ],
        ids=["schema", "invented-cell", "pydantic", "4xx"],
    )
    def test_it_does_not_fall_over_on_a_correctness_failure(self, exc: Exception) -> None:
        """A provider that returned the wrong shape has told us something.

        Retrying elsewhere would bury the finding, so these escape.
        """
        chain = FallbackProvider([_Boom(exc, "primary"), self.a_good()])
        with pytest.raises(type(exc)):
            chain.propose(a_request())
        assert chain.info.provider == "primary"

    def test_the_first_provider_wins_when_it_answers(self) -> None:
        chain = FallbackProvider([self.a_good("primary"), self.a_good("secondary")])
        chain.propose(a_request())
        assert chain.info.provider == "primary"
        assert chain.attempts == ["primary: ok"]

    def test_every_attempt_is_recorded(self) -> None:
        chain = FallbackProvider([_Boom(ProviderError("down"), "primary"), self.a_good()])
        chain.propose(a_request())
        assert len(chain.attempts) == 2
        assert chain.attempts[0].startswith("primary:")
        assert chain.attempts[1] == "secondary: ok"

    def test_it_raises_when_every_provider_fails(self) -> None:
        chain = FallbackProvider(
            [_Boom(ProviderError("a"), "one"), _Boom(ProviderError("b"), "two")]
        )
        with pytest.raises(ProviderError, match="every provider failed"):
            chain.propose(a_request())

    def test_an_empty_chain_is_refused(self) -> None:
        with pytest.raises(HypothesisError, match="at least one provider"):
            FallbackProvider([])


# -- provenance ------------------------------------------------------------


class TestProvenance:
    def test_a_validated_hypothesis_records_the_answering_provider(self) -> None:
        source = RecordedProposals(
            [proposal("higher_uplift")],
            info=ProviderInfo(
                provider="openrouter",
                model=OPENROUTER_MODEL,
                schema_mode=SchemaMode.JSON_SCHEMA.value,
            ),
        )
        payload = propose_hypothesis(source, a_request()).as_dict()
        assert payload["provider"] == "openrouter"
        assert payload["model"] == OPENROUTER_MODEL
        assert payload["schema_mode"] == "json_schema"
        assert payload["exploratory"] is True

    def test_a_fallback_records_the_provider_that_answered(self) -> None:
        """Not the one first tried — the one whose answer was used."""
        good = RecordedProposals(
            [proposal("lower_uplift")],
            info=ProviderInfo(provider="featherless", model="f", schema_mode="prompt_only"),
        )
        chain = FallbackProvider([_Boom(ProviderError("down"), "openrouter"), good])
        payload = propose_hypothesis(chain, a_request()).as_dict()
        assert payload["provider"] == "featherless"
        assert payload["schema_mode"] == "prompt_only"

    def test_provenance_is_optional_for_a_hand_built_hypothesis(self) -> None:
        payload = validate(a_request(), proposal("higher_uplift")).as_dict()
        assert "provider" not in payload
        assert payload["exploratory"] is True

    def test_provenance_carries_no_credential(self) -> None:
        source = RecordedProposals(
            [proposal("higher_uplift")],
            info=ProviderInfo(provider="openrouter", model="m", schema_mode="json_schema"),
        )
        rendered = json.dumps(propose_hypothesis(source, a_request()).as_dict())
        for term in ("sk-or", "api_key", "test-placeholder", "secret"):
            assert term not in rendered, term


# -- an empty response must say why ---------------------------------------


class _Usage:
    def __init__(
        self,
        completion_tokens: int | None = 2_048,
        prompt_tokens: int | None = 4_100,
        reasoning_tokens: int | None = None,
    ) -> None:
        self.completion_tokens = completion_tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens_details = (
            type("Details", (), {"reasoning_tokens": reasoning_tokens})()
            if reasoning_tokens is not None
            else None
        )


class _Message:
    def __init__(
        self,
        content: str | None = None,
        refusal: str | None = None,
        model_extra: dict[str, object] | None = None,
    ) -> None:
        self.content = content
        self.refusal = refusal
        self.model_extra = model_extra or {}


class _Choice:
    def __init__(self, message: _Message, finish_reason: str | None = "length") -> None:
        self.message = message
        self.finish_reason = finish_reason


class _Response:
    """The shape `chat.completions.create` returns. Built, never fetched."""

    def __init__(
        self,
        choices: list[_Choice] | None = None,
        usage: _Usage | None = None,
        response_id: str | None = "gen-abc123",
    ) -> None:
        self.choices = [] if choices is None else choices
        self.usage = usage
        self.id = response_id


def a_reasoning_response(**kwargs: object) -> _Response:
    """A reasoning model that spent its budget thinking and emitted no answer.

    The shape the first live call almost certainly returned: `content` empty,
    `finish_reason='length'`, reasoning tokens spent, a reasoning field present.
    """
    message = _Message(
        content=None,
        model_extra={"reasoning": "REASONING TEXT THAT MUST NEVER BE QUOTED"},
    )
    return _Response(
        choices=[_Choice(message, finish_reason="length")],
        usage=_Usage(completion_tokens=2_048, reasoning_tokens=2_048),
        **kwargs,  # type: ignore[arg-type]
    )


def an_openrouter_error_response() -> object:
    """A 200 with an error body, built the way the SDK actually builds one.

    Not hand-shaped: `construct_type` is what the SDK uses to parse responses,
    and it is *lenient* — a body with no `choices` key yields
    `ChatCompletion(choices=None)` rather than a validation error. That is how a
    successful HTTP response reaches the no-choices branch, and a hand-built
    stub would prove nothing about it.
    """
    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    return construct_type(
        value={
            "id": "gen-err-1",
            "object": "chat.completion",
            "created": 1,
            "model": "nvidia/nemotron-3-super-120b-a12b:free",
            "error": {
                "code": 429,
                "message": "Rate limit exceeded: free-models-per-day. sk-or-v1-NEVER-LEAK",
                "metadata": {"provider_name": "upstream"},
            },
        },
        type_=ChatCompletion,
    )


class TestTheNoChoicesDiagnosis:
    """The branch the second live failure hit, which reported nothing at all."""

    def test_an_empty_array_is_distinguished_from_an_absent_key(self) -> None:
        """`choices=[]` and `choices` absent are different events.

        An empty array is a well-formed completion with zero choices; an absent
        key means the body was not a completion at all. Collapsing them would
        lose the distinction that identifies which happened.
        """
        empty = _no_choices_diagnosis(_Response(choices=[], response_id="gen-empty"))
        absent = _no_choices_diagnosis(an_openrouter_error_response())
        assert "choices=0" in empty
        assert "choices=absent" in absent

    def test_it_reports_the_envelope(self) -> None:
        message = _no_choices_diagnosis(an_openrouter_error_response())
        assert "id='gen-err-1'" in message
        assert "model='nvidia/nemotron-3-super-120b-a12b:free'" in message
        assert "object='chat.completion'" in message

    def test_it_surfaces_a_two_hundred_with_an_error_body(self) -> None:
        """The likely shape of the second failure, and the reason for this work."""
        message = _no_choices_diagnosis(an_openrouter_error_response())
        assert "error_code=429" in message
        assert "extra_keys=['error']" in message
        assert "Rate limit exceeded" in message

    def test_a_long_error_message_is_bounded(self) -> None:
        """A moderation refusal can echo the request; the text is capped."""
        from openai._models import construct_type
        from openai.types.chat import ChatCompletion

        response = construct_type(
            value={
                "id": "gen-long",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                # Prose, not one long token: a single opaque run is removed by
                # the redactor before truncation can fire, which would test the
                # wrong guard.
                "error": {"code": 400, "message": "upstream refused the request. " * 200},
            },
            type_=ChatCompletion,
        )
        message = _no_choices_diagnosis(response)
        assert len(message) < 1_000
        assert "…" in message
        assert message.count("upstream refused") < 20

    def test_it_never_leaks_a_credential_from_an_error_body(self) -> None:
        """The planted key sits inside the very text that is reported."""
        message = _no_choices_diagnosis(an_openrouter_error_response())
        assert "sk-or-v1-NEVER-LEAK" not in message

    def test_it_reports_usage_when_present(self) -> None:
        response = _Response(choices=[], usage=_Usage(completion_tokens=0, prompt_tokens=4_100))
        message = _no_choices_diagnosis(response)
        assert "prompt_tokens=4100" in message
        assert "completion_tokens=0" in message

    def test_missing_usage_is_unavailable_not_zero(self) -> None:
        message = _no_choices_diagnosis(_Response(choices=[], usage=None))
        assert "usage=unavailable" in message

    def test_it_reports_metadata_names_only(self) -> None:
        response = _Response(choices=[])
        response.metadata = {"trace": "SECRET-TRACE-VALUE", "region": "eu"}  # type: ignore[attr-defined]
        message = _no_choices_diagnosis(response)
        assert "metadata_keys=['region', 'trace']" in message
        assert "SECRET-TRACE-VALUE" not in message

    def test_it_never_quotes_reasoning_from_an_extra_field(self) -> None:
        response = _Response(choices=[])
        response.model_extra = {"reasoning": "THINKING TEXT"}  # type: ignore[attr-defined]
        message = _no_choices_diagnosis(response)
        assert "extra_keys=['reasoning']" in message
        assert "THINKING TEXT" not in message

    def test_it_survives_a_response_missing_everything(self) -> None:
        message = _no_choices_diagnosis(object())
        assert "response carried no choices" in message
        assert "choices=absent" in message
        assert "usage=unavailable" in message


#: The three malformed shapes a structured-output model actually produces.
#: Each fails at `line 1 column 1` or thereabouts, so the decoder's own message
#: cannot tell them apart — which is the reason the shape diagnostic exists.
TRUNCATED = "{\n\n"
WITH_COMMENT = "{\n\n// note\n"
SINGLE_QUOTED = "{\n\n'k': 1}"


def a_parsed_response(content: str) -> object:
    """A well-formed completion whose content will not parse."""
    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    return construct_type(
        value={
            "id": "gen-parse-1",
            "object": "chat.completion",
            "created": 1,
            "model": "nvidia/nemotron-3-super-120b-a12b:free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {
                "prompt_tokens": 4_100,
                "completion_tokens": 2_048,
                "completion_tokens_details": {"reasoning_tokens": 1_900},
            },
        },
        type_=ChatCompletion,
    )


class TestTheShapePrefix:
    """Letters and digits flattened; structure kept."""

    def test_letters_become_a_and_digits_become_zero(self) -> None:
        assert _shape_prefix('{"cell_key": "abc", "n": 847}') == '{"aaaa_aaa": "aaa", "a": 000}'

    def test_punctuation_and_whitespace_survive(self) -> None:
        assert _shape_prefix(TRUNCATED) == "{\n\n"
        assert _shape_prefix(WITH_COMMENT) == "{\n\n// aaaa\n"
        assert _shape_prefix(SINGLE_QUOTED) == "{\n\n'a': 0}"

    def test_it_is_bounded(self) -> None:
        assert len(_shape_prefix("x" * 500)) == SHAPE_PREFIX_LIMIT

    def test_no_value_survives_flattening(self) -> None:
        """A cell key, a customer name and a token all flatten identically."""
        for secret in ("insufficient_funds", "sk-or-v1-abcdef", "Jane Doe"):
            flattened = _shape_prefix(f'{{"k": "{secret}"}}')
            assert secret not in flattened
            assert secret.lower() not in flattened.lower()

    def test_the_three_shapes_are_distinguishable(self) -> None:
        """The whole point: one decoder message, three different findings."""
        shapes = {_shape_prefix(c) for c in (TRUNCATED, WITH_COMMENT, SINGLE_QUOTED)}
        assert len(shapes) == 3


class TestTheParseFailureDiagnostic:
    """The exact contents named in the brief, each raising with its evidence."""

    def _provider_returning(self, content: str) -> OpenAICompatibleProvider:
        response = a_parsed_response(content)
        provider = a_provider(SchemaMode.JSON_SCHEMA)
        provider._client = type(  # noqa: SLF001
            "Client",
            (),
            {
                "chat": type(
                    "Chat",
                    (),
                    {
                        "completions": type(
                            "Completions", (), {"create": staticmethod(lambda **_: response)}
                        )()
                    },
                )()
            },
        )()
        return provider

    @pytest.mark.parametrize(
        ("content", "shape"),
        [
            (TRUNCATED, "{\n\n"),
            (WITH_COMMENT, "{\n\n// aaaa\n"),
            (SINGLE_QUOTED, "{\n\n'a': 0}"),
        ],
        ids=["truncated", "comment", "single-quoted-key"],
    )
    def test_each_shape_raises_with_its_diagnostic(self, content: str, shape: str) -> None:
        provider = self._provider_returning(content)
        with pytest.raises(HypothesisError) as caught:
            provider.propose(a_request())
        message = str(caught.value)
        assert "is not JSON" in message
        assert f"shape_prefix={shape!r}" in message
        assert f"content_length={len(content)}" in message
        assert "id='gen-parse-1'" in message
        assert "finish_reason='length'" in message
        assert "prompt_tokens=4100" in message
        assert "completion_tokens=2048" in message
        assert "reasoning_tokens=1900" in message
        assert "provider='openrouter'" in message

    @pytest.mark.parametrize(
        ("content", "first", "last"),
        [(TRUNCATED, "{", "{"), (WITH_COMMENT, "{", "e"), (SINGLE_QUOTED, "{", "}")],
        ids=["truncated", "comment", "single-quoted-key"],
    )
    def test_the_boundary_characters_are_reported(
        self, content: str, first: str, last: str
    ) -> None:
        message = _parse_failure_diagnosis(content, a_parsed_response(content), "openrouter")
        assert f"first_non_whitespace_char={first!r}" in message
        assert f"last_non_whitespace_char={last!r}" in message

    def test_a_fence_is_detected_at_both_ends(self) -> None:
        """The most common structured-output failure, invisible to a decoder."""
        fenced = '```json\n{"cell_key": "x"}\n```'
        message = _parse_failure_diagnosis(fenced, a_parsed_response(fenced), "openrouter")
        assert "starts_with_fence=True" in message
        assert "ends_with_fence=True" in message

    def test_an_unfenced_shape_says_so(self) -> None:
        message = _parse_failure_diagnosis(TRUNCATED, a_parsed_response(TRUNCATED), "openrouter")
        assert "starts_with_fence=False" in message
        assert "ends_with_fence=False" in message

    def test_it_never_reproduces_the_content(self) -> None:
        """A real-looking malformed payload; no value may survive."""
        content = '{"cell_key": "insufficient_funds|upi", "rationale": "Jane Doe paid",'
        message = _parse_failure_diagnosis(content, a_parsed_response(content), "openrouter")
        for secret in ("insufficient_funds", "Jane Doe", "rationale", "cell_key"):
            assert secret not in message, secret

    def test_it_never_leaks_a_credential_in_the_content(self) -> None:
        content = '{"k": "sk-or-v1-LEAKED-KEY-MATERIAL"}'
        message = _parse_failure_diagnosis(content, a_parsed_response(content), "openrouter")
        assert "sk-or" not in message
        assert "LEAKED" not in message

    def test_it_never_leaks_reasoning_text(self) -> None:
        from openai._models import construct_type
        from openai.types.chat import ChatCompletion

        response = construct_type(
            value={
                "id": "gen-r",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": TRUNCATED,
                            "reasoning": "PRIVATE CHAIN OF THOUGHT",
                        },
                    }
                ],
            },
            type_=ChatCompletion,
        )
        message = _parse_failure_diagnosis(TRUNCATED, response, "openrouter")
        assert "PRIVATE CHAIN OF THOUGHT" not in message

    def test_missing_usage_is_unavailable_not_zero(self) -> None:
        message = _parse_failure_diagnosis(TRUNCATED, _Response(choices=[]), "openrouter")
        assert "usage=unavailable" in message

    def test_it_survives_a_response_missing_everything(self) -> None:
        message = _parse_failure_diagnosis(TRUNCATED, object(), "openrouter")
        assert "shape_prefix=" in message
        assert "usage=unavailable" in message


@pytest.mark.db
class TestAParseFailurePersistsNothing:
    """A malformed response is a finding about the model, not about the run."""

    @pytest.mark.parametrize(
        "content", [TRUNCATED, WITH_COMMENT, SINGLE_QUOTED], ids=["truncated", "comment", "quoted"]
    )
    def test_nothing_is_written_and_nothing_is_committed(
        self, db_session: Session, content: str
    ) -> None:
        from sqlalchemy import func, select

        from app.models.audit_event import AuditEvent
        from app.services.hypothesis.service import generate_and_record

        def rows() -> int:
            return db_session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()

        before = rows()
        provider = TestTheParseFailureDiagnostic()._provider_returning(content)
        with pytest.raises(HypothesisError, match="is not JSON"):
            generate_and_record(
                db_session,
                EXPERIMENT,
                provider=provider,
                as_of=AS_OF,
                alpha_bps=500,
                mde_bps=1_000,
                request=a_request(),
            )
        assert rows() == before
        assert db_session.in_transaction()


def a_body_error(code: object, message: str = "upstream said no") -> object:
    """A 200 whose body carries an error code, built the way the SDK builds one."""
    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    return construct_type(
        value={
            "id": "gen-code",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "error": {"code": code, "message": message},
        },
        type_=ChatCompletion,
    )


class TestBodyErrorsAreClassifiedLikeHttpStatuses:
    """A failure reported inside a 200 must be routed like the same HTTP status.

    OpenRouter reports some conditions as a real status and others as HTTP 200
    with an error body. Before this, the body channel made *everything*
    retryable — so a schema error reported one way was a permanent finding and
    the identical error reported the other way looked like an outage. The
    module's own docstring states the rule; only one of the two channels was
    obeying it.
    """

    def _provider(self) -> OpenAICompatibleProvider:
        return a_provider(SchemaMode.JSON_SCHEMA)

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 499])
    def test_a_client_error_is_permanent(self, code: int) -> None:
        """Not a `ProviderError`: retrying asks the same bad question again."""
        assert _is_client_error(code) is True
        assert _body_error_code(a_body_error(code)) == code

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_a_rate_limit_or_server_error_stays_retryable(self, code: int) -> None:
        assert _is_client_error(code) is False
        assert _body_error_code(a_body_error(code)) == code

    def test_429_is_excluded_from_the_client_error_rule(self) -> None:
        """A rate limit is a statement about timing, not about the request.

        The exact carve-out the HTTP branch makes, asserted here so the two
        channels cannot drift apart.
        """
        assert _is_client_error(429) is False
        assert _is_client_error(428) is True
        assert _is_client_error(430) is True

    def test_no_error_body_yields_no_code(self) -> None:
        assert _body_error_code(_Response(choices=[])) is None
        assert _is_client_error(None) is False

    def test_a_non_integer_code_is_not_evidence(self) -> None:
        """A code that cannot be compared is not a code. Falls through to
        transient rather than guessing at a permanent failure."""
        for code in ("400", None, 4.5, {"nested": 1}, True):
            assert _body_error_code(a_body_error(code)) is None, code

    def test_it_survives_a_response_with_no_extra(self) -> None:
        assert _body_error_code(object()) is None


class TestProposeRoutesBodyErrors:
    """The classification, exercised through `propose` rather than the helper."""

    def _provider_returning(self, response: object) -> OpenAICompatibleProvider:
        provider = a_provider(SchemaMode.JSON_SCHEMA)
        provider._client = type(  # noqa: SLF001
            "Client",
            (),
            {
                "chat": type(
                    "Chat",
                    (),
                    {
                        "completions": type(
                            "Completions", (), {"create": staticmethod(lambda **_: response)}
                        )()
                    },
                )()
            },
        )()
        return provider

    @pytest.mark.parametrize("code", [400, 404])
    def test_a_client_error_raises_hypothesis_error(self, code: int) -> None:
        provider = self._provider_returning(a_body_error(code))
        with pytest.raises(HypothesisError) as caught:
            provider.propose(a_request())
        assert f"error_code={code}" in str(caught.value)
        assert "response carried no choices" in str(caught.value)

    @pytest.mark.parametrize("code", [429, 502])
    def test_a_transient_error_raises_provider_error(self, code: int) -> None:
        provider = self._provider_returning(a_body_error(code))
        with pytest.raises(ProviderError) as caught:
            provider.propose(a_request())
        assert f"error_code={code}" in str(caught.value)

    def test_the_measured_502_is_retryable(self) -> None:
        """The third live failure, replayed from its recorded shape."""
        response = a_body_error(502, "Upstream error from Nvidia: Service temporarily overloaded")
        provider = self._provider_returning(response)
        with pytest.raises(ProviderError) as caught:
            provider.propose(a_request())
        message = str(caught.value)
        assert "error_code=502" in message
        assert "Service temporarily overloaded" in message
        assert "choices=absent" in message

    def test_empty_choices_with_no_error_body_stays_retryable(self) -> None:
        """No error body means no evidence of a bad request; stay transient."""
        provider = self._provider_returning(_Response(choices=[]))
        with pytest.raises(ProviderError) as caught:
            provider.propose(a_request())
        assert "choices=0" in str(caught.value)

    @pytest.mark.parametrize("code", [400, 404])
    def test_a_client_error_is_not_a_provider_error(self, code: int) -> None:
        """`HypothesisError` is not a `ProviderError`, so a chain cannot fall
        over on it — the property the whole change exists to establish."""
        assert not issubclass(HypothesisError, ProviderError)
        provider = self._provider_returning(a_body_error(code))
        with pytest.raises(HypothesisError):
            provider.propose(a_request())

    def test_a_body_client_error_does_not_fall_over_in_a_chain(self) -> None:
        """End to end: a two-provider chain must not retry a bad request."""
        bad = self._provider_returning(a_body_error(400))
        good = RecordedProposals(
            [proposal("higher_uplift")],
            info=ProviderInfo(provider="second", model="m", schema_mode="json_schema"),
        )
        chain = FallbackProvider([bad, good])
        with pytest.raises(HypothesisError):
            chain.propose(a_request())
        assert chain.attempts == []

    def test_a_body_server_error_does_fall_over_in_a_chain(self) -> None:
        """The contrast that makes the test above non-vacuous."""
        down = self._provider_returning(a_body_error(502))
        good = RecordedProposals(
            [proposal("higher_uplift")],
            info=ProviderInfo(provider="second", model="m", schema_mode="json_schema"),
        )
        chain = FallbackProvider([down, good])
        assert chain.propose(a_request()).cell_key == "insufficient_funds|upi"
        assert chain.info.provider == "second"


class TestTheEmptyResponseDiagnosis:
    """`empty response` was true and useless.

    It could not distinguish a refusal, an early stop, and a model that spent
    its entire token budget reasoning — which is the whole question when a live
    call fails, discarded at the one moment it mattered.
    """

    def test_it_names_the_stop_reason(self) -> None:
        message = _empty_content_diagnosis(a_reasoning_response())
        assert "finish_reason='length'" in message

    def test_it_reports_that_a_reasoning_field_was_present(self) -> None:
        message = _empty_content_diagnosis(a_reasoning_response())
        assert "reasoning_present=True" in message
        assert "reasoning_field='reasoning'" in message

    def test_it_never_quotes_the_reasoning_text(self) -> None:
        """The field's presence locates the channel; its contents stay out.

        Reasoning is model output about the payload, and an exception message
        travels into logs that the payload itself is kept out of.
        """
        message = _empty_content_diagnosis(a_reasoning_response())
        assert "REASONING TEXT THAT MUST NEVER BE QUOTED" not in message
        assert "MUST NEVER" not in message

    def test_it_reports_token_usage(self) -> None:
        message = _empty_content_diagnosis(a_reasoning_response())
        assert "completion_tokens=2048" in message
        assert "prompt_tokens=4100" in message
        assert "reasoning_tokens=2048" in message

    def test_it_reports_the_completion_id(self) -> None:
        """So a failure can be matched against the provider's own record."""
        assert "id='gen-abc123'" in _empty_content_diagnosis(a_reasoning_response())

    def test_it_reports_a_refusal_distinctly_from_a_length_stop(self) -> None:
        refused = _Response(
            choices=[_Choice(_Message(content=None, refusal="I cannot help"), "stop")],
            usage=_Usage(completion_tokens=12),
        )
        message = _empty_content_diagnosis(refused)
        assert "refused=True" in message
        assert "finish_reason='stop'" in message
        assert "reasoning_present=False" in message
        assert "I cannot help" not in message

    def test_missing_usage_is_reported_as_unavailable_not_zero(self) -> None:
        """Absent is not zero — the same rule the ledger holds."""
        bare = _Response(choices=[_Choice(_Message(), "stop")], usage=None)
        message = _empty_content_diagnosis(bare)
        assert "usage=unavailable" in message
        assert "completion_tokens=0" not in message

    def test_it_survives_a_response_missing_everything(self) -> None:
        """It runs on a path that is already failing; a surprise here would
        replace a useful error with a confusing one."""
        message = _empty_content_diagnosis(object())
        assert "empty content" in message
        assert "usage=unavailable" in message

    @pytest.mark.parametrize("field", list(REASONING_FIELDS))
    def test_every_known_reasoning_field_is_detected(self, field: str) -> None:
        response = _Response(choices=[_Choice(_Message(model_extra={field: "thinking"}), "length")])
        message = _empty_content_diagnosis(response)
        assert "reasoning_present=True" in message
        assert f"reasoning_field={field!r}" in message
        assert "thinking" not in message.replace("reasoning", "")

    def test_no_credential_can_appear_in_the_diagnosis(self) -> None:
        """Only metadata is read, so a key in the response cannot escape."""
        response = _Response(
            choices=[_Choice(_Message(model_extra={"api_key": "sk-or-v1-SECRET"}), "stop")]
        )
        message = _empty_content_diagnosis(response)
        assert "sk-or" not in message
        assert "SECRET" not in message


class TestProposeUsesTheDiagnosis:
    """The provider must emit the diagnosis, not just possess it."""

    def _provider_returning(self, response: object) -> OpenAICompatibleProvider:
        provider = a_provider(SchemaMode.JSON_SCHEMA)
        provider._client = type(  # noqa: SLF001
            "Client",
            (),
            {
                "chat": type(
                    "Chat",
                    (),
                    {
                        "completions": type(
                            "Completions",
                            (),
                            {"create": staticmethod(lambda **_: response)},
                        )()
                    },
                )()
            },
        )()
        return provider

    def test_an_empty_content_response_raises_the_diagnosis(self) -> None:
        provider = self._provider_returning(a_reasoning_response())
        with pytest.raises(ProviderError) as caught:
            provider.propose(a_request())
        message = str(caught.value)
        assert "openrouter: empty content" in message
        assert "finish_reason='length'" in message
        assert "reasoning_present=True" in message
        assert "completion_tokens=2048" in message
        assert "REASONING TEXT" not in message

    def test_a_response_with_no_choices_is_named_distinctly(self) -> None:
        """A different failure from an empty message, and it now says so."""
        provider = self._provider_returning(_Response(choices=[]))
        with pytest.raises(ProviderError, match="response carried no choices"):
            provider.propose(a_request())

    def test_the_no_choices_error_carries_the_envelope(self) -> None:
        """The second live failure landed here and reported nothing.

        The branch was split out and left uninstrumented — the same mistake
        that had just been fixed one line below it.
        """
        provider = self._provider_returning(an_openrouter_error_response())
        with pytest.raises(ProviderError) as caught:
            provider.propose(a_request())
        message = str(caught.value)
        assert "response carried no choices" in message
        assert "choices=absent" in message
        assert "error_code=429" in message
        assert "id='gen-err-1'" in message

    def test_the_bare_empty_response_wording_is_gone(self) -> None:
        provider = self._provider_returning(a_reasoning_response())
        with pytest.raises(ProviderError) as caught:
            provider.propose(a_request())
        assert str(caught.value) != "openrouter: empty response"

    def test_a_populated_response_still_parses(self) -> None:
        """The happy path is untouched."""
        payload = json.dumps(fixtures()["higher_uplift"])
        provider = self._provider_returning(
            _Response(choices=[_Choice(_Message(content=payload), "stop")])
        )
        assert provider.propose(a_request()).cell_key == "insufficient_funds|upi"


# -- free-only: no rate limit may become a bill ----------------------------


def a_free_provider() -> OpenAICompatibleProvider:
    """The real OpenRouter config, with a synthetic key. Never asked to propose."""
    return OpenAICompatibleProvider(openrouter_config(free_only_settings()))


def a_paid_provider() -> OpenAICompatibleProvider:
    """The real Featherless config, with a synthetic key. Never asked to propose."""
    return OpenAICompatibleProvider(featherless_config(free_only_settings()))


def free_only_settings() -> Settings:
    """Both keys present and synthetic, so either config can be constructed.

    Written out rather than defaulted: `Settings` reads the repository `.env`,
    and a test that builds a client must not inherit a real credential.
    """
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://localhost/x",
        openrouter_api_key=TEST_KEY.get_secret_value(),
        featherless_api_key=TEST_KEY.get_secret_value(),
    )


class _FreeBoom(_Boom):
    """A free provider that always raises. Declares its freeness explicitly."""

    is_free = True


class _PaidAnswerer:
    """A billable provider that would answer instantly. Never opens a socket.

    `RecordedProposals` cannot stand in for this: it declares itself free
    because it has no client, so a chain containing one is legitimately free
    and the guard would rightly let it through.
    """

    is_free = False

    def __init__(self, name: str = "featherless") -> None:
        self._info = ProviderInfo(provider=name, model="paid", schema_mode="prompt_only")

    @property
    def info(self) -> ProviderInfo:
        return self._info

    def propose(self, request: HypothesisRequest) -> HypothesisProposal:
        return proposal("higher_uplift")


class TestFreeOnly:
    def test_a_free_chain_is_accepted(self) -> None:
        chain = FallbackProvider([a_free_provider()], free_only=True)
        assert chain.free_only is True
        assert chain.is_free is True

    def test_a_paid_provider_is_refused(self) -> None:
        with pytest.raises(HypothesisError, match="featherless did not declare is_free"):
            FallbackProvider([a_paid_provider()], free_only=True)

    def test_a_free_then_paid_chain_is_refused(self) -> None:
        """The exact shape that turns a 429 into a charge.

        Refused at construction, so there is no object on which a fallback
        could later be attempted.
        """
        with pytest.raises(HypothesisError, match="free-only chain refused"):
            FallbackProvider([a_free_provider(), a_paid_provider()], free_only=True)

    def test_an_undeclared_provider_is_treated_as_billable(self) -> None:
        """Silence is not consent. `_Boom` never mentions billing, so it is paid."""
        with pytest.raises(HypothesisError, match="did not declare is_free"):
            FallbackProvider([_Boom(ProviderError("x"), "mystery")], free_only=True)

    def test_a_rate_limit_cannot_cross_into_a_paid_provider(self) -> None:
        """429 is the free tier's normal signal; it must surface, not spend.

        The paid provider here answers instantly if it is ever reached, so a
        merely advisory guard would leave this test passing with a proposal in
        hand instead of raising.
        """
        would_answer = _PaidAnswerer()
        rate_limited = _FreeBoom(ProviderError("RateLimitError"), "openrouter")

        with pytest.raises(HypothesisError, match="free-only chain refused"):
            FallbackProvider([rate_limited, would_answer], free_only=True)

        # The same chain without the guard reaches the paid provider — which is
        # the behaviour being prevented, asserted so the test above cannot pass
        # vacuously against a chain that would never have fallen over anyway.
        unguarded = FallbackProvider([rate_limited, would_answer])
        unguarded.propose(a_request())
        assert unguarded.info.provider == "featherless"

        # And the free-only chain that *is* buildable raises rather than retrying.
        chain = FallbackProvider([rate_limited], free_only=True)
        with pytest.raises(ProviderError, match="every provider failed"):
            chain.propose(a_request())
        assert chain.attempts == ["openrouter: RateLimitError"]

    def test_the_production_chain_holds_openrouter_alone(self) -> None:
        chain = free_only_chain(free_only_settings())
        assert chain.free_only is True
        assert chain.info.provider == "openrouter"
        assert chain.info.model == OPENROUTER_MODEL
        assert chain.info.model.endswith(FREE_MODEL_SUFFIX)

    def test_the_production_chain_never_constructs_featherless(self) -> None:
        """No call to a paid config is made anywhere in the function.

        Stronger than inspecting the built chain, which would only show that
        Featherless had been filtered out of a list it was still built into.
        Walked as an AST rather than scanned as text: the function's docstring
        says the word "featherless" while explaining its absence, which is
        exactly the sentence a substring check would trip over.
        """
        tree = ast.parse(inspect.getsource(free_only_chain))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "featherless_config" not in called
        assert called >= {"OpenAICompatibleProvider", "openrouter_config", "FallbackProvider"}

    def test_the_free_declaration_is_not_inferred_from_the_model_name(self) -> None:
        """A `:free` model with no declaration is still treated as billable."""
        config = ProviderConfig(
            name="lookalike",
            base_url=OPENROUTER_BASE_URL,
            model=OPENROUTER_MODEL,
            api_key=TEST_KEY,
            schema_mode=SchemaMode.JSON_SCHEMA,
        )
        assert config.is_free is False
        with pytest.raises(HypothesisError, match="did not declare is_free"):
            FallbackProvider([OpenAICompatibleProvider(config)], free_only=True)

    def test_a_free_declaration_is_corroborated_by_the_suffix(self) -> None:
        """Declaring free while naming a paid model is refused, not believed."""
        with pytest.raises(HypothesisError, match="declared free but does not end"):
            ProviderConfig(
                name="mismatch",
                base_url=FEATHERLESS_BASE_URL,
                model=FEATHERLESS_MODEL,
                api_key=TEST_KEY,
                schema_mode=SchemaMode.PROMPT_ONLY,
                is_free=True,
            )

    def test_a_recorded_source_is_free_because_it_has_no_client(self) -> None:
        source = RecordedProposals([proposal("higher_uplift")])
        assert source.is_free is True
        chain = FallbackProvider([source], free_only=True)
        assert chain.propose(a_request()).cell_key == "insufficient_funds|upi"

    def test_provenance_survives_the_free_only_path(self) -> None:
        """The guard changes what may be called, not what is recorded."""
        source = RecordedProposals(
            [proposal("higher_uplift")],
            info=ProviderInfo(
                provider="openrouter",
                model=OPENROUTER_MODEL,
                schema_mode=SchemaMode.JSON_SCHEMA.value,
            ),
        )
        chain = FallbackProvider([source], free_only=True)
        payload = propose_hypothesis(chain, a_request()).as_dict()
        assert payload["provider"] == "openrouter"
        assert payload["model"] == OPENROUTER_MODEL
        assert payload["schema_mode"] == "json_schema"
        assert payload["exploratory"] is True

    def test_the_free_provider_declares_no_tools(self) -> None:
        body = a_free_provider()._request_kwargs(a_request())
        assert "tools" not in body
        assert "tool_choice" not in body

    def test_the_free_request_body_carries_no_credential(self) -> None:
        """The key configures the transport; it never enters the payload."""
        rendered = json.dumps(a_free_provider()._request_kwargs(a_request()))
        for term in (TEST_KEY.get_secret_value(), "api_key", "Authorization", "sk-or"):
            assert term not in rendered, term


# -- the contracts the guard was not allowed to widen ----------------------


class TestContractsUnchanged:
    def test_provider_info_has_exactly_three_fields(self) -> None:
        """Billing lives on `ProviderConfig`; provenance stays provenance.

        `is_free` was briefly added here and removed: an audit record should not
        have to carry an operational safety flag for the safety guard to work,
        and this asserts it did not creep back.
        """
        assert tuple(ProviderInfo.__dataclass_fields__) == ("provider", "model", "schema_mode")
        assert set(ProviderInfo("p", "m", "s").as_dict()) == {
            "provider",
            "model",
            "schema_mode",
        }
        assert not hasattr(ProviderInfo("p", "m", "s"), "is_free")

    def test_the_proposal_schema_is_unchanged(self) -> None:
        assert tuple(HypothesisProposal.model_fields) == (
            "cell_key",
            "ladder_level",
            "claim",
            "rationale",
            "evidence_cited",
        )
        assert HypothesisProposal.model_config["extra"] == "forbid"
        assert HypothesisProposal.model_config["frozen"] is True
