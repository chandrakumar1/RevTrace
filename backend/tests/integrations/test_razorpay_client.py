"""The Razorpay client boundary: Test Mode, and no secret anywhere.

No network call is made in this file, and none is possible: `build_client`
constructs an SDK object, which configures a session and returns. The only
method that would open a socket is never called.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.razorpay.client import (
    LIVE_KEY_PREFIX,
    TEST_KEY_PREFIX,
    RazorpayLiveModeRefused,
    RazorpayNotConfigured,
    build_client,
    is_test_mode,
    validate_key_id,
)

#: Synthetic. A literal in this file — nothing reads an environment variable or
#: a `.env`, so no real credential can reach these tests.
TEST_KEY_ID = "rzp_test_00000000000000"
TEST_KEY_SECRET = "synthetic-not-a-real-secret-0000"
LIVE_KEY_ID = "rzp_live_00000000000000"


def a_settings(*, key_id: str = TEST_KEY_ID, secret: str = TEST_KEY_SECRET) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://localhost/x",
        razorpay_key_id=SecretStr(key_id),
        razorpay_key_secret=SecretStr(secret),
    )


class TestTestModeIsEnforced:
    def test_a_test_key_is_accepted(self) -> None:
        assert validate_key_id(TEST_KEY_ID).is_test_mode is True

    def test_a_live_key_is_refused(self) -> None:
        """The refusal that matters. No client object is ever produced."""
        with pytest.raises(RazorpayLiveModeRefused, match="live key"):
            validate_key_id(LIVE_KEY_ID)

    @pytest.mark.parametrize(
        "key_id", ["rzp_", "abc123", "test_rzp_x", "rzp_testing_x", "RZP_TEST_X"]
    )
    def test_an_unrecognised_shape_is_refused(self, key_id: str) -> None:
        """Not evidence of safety. Guessing permissively is how live keys leak in."""
        with pytest.raises(RazorpayLiveModeRefused):
            validate_key_id(key_id)

    def test_an_empty_key_is_refused_as_unconfigured(self) -> None:
        with pytest.raises(RazorpayNotConfigured, match="RAZORPAY_KEY_ID is not set"):
            validate_key_id("")

    def test_the_two_prefixes_are_distinct(self) -> None:
        assert TEST_KEY_PREFIX != LIVE_KEY_PREFIX
        assert not LIVE_KEY_ID.startswith(TEST_KEY_PREFIX)

    def test_a_live_key_is_refused_before_a_client_exists(self) -> None:
        with pytest.raises(RazorpayLiveModeRefused):
            build_client(a_settings(key_id=LIVE_KEY_ID))

    def test_a_missing_secret_is_refused(self) -> None:
        with pytest.raises(RazorpayNotConfigured, match="RAZORPAY_KEY_SECRET"):
            build_client(a_settings(secret=""))

    def test_a_constructed_client_reports_test_mode(self) -> None:
        assert is_test_mode(build_client(a_settings())) is True

    def test_is_test_mode_is_false_for_anything_else(self) -> None:
        assert is_test_mode(object()) is False
        assert is_test_mode(None) is False


class TestNoSecretEscapes:
    def test_the_credential_object_does_not_hold_the_secret(self) -> None:
        """Only the key id, which is not secret. The secret goes straight to the
        SDK and is never stored on a RevTrace object."""
        credentials = validate_key_id(TEST_KEY_ID)
        rendered = repr(credentials)
        assert TEST_KEY_SECRET not in rendered
        assert not hasattr(credentials, "key_secret")
        assert not hasattr(credentials, "secret")

    @pytest.mark.parametrize(
        ("key_id", "secret"),
        [(LIVE_KEY_ID, TEST_KEY_SECRET), ("bad", TEST_KEY_SECRET), (TEST_KEY_ID, "")],
    )
    def test_no_refusal_message_contains_the_secret(self, key_id: str, secret: str) -> None:
        with pytest.raises(Exception) as caught:  # noqa: B017 - several refusal types
            build_client(a_settings(key_id=key_id, secret=secret))
        assert TEST_KEY_SECRET not in str(caught.value)

    def test_a_refusal_names_the_variable_not_its_value(self) -> None:
        with pytest.raises(RazorpayNotConfigured) as caught:
            build_client(a_settings(secret=""))
        message = str(caught.value)
        assert "RAZORPAY_KEY_SECRET" in message
        assert TEST_KEY_SECRET not in message


class TestConfiguration:
    def test_the_webhook_secret_is_configured_independently(self) -> None:
        """Holding API keys says nothing about being able to verify a webhook."""
        keys_only = a_settings()
        assert keys_only.razorpay_configured is True
        assert keys_only.razorpay_webhook_configured is False

        webhook_only = Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://localhost/x",
            razorpay_webhook_secret=SecretStr("synthetic-webhook-secret"),
        )
        assert webhook_only.razorpay_configured is False
        assert webhook_only.razorpay_webhook_configured is True

    def test_an_empty_webhook_secret_is_not_configured(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://localhost/x",
            razorpay_webhook_secret=SecretStr(""),
        )
        assert settings.razorpay_webhook_configured is False
