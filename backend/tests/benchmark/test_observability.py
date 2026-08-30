"""Are the planted strata distinguishable from what the database actually holds?

The Day 5 acceptance criterion asks for stratum 1 to be classified Sure Thing
and stratum 6 Sleeping Dog. No model can do that unless the two differ in the
columns a model is allowed to read, and until this gate they did not: the bridge
wrote a constant failure code and stamped `detected_at` from a sequential
counter, so the only stratum-bearing column was `payment_method` — on which
strata 1 and 6 are *identical*, both preferring UPI.

This file proves the repair. It groups by `truth_segment`, which a test may do
and a model may not, and measures how far apart the observable distributions
are. The separation has to be real and it has to be incomplete: a code that
identified a stratum outright would turn the Day 5 result into a lookup.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from collections import Counter

import pytest
from simulator.potential_outcomes import generate
from simulator.segments import SEGMENTS_BY_ID, SegmentId
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseOutcome, Customer, Order, PaymentAttempt, RevenueRisk
from app.models.enums import PaymentStatus
from tests.benchmark.bridge import (
    CHARACTERISTIC_CODE_PERCENT,
    FAILURE_CODES,
    detection_instant,
    materialise,
    observed_failure_code,
)

pytestmark = pytest.mark.db

#: Large enough that per-stratum shares are stable to a few percentage points.
SIZE = 3_000

SURE_THING = SegmentId.TRANSIENT_UPI_TIMEOUT
SLEEPING_DOG = SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER


def observable_rows(session: Session, merchant_id) -> list[dict]:  # noqa: ANN001
    """Everything a model is allowed to see, plus the label for the test only.

    The label comes back on a separate key and is used exclusively to *group*
    rows. No assertion here treats it as a feature, and Day 5's model will not
    receive it at all.
    """
    statement = (
        select(RevenueRisk, PaymentAttempt, CaseOutcome, Customer)
        .join(Order, Order.id == RevenueRisk.order_id)
        .join(PaymentAttempt, PaymentAttempt.order_id == Order.id)
        .join(CaseOutcome, CaseOutcome.risk_id == RevenueRisk.id)
        .join(Customer, Customer.id == RevenueRisk.customer_id)
        .where(
            RevenueRisk.merchant_id == merchant_id,
            PaymentAttempt.status == PaymentStatus.FAILED.value,
            PaymentAttempt.attempt_number == 1,
        )
    )
    return [
        {
            "risk_id": risk.id,
            "failure_code": attempt.failure_code,
            "payment_method": attempt.payment_method,
            "amount_at_risk": risk.amount_at_risk,
            "hour_of_day": risk.detected_at.hour,
            "day_of_month": risk.detected_at.day,
            "tenure_days": (risk.detected_at - customer.created_at).days,
            "_label": outcome.truth_segment,
        }
        for risk, attempt, outcome, customer in session.execute(statement).all()
    ]


def share_bps(counter: Counter, key: str) -> int:
    total = sum(counter.values())
    return 0 if total == 0 else counter[key] * 10_000 // total


@pytest.fixture(scope="module")
def rows(request):  # noqa: ANN001, ANN201
    """One materialisation shared across this module's read-only assertions."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tests.conftest_db import resolve_test_dsn

    engine = create_engine(resolve_test_dsn(), future=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    try:
        run = materialise(session, case_count=SIZE)
        yield observable_rows(session, run.merchant_id)
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


def codes_for(rows: list[dict], segment: SegmentId) -> Counter:
    return Counter(row["failure_code"] for row in rows if row["_label"] == segment.value)


def methods_for(rows: list[dict], segment: SegmentId) -> Counter:
    return Counter(row["payment_method"] for row in rows if row["_label"] == segment.value)


class TestTheProblemThatMotivatedThis:
    def test_the_two_strata_prefer_the_same_payment_method(self) -> None:
        """The reason the criterion was unreachable: on the only stratum-bearing
        column the database used to hold, these two are the same population."""
        assert SEGMENTS_BY_ID[SURE_THING].preferred_method == "upi"
        assert SEGMENTS_BY_ID[SLEEPING_DOG].preferred_method == "upi"

    def test_payment_method_alone_still_cannot_tell_them_apart(self, rows) -> None:  # noqa: ANN001
        """Unchanged by this gate, and stated so the fix is not credited to the
        wrong column. Both sit at roughly 80% UPI."""
        sure = share_bps(methods_for(rows, SURE_THING), "upi")
        dog = share_bps(methods_for(rows, SLEEPING_DOG), "upi")
        assert abs(sure - dog) < 700, f"upi share {sure} vs {dog} bps"

    def test_amount_alone_cannot_tell_them_apart(self, rows) -> None:
        """Only the high-value stratum overrides the amount draw; these two
        share it."""

        def mean_amount(segment: SegmentId) -> int:
            values = [r["amount_at_risk"] for r in rows if r["_label"] == segment.value]
            return sum(values) // len(values)

        sure, dog = mean_amount(SURE_THING), mean_amount(SLEEPING_DOG)
        assert abs(sure - dog) * 10 < max(sure, dog), f"means {sure} vs {dog}"


class TestFailureCodeSeparatesThem:
    def test_each_stratum_shows_its_characteristic_code_most_often(self, rows) -> None:
        for segment in (SURE_THING, SLEEPING_DOG):
            counter = codes_for(rows, segment)
            expected = SEGMENTS_BY_ID[segment].failure_code
            assert counter.most_common(1)[0][0] == expected, segment.value

    def test_the_characteristic_share_is_near_the_configured_rate(self, rows) -> None:
        """70% characteristic plus a 1-in-5 chance from the noise draw is about
        76%. Anything near 100% would mean the noise is not working."""
        for segment in (SURE_THING, SLEEPING_DOG):
            counter = codes_for(rows, segment)
            share = share_bps(counter, SEGMENTS_BY_ID[segment].failure_code)
            assert 7_000 <= share <= 8_400, f"{segment.value}: {share} bps"

    def test_the_two_distributions_are_far_apart(self, rows) -> None:
        """Total variation distance. Identical distributions score 0; disjoint
        ones score 10000. Anything above ~5000 is a usable signal."""
        sure, dog = codes_for(rows, SURE_THING), codes_for(rows, SLEEPING_DOG)
        distance = (
            sum(abs(share_bps(sure, code) - share_bps(dog, code)) for code in FAILURE_CODES) // 2
        )
        assert distance > 5_000, f"total variation {distance} bps"

    def test_each_stratum_is_rare_under_the_other_code(self, rows) -> None:
        sure, dog = codes_for(rows, SURE_THING), codes_for(rows, SLEEPING_DOG)
        assert share_bps(sure, "mandate_inactive") < 1_500
        assert share_bps(dog, "gateway_timeout") < 1_500

    def test_a_cell_rate_classifier_would_separate_them(self, rows) -> None:
        """The concrete Day 5 mechanism: group by failure code, and check the
        two strata land in different cells. `gateway_timeout` must be dominated
        by stratum 1 and `mandate_inactive` by stratum 6."""

        def dominant_label(code: str) -> str:
            labels = Counter(r["_label"] for r in rows if r["failure_code"] == code)
            return labels.most_common(1)[0][0]

        assert dominant_label("gateway_timeout") == SURE_THING.value
        assert dominant_label("mandate_inactive") == SLEEPING_DOG.value


class TestTheSignalIsIncomplete:
    def test_no_code_identifies_a_single_stratum(self, rows) -> None:
        """If a code mapped to exactly one stratum, Day 5 would be a lookup
        rather than a finding."""
        for code in FAILURE_CODES:
            labels = {r["_label"] for r in rows if r["failure_code"] == code}
            assert len(labels) > 1, f"{code} identifies exactly {labels}"

    def test_no_stratum_shows_only_one_code(self, rows) -> None:
        labels = {row["_label"] for row in rows}
        for label in labels:
            codes = {r["failure_code"] for r in rows if r["_label"] == label}
            assert len(codes) > 1, f"{label} shows only {codes}"

    def test_two_strata_share_a_code(self, rows) -> None:
        """`card_declined` is characteristic of both the blocked-card and the
        churner strata, so the code cannot be a bijection even in principle."""
        assert SEGMENTS_BY_ID[SegmentId.EXPIRED_OR_BLOCKED_CARD].failure_code == "card_declined"
        assert SEGMENTS_BY_ID[SegmentId.INTENTIONAL_CHURNER].failure_code == "card_declined"

    def test_the_configured_rate_is_not_certainty(self) -> None:
        assert CHARACTERISTIC_CODE_PERCENT == 70
        assert len(FAILURE_CODES) == 5


class TestTheCodeDerivationIsPure:
    def test_it_is_deterministic(self) -> None:
        for case in generate(seed=42, case_count=200).cases:
            assert observed_failure_code(case) == observed_failure_code(case)

    def test_it_always_returns_a_known_code(self) -> None:
        for case in generate(seed=42, case_count=500).cases:
            assert observed_failure_code(case) in FAILURE_CODES

    def test_it_does_not_correlate_with_the_capture_instant(self) -> None:
        """The two read disjoint slices of the case id, so a case showing its
        characteristic code is no more or less likely to recover early."""
        from tests.benchmark import bridge

        source = pathlib.Path(inspect.getfile(bridge)).read_text(encoding="utf-8")
        assert "hex[:8]" in source  # capture_instant
        assert "hex[8:12]" in source  # the characteristic draw
        assert "hex[12:16]" in source  # the noise draw

    def test_the_noise_reaches_every_code(self) -> None:
        """A stratum must be able to show any code, or the vocabulary would be
        partitioned and the signal would be perfect after all."""
        cases = generate(seed=42, case_count=4_000).cases
        observed = {
            observed_failure_code(case) for case in cases if case.segment_id is SLEEPING_DOG
        }
        assert observed == set(FAILURE_CODES)


class TestTheClockFeaturesSurvive:
    def test_the_hour_matches_the_generated_covariate(self) -> None:
        for index, case in enumerate(generate(seed=42, case_count=200).cases):
            assert detection_instant(case, index).hour == case.covariates.hour_of_day

    def test_the_day_matches_the_generated_covariate(self) -> None:
        for index, case in enumerate(generate(seed=42, case_count=200).cases):
            assert detection_instant(case, index).day == case.covariates.day_of_month

    def test_both_are_recoverable_from_the_stored_column(self, rows) -> None:
        assert len({row["hour_of_day"] for row in rows}) >= 20
        assert len({row["day_of_month"] for row in rows}) >= 25

    def test_the_hour_is_no_longer_a_function_of_row_order(self, rows) -> None:
        """Before this gate `detected_at` was `epoch + index * 60s`, so the hour
        described position in the file rather than the case."""
        hours = [row["hour_of_day"] for row in rows]
        assert hours != sorted(hours)

    def test_detection_is_deterministic(self) -> None:
        cases = generate(seed=42, case_count=50).cases
        first = [detection_instant(case, index) for index, case in enumerate(cases)]
        second = [detection_instant(case, index) for index, case in enumerate(cases)]
        assert first == second


class TestCustomerTenureSurvives:
    def test_tenure_is_recoverable_and_in_range(self, rows) -> None:
        tenures = [row["tenure_days"] for row in rows]
        assert min(tenures) >= 0
        assert max(tenures) <= 1_460
        assert len(set(tenures)) > 100

    def test_lifetime_value_is_a_placeholder_not_a_measurement(self, rows) -> None:
        """The column is NOT NULL and the generator models no such quantity, so
        it is written as zero — and must never be used as a feature."""
        from tests.benchmark import bridge

        source = pathlib.Path(inspect.getfile(bridge)).read_text(encoding="utf-8")
        assert "lifetime_value=0" in source

    def test_no_row_exposes_lifetime_value_as_a_feature(self, rows) -> None:
        assert "lifetime_value" not in rows[0]


class TestNoTruthLeaksIntoTheFeatures:
    def test_the_observable_row_carries_no_potential_outcome(self, rows) -> None:
        forbidden = {"truth_y0", "truth_y1", "truth_harm_0", "truth_harm_1", "y0", "y1"}
        assert not (forbidden & set(rows[0]))

    def test_the_label_is_present_only_under_a_reserved_key(self, rows) -> None:
        """Prefixed so it cannot be mistaken for a feature by a loop over keys."""
        assert "_label" in rows[0]
        assert [key for key in rows[0] if key.startswith("_")] == ["_label"]

    def test_the_features_are_exactly_what_the_database_holds(self, rows) -> None:
        assert set(rows[0]) - {"_label", "risk_id"} == {
            "failure_code",
            "payment_method",
            "amount_at_risk",
            "hour_of_day",
            "day_of_month",
            "tenure_days",
        }

    def test_the_bridge_never_writes_a_stratum_label_to_an_observable_column(self) -> None:
        """`truth_segment` is written once, to the truth column, and nowhere
        else. A stratum label in an observable column would be the leak this
        whole arrangement exists to prevent."""
        from tests.benchmark import bridge

        tree = ast.parse(pathlib.Path(inspect.getfile(bridge)).read_text(encoding="utf-8"))
        targets: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            reads = {
                inner.attr if isinstance(inner, ast.Attribute) else inner.id
                for inner in ast.walk(node.value)
                if isinstance(inner, ast.Attribute | ast.Name)
            }
            if "segment_id" not in reads:
                continue
            targets.extend(
                target.attr for target in node.targets if isinstance(target, ast.Attribute)
            )

        assert targets == ["truth_segment"], targets
