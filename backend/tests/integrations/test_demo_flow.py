"""The offline demo, end to end. **No network, no credentials.**

One test walks the whole path and asserts every property the buildathon demo
claims. It is deliberately a single narrative rather than a dozen fragments:
the claim being made is that these steps compose, and a suite of isolated
assertions would not show that.

Everything runs through production code. `DemoPaymentLinkClient` implements the
SDK's own surface, so `payment_links.create_payment_link` — the real mapping and
validation — is what executes. The webhooks carry genuine HMAC-SHA256
signatures over the exact delivered bytes, verified by the production verifier.
Only the provider bytes and the secret are synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.razorpay.demo import (
    DEMO_EPOCH,
    DEMO_EVENTS,
    DEMO_MARKER,
    DEMO_WEBHOOK_SECRET,
    PROVENANCE,
    DemoPaymentLinkClient,
    demo_id,
    demo_scenario,
    paid_client,
    signed_webhook,
    webhook_body,
)
from app.integrations.razorpay.mapper import map_event
from app.integrations.razorpay.payment_links import (
    LINK_CREATED,
    LINK_PAID,
    build_request,
    create_payment_link,
    fetch_payment_link,
)
from app.integrations.razorpay.webhooks import WebhookVerificationError, verify_signature
from app.models.enums import EventType
from app.models.event import Event
from app.services.verification.demo_scenario import DEMO_AMOUNT_MINOR, build_demo_population
from app.services.verification.service import VerificationError, apply_webhook

RECEIVED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def events(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Event)).scalar_one()


class TestTheDemoIsLabelledSynthetic:
    """A demo that looked real would be a false record."""

    def test_the_provenance_says_what_it_is(self) -> None:
        for term in ("DEMO", "SYNTHETIC", "OFFLINE"):
            assert term in PROVENANCE
        assert "not a Razorpay transaction" in PROVENANCE

    def test_every_identifier_is_marked(self) -> None:
        for kind in ("plink", "pay", "order", "cust"):
            assert DEMO_MARKER in demo_id(kind, "seed")

    def test_the_demo_secret_announces_itself(self) -> None:
        assert "demo" in DEMO_WEBHOOK_SECRET
        assert "not-a-real-credential" in DEMO_WEBHOOK_SECRET

    def test_the_link_url_cannot_resolve(self) -> None:
        """`.invalid` is reserved by RFC 2606 and can never be registered."""
        client = DemoPaymentLinkClient(reference_id="ref-1")
        assert ".invalid" in client.payment_link.create({"amount": 1})["short_url"]

    def test_it_is_deterministic(self) -> None:
        """Two runs produce identical bytes, so output can be diffed."""
        first = webhook_body("payment.captured", payment_id="pay_X")
        second = webhook_body("payment.captured", payment_id="pay_X")
        assert first == second
        assert demo_id("plink", "s") == demo_id("plink", "s")


class TestTheDemoUsesTheRealAdapter:
    def test_a_link_is_created_through_production_code(self) -> None:
        request = build_request(
            amount_minor=DEMO_AMOUNT_MINOR, reference_id="ref-1", description="d"
        )
        link = create_payment_link(DemoPaymentLinkClient(reference_id="ref-1"), request)
        assert link.provider_link_id.startswith("plink_")
        assert DEMO_MARKER in link.provider_link_id
        assert link.amount_minor == DEMO_AMOUNT_MINOR
        assert link.status == LINK_CREATED
        assert link.reference_id == "ref-1"

    def test_a_link_can_be_fetched_back(self) -> None:
        link = fetch_payment_link(paid_client("ref-1"), "plink_DEMOabc")
        assert link.provider_link_id == "plink_DEMOabc"
        assert link.status == LINK_PAID
        assert link.is_paid is True

    def test_the_adapter_still_refuses_a_bad_demo_response(self) -> None:
        """The demo does not get a relaxed mapping."""
        from app.integrations.razorpay.payment_links import PaymentLinkError, to_payment_link

        with pytest.raises(PaymentLinkError):
            to_payment_link({"id": "plink_DEMOx", "status": "created"})  # no amount

    def test_money_stays_an_integer(self) -> None:
        request = build_request(amount_minor=DEMO_AMOUNT_MINOR, reference_id="r", description="d")
        link = create_payment_link(DemoPaymentLinkClient(reference_id="r"), request)
        assert isinstance(link.amount_minor, int)
        assert not isinstance(link.amount_minor, bool)


class TestTheSyntheticWebhooksAreRealSignatures:
    @pytest.mark.parametrize("event", list(DEMO_EVENTS))
    def test_each_fixture_verifies(self, event: str) -> None:
        raw, signature = signed_webhook(event, payment_id="pay_DEMO1")
        assert verify_signature(raw, signature, DEMO_WEBHOOK_SECRET) is None

    @pytest.mark.parametrize("event", list(DEMO_EVENTS))
    def test_each_fixture_maps_to_the_neutral_vocabulary(self, event: str) -> None:
        assert map_event(event).value in EventType.values()

    def test_payment_link_paid_maps_to_order_paid(self) -> None:
        """Worth showing: the neutral name is not the provider's name."""
        assert map_event("payment_link.paid") is EventType.ORDER_PAID

    def test_a_tampered_demo_body_is_refused(self) -> None:
        raw, signature = signed_webhook("payment.captured", payment_id="pay_DEMO1")
        with pytest.raises(WebhookVerificationError):
            verify_signature(raw.replace(b"50000", b"99999"), signature, DEMO_WEBHOOK_SECRET)

    def test_a_different_secret_is_refused(self) -> None:
        raw, signature = signed_webhook("payment.captured", payment_id="pay_DEMO1")
        with pytest.raises(WebhookVerificationError):
            verify_signature(raw, signature, "some-other-secret")

    def test_the_body_is_bytes_not_a_dict(self) -> None:
        """The signature covers octets; a dict would be re-serialised."""
        raw, _ = signed_webhook("payment.captured", payment_id="pay_DEMO1")
        assert isinstance(raw, bytes)
        assert json.dumps(json.loads(raw)).encode() != raw  # compact vs default separators


@pytest.mark.db
class TestTheCompleteDemoFlow:
    """The single narrative the buildathon demo shows."""

    def test_the_whole_recovery_path_offline(self, db_session: Session) -> None:
        # 1. A synthetic payment has failed — merchant A owns it.
        merchant_a = build_demo_population(db_session, label="A")
        assert merchant_a.attempt.status == "failed"

        # 2. RevTrace creates a payment link through the real adapter.
        request = build_request(
            amount_minor=DEMO_AMOUNT_MINOR,
            reference_id=merchant_a.reference_id,
            description="demo",
        )
        link = create_payment_link(
            DemoPaymentLinkClient(reference_id=merchant_a.reference_id), request
        )
        assert DEMO_MARKER in link.provider_link_id
        assert request["notify"] == {"sms": False, "email": False}

        # 3. Three signed webhooks arrive.
        scenario = demo_scenario(merchant_a.payment_id, link.provider_link_id)
        assert set(scenario) == set(DEMO_EVENTS)

        # 4. Each is verified over raw bytes, then applied.
        for name in DEMO_EVENTS:
            raw, signature = scenario[name]
            assert verify_signature(raw, signature, DEMO_WEBHOOK_SECRET) is None
            outcome = apply_webhook(db_session, json.loads(raw), received_at=RECEIVED)
            assert outcome.persisted is True, name

            row = db_session.execute(
                select(Event).where(Event.external_event_id == outcome.external_event_id)
            ).scalar_one()
            # Attributed to merchant A, derived from the signed payment id.
            assert row.merchant_id == merchant_a.merchant.id, name
            assert row.order_id == merchant_a.order.id, name

        # 5. The attempt advanced, and only forwards.
        db_session.flush()
        db_session.refresh(merchant_a.attempt)
        assert merchant_a.attempt.status == "captured"

        # 6. Ordering is by occurrence, not arrival.
        ordered = list(
            db_session.execute(
                select(Event.event_type)
                .where(Event.order_id == merchant_a.order.id)
                .order_by(Event.occurred_at)
            ).scalars()
        )
        assert ordered == ["payment.failed", "payment.captured", "order.paid"]

        # 7. Replay creates no second effect.
        before = events(db_session)
        for name in DEMO_EVENTS:
            raw, signature = scenario[name]
            assert verify_signature(raw, signature, DEMO_WEBHOOK_SECRET) is None
            replay = apply_webhook(db_session, json.loads(raw), received_at=RECEIVED)
            assert replay.duplicate is True, name
            assert replay.persisted is False, name
        assert events(db_session) == before
        db_session.flush()
        db_session.refresh(merchant_a.attempt)
        assert merchant_a.attempt.status == "captured"

    def test_a_second_merchant_cannot_claim_the_event(self, db_session: Session) -> None:
        """Cross-tenant attribution stays refused in the demo path."""
        merchant_a = build_demo_population(db_session, label="A")
        merchant_b = build_demo_population(db_session, label="B")

        raw, signature = signed_webhook("payment.captured", payment_id=merchant_a.payment_id)
        assert verify_signature(raw, signature, DEMO_WEBHOOK_SECRET) is None

        before = events(db_session)
        with pytest.raises(VerificationError, match="does not own the payment"):
            apply_webhook(
                db_session,
                json.loads(raw),
                received_at=RECEIVED,
                asserted_merchant_id=merchant_b.merchant.id,
            )
        assert events(db_session) == before
        db_session.refresh(merchant_a.attempt)
        assert merchant_a.attempt.status == "failed"

    def test_a_webhook_for_an_unknown_payment_is_refused(self, db_session: Session) -> None:
        build_demo_population(db_session, label="A")
        raw, signature = signed_webhook("payment.captured", payment_id="pay_DEMOunknown")
        assert verify_signature(raw, signature, DEMO_WEBHOOK_SECRET) is None
        with pytest.raises(VerificationError, match="no payment attempt matches"):
            apply_webhook(db_session, json.loads(raw), received_at=RECEIVED)

    def test_out_of_order_delivery_does_not_rewind(self, db_session: Session) -> None:
        """Captured first, the earlier failure after. The attempt stays captured."""
        population = build_demo_population(db_session, label="A")
        captured, sig_c = signed_webhook(
            "payment.captured", payment_id=population.payment_id, occurred_at=DEMO_EPOCH + 3_600
        )
        failed, sig_f = signed_webhook(
            "payment.failed", payment_id=population.payment_id, occurred_at=DEMO_EPOCH
        )
        verify_signature(captured, sig_c, DEMO_WEBHOOK_SECRET)
        apply_webhook(db_session, json.loads(captured), received_at=RECEIVED)
        db_session.flush()
        db_session.refresh(population.attempt)
        assert population.attempt.status == "captured"

        verify_signature(failed, sig_f, DEMO_WEBHOOK_SECRET)
        late = apply_webhook(db_session, json.loads(failed), received_at=RECEIVED)
        assert late.persisted is True
        assert late.payment_attempt_updated is False
        db_session.flush()
        db_session.refresh(population.attempt)
        assert population.attempt.status == "captured"

    def test_the_demo_writes_no_audit_row(self, db_session: Session) -> None:
        """The AI is not on this path, so there is no actor for it to become."""
        from app.models.audit_event import AuditEvent

        population = build_demo_population(db_session, label="A")
        raw, signature = signed_webhook("payment.captured", payment_id=population.payment_id)
        verify_signature(raw, signature, DEMO_WEBHOOK_SECRET)
        apply_webhook(db_session, json.loads(raw), received_at=RECEIVED)
        assert db_session.execute(select(func.count()).select_from(AuditEvent)).scalar_one() == 0

    def test_no_card_data_is_stored(self, db_session: Session) -> None:
        population = build_demo_population(db_session, label="A")
        raw, signature = signed_webhook("payment.captured", payment_id=population.payment_id)
        verify_signature(raw, signature, DEMO_WEBHOOK_SECRET)
        apply_webhook(db_session, json.loads(raw), received_at=RECEIVED)
        stored = json.dumps(db_session.execute(select(Event)).scalars().one().payload)
        for banned in ("card", "notes", "token"):
            assert f'"{banned}"' not in stored


class TestTheDemoNeedsNoCredentials:
    def test_the_demo_module_reads_no_setting(self) -> None:
        """It must run with an empty `.env`."""
        import ast
        import pathlib

        tree = ast.parse(
            pathlib.Path("app/integrations/razorpay/demo.py").read_text(encoding="utf-8")
        )
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any("config" in m for m in modules)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "get_settings" not in names
        assert "Settings" not in names

    def test_the_demo_module_builds_no_real_client(self) -> None:
        """`build_client` would demand a credential and open a session."""
        import ast
        import pathlib

        tree = ast.parse(
            pathlib.Path("app/integrations/razorpay/demo.py").read_text(encoding="utf-8")
        )
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "build_client" not in called
        assert "Client" not in called

    def test_the_runner_refuses_the_protected_databases(self) -> None:
        import run_demo

        assert run_demo.FORBIDDEN_DATABASE == "revtrace_dev"
        assert run_demo.HYPOTHESIS_DATABASE == "revtrace_hypothesis_test"
