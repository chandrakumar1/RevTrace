"""Settings loading and — critically — secret containment."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings, normalise_postgres_dsn
from app.core.security import REDACTED, mask, redact


def test_settings_load() -> None:
    s = get_settings()
    assert s.database_url.startswith("postgresql")
    assert s.app_env in {"development", "test", "staging", "production"}


def test_database_url_must_be_postgres() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///tmp.db")  # type: ignore[call-arg]


def test_database_url_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="   ")  # type: ignore[call-arg]


class TestPostgresDsnNormalisation:
    """One rule, shared by every DSN this application accepts.

    Shared deliberately: `DATABASE_URL` normalised its scheme while
    `DEMO_DATABASE_URL` did not, and the gap surfaced in production as
    `ModuleNotFoundError: No module named 'psycopg2'` on a single endpoint.
    """

    @pytest.mark.parametrize(
        "supplied",
        [
            "postgres://u:p@h:5432/d",  # Heroku-style
            "postgresql://u:p@h:5432/d",  # Render-style
            "postgresql+psycopg://u:p@h:5432/d",  # already pinned
        ],
    )
    def test_every_accepted_form_is_pinned_to_psycopg3(self, supplied: str) -> None:
        assert normalise_postgres_dsn(supplied, setting="X") == "postgresql+psycopg://u:p@h:5432/d"

    def test_the_settings_field_is_normalised(self) -> None:
        s = Settings(database_url="postgresql://u@localhost:5432/d")  # type: ignore[call-arg]
        assert s.database_url == "postgresql+psycopg://u@localhost:5432/d"

    def test_an_explicit_psycopg2_driver_is_refused(self) -> None:
        """Refused, not rewritten: a named driver is a deliberate request."""
        with pytest.raises(ValueError, match="psycopg"):
            normalise_postgres_dsn("postgresql+psycopg2://u@h/d", setting="X")

    def test_the_setting_name_appears_so_the_message_is_actionable(self) -> None:
        with pytest.raises(ValueError, match="DEMO_DATABASE_URL"):
            normalise_postgres_dsn("sqlite:///x.db", setting="DEMO_DATABASE_URL")

    def test_the_dsn_is_never_echoed(self) -> None:
        """These messages reach logs, and one of them reaches an HTTP response."""
        with pytest.raises(ValueError) as caught:
            normalise_postgres_dsn("mysql://user:hunter2@host/d", setting="X")
        assert "hunter2" not in str(caught.value)
        assert "://" not in str(caught.value)

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert normalise_postgres_dsn("  postgresql://h/d  ", setting="X") == (
            "postgresql+psycopg://h/d"
        )


def test_secrets_are_secretstr_not_plain_str() -> None:
    s = get_settings()
    for field in (
        "razorpay_key_id",
        "razorpay_key_secret",
        "razorpay_webhook_secret",
        "gemini_api_key",
    ):
        assert isinstance(getattr(s, field), SecretStr), f"{field} must be SecretStr"


def test_secret_value_never_appears_in_repr() -> None:
    """A populated secret must not leak through repr/str of Settings."""
    s = Settings(
        database_url="postgresql+psycopg://u@localhost:5432/d",
        razorpay_key_secret=SecretStr("super_secret_value_123"),
        gemini_api_key=SecretStr("gemini_secret_value_456"),
    )  # type: ignore[call-arg]

    rendered = repr(s) + str(s) + str(s.model_dump())
    assert "super_secret_value_123" not in rendered
    assert "gemini_secret_value_456" not in rendered


def test_configured_flags_report_presence_not_value() -> None:
    empty = Settings(database_url="postgresql+psycopg://u@localhost:5432/d")  # type: ignore[call-arg]
    assert empty.razorpay_configured is False
    assert empty.gemini_configured is False

    filled = Settings(
        database_url="postgresql+psycopg://u@localhost:5432/d",
        razorpay_key_id=SecretStr("rzp_test_abc"),
        razorpay_key_secret=SecretStr("shh"),
        gemini_api_key=SecretStr("shh"),
    )  # type: ignore[call-arg]
    assert filled.razorpay_configured is True
    assert filled.gemini_configured is True


def test_phase_1_has_no_real_credentials() -> None:
    """Phase 1 must run with empty Razorpay and Gemini credentials."""
    s = get_settings()
    assert s.razorpay_configured is False
    assert s.gemini_configured is False


class TestRedaction:
    def test_redacts_sensitive_keys(self) -> None:
        payload = {"key_secret": "abc", "amount": 1000, "webhook_signature": "sig"}
        out = redact(payload)
        assert out["key_secret"] == REDACTED
        assert out["webhook_signature"] == REDACTED
        assert out["amount"] == 1000

    def test_redacts_nested(self) -> None:
        out = redact({"outer": {"api_key": "abc", "safe": 1}})
        assert out["outer"]["api_key"] == REDACTED
        assert out["outer"]["safe"] == 1

    def test_redacts_secretstr(self) -> None:
        assert redact(SecretStr("value")) == REDACTED
        assert redact({"anything": SecretStr("value")})["anything"] == REDACTED

    def test_redacts_inside_lists(self) -> None:
        out = redact([{"token": "abc"}, {"amount": 5}])
        assert out[0]["token"] == REDACTED
        assert out[1]["amount"] == 5

    def test_mask_short_values_entirely(self) -> None:
        assert mask("abc") == REDACTED

    def test_mask_reveals_only_suffix(self) -> None:
        masked = mask("rzp_test_1234567890", keep_last=4)
        assert masked.endswith("7890")
        assert "rzp_test_123456" not in masked
