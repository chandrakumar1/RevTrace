"""Cross-origin access: off by default, and narrow when on.

Hermetic — no database, no network. The app is constructed with explicit
settings rather than whatever `.env` holds, so an ambient value cannot make a
test pass or fail for the wrong reason.

The property worth protecting is not "CORS works". It is that a deployment which
never thought about CORS gets **no middleware at all**, and one that did gets
exactly the origin it named.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings

ALLOWED = "https://revtrace.example.com"
OTHER = "https://not-revtrace.example.com"


def _settings(frontend_origin: str) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://localhost:5432/unused",
        frontend_origin=frontend_origin,
        razorpay_key_id=SecretStr(""),
        razorpay_key_secret=SecretStr(""),
        razorpay_webhook_secret=SecretStr(""),
        gemini_api_key=SecretStr(""),
        openrouter_api_key=SecretStr(""),
        featherless_api_key=SecretStr(""),
    )


def _client(frontend_origin: str) -> TestClient:
    """An app built for one origin.

    `create_app` decides on middleware at build time, so the settings must be in
    place before it runs — a dependency override would be too late.

    The patch targets `app.main.get_settings`, not the one in `app.core.config`:
    `main` imported the name directly, so rebinding it in the config module
    would leave `main`'s own reference untouched and the test would silently
    exercise the ambient `.env` instead.
    """
    from app import main as main_module

    original = main_module.get_settings
    main_module.get_settings = lambda: _settings(frontend_origin)  # type: ignore[assignment]
    try:
        return TestClient(main_module.create_app())
    finally:
        main_module.get_settings = original  # type: ignore[assignment]


class TestCorsIsOffByDefault:
    def test_no_origin_configured_means_no_cors_header(self) -> None:
        """The local-development case: the dev proxy needs no CORS."""
        response = _client("").get("/health", headers={"Origin": ALLOWED})

        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    def test_the_default_settings_field_is_empty(self) -> None:
        assert _settings("").frontend_origin == ""
        assert _settings("").cors_enabled is False


class TestCorsIsNarrowWhenOn:
    def test_the_configured_origin_is_allowed(self) -> None:
        response = _client(ALLOWED).get("/health", headers={"Origin": ALLOWED})

        assert response.headers["access-control-allow-origin"] == ALLOWED

    def test_another_origin_is_not_allowed(self) -> None:
        """The whole point: one origin, not any origin."""
        response = _client(ALLOWED).get("/health", headers={"Origin": OTHER})

        assert response.headers.get("access-control-allow-origin") != OTHER
        assert response.headers.get("access-control-allow-origin") != "*"

    def test_no_wildcard_is_ever_emitted(self) -> None:
        for origin in (ALLOWED, OTHER):
            response = _client(ALLOWED).get("/health", headers={"Origin": origin})
            assert response.headers.get("access-control-allow-origin") != "*"

    def test_credentials_are_not_enabled(self) -> None:
        """No cookie or Authorization header exists to send, so none is invited."""
        response = _client(ALLOWED).get("/health", headers={"Origin": ALLOWED})

        assert "access-control-allow-credentials" not in response.headers

    def test_preflight_permits_only_get_and_post(self) -> None:
        response = _client(ALLOWED).options(
            "/api/v1/demo/run",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "POST",
            },
        )

        allowed = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
        assert allowed == {"GET", "POST"}
        assert "DELETE" not in allowed
        assert "PUT" not in allowed

    def test_preflight_from_another_origin_is_refused(self) -> None:
        response = _client(ALLOWED).options(
            "/api/v1/demo/run",
            headers={"Origin": OTHER, "Access-Control-Request-Method": "POST"},
        )

        assert response.headers.get("access-control-allow-origin") != OTHER


class TestTheOriginMustBeAnOrigin:
    """A malformed origin matches nothing and fails as an opaque browser error.

    Refusing it at startup turns a mystery into a message.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "revtrace.example.com",  # no scheme
            "https://revtrace.example.com/",  # trailing slash
            "https://revtrace.example.com/app",  # a path
        ],
    )
    def test_a_malformed_origin_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            _settings(value)

    def test_a_valid_origin_with_a_port_is_accepted(self) -> None:
        assert _settings("http://localhost:5173").frontend_origin == "http://localhost:5173"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert _settings(f"  {ALLOWED}  ").frontend_origin == ALLOWED

    def test_whitespace_only_is_treated_as_unset(self) -> None:
        assert _settings("   ").frontend_origin == ""
        assert _settings("   ").cors_enabled is False
