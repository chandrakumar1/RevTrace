"""The incrementality gate, tested without a database.

Every rule in the gate is a pure function of its inputs, so this file needs no
session, no clock and no fixtures beyond builders. That is the point of keeping
the gate pure: the rules that decide whether to spend money are checkable in
milliseconds and cannot drift because a table changed.

Nothing here asserts a business outcome. The tests assert the *rules* — that a
holdout never acts, that an abstention always carries a reason, that the cost
comparison is strict, and that two identical inputs produce two identical
decisions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.engine.policy_engine import (
    BPS_SCALE,
    EXPLORATION_BUDGET_BPS,
    EXPLORE_SALT,
    GateDecision,
    GrayZonePolicy,
    InterventionTerms,
    PolicyError,
    UpliftEvidence,
    clears_cost,
    decide,
    expected_incremental_recovery,
    explore_bucket_for,
    explore_digest,
    is_explore_eligible,
)
from app.models.enums import AbstainReason, Arm, CaseDecision, Quadrant

AS_OF = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

#: A risk/experiment pair chosen for being *outside* the exploration budget, so
#: the evidence rules can be tested without exploration rescuing the case. The
#: helper below asserts that, so a salt change fails loudly here rather than
#: silently turning these into exploration tests.
RISK = uuid.UUID("11111111-1111-4111-8111-111111111111")
EXPERIMENT = uuid.UUID("22222222-2222-4222-8222-222222222222")


def a_link(**overrides: object) -> InterventionTerms:
    """The seeded `create_payment_link` terms, overridable."""
    terms = {
        "code": "create_payment_link",
        "unit_cost": 200,
        "cooldown_hours": 24,
        "max_per_customer_per_month": 3,
        "requires_afa": False,
        "is_active": True,
    }
    terms.update(overrides)
    return InterventionTerms(**terms)  # type: ignore[arg-type]


def evidence(
    *,
    uplift_bps: int = 1_500,
    low: int = 1_000,
    high: int = 2_000,
    quadrant: Quadrant = Quadrant.PERSUADABLE,
    qualified: bool = True,
) -> UpliftEvidence:
    return UpliftEvidence(
        uplift_bps=uplift_bps,
        uplift_ci_low_bps=low,
        uplift_ci_high_bps=high,
        quadrant=quadrant,
        qualified=qualified,
    )


#: Distinguishes "argument omitted" from an explicit `uplift=None`, which is a
#: meaningful input meaning "this risk has no estimate at all". Defaulting to
#: None would make those two indistinguishable and silently pass evidence to
#: tests that meant to withhold it.
UNSET = object()


def gate(
    *,
    risk_id: uuid.UUID = RISK,
    experiment_id: uuid.UUID = EXPERIMENT,
    arm: Arm = Arm.TREATMENT,
    uplift: UpliftEvidence | None | object = UNSET,
    intervention: InterventionTerms | None = None,
    expected_recovery: int = 100_000,
    max_cost: int = 5_000,
    **kwargs: object,
) -> GateDecision:
    return decide(
        risk_id,
        experiment_id,
        arm=arm,
        uplift=evidence() if uplift is UNSET else uplift,  # type: ignore[arg-type]
        intervention=intervention or a_link(),
        expected_recovery=expected_recovery,
        max_cost=max_cost,
        as_of=kwargs.pop("as_of", AS_OF),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def unexplored_pair() -> tuple[uuid.UUID, uuid.UUID]:
    """A risk/experiment pair outside the exploration budget."""
    assert not is_explore_eligible(RISK, EXPERIMENT), (
        "the default fixture pair fell inside the exploration budget; the "
        "evidence tests below would be testing exploration instead"
    )
    return RISK, EXPERIMENT


def an_explored_pair() -> tuple[uuid.UUID, uuid.UUID]:
    """Search deterministically for a pair inside the exploration budget."""
    for index in range(10_000):
        risk = uuid.uuid5(uuid.NAMESPACE_DNS, f"explore-{index}")
        if is_explore_eligible(risk, EXPERIMENT):
            return risk, EXPERIMENT
    raise AssertionError("no risk fell inside the exploration budget")


# -- 1. the holdout -------------------------------------------------------


class TestHoldoutAlwaysAbstains:
    """The counterfactual's basis. No other input may override it."""

    def test_a_holdout_abstains(self) -> None:
        decision = gate(arm=Arm.HOLDOUT)
        assert decision.decision is CaseDecision.ABSTAIN
        assert decision.reason is AbstainReason.HOLDOUT_ARM

    @pytest.mark.parametrize("uplift_bps", [9_000, 1, 0])
    def test_even_an_enormous_uplift_does_not_rescue_it(self, uplift_bps: int) -> None:
        decision = gate(
            arm=Arm.HOLDOUT,
            uplift=evidence(uplift_bps=uplift_bps, low=uplift_bps, high=uplift_bps + 1),
        )
        assert decision.reason is AbstainReason.HOLDOUT_ARM

    def test_a_free_intervention_does_not_rescue_it(self) -> None:
        """Cost zero would clear every value check. The arm still decides."""
        decision = gate(arm=Arm.HOLDOUT, intervention=a_link(unit_cost=0))
        assert decision.reason is AbstainReason.HOLDOUT_ARM

    def test_exploration_does_not_rescue_it(self) -> None:
        risk, experiment = an_explored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, arm=Arm.HOLDOUT, uplift=None)
        assert decision.decision is CaseDecision.ABSTAIN
        assert decision.reason is AbstainReason.HOLDOUT_ARM
        assert not decision.explored


# -- 2. evidence ----------------------------------------------------------


class TestEvidence:
    def test_a_qualifying_treatment_case_acts(self) -> None:
        risk, experiment = unexplored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment)
        assert decision.decision is CaseDecision.ACT
        assert decision.reason is None
        assert decision.acted

    def test_a_missing_estimate_abstains_for_insufficient_sample(self) -> None:
        """Absent is not zero. A missing score is not a measured null effect."""
        risk, experiment = unexplored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=None)
        assert decision.reason is AbstainReason.INSUFFICIENT_SAMPLE

    def test_a_non_qualifying_cell_abstains_for_insufficient_sample(self) -> None:
        risk, experiment = unexplored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=evidence(qualified=False))
        assert decision.reason is AbstainReason.INSUFFICIENT_SAMPLE

    def test_an_interval_containing_zero_abstains(self) -> None:
        risk, experiment = unexplored_pair()
        decision = gate(
            risk_id=risk,
            experiment_id=experiment,
            uplift=evidence(uplift_bps=50, low=-200, high=300),
        )
        assert decision.reason is AbstainReason.UPLIFT_NOT_SIGNIFICANT

    def test_a_negative_uplift_abstains(self) -> None:
        decision = gate(uplift=evidence(uplift_bps=-500, low=-900, high=-100))
        assert decision.reason is AbstainReason.NEGATIVE_UPLIFT

    @pytest.mark.parametrize(
        ("quadrant", "reason"),
        [
            (Quadrant.SLEEPING_DOG, AbstainReason.SLEEPING_DOG),
            (Quadrant.SURE_THING, AbstainReason.SURE_THING),
            (Quadrant.LOST_CAUSE, AbstainReason.LOST_CAUSE),
        ],
    )
    def test_a_quadrant_label_settles_the_decision(
        self, quadrant: Quadrant, reason: AbstainReason
    ) -> None:
        assert gate(uplift=evidence(quadrant=quadrant)).reason is reason

    def test_negative_uplift_outranks_the_interval_rule(self) -> None:
        """Both fire; the documented order names the more specific reason."""
        decision = gate(uplift=evidence(uplift_bps=-100, low=-500, high=500))
        assert decision.reason is AbstainReason.NEGATIVE_UPLIFT

    def test_an_inverted_interval_is_refused_at_construction(self) -> None:
        with pytest.raises(PolicyError, match="inverted"):
            UpliftEvidence(
                uplift_bps=0,
                uplift_ci_low_bps=100,
                uplift_ci_high_bps=-100,
                quadrant=Quadrant.PERSUADABLE,
                qualified=True,
            )

    def test_an_estimate_outside_its_own_interval_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="outside its interval"):
            UpliftEvidence(
                uplift_bps=5_000,
                uplift_ci_low_bps=0,
                uplift_ci_high_bps=100,
                quadrant=Quadrant.PERSUADABLE,
                qualified=True,
            )


# -- 3. the cost-recovery check -------------------------------------------


class TestIncrementalValue:
    def test_expected_incremental_is_recovery_times_uplift(self) -> None:
        # 15% of Rs 1,000.00 = Rs 150.00
        assert expected_incremental_recovery(100_000, 1_500) == 15_000

    def test_it_rounds_half_away_from_zero(self) -> None:
        # 1 x 5000 / 10000 = 0.5 -> 1
        assert expected_incremental_recovery(1, 5_000) == 1
        assert expected_incremental_recovery(1, 4_999) == 0

    def test_a_negative_uplift_gives_a_negative_expectation(self) -> None:
        assert expected_incremental_recovery(100_000, -1_500) == -15_000

    def test_no_float_is_ever_formed(self) -> None:
        value = expected_incremental_recovery(447_880_605, 1_564)
        assert isinstance(value, int)
        assert not isinstance(value, bool)

    def test_a_negative_expected_recovery_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="must not be negative"):
            expected_incremental_recovery(-1, 1_000)

    def test_break_even_does_not_clear(self) -> None:
        """Strict. Spending exactly what you expect back is a coin flip at par."""
        assert not clears_cost(200, 200)
        assert clears_cost(201, 200)
        assert not clears_cost(199, 200)

    def test_the_cost_boundary_is_deterministic(self) -> None:
        """One minor unit either side of break-even flips the decision."""
        risk, experiment = unexplored_pair()
        # uplift 1000bps of 2000 = 200, exactly the unit cost -> refused
        at_par = gate(
            risk_id=risk,
            experiment_id=experiment,
            uplift=evidence(uplift_bps=1_000, low=900, high=1_100),
            expected_recovery=2_000,
        )
        assert at_par.reason is AbstainReason.NEGATIVE_NET_VALUE
        assert at_par.expected_incremental_recovery == 200

        # one minor unit more of recovery clears it
        above = gate(
            risk_id=risk,
            experiment_id=experiment,
            uplift=evidence(uplift_bps=1_000, low=900, high=1_100),
            expected_recovery=2_010,
        )
        assert above.decision is CaseDecision.ACT
        assert above.expected_incremental_recovery == 201

    def test_a_unit_cost_above_the_case_maximum_abstains(self) -> None:
        decision = gate(intervention=a_link(unit_cost=9_000), max_cost=5_000)
        assert decision.reason is AbstainReason.NEGATIVE_NET_VALUE
        assert "exceeds the case maximum" in decision.rationale

    def test_no_margin_or_ltv_concept_appears(self) -> None:
        """The check prices an action against its own cost and nothing else.

        Checked over *identifiers*, not raw text. The module docstring names
        these concepts in order to say they are deliberately absent, so a
        substring scan over the source would fail on the very sentence that
        documents the constraint.
        """
        import ast
        import pathlib

        banned = {
            "gross_margin",
            "take_rate",
            "lifetime_value",
            "ltv",
            "mdr",
            "commission",
            "margin_bps",
            "net_incremental_value",
        }
        tree = ast.parse(pathlib.Path("app/engine/policy_engine.py").read_text(encoding="utf-8"))
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                identifiers.add(node.name)

        offenders = {name for name in identifiers if name.lower() in banned}
        assert offenders == set(), offenders


# -- 4. exploration -------------------------------------------------------


class TestExploration:
    def test_the_salt_differs_from_the_assignment_salt(self) -> None:
        """Sharing one would make exploration a second view of the same draw."""
        from app.causal.cells import FOLD_SALT
        from app.core.config import Settings

        assignment_salt = Settings.model_fields["assignment_salt"].default
        assert EXPLORE_SALT != assignment_salt
        assert EXPLORE_SALT != FOLD_SALT
        assert len({EXPLORE_SALT, assignment_salt, FOLD_SALT}) == 3

    def test_the_draw_is_deterministic(self) -> None:
        first = explore_bucket_for(RISK, EXPERIMENT)
        second = explore_bucket_for(RISK, EXPERIMENT)
        assert first == second
        assert 0 <= first < BPS_SCALE

    def test_the_digest_is_recomputable_by_hand(self) -> None:
        import hashlib

        expected = hashlib.sha256(f"{RISK}:{EXPERIMENT}:{EXPLORE_SALT}".encode()).hexdigest()
        assert explore_digest(RISK, EXPERIMENT) == expected

    def test_a_different_experiment_gives_a_different_draw(self) -> None:
        other = uuid.UUID("33333333-3333-4333-8333-333333333333")
        assert explore_digest(RISK, EXPERIMENT) != explore_digest(RISK, other)

    def test_an_empty_salt_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="must not be empty"):
            explore_digest(RISK, EXPERIMENT, salt="   ")

    def test_a_budget_outside_the_scale_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="within 0"):
            is_explore_eligible(RISK, EXPERIMENT, budget_bps=-1)
        with pytest.raises(PolicyError, match="within 0"):
            is_explore_eligible(RISK, EXPERIMENT, budget_bps=BPS_SCALE + 1)

    def test_a_zero_budget_explores_nobody(self) -> None:
        risk, experiment = an_explored_pair()
        assert not is_explore_eligible(risk, experiment, budget_bps=0)
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=None, budget_bps=0)
        assert decision.reason is AbstainReason.INSUFFICIENT_SAMPLE

    def test_a_full_budget_explores_everybody(self) -> None:
        assert is_explore_eligible(RISK, EXPERIMENT, budget_bps=BPS_SCALE)

    def test_an_eligible_case_acts_despite_missing_evidence(self) -> None:
        risk, experiment = an_explored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=None)
        assert decision.decision is CaseDecision.ACT
        assert decision.explored
        assert "exploration budget" in decision.rationale

    def test_exploration_does_not_rescue_a_negative_uplift(self) -> None:
        """Exploring the undecidable is not the same as ignoring an answer."""
        risk, experiment = an_explored_pair()
        decision = gate(
            risk_id=risk,
            experiment_id=experiment,
            uplift=evidence(uplift_bps=-500, low=-900, high=-100),
        )
        assert decision.reason is AbstainReason.NEGATIVE_UPLIFT

    def test_the_budget_constant_is_a_module_constant_not_a_column(self) -> None:
        from app.models.experiment import Experiment

        assert isinstance(EXPLORATION_BUDGET_BPS, int)
        assert 0 <= EXPLORATION_BUDGET_BPS <= BPS_SCALE
        columns = {column.name for column in Experiment.__table__.columns}
        assert not any("explor" in name for name in columns)


# -- 5. eligibility -------------------------------------------------------


class TestEligibility:
    def test_an_opted_out_customer_abstains(self) -> None:
        decision = gate(customer_contactable=False)
        assert decision.reason is AbstainReason.CUSTOMER_OPTED_OUT

    def test_an_afa_intervention_without_consent_abstains(self) -> None:
        decision = gate(intervention=a_link(requires_afa=True))
        assert decision.reason is AbstainReason.REGULATORY_BLOCK

    def test_an_afa_intervention_with_consent_acts(self) -> None:
        risk, experiment = unexplored_pair()
        decision = gate(
            risk_id=risk,
            experiment_id=experiment,
            intervention=a_link(requires_afa=True),
            afa_consent=True,
        )
        assert decision.decision is CaseDecision.ACT

    def test_the_contact_cap_abstains_at_the_boundary(self) -> None:
        assert gate(contacts_this_month=2).decision is CaseDecision.ACT
        decision = gate(contacts_this_month=3)
        assert decision.reason is AbstainReason.CONTACT_BUDGET_EXHAUSTED

    def test_the_cooldown_abstains_inside_the_window(self) -> None:
        decision = gate(last_contacted_at=AS_OF - timedelta(hours=23))
        assert decision.reason is AbstainReason.CONTACT_BUDGET_EXHAUSTED
        assert "cooldown" in decision.rationale

    def test_the_cooldown_clears_at_the_boundary(self) -> None:
        decision = gate(last_contacted_at=AS_OF - timedelta(hours=24))
        assert decision.decision is CaseDecision.ACT

    def test_a_naive_last_contacted_at_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="timezone-aware"):
            gate(last_contacted_at=datetime(2026, 8, 29, 12, 0))

    def test_a_retired_intervention_is_a_caller_error_not_an_abstention(self) -> None:
        """Selecting a retired action is a bug; silently abstaining would hide it."""
        with pytest.raises(PolicyError, match="not active"):
            gate(intervention=a_link(is_active=False))


# -- purity and invariants ------------------------------------------------


class TestPurity:
    def test_as_of_must_be_timezone_aware(self) -> None:
        with pytest.raises(PolicyError, match="timezone-aware"):
            gate(as_of=datetime(2026, 8, 30, 12, 0))

    def test_the_module_never_reads_a_clock(self) -> None:
        import inspect

        from app.engine import policy_engine

        source = inspect.getsource(policy_engine)
        for banned in ("datetime.now(", "utcnow(", "date.today(", "time.time("):
            assert banned not in source, banned

    def test_the_module_draws_no_randomness(self) -> None:
        import inspect

        from app.engine import policy_engine

        source = inspect.getsource(policy_engine)
        for banned in ("import random", "secrets.", "uuid.uuid4("):
            assert banned not in source, banned

    def test_repeated_evaluation_is_identical(self) -> None:
        """Same inputs, same decision — the property that makes it auditable."""
        first = gate()
        second = gate()
        assert first == second
        assert first.numeric_snapshot() == second.numeric_snapshot()

    def test_it_imports_no_causal_or_reporting_module(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path("app/engine/policy_engine.py").read_text(encoding="utf-8")
        modules: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        assert not [m for m in modules if "causal" in m or "reporting" in m], modules

    def test_it_names_no_truth_column(self) -> None:
        import inspect

        from app.engine import policy_engine

        assert "truth_" not in inspect.getsource(policy_engine)


class TestDecisionInvariants:
    def test_an_abstention_always_carries_a_reason(self) -> None:
        """The invariant the two CHECK constraints enforce in the database."""
        cases = [
            gate(arm=Arm.HOLDOUT),
            gate(uplift=None),
            gate(uplift=evidence(qualified=False)),
            gate(uplift=evidence(uplift_bps=-1, low=-100, high=-1)),
            gate(uplift=evidence(quadrant=Quadrant.SLEEPING_DOG)),
            gate(expected_recovery=0),
            gate(customer_contactable=False),
            gate(intervention=a_link(requires_afa=True)),
            gate(contacts_this_month=99),
        ]
        for decision in cases:
            if decision.decision is CaseDecision.ABSTAIN:
                assert decision.reason is not None, decision.rationale
                assert decision.rationale.strip()

    def test_an_action_never_carries_a_reason(self) -> None:
        assert gate().reason is None

    def test_constructing_a_reasonless_abstention_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="must carry a reason"):
            GateDecision(
                risk_id=RISK,
                experiment_id=EXPERIMENT,
                decision=CaseDecision.ABSTAIN,
                reason=None,
                rationale="",
                expected_incremental_recovery=0,
                unit_cost=0,
                explore_bucket=0,
                explored=False,
            )

    def test_constructing_an_action_with_a_reason_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="must not carry an abstain reason"):
            GateDecision(
                risk_id=RISK,
                experiment_id=EXPERIMENT,
                decision=CaseDecision.ACT,
                reason=AbstainReason.SURE_THING,
                rationale="",
                expected_incremental_recovery=0,
                unit_cost=0,
                explore_bucket=0,
                explored=False,
            )

    def test_the_snapshot_is_json_safe_and_exact(self) -> None:
        import json

        snapshot = gate().numeric_snapshot()
        assert json.loads(json.dumps(snapshot)) == snapshot
        for key, value in snapshot.items():
            assert not isinstance(value, float), key

    def test_every_reason_used_exists_in_the_stored_vocabulary(self) -> None:
        for reason in AbstainReason:
            assert reason.value in AbstainReason.values()


# -- the Gray Zone never yields an ordinary action (DR-4) ------------------


def a_gray_zone_self_recovery() -> UpliftEvidence:
    """A real, significant lift — the half that used to act as if Persuadable.

    `ci_low > 0` and the interval excludes zero, which is what made this
    population invisible to the old interval-based branch. The label is what
    carries "the control arm recovers at or above the self-recovery ceiling";
    the gate reads the label rather than recomputing the ceiling.
    """
    return evidence(uplift_bps=1_500, low=1_000, high=2_000, quadrant=Quadrant.GRAY_ZONE)


def a_gray_zone_null_effect() -> UpliftEvidence:
    return evidence(uplift_bps=50, low=-200, high=300, quadrant=Quadrant.GRAY_ZONE)


class TestGrayZoneNeverActsOrdinarily:
    def test_a_self_recovery_unit_abstains_with_its_own_reason(self) -> None:
        risk, experiment = unexplored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=a_gray_zone_self_recovery())
        assert decision.decision is CaseDecision.ABSTAIN
        assert decision.reason is AbstainReason.SELF_RECOVERY_LIKELY
        assert decision.explored is False

    def test_the_reason_is_not_one_of_the_false_alternatives(self) -> None:
        """Each of these would be a false statement about this unit."""
        risk, experiment = unexplored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=a_gray_zone_self_recovery())
        assert decision.reason not in {
            AbstainReason.UPLIFT_NOT_SIGNIFICANT,  # the interval excludes zero
            AbstainReason.INSUFFICIENT_SAMPLE,  # the cell qualified
            AbstainReason.NEGATIVE_NET_VALUE,  # cost recovery passes
            AbstainReason.SURE_THING,  # a label this unit does not carry
        }

    def test_a_null_effect_unit_keeps_its_existing_reason(self) -> None:
        """DR-4 must not relabel the population it did not set out to change."""
        risk, experiment = unexplored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=a_gray_zone_null_effect())
        assert decision.reason is AbstainReason.UPLIFT_NOT_SIGNIFICANT

    @pytest.mark.parametrize(
        "uplift", [a_gray_zone_self_recovery(), a_gray_zone_null_effect()], ids=["lift", "null"]
    )
    def test_an_explorable_unit_acts_but_is_marked_explored(self, uplift: UpliftEvidence) -> None:
        """Acting is still permitted — but as exploration, and it says so."""
        risk, experiment = an_explored_pair()
        decision = gate(risk_id=risk, experiment_id=experiment, uplift=uplift)
        assert decision.decision is CaseDecision.ACT
        assert decision.explored is True
        assert decision.reason is None

    def test_no_gray_zone_input_produces_an_unexplored_action(self) -> None:
        """The property the whole change exists to establish.

        Swept over both halves and both exploration outcomes rather than
        asserted on one example, because the defect being fixed was precisely
        an input shape nobody thought to try.
        """
        for uplift in (a_gray_zone_self_recovery(), a_gray_zone_null_effect()):
            for pair in (unexplored_pair(), an_explored_pair()):
                decision = gate(risk_id=pair[0], experiment_id=pair[1], uplift=uplift)
                acted_ordinarily = decision.decision is CaseDecision.ACT and not decision.explored
                assert not acted_ordinarily, (uplift, pair)

    def test_negative_uplift_still_outranks_the_gray_zone_branch(self) -> None:
        """Precedence is preserved: a negative estimate is named as one."""
        decision = gate(
            uplift=evidence(uplift_bps=-100, low=-500, high=500, quadrant=Quadrant.GRAY_ZONE)
        )
        assert decision.reason is AbstainReason.NEGATIVE_UPLIFT

    def test_null_only_still_governs_the_null_half_alone(self) -> None:
        """NULL_ONLY suppresses null-effect exploration and nothing else."""
        risk, experiment = an_explored_pair()
        null = gate(
            risk_id=risk,
            experiment_id=experiment,
            uplift=a_gray_zone_null_effect(),
            gray_zone_policy=GrayZonePolicy.NULL_ONLY,
        )
        assert null.decision is CaseDecision.ABSTAIN
        assert null.reason is AbstainReason.UPLIFT_NOT_SIGNIFICANT

        lift = gate(
            risk_id=risk,
            experiment_id=experiment,
            uplift=a_gray_zone_self_recovery(),
            gray_zone_policy=GrayZonePolicy.NULL_ONLY,
        )
        assert lift.decision is CaseDecision.ACT
        assert lift.explored is True

    @pytest.mark.parametrize(
        "quadrant",
        [Quadrant.PERSUADABLE, Quadrant.SLEEPING_DOG, Quadrant.SURE_THING, Quadrant.LOST_CAUSE],
    )
    def test_no_other_quadrant_reaches_the_gray_zone_branch(self, quadrant: Quadrant) -> None:
        """Neither reason the new branch can produce may escape it."""
        decision = gate(uplift=evidence(quadrant=quadrant))
        assert decision.reason is not AbstainReason.SELF_RECOVERY_LIKELY
