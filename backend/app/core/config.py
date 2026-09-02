"""Application settings.

Loaded from the repository-root .env (gitignored) with environment variables
taking precedence. Secret-bearing fields are typed as SecretStr so that they
cannot be leaked by an accidental repr(), log line, or exception traceback.

Phase 1 requires only DATABASE_URL. The Razorpay and Gemini credentials are
declared here so the shape of configuration is settled, but they are optional
and empty until their respective phases (8 and 5).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core -> app -> backend -> repository root
REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Runtime configuration.

    Never add a plain `str` field for anything secret; use SecretStr.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application -----------------------------------------------------
    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Database --------------------------------------------------------
    database_url: str = Field(
        ...,
        description="SQLAlchemy DSN for PostgreSQL, e.g. postgresql+psycopg://user@host:5432/db",
    )
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    #: Where the browser-facing demo runs. **Empty by default, and empty means
    #: the demo is off.**
    #:
    #: Deliberately a second DSN rather than a reuse of `database_url`. The demo
    #: writes rows — a merchant, an order, a failed payment attempt, three
    #: events — and `database_url` names the development database, which must
    #: never receive them. Requiring a separate, explicitly-set value means a
    #: deployment that has simply not thought about the demo cannot accidentally
    #: run one anywhere: the endpoint reports itself disabled instead.
    #:
    #: The demo runner refuses `revtrace_dev` and `revtrace_hypothesis_test` by
    #: name whatever this holds, and rolls its transaction back unconditionally.
    #: Those are the guarantees; this field only decides whether the demo is
    #: available at all.
    demo_database_url: str = ""

    # --- Razorpay (Phase 8; empty in Phase 1) ----------------------------
    razorpay_key_id: SecretStr = SecretStr("")
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")

    # --- AI (Phase 5; empty in Phase 1) ----------------------------------
    gemini_api_key: SecretStr = SecretStr("")

    #: Read by the hypothesis agent and nothing else. Empty by default, and a
    #: provider refuses to construct a client without its key rather than
    #: falling back to an ambient credential — a deliberate run must name its
    #: own.
    #:
    #: OpenRouter is the **only** provider RevTrace calls, on a free model.
    #: `featherless_api_key` is declared so the paid config has a documented
    #: shape, but `free_only_chain` never constructs that provider and the
    #: free-only guard refuses any chain containing it.
    openrouter_api_key: SecretStr = SecretStr("")
    featherless_api_key: SecretStr = SecretStr("")

    # --- Experiment assignment -------------------------------------------
    #: Decorrelates the assignment hash. Deliberately **not** a secret and
    #: deliberately **not** generated at runtime: the arm a risk lands in must
    #: be reproducible by an auditor from stored inputs alone.
    #:
    #: Changing this re-randomises every assignment in every experiment. It is a
    #: breaking reassignment, not a tuning knob, and a running experiment's
    #: results are void if it moves mid-flight.
    assignment_salt: str = "revtrace-demo-salt-v1"

    @field_validator("assignment_salt")
    @classmethod
    def _validate_assignment_salt(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "ASSIGNMENT_SALT must not be empty: an empty salt would make the "
                "assignment hash depend on identity alone and is almost certainly "
                "a misconfiguration rather than a choice."
            )
        return v


    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("DATABASE_URL must not be empty")

        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]

        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]

        if not v.startswith("postgresql+psycopg://"):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL DSN using the psycopg driver; "
                "RevTrace requires PostgreSQL (JSONB and native uuid types are used)."
            )

        return v


    @property
    def razorpay_configured(self) -> bool:
        """True only when a full Razorpay TEST-mode credential set is present."""
        return bool(
            self.razorpay_key_id.get_secret_value() and self.razorpay_key_secret.get_secret_value()
        )

    @property
    def razorpay_webhook_configured(self) -> bool:
        """True when a webhook secret is present, independent of the API keys.

        Separate from `razorpay_configured` because the two capabilities are
        genuinely independent: a deployment can receive and verify webhooks
        without holding keys that can move money, and holding those keys says
        nothing about whether an inbound signature can be checked. Collapsing
        them would let a missing webhook secret hide behind a configured API
        key, and the webhook secret is the *only* authentication that endpoint
        has.
        """
        return bool(self.razorpay_webhook_secret.get_secret_value())

    @property
    def demo_configured(self) -> bool:
        """True when a demo database has been named. Says nothing about safety.

        Whether that database is *allowed* is a separate question the demo
        runner answers, because the answer names the refused database and this
        property has nowhere to put that.
        """
        return bool(self.demo_database_url.strip())

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value())

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key.get_secret_value())

    @property
    def featherless_configured(self) -> bool:
        return bool(self.featherless_api_key.get_secret_value())

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Call this rather than instantiating Settings."""
    return Settings()  # type: ignore[call-arg]
