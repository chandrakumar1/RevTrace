"""The verification seam and the webhook route, against PostgreSQL.

Every test rolls back. What is being checked is the behaviour the webhook
contract demands — idempotency, order-independence, delayed delivery — and that
none of it is implemented here: it is carried by the unique constraint, by
`occurred_at`, and by the state-machine rank table.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.integrations.razorpay.webhooks import SIGNATURE_HEADER, sign
from app.main import app
from app.models.audit_event import AuditEvent
from app.models.customer import Customer
from app.models.enums import PaymentMethod, PaymentStatus
from app.models.event import Event
from app.models.merchant import Merchant
from app.models.order import Order
from app.models.payment_attempt import PaymentAttempt
from app.services.verification.service import (
    STRIPPED_KEYS,
    VerificationError,
    advance_status,
    apply_webhook,
    external_event_id_of,
    occurred_at_of,
    resolve_merchant,
)

SECRET = "synthetic-webhook-secret-not-real"
PAYMENT_ID = "pay_TESTONLY01"
AUTHORIZED_AT = 1_767_225_600
CAPTURED_AT = AUTHORIZED_AT + 60
RECEIVED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def a_webhook(event: str, *, payment_id: str = PAYMENT_ID, created_at: int = CAPTURED_AT) -> dict:
    return {
        "event": event,
        "created_at": created_at,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 50_000,
                    "currency": "INR",
                    "created_at": created_at,
                    # Must never be stored.
                    "card": {"last4": "1111", "network": "Visa"},
                    "notes": {"free": "text"},
                }
            }
        },
    }


@pytest.fixture
def merchant(db_session: Session) -> Merchant:
    row = Merchant(name="Webhook Test", currency="INR", timezone="Asia/Kolkata")
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def attempt(db_session: Session, merchant: Merchant) -> PaymentAttempt:
    customer = Customer(
        merchant_id=merchant.id,
        external_customer_id=f"cust_{uuid.uuid4().hex[:8]}",
        lifetime_value=0,
    )
    db_session.add(customer)
    db_session.flush()
    order = Order(
        merchant_id=merchant.id,
        customer_id=customer.id,
        external_order_id=f"order_{uuid.uuid4().hex[:8]}",
        amount=50_000,
        currency="INR",
        status="created",
    )
    db_session.add(order)
    db_session.flush()
    row = PaymentAttempt(
        order_id=order.id,
        customer_id=customer.id,
        external_payment_id=PAYMENT_ID,
        amount=50_000,
        currency="INR",
        payment_method=PaymentMethod.CARD.value,
        provider="razorpay",
        status=PaymentStatus.CREATED.value,
        attempt_number=1,
        attempted_at=RECEIVED,
    )
    db_session.add(row)
    db_session.flush()
    return row


def events(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Event)).scalar_one()


# -- the state machine -----------------------------------------------------


class TestStatusOnlyAdvances:
    @pytest.mark.parametrize(
        ("current", "proposed", "expected"),
        [
            ("created", "authorized", "authorized"),
            ("authorized", "captured", "captured"),
            ("created", "failed", "failed"),
            ("captured", "refunded", "refunded"),
        ],
    )
    def test_it_moves_forward(self, current: str, proposed: str, expected: str) -> None:
        assert advance_status(current, proposed) == expected

    @pytest.mark.parametrize(
        ("current", "proposed"),
        [
            ("captured", "authorized"),
            ("captured", "failed"),
            ("authorized", "created"),
            ("refunded", "captured"),
        ],
    )
    def test_it_never_moves_back(self, current: str, proposed: str) -> None:
        """Out-of-order delivery is the reason. A late `payment.authorized`
        must not rewrite a payment that was already captured."""
        assert advance_status(current, proposed) == current

    def test_an_unknown_status_is_not_assumed_newer(self) -> None:
        assert advance_status("captured", "invented") == "captured"
        assert advance_status("invented", "captured") == "invented"


# -- ordering --------------------------------------------------------------


class TestOccurredAtComesFromTheProvider:
    def test_it_prefers_the_top_level_timestamp(self) -> None:
        at = occurred_at_of(a_webhook("payment.captured"), received_at=RECEIVED)
        assert at == datetime.fromtimestamp(CAPTURED_AT, tz=UTC)
        assert at != RECEIVED

    def test_it_falls_back_to_the_entity_timestamp(self) -> None:
        payload = a_webhook("payment.captured")
        del payload["created_at"]
        assert occurred_at_of(payload, received_at=RECEIVED) == datetime.fromtimestamp(
            CAPTURED_AT, tz=UTC
        )

    def test_it_falls_back_to_received_at_only_when_nothing_is_stated(self) -> None:
        payload = {"event": "payment.captured", "payload": {}}
        assert occurred_at_of(payload, received_at=RECEIVED) == RECEIVED


class TestTheIdempotencyKey:
    def test_it_is_derived_from_the_signed_body(self) -> None:
        key = external_event_id_of(a_webhook("payment.captured"), None)
        assert key == f"payment.captured:{PAYMENT_ID}"

    def test_the_header_is_only_a_fallback(self) -> None:
        assert external_event_id_of({"event": "x", "payload": {}}, "evt_header") == "evt_header"

    def test_the_same_payment_under_two_events_is_two_keys(self) -> None:
        """Authorised and captured are different facts about one payment."""
        first = external_event_id_of(a_webhook("payment.authorized"), None)
        second = external_event_id_of(a_webhook("payment.captured"), None)
        assert first != second


# -- applying a webhook ----------------------------------------------------


@pytest.mark.db
class TestApplyWebhook:
    def test_a_mapped_event_is_persisted_and_advances_the_attempt(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        outcome = apply_webhook(
            db_session,
            a_webhook("payment.captured"),
            received_at=RECEIVED,
        )
        assert outcome.persisted is True
        assert outcome.duplicate is False
        assert outcome.event_type == "payment.captured"
        assert outcome.payment_attempt_updated is True
        assert outcome.previous_status == "created"
        assert outcome.new_status == "captured"
        assert attempt.status == "captured"

    def test_a_duplicate_delivery_creates_no_second_effect(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        """The unique constraint declines it; nothing here detects it."""
        payload = a_webhook("payment.captured")
        first = apply_webhook(db_session, payload, received_at=RECEIVED)
        before = events(db_session)
        second = apply_webhook(db_session, payload, received_at=RECEIVED)
        assert first.persisted is True
        assert second.persisted is False
        assert second.duplicate is True
        assert events(db_session) == before
        assert second.payment_attempt_updated is False

    def test_out_of_order_delivery_does_not_rewind_the_attempt(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        """Captured arrives first, authorised arrives after. Both are recorded;
        the attempt stays captured."""
        apply_webhook(
            db_session,
            a_webhook("payment.captured", created_at=CAPTURED_AT),
            received_at=RECEIVED,
        )
        assert attempt.status == "captured"

        late = apply_webhook(
            db_session,
            a_webhook("payment.authorized", created_at=AUTHORIZED_AT),
            received_at=RECEIVED + timedelta(minutes=5),
        )
        assert late.persisted is True
        assert late.payment_attempt_updated is False
        assert attempt.status == "captured"

    def test_the_timeline_orders_by_occurrence_not_arrival(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        """The captured event was received first but happened second."""
        apply_webhook(
            db_session,
            a_webhook("payment.captured", created_at=CAPTURED_AT),
            received_at=RECEIVED,
        )
        apply_webhook(
            db_session,
            a_webhook("payment.authorized", created_at=AUTHORIZED_AT),
            received_at=RECEIVED + timedelta(minutes=5),
        )
        ordered = list(
            db_session.execute(
                select(Event.event_type)
                .where(Event.order_id == attempt.order_id)
                .order_by(Event.occurred_at)
            ).scalars()
        )
        assert ordered == ["payment.authorized", "payment.captured"]

    def test_a_delayed_delivery_keeps_both_timestamps(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        late_arrival = RECEIVED + timedelta(days=2)
        apply_webhook(
            db_session,
            a_webhook("payment.captured", created_at=CAPTURED_AT),
            received_at=late_arrival,
        )
        row = db_session.execute(select(Event)).scalars().one()
        assert row.occurred_at == datetime.fromtimestamp(CAPTURED_AT, tz=UTC)
        assert row.received_at == late_arrival
        assert row.received_at > row.occurred_at

    def test_an_unknown_event_is_refused_and_stored_nowhere(
        self, db_session: Session, merchant: Merchant
    ) -> None:
        before = events(db_session)
        outcome = apply_webhook(
            db_session,
            {"event": "settlement.processed", "payload": {}},
            received_at=RECEIVED,
        )
        assert outcome.unmapped is True
        assert outcome.persisted is False
        assert outcome.event_type is None
        assert events(db_session) == before

    def test_a_knowingly_ignored_event_writes_nothing(
        self, db_session: Session, merchant: Merchant
    ) -> None:
        before = events(db_session)
        outcome = apply_webhook(
            db_session,
            {"event": "payment_link.created", "payload": {}},
            received_at=RECEIVED,
        )
        assert outcome.ignored is True
        assert outcome.unmapped is False
        assert events(db_session) == before

    def test_a_naive_timestamp_is_refused(self, db_session: Session, merchant: Merchant) -> None:
        with pytest.raises(VerificationError, match="timezone-aware"):
            apply_webhook(
                db_session,
                a_webhook("payment.captured"),
                received_at=datetime(2026, 9, 1, 12, 0),
            )

    def test_an_unknown_merchant_assertion_is_refused(self, db_session: Session) -> None:
        """Refused, and now for a stronger reason than before.

        This used to assert `no merchant`: the merchant was supplied and merely
        existence-checked. It is now derived from the signed payment id, so an
        unattributable event is refused *before* any merchant is considered —
        which is why the message names the payment, not the merchant.
        """
        with pytest.raises(VerificationError, match="no payment attempt matches"):
            apply_webhook(
                db_session,
                a_webhook("payment.captured"),
                received_at=RECEIVED,
                asserted_merchant_id=uuid.uuid4(),
            )

    def test_a_payload_with_no_event_name_is_refused(
        self, db_session: Session, merchant: Merchant
    ) -> None:
        with pytest.raises(VerificationError, match="no event name"):
            apply_webhook(db_session, {"payload": {}}, received_at=RECEIVED)


@pytest.mark.db
class TestWhatIsStored:
    def test_card_details_and_notes_are_stripped(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        apply_webhook(db_session, a_webhook("payment.captured"), received_at=RECEIVED)
        stored = json.dumps(db_session.execute(select(Event)).scalars().one().payload)
        for key in STRIPPED_KEYS:
            assert f'"{key}"' not in stored, key
        assert "1111" not in stored
        assert "Visa" not in stored

    def test_the_payment_id_survives_for_reconciliation(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        apply_webhook(db_session, a_webhook("payment.captured"), received_at=RECEIVED)
        stored = json.dumps(db_session.execute(select(Event)).scalars().one().payload)
        assert PAYMENT_ID in stored

    def test_no_audit_event_names_an_ai_actor(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        """A webhook is a provider fact. The AI is not part of this path at all,
        and the database independently refuses an ai_agent on an execution."""
        apply_webhook(db_session, a_webhook("payment.captured"), received_at=RECEIVED)
        ai_rows = db_session.execute(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.actor == "ai_agent")
        ).scalar_one()
        assert ai_rows == 0

    def test_it_does_not_commit(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        apply_webhook(db_session, a_webhook("payment.captured"), received_at=RECEIVED)
        assert db_session.in_transaction()


# -- the route -------------------------------------------------------------


@pytest.mark.db
class TestTheWebhookRoute:
    @pytest.fixture
    def client(self, db_session: Session):  # noqa: ANN201
        from fastapi.testclient import TestClient

        def override_db():  # noqa: ANN202
            yield db_session

        def override_settings() -> Settings:
            return Settings(  # type: ignore[call-arg]
                database_url="postgresql+psycopg://localhost/x",
                razorpay_webhook_secret=SecretStr(SECRET),
            )

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_settings] = override_settings
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def _post(
        self,
        client,
        merchant_id: uuid.UUID,
        payload: dict,
        *,
        secret: str = SECRET,
        signature: str | None = None,
        body: bytes | None = None,
    ):  # noqa: ANN001, ANN202
        raw = body if body is not None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        header_signature = signature if signature is not None else sign(raw, secret)
        if header_signature:
            headers[SIGNATURE_HEADER] = header_signature
        return client.post(
            f"/api/v1/webhooks/razorpay?merchant_id={merchant_id}", content=raw, headers=headers
        )

    def test_a_valid_signature_is_accepted(
        self, client, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:  # noqa: ANN001
        response = self._post(client, merchant.id, a_webhook("payment.captured"))
        assert response.status_code == 200
        assert response.json()["persisted"] is True

    def test_a_missing_signature_is_rejected(
        self, client, db_session: Session, merchant: Merchant
    ) -> None:  # noqa: ANN001
        response = self._post(client, merchant.id, a_webhook("payment.captured"), signature="")
        assert response.status_code == 401
        assert events(db_session) == 0

    def test_a_wrong_signature_is_rejected(
        self, client, db_session: Session, merchant: Merchant
    ) -> None:  # noqa: ANN001
        response = self._post(
            client, merchant.id, a_webhook("payment.captured"), signature="deadbeef"
        )
        assert response.status_code == 401
        assert events(db_session) == 0

    def test_a_signature_from_the_wrong_secret_is_rejected(
        self, client, db_session: Session, merchant: Merchant
    ) -> None:  # noqa: ANN001
        response = self._post(
            client, merchant.id, a_webhook("payment.captured"), secret="a-different-secret"
        )
        assert response.status_code == 401
        assert events(db_session) == 0

    def test_a_tampered_body_is_rejected(
        self, client, db_session: Session, merchant: Merchant
    ) -> None:  # noqa: ANN001
        payload = a_webhook("payment.captured")
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = sign(raw, SECRET)
        tampered = raw.replace(b"50000", b"99999")
        response = self._post(client, merchant.id, payload, signature=signature, body=tampered)
        assert response.status_code == 401
        assert events(db_session) == 0

    def test_a_rejection_reveals_nothing(self, client, merchant: Merchant) -> None:  # noqa: ANN001
        """A wrong secret and a wrong signature are indistinguishable."""
        wrong_sig = self._post(
            client, merchant.id, a_webhook("payment.captured"), signature="deadbeef"
        )
        wrong_secret = self._post(
            client, merchant.id, a_webhook("payment.captured"), secret="other-secret"
        )
        assert wrong_sig.json() == wrong_secret.json()
        assert SECRET not in wrong_sig.text

    def test_an_unmapped_event_is_acknowledged_not_refused(
        self, client, merchant: Merchant
    ) -> None:  # noqa: ANN001
        """A 4xx would make Razorpay redeliver something we will never map."""
        response = self._post(client, merchant.id, {"event": "settlement.processed", "payload": {}})
        assert response.status_code == 200
        assert response.json()["unmapped"] is True
        assert response.json()["persisted"] is False

    def test_a_duplicate_post_is_idempotent(
        self, client, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:  # noqa: ANN001
        payload = a_webhook("payment.captured")
        assert self._post(client, merchant.id, payload).json()["persisted"] is True
        assert self._post(client, merchant.id, payload).json()["duplicate"] is True

    def test_the_response_carries_no_payload_field(
        self, client, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:  # noqa: ANN001
        body = self._post(client, merchant.id, a_webhook("payment.captured")).text
        assert "1111" not in body
        assert "Visa" not in body
        assert SECRET not in body

    def test_no_payment_execution_route_exists(self) -> None:
        """The endpoint is inbound-only. An unauthenticated route that created a
        payment link would be a money-moving primitive open to the internet."""
        paths = {getattr(route, "path", "") for route in app.routes}
        for forbidden in ("payment_link", "payment-links", "execute", "pay"):
            assert not any(forbidden in path for path in paths), forbidden


# -- tenancy comes from the signed body ------------------------------------


@pytest.mark.db
class TestMerchantIdentityIsDerivedNotSupplied:
    """The cross-tenant defect, pinned so it cannot return.

    Before this, `merchant_id` was a required query parameter and authoritative.
    A validly-signed webhook about one merchant's payment could be posted naming
    another merchant: the event row was written under the caller's merchant
    while pointing at the victim's order and customer, and the victim's payment
    attempt was advanced. Existence-checking the merchant caught none of it,
    because the merchant existed — it simply did not own the payment.
    """

    @pytest.fixture
    def attacker(self, db_session: Session) -> Merchant:
        row = Merchant(name="Attacker Ltd", currency="INR", timezone="Asia/Kolkata")
        db_session.add(row)
        db_session.flush()
        return row

    def test_a_foreign_merchant_assertion_is_rejected(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt, attacker: Merchant
    ) -> None:
        """The exact attack, now refused."""
        before = events(db_session)
        with pytest.raises(VerificationError, match="does not own the payment"):
            apply_webhook(
                db_session,
                a_webhook("payment.captured"),
                received_at=RECEIVED,
                asserted_merchant_id=attacker.id,
            )
        assert events(db_session) == before

    def test_no_cross_tenant_event_row_is_written_on_rejection(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt, attacker: Merchant
    ) -> None:
        with pytest.raises(VerificationError):
            apply_webhook(
                db_session,
                a_webhook("payment.captured"),
                received_at=RECEIVED,
                asserted_merchant_id=attacker.id,
            )
        rows = db_session.execute(
            select(func.count()).select_from(Event).where(Event.merchant_id == attacker.id)
        ).scalar_one()
        assert rows == 0

    def test_the_victim_payment_attempt_is_not_mutated_on_rejection(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt, attacker: Merchant
    ) -> None:
        """The half of the defect that touched money state."""
        assert attempt.status == "created"
        with pytest.raises(VerificationError):
            apply_webhook(
                db_session,
                a_webhook("payment.captured"),
                received_at=RECEIVED,
                asserted_merchant_id=attacker.id,
            )
        db_session.refresh(attempt)
        assert attempt.status == "created"

    def test_a_matching_assertion_is_accepted(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        outcome = apply_webhook(
            db_session,
            a_webhook("payment.captured"),
            received_at=RECEIVED,
            asserted_merchant_id=merchant.id,
        )
        assert outcome.persisted is True

    def test_no_assertion_at_all_is_accepted(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        """The normal case: Razorpay sends no merchant id, and none is needed."""
        outcome = apply_webhook(db_session, a_webhook("payment.captured"), received_at=RECEIVED)
        assert outcome.persisted is True

    def test_the_row_is_written_under_the_derived_merchant(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        apply_webhook(db_session, a_webhook("payment.captured"), received_at=RECEIVED)
        row = db_session.execute(select(Event)).scalars().one()
        assert row.merchant_id == merchant.id
        assert row.order_id == attempt.order_id
        assert row.customer_id == attempt.customer_id

    def test_an_unmatched_payment_is_refused(self, db_session: Session, merchant: Merchant) -> None:
        """No attempt, no derivable owner. Refusing beats trusting the caller."""
        before = events(db_session)
        with pytest.raises(VerificationError, match="no payment attempt matches"):
            apply_webhook(
                db_session,
                a_webhook("payment.captured", payment_id="pay_UNKNOWN99"),
                received_at=RECEIVED,
            )
        assert events(db_session) == before

    def test_an_unmatched_payment_is_refused_even_with_a_valid_merchant(
        self, db_session: Session, merchant: Merchant
    ) -> None:
        """A real merchant id does not make an unattributable event attributable."""
        with pytest.raises(VerificationError, match="no payment attempt matches"):
            apply_webhook(
                db_session,
                a_webhook("payment.captured", payment_id="pay_UNKNOWN99"),
                received_at=RECEIVED,
                asserted_merchant_id=merchant.id,
            )

    def test_resolve_merchant_reads_through_the_order(
        self, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:
        assert resolve_merchant(db_session, attempt) == merchant.id


@pytest.mark.db
class TestTheRouteTreatsMerchantIdAsAnAssertion:
    @pytest.fixture
    def client(self, db_session: Session):  # noqa: ANN201
        from fastapi.testclient import TestClient

        def override_db():  # noqa: ANN202
            yield db_session

        def override_settings() -> Settings:
            return Settings(  # type: ignore[call-arg]
                database_url="postgresql+psycopg://localhost/x",
                razorpay_webhook_secret=SecretStr(SECRET),
            )

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_settings] = override_settings
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def _post(self, client, payload: dict, merchant_id=None):  # noqa: ANN001, ANN202
        raw = json.dumps(payload, separators=(",", ":")).encode()
        url = "/api/v1/webhooks/razorpay"
        if merchant_id is not None:
            url += f"?merchant_id={merchant_id}"
        return client.post(
            url,
            content=raw,
            headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(raw, SECRET)},
        )

    def test_it_is_optional(self, client, merchant: Merchant, attempt: PaymentAttempt) -> None:  # noqa: ANN001
        response = self._post(client, a_webhook("payment.captured"))
        assert response.status_code == 200
        assert response.json()["persisted"] is True

    def test_a_foreign_merchant_is_rejected_with_400(
        self, client, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:  # noqa: ANN001
        other = Merchant(name="Other Ltd", currency="INR", timezone="Asia/Kolkata")
        db_session.add(other)
        db_session.flush()
        response = self._post(client, a_webhook("payment.captured"), merchant_id=other.id)
        assert response.status_code == 400
        assert events(db_session) == 0

    def test_a_matching_merchant_is_accepted(
        self, client, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:  # noqa: ANN001
        response = self._post(client, a_webhook("payment.captured"), merchant_id=merchant.id)
        assert response.status_code == 200

    def test_a_rejection_leaks_no_secret_or_body(
        self, client, db_session: Session, merchant: Merchant, attempt: PaymentAttempt
    ) -> None:  # noqa: ANN001
        other = Merchant(name="Other Ltd", currency="INR", timezone="Asia/Kolkata")
        db_session.add(other)
        db_session.flush()
        body = self._post(client, a_webhook("payment.captured"), merchant_id=other.id).text
        assert SECRET not in body
        assert "1111" not in body
        assert "Visa" not in body
