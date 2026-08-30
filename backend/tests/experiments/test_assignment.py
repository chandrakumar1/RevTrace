"""Deterministic stratified randomisation.

The pure half — bucketing, strata, arms, exclusions — is tested without a
database. The storage half is tested against `revtrace_test` in
`tests/integration/test_day3_assignment.py`.

The properties that matter, in order of how badly a bug would hurt:

1. **Idempotent.** The same risk always lands in the same arm.
2. **Independent of the outcome.** Nothing about what happened to a risk can
   influence its arm.
3. **Roughly the pre-registered split**, and unbiased across strata.
4. **Auditable.** The digest is stored so anyone can recompute the draw.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime

import pytest

from app.experiments.assignment import (
    AMOUNT_BANDS,
    BPS_SCALE,
    EXCLUDED_RISK_TYPES,
    TOP_AMOUNT_BAND,
    AssignmentError,
    amount_band,
    arm_for_bucket,
    assignment_digest,
    bucket_for,
    decide,
    stratum_key,
)
from app.models.enums import Arm, RiskType

EXPERIMENT_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SALT = "revtrace-demo-salt-v1"
HOLDOUT_BPS = 5_000
_AS_OF = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def a_risk_id(index: int) -> uuid.UUID:
    return uuid.uuid5(uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002"), str(index))


def decide_for(index: int, **overrides: object) -> object:
    fields: dict[str, object] = {
        "risk_type": RiskType.REPEATED_PAYMENT_FAILURE.value,
        "amount_minor": 230_400,
        "holdout_bps": HOLDOUT_BPS,
        "salt": SALT,
    }
    fields.update(overrides)
    return decide(a_risk_id(index), EXPERIMENT_ID, **fields)  # type: ignore[arg-type]


class TestAmountBands:
    def test_the_pre_registered_boundaries(self) -> None:
        assert amount_band(0) == "<500"
        assert amount_band(49_999) == "<500"
        assert amount_band(50_000) == "500-2000"
        assert amount_band(199_999) == "500-2000"
        assert amount_band(200_000) == "2000-5000"
        assert amount_band(499_999) == "2000-5000"
        assert amount_band(500_000) == "5000-15000"
        assert amount_band(1_499_999) == "5000-15000"

    def test_the_afa_threshold_opens_the_top_band(self) -> None:
        """15,00,000 paise is the RBI additional-factor-authentication
        threshold for recurring debits, so it must start a band."""
        assert amount_band(1_500_000) == TOP_AMOUNT_BAND
        assert amount_band(50_000_000) == TOP_AMOUNT_BAND

    def test_every_band_is_reachable(self) -> None:
        produced = {amount_band(a) for a in (0, 60_000, 300_000, 900_000, 2_000_000)}
        assert produced == {label for label, _ in AMOUNT_BANDS} | {TOP_AMOUNT_BAND}

    def test_bands_are_contiguous_and_ordered(self) -> None:
        thresholds = [upper for _, upper in AMOUNT_BANDS]
        assert thresholds == sorted(thresholds)
        for upper in thresholds:
            assert amount_band(upper - 1) != amount_band(upper)

    def test_a_float_amount_is_rejected(self) -> None:
        with pytest.raises(AssignmentError, match="integer"):
            amount_band(230_400.0)  # type: ignore[arg-type]

    def test_a_bool_is_not_an_amount(self) -> None:
        with pytest.raises(AssignmentError, match="integer"):
            amount_band(True)  # type: ignore[arg-type]

    def test_a_negative_amount_is_rejected(self) -> None:
        with pytest.raises(AssignmentError, match="non-negative"):
            amount_band(-1)


class TestStratumKey:
    def test_it_is_risk_type_and_amount_band_only(self) -> None:
        assert stratum_key(RiskType.REPEATED_PAYMENT_FAILURE.value, 230_400) == (
            "repeated_payment_failure|2000-5000"
        )

    def test_it_carries_no_unavailable_covariate(self) -> None:
        """`issuer` and `customer_tier` exist nowhere in the schema, and
        `payment_method` is absent for two of the four risk types."""
        key = stratum_key(RiskType.CHECKOUT_ABANDONMENT.value, 60_000)
        assert key.count("|") == 1
        for absent in ("issuer", "tier", "card", "upi", "netbanking"):
            assert absent not in key

    def test_it_is_computable_for_every_detectable_risk_type(self) -> None:
        """The reason the key is these two fields: nothing else is universal."""
        for risk_type in RiskType.values():
            assert stratum_key(risk_type, 100_000)

    def test_an_unknown_risk_type_is_rejected(self) -> None:
        with pytest.raises(AssignmentError, match="unknown risk_type"):
            stratum_key("vibes", 100_000)

    def test_the_same_inputs_give_the_same_key(self) -> None:
        assert stratum_key("checkout_abandonment", 1_000) == stratum_key(
            "checkout_abandonment", 1_000
        )


class TestDigestAndBucket:
    def test_the_digest_is_reproducible(self) -> None:
        first = assignment_digest(a_risk_id(1), EXPERIMENT_ID, SALT)
        second = assignment_digest(a_risk_id(1), EXPERIMENT_ID, SALT)
        assert first == second
        assert len(first) == 64

    def test_a_different_risk_gives_a_different_digest(self) -> None:
        assert assignment_digest(a_risk_id(1), EXPERIMENT_ID, SALT) != assignment_digest(
            a_risk_id(2), EXPERIMENT_ID, SALT
        )

    def test_a_different_experiment_gives_a_different_digest(self) -> None:
        other = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000009")
        assert assignment_digest(a_risk_id(1), EXPERIMENT_ID, SALT) != assignment_digest(
            a_risk_id(1), other, SALT
        )

    def test_a_different_salt_re_randomises(self) -> None:
        """Which is exactly why changing the salt is a breaking reassignment."""
        assert assignment_digest(a_risk_id(1), EXPERIMENT_ID, SALT) != assignment_digest(
            a_risk_id(1), EXPERIMENT_ID, "some-other-salt"
        )

    def test_an_empty_salt_is_rejected(self) -> None:
        for bad in ("", "   "):
            with pytest.raises(AssignmentError, match="salt"):
                assignment_digest(a_risk_id(1), EXPERIMENT_ID, bad)

    def test_buckets_stay_in_range(self) -> None:
        for index in range(500):
            bucket = bucket_for(assignment_digest(a_risk_id(index), EXPERIMENT_ID, SALT))
            assert 0 <= bucket < BPS_SCALE

    def test_the_bucket_is_an_integer(self) -> None:
        bucket = bucket_for(assignment_digest(a_risk_id(1), EXPERIMENT_ID, SALT))
        assert isinstance(bucket, int) and not isinstance(bucket, bool)


class TestArmBoundaries:
    def test_below_the_threshold_is_holdout(self) -> None:
        assert arm_for_bucket(0, 5_000) is Arm.HOLDOUT
        assert arm_for_bucket(4_999, 5_000) is Arm.HOLDOUT

    def test_at_and_above_the_threshold_is_treatment(self) -> None:
        assert arm_for_bucket(5_000, 5_000) is Arm.TREATMENT
        assert arm_for_bucket(9_999, 5_000) is Arm.TREATMENT

    def test_a_tiny_holdout_still_assigns_treatment(self) -> None:
        assert arm_for_bucket(500, 500) is Arm.TREATMENT
        assert arm_for_bucket(499, 500) is Arm.HOLDOUT

    def test_an_out_of_range_bucket_is_rejected(self) -> None:
        for bad in (-1, BPS_SCALE, BPS_SCALE + 1):
            with pytest.raises(AssignmentError, match="outside"):
                arm_for_bucket(bad, 5_000)


class TestDecide:
    def test_the_same_risk_always_lands_in_the_same_arm(self) -> None:
        """Property 1: idempotence. Re-running detection must not re-roll."""
        for index in range(200):
            assert decide_for(index).arm is decide_for(index).arm  # type: ignore[attr-defined]

    def test_the_decision_is_fully_reproducible(self) -> None:
        assert decide_for(7) == decide_for(7)

    def test_a_degenerate_holdout_is_rejected(self) -> None:
        for bad in (0, BPS_SCALE, -1):
            with pytest.raises(AssignmentError, match="holdout_bps"):
                decide_for(1, holdout_bps=bad)

    def test_the_arm_does_not_depend_on_the_amount(self) -> None:
        """Property 2: nothing about the case may steer the arm. The amount
        changes the stratum, never the draw."""
        base = decide_for(3, amount_minor=230_400)
        richer = decide_for(3, amount_minor=4_000_000)
        assert base.arm is richer.arm  # type: ignore[attr-defined]
        assert base.stratum_key != richer.stratum_key  # type: ignore[attr-defined]

    def test_the_arm_does_not_depend_on_the_risk_type(self) -> None:
        base = decide_for(4, risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value)
        other = decide_for(4, risk_type=RiskType.CHECKOUT_ABANDONMENT.value)
        assert base.arm is other.arm  # type: ignore[attr-defined]

    def test_the_digest_is_carried_for_audit(self) -> None:
        decision = decide_for(5)
        assert decision.assignment_hash == assignment_digest(  # type: ignore[attr-defined]
            a_risk_id(5), EXPERIMENT_ID, SALT
        )
        assert decision.bucket == bucket_for(decision.assignment_hash)  # type: ignore[attr-defined]

    def test_is_holdout_agrees_with_the_arm(self) -> None:
        for index in range(50):
            decision = decide_for(index)
            assert decision.is_holdout == (decision.arm is Arm.HOLDOUT)  # type: ignore[attr-defined]


class TestAllocation:
    """Property 3: the split is roughly what was pre-registered."""

    def _arms(self, n: int, holdout_bps: int) -> Counter[str]:
        return Counter(
            decide_for(i, holdout_bps=holdout_bps).arm.value  # type: ignore[attr-defined]
            for i in range(n)
        )

    def test_a_fifty_fifty_split_lands_near_half(self) -> None:
        arms = self._arms(4_000, 5_000)
        assert 1_850 <= arms["holdout"] <= 2_150, arms

    def test_a_ten_percent_holdout_lands_near_a_tenth(self) -> None:
        arms = self._arms(4_000, 1_000)
        assert 330 <= arms["holdout"] <= 470, arms

    def test_both_arms_are_always_populated(self) -> None:
        for holdout_bps in (500, 2_500, 5_000, 7_500, 9_500):
            arms = self._arms(2_000, holdout_bps)
            assert arms["holdout"] > 0 and arms["treatment"] > 0, holdout_bps

    def test_the_split_is_unbiased_within_each_stratum(self) -> None:
        """A stratum that skewed would mean the covariate leaked into the draw."""
        for amount in (10_000, 100_000, 300_000, 900_000, 3_000_000):
            arms = Counter(
                decide_for(i, amount_minor=amount).arm.value  # type: ignore[attr-defined]
                for i in range(2_000)
            )
            assert 880 <= arms["holdout"] <= 1_120, (amount, arms)

    def test_the_split_is_unbiased_within_each_risk_type(self) -> None:
        for risk_type in (
            RiskType.REPEATED_PAYMENT_FAILURE.value,
            RiskType.CHECKOUT_ABANDONMENT.value,
            RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
        ):
            arms = Counter(
                decide_for(i, risk_type=risk_type).arm.value  # type: ignore[attr-defined]
                for i in range(2_000)
            )
            assert 880 <= arms["holdout"] <= 1_120, (risk_type, arms)


class TestExclusions:
    def test_reconciliation_mismatch_is_excluded(self) -> None:
        """Zero amount at risk (ADR 0007) — nothing to recover, and including
        it would drag the effect estimate toward zero."""
        assert RiskType.RECONCILIATION_MISMATCH.value in EXCLUDED_RISK_TYPES

    def test_payment_degradation_is_excluded(self) -> None:
        """No detector produces it; listed so adding one forces a decision."""
        assert RiskType.PAYMENT_DEGRADATION.value in EXCLUDED_RISK_TYPES

    def test_the_three_measurable_risk_types_are_included(self) -> None:
        included = set(RiskType.values()) - EXCLUDED_RISK_TYPES
        assert included == {
            RiskType.REPEATED_PAYMENT_FAILURE.value,
            RiskType.CHECKOUT_ABANDONMENT.value,
            RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
        }

    def test_every_excluded_type_is_a_real_risk_type(self) -> None:
        assert EXCLUDED_RISK_TYPES <= set(RiskType.values())


class TestPurity:
    """Assignment computes; it does not act."""

    @staticmethod
    def _identifiers() -> set[str]:
        import ast
        import inspect
        import pathlib

        from app.experiments import assignment as module

        tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                found.add(node.name)
        return found

    def test_it_reads_no_clock(self) -> None:
        for name in ("now", "utcnow", "today"):
            assert name not in self._identifiers()

    def test_it_draws_no_randomness(self) -> None:
        """A random draw would be neither reproducible nor idempotent."""
        import ast
        import inspect
        import pathlib

        from app.experiments import assignment as module

        source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        assert "random" not in modules
        assert "secrets" not in modules
        assert "uuid4" not in self._identifiers()

    def test_it_touches_no_recovery_or_policy_concept(self) -> None:
        """Checked against authority *concepts*, not the word "execute" — that
        also names `session.execute(SELECT ...)`, which is a read."""
        identifiers = self._identifiers()
        for banned in (
            "RecoveryCase",
            "RecoveryAction",
            "AuditEvent",
            "approve",
            "approved",
            "policy_status",
            "execute_action",
            "recommend",
        ):
            assert banned not in identifiers, banned

    def test_it_writes_only_assignments(self) -> None:
        """The only ORM models it may touch are the assignment it creates and
        the risk it reads."""
        import ast
        import inspect
        import pathlib

        from app.experiments import assignment as module

        source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                imported.update(alias.name for alias in node.names)
        assert imported == {"CaseAssignment", "RevenueRisk"}

    def test_it_builds_an_audit_payload_without_writing_one(self) -> None:
        """`audit_entry_for` returns a dict. Persisting it is the caller's
        decision, so assignment cannot quietly become an audit writer."""
        from app.experiments.assignment import audit_entry_for

        entry = audit_entry_for(decide_for(1), _AS_OF)  # type: ignore[arg-type]
        assert isinstance(entry, dict)
        assert entry["decision_type"] == "assign"
        assert entry["is_execution"] is False
        # Anchored by the risk, because no recovery case exists yet — and a
        # holdout risk never gets one.
        assert "risk_id" in entry
        assert "case_id" not in entry

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_")

    def test_the_pure_decision_needs_no_database(self) -> None:
        """`decide()` is importable and callable with no session at all."""
        assert decide_for(1).arm in (Arm.TREATMENT, Arm.HOLDOUT)  # type: ignore[attr-defined]


class TestSaltConfiguration:
    def test_the_default_salt_is_stable_and_documented(self) -> None:
        """Never generated at runtime: a fresh salt each boot would silently
        re-randomise every assignment."""
        from app.core.config import Settings

        default = Settings.model_fields["assignment_salt"].default
        assert isinstance(default, str)
        assert default.strip()
        assert default == SALT

    def test_an_empty_salt_is_rejected_by_configuration(self) -> None:
        import pydantic

        from app.core.config import Settings

        with pytest.raises(pydantic.ValidationError):
            Settings(
                database_url="postgresql+psycopg://u@localhost:5432/x",
                assignment_salt="",
            )

    def test_the_salt_is_not_a_secret(self) -> None:
        """It decorrelates the hash; it does not protect anything. Marking it
        secret would imply a confidentiality property it does not have."""
        from pydantic import SecretStr

        from app.core.config import Settings

        assert Settings.model_fields["assignment_salt"].annotation is not SecretStr
