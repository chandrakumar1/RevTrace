"""The Razorpay client, constructed only for Test Mode.

Everything provider-specific stays inside this package. The rest of the
application depends on RevTrace's own service interfaces, so the provider can
be swapped, mocked or removed without touching a service.

**Test Mode is enforced structurally, not documented.** Razorpay's own
convention is that a Test-Mode key id begins `rzp_test_` and a live key begins
`rzp_live_`. A live-looking key is refused here, before a client object exists —
so there is no object on which a live call could be made, and no runtime branch
that could be reached with the wrong credentials. This project moves money only
in Test Mode, and a refusal at construction is the only version of that promise
that cannot be bypassed later.

**No secret reaches a log or an exception.** Errors name the *variable* that is
wrong, never its value, and `RazorpayError` deliberately carries no payload from
the credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    import razorpay

#: Razorpay's documented prefix for a Test-Mode key id.
TEST_KEY_PREFIX = "rzp_test_"

#: The live prefix, named so the refusal can say *why* a key was rejected
#: rather than only that it did not match.
LIVE_KEY_PREFIX = "rzp_live_"

#: Seconds. Razorpay's SDK takes this per request.
TIMEOUT_SECONDS = 30.0


class RazorpayError(RuntimeError):
    """A Razorpay operation could not be performed.

    Carries no credential and no raw provider payload: an exception message is
    the least controlled thing in a system, and a secret in one outlives every
    redaction downstream.
    """


class RazorpayNotConfigured(RazorpayError):
    """No Test-Mode credentials are present, so no client can exist."""


class RazorpayLiveModeRefused(RazorpayError):
    """A live key was offered. RevTrace does not run against live money."""


@dataclass(frozen=True, slots=True)
class RazorpayCredentials:
    """A validated Test-Mode credential pair.

    Holds the key id — which is not secret and appears in Razorpay's own
    dashboard — and deliberately does **not** hold the secret. The secret goes
    straight from `Settings` into the SDK's auth tuple and is never stored on a
    RevTrace object, so it cannot be reached by a repr, a traceback, or an
    accidental `as_dict`.
    """

    key_id: str

    @property
    def is_test_mode(self) -> bool:
        return self.key_id.startswith(TEST_KEY_PREFIX)


def validate_key_id(key_id: str) -> RazorpayCredentials:
    """Refuse anything that is not a Test-Mode key id.

    Checked by prefix because that is Razorpay's own documented convention and
    the only signal available without a network call. A key that matches
    neither prefix is refused too: an unrecognised shape is not evidence of
    safety, and guessing in the permissive direction is how live credentials
    get used by accident.
    """
    if not key_id:
        raise RazorpayNotConfigured(
            "RAZORPAY_KEY_ID is not set. RevTrace will not fall back to an "
            "ambient credential: a run that talks to a payment provider must "
            "name its own key."
        )
    if key_id.startswith(LIVE_KEY_PREFIX):
        raise RazorpayLiveModeRefused(
            f"RAZORPAY_KEY_ID is a live key (prefix {LIVE_KEY_PREFIX!r}). "
            "RevTrace runs in Test Mode only and refuses to construct a client "
            "that could move real money."
        )
    if not key_id.startswith(TEST_KEY_PREFIX):
        raise RazorpayLiveModeRefused(
            f"RAZORPAY_KEY_ID does not begin with {TEST_KEY_PREFIX!r}. Only a "
            "Test-Mode key is accepted; an unrecognised key shape is not "
            "evidence that it is safe to use."
        )
    return RazorpayCredentials(key_id=key_id)


def build_client(settings: Settings) -> razorpay.Client:
    """The one place a Razorpay client is constructed.

    Raises rather than returning None when credentials are absent, so a caller
    cannot proceed with a client-shaped nothing.

    **Retries are left at the SDK's default and not enabled here.** A retry on a
    creation call is a retry on a money-moving operation, and the SDK gives no
    idempotency key to make that safe. `recovery_actions.idempotency_key` is
    RevTrace's guard against duplicate execution, and it protects a *deliberate*
    re-run — not a transparent retry the caller never saw. Adding retries would
    need that guarantee to reach the provider first.
    """
    key_id = settings.razorpay_key_id.get_secret_value()
    key_secret = settings.razorpay_key_secret.get_secret_value()

    credentials = validate_key_id(key_id)
    if not key_secret:
        raise RazorpayNotConfigured("RAZORPAY_KEY_SECRET is not set.")

    import razorpay as razorpay_sdk

    client = razorpay_sdk.Client(auth=(credentials.key_id, key_secret))
    client.set_app_details({"title": "RevTrace", "version": "0.0.0"})
    return client


def is_test_mode(client: Any) -> bool:
    """Whether a constructed client is authenticated with a Test-Mode key.

    Reads the auth the SDK actually holds — `Client.auth`, a `(key_id, secret)`
    tuple — rather than re-reading configuration, so a client built by some
    other path is still checked against the same rule. Only element zero is
    touched; the secret half is never read.
    """
    auth = getattr(client, "auth", None)
    if not isinstance(auth, tuple | list) or not auth:
        return False
    key_id = auth[0]
    return isinstance(key_id, str) and key_id.startswith(TEST_KEY_PREFIX)


__all__ = [
    "LIVE_KEY_PREFIX",
    "TEST_KEY_PREFIX",
    "TIMEOUT_SECONDS",
    "RazorpayCredentials",
    "RazorpayError",
    "RazorpayLiveModeRefused",
    "RazorpayNotConfigured",
    "build_client",
    "is_test_mode",
    "validate_key_id",
]
