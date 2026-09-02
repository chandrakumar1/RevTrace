"""The payment-link adapter: mapping, and refusing what it cannot map.

No network call. `create_payment_link` and `fetch_payment_link` are driven with
a stub client, so what is tested is the translation and the error boundary —
not Razorpay.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.razorpay.payment_links import (
    CURRENCY,
    LINK_CREATED,
    LINK_PAID,
    TERMINAL_STATUSES,
    PaymentLink,
    PaymentLinkError,
    build_request,
    create_payment_link,
    fetch_payment_link,
    to_payment_link,
)

#: A response shaped as Razorpay documents one, trimmed to what is mapped plus
#: several fields that must be ignored.
RESPONSE: dict[str, Any] = {
    "id": "plink_TESTONLY01",
    "status": LINK_CREATED,
    "amount": 50_000,
    "currency": "INR",
    "short_url": "https://rzp.io/i/TESTONLY",
    "reference_id": "action-0001",
    "amount_paid": 0,
    "description": "Complete your payment",
    "customer": {"name": "Test Person", "email": "person@example.test"},
    "notes": {"internal": "should not be mapped"},
    "notify": {"sms": False, "email": False},
}


class _StubLink:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.created_with: Any = None
        self.fetched: str | None = None

    def create(self, data: Any) -> Any:
        self.created_with = data
        if self._error:
            raise self._error
        return self._response

    def fetch(self, payment_link_id: str) -> Any:
        self.fetched = payment_link_id
        if self._error:
            raise self._error
        return self._response


class _StubClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.payment_link = _StubLink(response, error)


class TestMapping:
    def test_it_maps_the_fields_revtrace_uses(self) -> None:
        link = to_payment_link(RESPONSE)
        assert link.provider_link_id == "plink_TESTONLY01"
        assert link.status == LINK_CREATED
        assert link.amount_minor == 50_000
        assert link.currency == "INR"
        assert link.short_url == "https://rzp.io/i/TESTONLY"
        assert link.reference_id == "action-0001"

    def test_it_maps_nothing_else(self) -> None:
        """A field that is not mapped cannot be depended on."""
        link = to_payment_link(RESPONSE)
        for absent in ("notes", "customer", "description", "amount_paid", "notify"):
            assert not hasattr(link, absent), absent

    def test_the_stored_result_is_scalars_only(self) -> None:
        """`recovery_actions.result` is JSONB read by the audit snapshot guard,
        which rejects a float and anything not JSON-safe."""
        result = to_payment_link(RESPONSE).as_result()
        assert result["provider"] == "razorpay"
        assert result["provider_link_id"] == "plink_TESTONLY01"
        for value in result.values():
            assert not isinstance(value, float)
            assert isinstance(value, str | int | bool | type(None))

    def test_the_customer_is_not_carried_into_the_result(self) -> None:
        rendered = str(to_payment_link(RESPONSE).as_result())
        assert "Test Person" not in rendered
        assert "person@example.test" not in rendered

    @pytest.mark.parametrize("field", ["id", "status", "amount"])
    def test_a_missing_required_field_is_refused(self, field: str) -> None:
        payload = {k: v for k, v in RESPONSE.items() if k != field}
        with pytest.raises(PaymentLinkError, match=field):
            to_payment_link(payload)

    @pytest.mark.parametrize("amount", [500.0, "50000", None, True])
    def test_a_non_integer_amount_is_refused(self, amount: object) -> None:
        """Money is integer minor units everywhere; a float must not enter."""
        with pytest.raises(PaymentLinkError, match="minor units"):
            to_payment_link({**RESPONSE, "amount": amount})

    def test_a_non_object_response_is_refused(self) -> None:
        with pytest.raises(PaymentLinkError):
            to_payment_link(["not", "an", "object"])

    def test_currency_defaults_only_when_absent(self) -> None:
        payload = {k: v for k, v in RESPONSE.items() if k != "currency"}
        assert to_payment_link(payload).currency == CURRENCY

    def test_status_helpers(self) -> None:
        assert to_payment_link(RESPONSE).is_paid is False
        paid = to_payment_link({**RESPONSE, "status": LINK_PAID})
        assert paid.is_paid is True
        assert paid.is_terminal is True
        assert all(s in TERMINAL_STATUSES for s in ("paid", "expired", "cancelled"))

    def test_a_link_with_no_id_cannot_be_constructed(self) -> None:
        with pytest.raises(PaymentLinkError, match="cannot be reconciled"):
            PaymentLink(
                provider_link_id="",
                status=LINK_CREATED,
                amount_minor=1,
                currency="INR",
                short_url=None,
                reference_id=None,
            )


class TestTheRequestBody:
    def test_it_carries_the_reference_id(self) -> None:
        """Razorpay echoes it back on the link and its webhooks, which is what
        lets an out-of-order event be matched without trusting delivery order."""
        body = build_request(amount_minor=50_000, reference_id="action-1", description="d")
        assert body["reference_id"] == "action-1"
        assert body["amount"] == 50_000
        assert body["currency"] == CURRENCY

    def test_provider_notifications_are_disabled(self) -> None:
        """The policy engine counts contacts and caps them. A provider-sent SMS
        would be a second, uncounted channel outside that budget."""
        body = build_request(amount_minor=1, reference_id="r", description="d")
        assert body["notify"] == {"sms": False, "email": False}
        assert body["reminder_enable"] is False

    def test_customer_fields_are_omitted_when_absent(self) -> None:
        assert "customer" not in build_request(amount_minor=1, reference_id="r", description="d")

    def test_customer_fields_are_included_when_given(self) -> None:
        body = build_request(
            amount_minor=1,
            reference_id="r",
            description="d",
            customer_name="A",
            customer_email="a@example.test",
        )
        assert body["customer"] == {"name": "A", "email": "a@example.test"}

    @pytest.mark.parametrize("amount", [0, -1, 5.0, True, "100"])
    def test_a_bad_amount_is_refused(self, amount: object) -> None:
        with pytest.raises(PaymentLinkError):
            build_request(amount_minor=amount, reference_id="r", description="d")  # type: ignore[arg-type]

    def test_a_missing_reference_is_refused(self) -> None:
        with pytest.raises(PaymentLinkError, match="reference_id"):
            build_request(amount_minor=1, reference_id="", description="d")


class TestTheErrorBoundary:
    def test_create_returns_a_mapped_link(self) -> None:
        client = _StubClient(RESPONSE)
        body = build_request(amount_minor=50_000, reference_id="action-1", description="d")
        link = create_payment_link(client, body)
        assert link.provider_link_id == "plink_TESTONLY01"
        assert client.payment_link.created_with == body

    def test_fetch_passes_the_id_through(self) -> None:
        client = _StubClient(RESPONSE)
        assert fetch_payment_link(client, "plink_TESTONLY01").status == LINK_CREATED
        assert client.payment_link.fetched == "plink_TESTONLY01"

    def test_an_empty_id_is_refused_before_the_call(self) -> None:
        client = _StubClient(RESPONSE)
        with pytest.raises(PaymentLinkError, match="id is required"):
            fetch_payment_link(client, "")
        assert client.payment_link.fetched is None

    @pytest.mark.parametrize("error_name", ["BadRequestError", "ServerError", "GatewayError"])
    def test_no_provider_exception_escapes_the_package(self, error_name: str) -> None:
        import razorpay.errors as razorpay_errors

        error_type = getattr(razorpay_errors, error_name)
        client = _StubClient(error=error_type("provider detail that must not escape"))
        with pytest.raises(PaymentLinkError) as caught:
            create_payment_link(
                client, build_request(amount_minor=1, reference_id="r", description="d")
            )
        assert not isinstance(caught.value, error_type)
        assert "must not escape" not in str(caught.value)
        assert error_name in str(caught.value)
