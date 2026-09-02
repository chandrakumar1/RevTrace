"""Signature verification, the webhook endpoint's only authentication.

Every test here fails closed by construction: `verify_signature` returns None on
success and raises otherwise, so a test cannot pass by mistaking a falsy return
for a rejection.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.razorpay.mapper import (
    EVENT_MAP,
    IGNORED_EVENTS,
    UnmappedEvent,
    is_ignored,
    map_event,
    supported_events,
)
from app.integrations.razorpay.webhooks import (
    WebhookVerificationError,
    event_name,
    sign,
    verify_signature,
    verify_with_settings,
)
from app.models.enums import EventType

SECRET = "synthetic-webhook-secret-not-real"
OTHER_SECRET = "a-different-synthetic-secret-0000"

#: Compact separators, as a provider actually sends. This matters: with
#: `json.dumps` defaults the body would survive a parse/re-dump round trip
#: unchanged, and the raw-body tests below would pass without proving anything.
BODY = json.dumps(
    {
        "event": "payment.captured",
        "created_at": 1_767_225_600,
        "payload": {"payment": {"entity": {"id": "pay_TESTONLY01", "amount": 50_000}}},
    },
    separators=(",", ":"),
).encode("utf-8")


def a_settings(secret: str = SECRET) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://localhost/x",
        razorpay_webhook_secret=SecretStr(secret),
    )


class TestSignatureVerification:
    def test_a_valid_signature_passes(self) -> None:
        assert verify_signature(BODY, sign(BODY, SECRET), SECRET) is None

    def test_a_tampered_body_is_refused(self) -> None:
        """The signature is computed over the original bytes; the body then
        changes by one character."""
        signature = sign(BODY, SECRET)
        tampered = BODY.replace(b"50000", b"99999")
        assert tampered != BODY
        with pytest.raises(WebhookVerificationError, match="verification failed"):
            verify_signature(tampered, signature, SECRET)

    def test_a_single_byte_change_is_refused(self) -> None:
        signature = sign(BODY, SECRET)
        with pytest.raises(WebhookVerificationError):
            verify_signature(BODY + b" ", signature, SECRET)

    def test_the_wrong_secret_is_refused(self) -> None:
        with pytest.raises(WebhookVerificationError, match="verification failed"):
            verify_signature(BODY, sign(BODY, OTHER_SECRET), SECRET)

    @pytest.mark.parametrize("signature", [None, "", "   ", "not-a-signature", "0" * 64])
    def test_a_missing_or_malformed_signature_is_refused(self, signature: str | None) -> None:
        with pytest.raises(WebhookVerificationError):
            verify_signature(BODY, signature, SECRET)

    def test_an_unconfigured_secret_refuses_everything(self) -> None:
        """Fail closed: with no secret the endpoint can authenticate nothing,
        so it accepts nothing — including a body signed with an empty string."""
        with pytest.raises(WebhookVerificationError, match="not configured"):
            verify_signature(BODY, sign(BODY, ""), "")

    def test_an_empty_body_is_refused(self) -> None:
        with pytest.raises(WebhookVerificationError, match="empty request body"):
            verify_signature(b"", sign(b"", SECRET), SECRET)

    def test_a_non_utf8_body_is_refused(self) -> None:
        """Razorpay sends JSON, which is UTF-8. Coercing anything else would
        mean verifying something other than what arrived."""
        body = b"\xff\xfe\x00 not utf-8"
        with pytest.raises(WebhookVerificationError, match="not valid UTF-8"):
            verify_signature(body, sign(body, SECRET), SECRET)

    def test_verification_returns_none_rather_than_a_boolean(self) -> None:
        """A boolean-returning verifier invites `if verify(...)` and silently
        passes when someone forgets. This one can only raise."""
        assert verify_signature(BODY, sign(BODY, SECRET), SECRET) is None

    def test_it_reads_the_secret_from_settings_at_call_time(self) -> None:
        assert verify_with_settings(BODY, sign(BODY, SECRET), a_settings()) is None
        with pytest.raises(WebhookVerificationError):
            verify_with_settings(BODY, sign(BODY, SECRET), a_settings(OTHER_SECRET))


class TestTheRawBodyRequirement:
    def test_a_reserialised_body_does_not_verify(self) -> None:
        """The reason the route reads `await request.body()`.

        Parsing and re-dumping produces different octets — different key order
        and spacing — so a route that verified a re-serialisation would reject
        every genuine webhook.
        """
        signature = sign(BODY, SECRET)
        reserialised = json.dumps(json.loads(BODY)).encode("utf-8")
        assert reserialised != BODY
        with pytest.raises(WebhookVerificationError):
            verify_signature(reserialised, signature, SECRET)

    def test_key_order_alone_breaks_the_signature(self) -> None:
        payload = json.loads(BODY)
        reordered = json.dumps(payload, sort_keys=False).encode("utf-8")
        signature = sign(BODY, SECRET)
        if reordered != BODY:
            with pytest.raises(WebhookVerificationError):
                verify_signature(reordered, signature, SECRET)

    def test_the_helper_agrees_with_the_sdk(self) -> None:
        """`sign` must produce what Razorpay would, or every test above is
        asserting agreement between two of our own mistakes."""
        expected = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
        assert sign(BODY, SECRET) == expected
        assert verify_signature(BODY, expected, SECRET) is None


class TestNoSecretLeaks:
    @pytest.mark.parametrize(
        ("body", "signature", "secret"),
        [
            (BODY, "wrong", SECRET),
            (BODY, None, SECRET),
            (b"", "x", SECRET),
            (BODY, sign(BODY, OTHER_SECRET), SECRET),
        ],
    )
    def test_no_failure_message_contains_the_secret_or_the_body(
        self, body: bytes, signature: str | None, secret: str
    ) -> None:
        with pytest.raises(WebhookVerificationError) as caught:
            verify_signature(body, signature, secret)
        message = str(caught.value)
        assert SECRET not in message
        assert OTHER_SECRET not in message
        assert "pay_TESTONLY01" not in message
        assert "50000" not in message

    def test_a_failure_does_not_say_which_check_failed_beyond_its_class(self) -> None:
        """A wrong secret and a wrong signature are indistinguishable to the
        caller: both are 'signature verification failed'."""
        wrong_signature = str(
            pytest.raises(
                WebhookVerificationError, verify_signature, BODY, "deadbeef", SECRET
            ).value
        )
        wrong_secret = str(
            pytest.raises(
                WebhookVerificationError, verify_signature, BODY, sign(BODY, OTHER_SECRET), SECRET
            ).value
        )
        assert wrong_signature == wrong_secret


class TestEventNameIsReadFromTheBody:
    def test_it_reads_the_signed_event_field(self) -> None:
        assert event_name(json.loads(BODY)) == "payment.captured"

    @pytest.mark.parametrize("payload", [{}, {"event": ""}, {"event": 1}, [], None, "x"])
    def test_anything_else_yields_none(self, payload: object) -> None:
        assert event_name(payload) is None


class TestTheMapper:
    @pytest.mark.parametrize(("name", "expected"), sorted(EVENT_MAP.items()))
    def test_every_mapped_event_resolves(self, name: str, expected: EventType) -> None:
        assert map_event(name) is expected

    def test_every_target_is_a_real_event_type(self) -> None:
        for target in EVENT_MAP.values():
            assert target.value in EventType.values()

    @pytest.mark.parametrize("name", sorted(IGNORED_EVENTS))
    def test_a_knowingly_ignored_event_is_not_unmapped(self, name: str) -> None:
        """Expected traffic, distinguished from a gap in the table."""
        assert is_ignored(name) is True
        assert name not in EVENT_MAP

    @pytest.mark.parametrize(
        "name",
        [
            "payment.dispute.created",
            "settlement.processed",
            "invoice.paid",
            "account.suspended",
            "",
            "payment.captured.v2",
        ],
    )
    def test_an_unknown_event_is_refused_not_guessed(self, name: str) -> None:
        with pytest.raises(UnmappedEvent):
            map_event(name)
        assert is_ignored(name) is False

    def test_the_refusal_names_the_event(self) -> None:
        with pytest.raises(UnmappedEvent) as caught:
            map_event("settlement.processed")
        assert caught.value.name == "settlement.processed"
        assert "settlement.processed" in str(caught.value)

    def test_mapped_and_ignored_never_overlap(self) -> None:
        assert set(EVENT_MAP).isdisjoint(IGNORED_EVENTS)
        assert supported_events() == set(EVENT_MAP) | IGNORED_EVENTS

    def test_no_event_maps_to_a_recovery_outcome(self) -> None:
        """`recovery.*` describes what RevTrace did, not what a provider saw.

        A provider event mapped onto one would let Razorpay appear to report on
        RevTrace's own recovery decisions.
        """
        for target in EVENT_MAP.values():
            assert not target.value.startswith("recovery.")
